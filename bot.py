from __future__ import annotations

import asyncio
import io
import itertools
import logging
import random
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram import InputFile
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    Job,
    JobQueue,
)

import config
from brainy_core import (
    GroundedSynthesizer,
    ProviderError,
    RouteIntent,
    SearchGateway,
    SearchQuery,
    build_fast_chat_request,
)
from brainy_core.feedback import FeedbackEntry, FeedbackStore
from brainy_core.providers import OllamaProvider
from brainy_core.providers.web_search import RotatingSearchProvider, build_rotating_provider
from brainy_core.scheduling import StablePriorityQueue
from brainy_core.voice import WhisperCppTranscriber, WhisperTranscriber
from localization import Translator
from page_processor import PageFetcher
from telegram_renderer import RichMessageRenderer, sanitize_untrusted_markdown
from utils import strip_think

# ---------------------------------------------------------------------------#
#                                 Logging                                    #
# ---------------------------------------------------------------------------#
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
_draft_ids = itertools.count(1)
_DRAFT_UPDATE_INTERVAL_SECONDS = 0.35
_DRAFT_TEXT_LIMIT = 4096

# ---------------------------------------------------------------------------#
#                           Language Detection                               #
# ---------------------------------------------------------------------------#


# ---------------------------------------------------------------------------#
#                               Constants                                  #
# ---------------------------------------------------------------------------#
ACTION_SHOW_LANGUAGES = "ACTION_SHOW_LANGUAGES"
ACTION_SET_LANGUAGE = "ACTION_SET_LANGUAGE"
ACTION_TOGGLE_WEB = "ACTION_TOGGLE_WEB"
ACTION_FEEDBACK = "ACTION_FEEDBACK"

_FEEDBACK_SAMPLE_MIN = 8
_FEEDBACK_SAMPLE_MAX = 12

# ---------------------------------------------------------------------------#
#                         State and Request Queue                            #
# ---------------------------------------------------------------------------#
user_message_buffers: dict[int, list[str]] = {}
user_job_trackers: dict[int, "Job"] = {}
user_last_update: dict[int, Update] = {}
user_request_snapshots: dict[int, tuple[str, RouteIntent]] = {}
feedback_store = FeedbackStore()


@dataclass(frozen=True)
class Request:
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    chat_id: int
    query: str
    language: str
    route_intent: RouteIntent


# ---------------------------------------------------------------------------#
# Markdown V2 Escaping (final)
# ---------------------------------------------------------------------------#

_SPECIAL = re.compile(r"([\\_\[\]\(\)~>#+\-=|{}\.!])")  # что экранируем всегда
_SINGLE_STAR = re.compile(r"(?<!\*)\*(?!\*)")  # одиночная *
_LIST_MARKER = re.compile(r"^( *)([-+*])(\s+)", re.MULTILINE)  # "- ", "+ ", "* "
_QUOTE_MARKER = re.compile(r"^( *)(>+)(\s+)", re.MULTILINE)  # "> ", ">> ", …
_NUMERIC_MARK = re.compile(r"^( *\d+)(\.)(\s+)", re.MULTILINE)  # "1. "
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`]*`)", re.S)  # тройной/инлайн код
_HEADING_LINE = re.compile(r"^(?:\s*#+\s*)+(?P<txt>\S[^\n]*)\s*$", re.MULTILINE)
_URL_IN_PARENS = re.compile(r"\((https?://[^)\s]+)\)")
_UNINDENT = re.compile(r"(?m)^(?![ \t]*(?:[-+*]|\d+\.|>))\s{2,}(?=\S)")
_UNINDENT_MARKERS = re.compile(r"(?m)^[ \t]+(?=(?:[-+*]\s|\d+\\\.\s|>))")

# жирный: **…** и *…* (не захватываем "* " маркер списка)
_DBL_BOLD = re.compile(r"(?<!\\)\*\*([^*\n]+?)\*\*")
_BOLD_PAIR = re.compile(r"(?<!\\)\*(?!\s)([^*\n]+?)\*")

# строки "1. https://..." (источники)
_SOURCES_LINE = re.compile(r"^\s*(\d+)\.\s+(https?://\S+)\s*$", re.M)

# плейсхолдеры
PH_MINUS = "\ufff1"
PH_PLUS = "\ufff2"
PH_STAR = "\ufff3"
PH_QUOTE = "\ufff4"
PH_DOT = "\ufff5"
PH_BOPEN = "\ufff6"
PH_BCLOSE = "\ufff7"
PH_LB = "\uffca"
PH_RB = "\uffcb"
PH_LP = "\uffcc"
PH_RP = "\uffcd"  # [ ] ( ) в ссылках


def normalize(text: str) -> str:
    if not text:
        return text
    return (
        text.replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u2009", " ")
        .replace("\u2011", "-")
    )


def _headings_to_bold(seg: str) -> str:
    seg = _HEADING_LINE.sub(lambda m: f"*{m.group('txt')}*\n\n", seg)
    # не даём накапливаться лишним переносам
    return re.sub(r"\n{3,}", "\n\n", seg)


_BULLET_PH = {"-": PH_MINUS, "+": PH_PLUS, "*": PH_STAR}


def _hide_markers(seg: str) -> str:
    def repl_list(m):
        return f"{m.group(1)}{_BULLET_PH[m.group(2)]}{m.group(3)}"

    seg = _LIST_MARKER.sub(repl_list, seg)
    seg = _QUOTE_MARKER.sub(lambda m: f"{m.group(1)}{PH_QUOTE * len(m.group(2))}{m.group(3)}", seg)
    seg = _NUMERIC_MARK.sub(lambda m: f"{m.group(1)}{PH_DOT}{m.group(3)}", seg)
    return seg


def _restore_markers(seg: str) -> str:
    # точку в нумсписке возвращаем экранированной (1\. )
    return (
        seg.replace(PH_MINUS, "-")
        .replace(PH_PLUS, "+")
        .replace(PH_STAR, "*")
        .replace(PH_QUOTE, ">")
        .replace(PH_DOT, "\\.")
    )


def escape_markdown_v2(text: str) -> str:
    if not text:
        return text
    text = strip_think(normalize(text))
    parts = _CODE_SPLIT.split(text)  # [non-code, code, non-code, ...]
    for i in range(0, len(parts), 2):
        seg = parts[i]

        # источники "1. https://..." -> читаемая ссылка
        def _src_repl(m):
            url = m.group(2)
            link_target = url.replace(")", r"\)").replace("(", r"\(")
            link_text = unquote(url)
            return f"{PH_LB}{link_text}{PH_RB}{PH_LP}{link_target}{PH_RP}"

        seg = _SOURCES_LINE.sub(_src_repl, seg)

        seg = _headings_to_bold(seg)  # # Заголовки -> *жирный*

        # прячем жирный
        seg = _DBL_BOLD.sub(lambda m: f"{PH_BOPEN}{m.group(1)}{PH_BCLOSE}", seg)
        seg = _BOLD_PAIR.sub(lambda m: f"{PH_BOPEN}{m.group(1)}{PH_BCLOSE}", seg)

        # прячем маркеры, экранируем спецсимволы, возвращаем маркеры
        seg = _hide_markers(seg)
        seg = _SPECIAL.sub(r"\\\1", seg)
        seg = _SINGLE_STAR.sub(r"\\*", seg)
        seg = _restore_markers(seg)

        # убрать ведущие пробелы перед маркерами списков/нумерации/цитат
        seg = _UNINDENT_MARKERS.sub("", seg)

        # ← вот это новенькое: убираем лишние отступы в начале строк, кроме настоящих маркеров
        seg = _UNINDENT.sub("", seg)

        # возвращаем жирный и синтаксис ссылок
        seg = seg.replace(PH_BOPEN, "*").replace(PH_BCLOSE, "*")
        seg = seg.replace(PH_LB, "[").replace(PH_RB, "]").replace(PH_LP, "(").replace(PH_RP, ")")

        # гарантируем пустую строку ПЕРЕД строками-заголовками вида *...*\n\n
        # (если 0 или 1 перенос — делаем два; если уже два, не трогаем)
        seg = re.sub(r"(?<!\n)\n?(\*[^*\n]+\*\n\n)", r"\n\n\1", seg)
        # не даём накапливаться лишним переносам
        seg = re.sub(r"\n{3,}", "\n\n", seg)

        # снять экранирование внутри URL
        seg = _URL_IN_PARENS.sub(lambda m: f"({m.group(1).replace(r'', '')})", seg)

        # если маркеры цитаты/нумерации встретились не в начале строки — перенос
        seg = re.sub(r"(?<!^)(?<![\n\r])((?:\d+\\\.|>))(?=\s)", r"\n\1", seg)

        parts[i] = seg

    return "".join(parts)


def get_language_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    translator.get_string("keep_language_button", lang),
                    callback_data=f"{ACTION_SET_LANGUAGE}_{lang}",
                ),
                InlineKeyboardButton(
                    translator.get_string("change_language_button", lang),
                    callback_data=ACTION_SHOW_LANGUAGES,
                ),
            ]
        ]
    )


def get_all_languages_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    keyboard = [
        [InlineKeyboardButton(lang.upper(), callback_data=f"{ACTION_SET_LANGUAGE}_{lang}")]
        for lang in translator.supported_languages
    ]
    return InlineKeyboardMarkup(keyboard)


def _current_route_intent(context: ContextTypes.DEFAULT_TYPE) -> RouteIntent:
    return RouteIntent.WEB if context.chat_data.get("web_enabled", False) else RouteIntent.LOCAL


def get_route_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    key = "web_status_on" if _current_route_intent(context) is RouteIntent.WEB else "web_status_off"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(translator.get_string(key, lang), callback_data=ACTION_TOGGLE_WEB)]]
    )


def _should_show_feedback_keyboard(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Sample keyboard display: always on a chat's first reply, then every ~8-12 replies."""

    countdown = context.chat_data.get("feedback_prompt_countdown")
    if countdown is None or countdown <= 0:
        context.chat_data["feedback_prompt_countdown"] = random.randint(
            _FEEDBACK_SAMPLE_MIN, _FEEDBACK_SAMPLE_MAX
        )
        return True
    context.chat_data["feedback_prompt_countdown"] = countdown - 1
    return False


def get_feedback_keyboard(
    context: ContextTypes.DEFAULT_TYPE, lang: str, request_id: str
) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    translator.get_string("feedback_thumbs_up_button", lang),
                    callback_data=f"{ACTION_FEEDBACK}_up_{request_id}",
                ),
                InlineKeyboardButton(
                    translator.get_string("feedback_thumbs_down_button", lang),
                    callback_data=f"{ACTION_FEEDBACK}_down_{request_id}",
                ),
            ]
        ]
    )


def _capture_request_snapshot(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    language: str | None = None,
    route_intent: RouteIntent | None = None,
) -> None:
    user_request_snapshots.setdefault(
        chat_id,
        (
            language or context.chat_data.get("language", "en"),
            route_intent or _current_route_intent(context),
        ),
    )


# ---------------------------------------------------------------------------#
#                               Commands                                     #
# ---------------------------------------------------------------------------#
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_lang = context.chat_data.get("language")
    translator = context.application.bot_data["translator"]

    if not user_lang:
        detected_lang = update.effective_user.language_code
        user_lang = detected_lang if detected_lang in translator.supported_languages else "en"
        context.chat_data["language"] = user_lang

        text = translator.get_string("welcome_new_user", user_lang)
        keyboard = get_language_keyboard(context, user_lang)
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    else:
        status_key = (
            "web_status_on"
            if _current_route_intent(context) is RouteIntent.WEB
            else "web_status_off"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=translator.get_string(status_key, user_lang),
            reply_markup=get_route_keyboard(context, user_lang),
        )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-open the persistent route and language controls on demand."""

    chat_id = update.effective_chat.id
    lang = context.chat_data.get("language", "en")
    translator = context.application.bot_data["translator"]
    status_key = (
        "web_status_on" if _current_route_intent(context) is RouteIntent.WEB else "web_status_off"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=translator.get_string(status_key, lang),
        reply_markup=get_route_keyboard(context, lang),
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=translator.get_string("language_selection_prompt", lang),
        reply_markup=get_all_languages_keyboard(context),
    )


# ---------------------------------------------------------------------------#
#                       Button Callback Handler                              #
# ---------------------------------------------------------------------------#
async def _handle_feedback_callback(
    query, context: ContextTypes.DEFAULT_TYPE, translator, lang: str, action: str
) -> None:
    vote, _, request_id = action[len(f"{ACTION_FEEDBACK}_") :].partition("_")
    entry = feedback_store.pop(request_id)
    if entry is None:
        await query.answer(text=translator.get_string("feedback_expired", lang))
        return

    logger.info(
        "feedback_recorded request_id=%s vote=%s provider=%s model=%s "
        "latency_ms=%.1f lang=%s route=%s",
        request_id,
        vote,
        entry.provider,
        entry.model,
        entry.latency_ms,
        entry.lang,
        entry.route,
    )
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    confirm_key = "feedback_recorded_up" if vote == "up" else "feedback_recorded_down"
    await query.answer(text=translator.get_string(confirm_key, lang))


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    translator = context.application.bot_data["translator"]
    action = query.data
    lang = context.chat_data.get("language", "en")

    if action.startswith(f"{ACTION_FEEDBACK}_"):
        await _handle_feedback_callback(query, context, translator, lang, action)
        return

    await query.answer()

    if action == ACTION_SHOW_LANGUAGES:
        text = translator.get_string("language_selection_prompt", lang)
        await query.edit_message_text(text=text, reply_markup=get_all_languages_keyboard(context))

    elif action.startswith(f"{ACTION_SET_LANGUAGE}_"):
        new_lang = action.replace(f"{ACTION_SET_LANGUAGE}_", "")
        context.chat_data["language"] = new_lang
        status_key = (
            "web_status_on"
            if _current_route_intent(context) is RouteIntent.WEB
            else "web_status_off"
        )
        text = translator.get_string(status_key, new_lang)
        await query.edit_message_text(
            text=text,
            reply_markup=get_route_keyboard(context, new_lang),
        )

    elif action == ACTION_TOGGLE_WEB:
        context.chat_data["web_enabled"] = _current_route_intent(context) is RouteIntent.LOCAL
        status_key = (
            "web_status_on"
            if _current_route_intent(context) is RouteIntent.WEB
            else "web_status_off"
        )
        await query.edit_message_text(
            text=translator.get_string(status_key, lang),
            reply_markup=get_route_keyboard(context, lang),
        )

    elif action in {"web", "deep_research", "fast_reply", "deep_search", "deepseek_r1"}:
        # Old inline keyboards may survive a deploy. Preserve their route intent.
        context.chat_data["web_enabled"] = action in {"web", "deep_research", "deep_search"}
        status_key = "web_status_on" if context.chat_data["web_enabled"] else "web_status_off"
        await query.edit_message_text(
            text=translator.get_string(status_key, lang),
            reply_markup=get_route_keyboard(context, lang),
        )


# ---------------------------------------------------------------------------#
#                         Request Handlers                                   #
# ---------------------------------------------------------------------------#
async def send_typing_periodically(bot, chat_id):
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(8)  # Send typing action every 8 seconds
            except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
                logger.warning(f"Failed to send typing action due to network error: {e}")
                await asyncio.sleep(15)  # Wait longer before retrying
    except asyncio.CancelledError:
        pass  # Task was cancelled, expected behavior


async def _send_progress_draft(
    bot,
    chat_id: int,
    *,
    draft_id: int | None = None,
    text: str = "",
) -> int | None:
    """Publish one ephemeral draft revision when Telegram supports it."""

    send_draft = getattr(bot, "send_message_draft", None)
    if send_draft is None:
        return None
    active_draft_id = draft_id or next(_draft_ids)
    try:
        await asyncio.wait_for(
            send_draft(chat_id=chat_id, draft_id=active_draft_id, text=text),
            timeout=2,
        )
        return active_draft_id
    except (TimeoutError, telegram.error.TelegramError):
        logger.info("Telegram message draft unavailable; using typing fallback")
        return None


def _queue_latest_draft(updates: asyncio.Queue[str], text: str) -> None:
    """Keep only the newest preview so Telegram cannot backpressure inference."""

    if updates.full():
        try:
            updates.get_nowait()
        except asyncio.QueueEmpty:
            pass
    updates.put_nowait(text)


async def _publish_draft_updates(
    bot,
    chat_id: int,
    draft_id: int,
    updates: asyncio.Queue[str],
) -> None:
    """Publish latest-wins plain-text previews at a bounded rate."""

    if await _send_progress_draft(bot, chat_id, draft_id=draft_id) is None:
        return
    loop = asyncio.get_running_loop()
    next_update_at = loop.time()
    while True:
        preview = await updates.get()
        while not updates.empty():
            preview = updates.get_nowait()
        delay = next_update_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if (
            await _send_progress_draft(
                bot,
                chat_id,
                draft_id=draft_id,
                text=preview[:_DRAFT_TEXT_LIMIT],
            )
            is None
        ):
            return
        next_update_at = loop.time() + _DRAFT_UPDATE_INTERVAL_SECONDS


def _clean_text_for_plain_send(text: str) -> str:
    # Rule 1: Remove all backslashes and all asterisks, except for newlines.
    cleaned_text = text.replace("\\", "").replace("*", "")

    # Rule 2: Detect and remove ONLY URLs in (...) including "(",")" themselves.
    # Use the existing _URL_IN_PARENS regex.
    cleaned_text = _URL_IN_PARENS.sub("", cleaned_text)

    # Rule 3: If there is a line that equals "---" (ignoring whitespace) remove this line
    lines = cleaned_text.split("\n")
    filtered_lines = [line for line in lines if line.strip() != "---"]
    cleaned_text = "\n".join(filtered_lines)

    # Rule 4: Check for empty lines, no more than 2 empty lines (\n\n)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)

    return cleaned_text


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if "language" not in context.chat_data:
        await start(update, context)
        return

    lang = context.chat_data.get("language", "en")
    route_intent = _current_route_intent(context)
    translator = context.application.bot_data["translator"]

    # Send an animated hourglass emoji as a status indicator
    status_message = await context.bot.send_message(chat_id, "⏳")

    try:
        voice = update.message.voice
        with tempfile.NamedTemporaryFile(suffix=".oga") as temp_audio_file:
            voice_file = await voice.get_file()
            await voice_file.download_to_drive(temp_audio_file.name)

            whisper_transcriber = context.application.bot_data["whisper_transcriber"]
            transcribed_text = await whisper_transcriber.transcribe(
                temp_audio_file.name,
                language=lang,
            )
    except Exception as exc:
        logger.warning("Voice transcription failed type=%s", type(exc).__name__)
        await update.message.reply_text(translator.get_string("error_generic", lang))
        return
    finally:
        try:
            await context.bot.delete_message(chat_id, status_message.message_id)
        except Exception as exc:
            logger.warning("Voice status cleanup failed type=%s", type(exc).__name__)

    if transcribed_text:
        # Send the transcribed text back to the user
        await context.bot.send_message(chat_id, transcribed_text)

        # Add message to buffer and store the latest update object
        _capture_request_snapshot(
            context,
            chat_id,
            language=lang,
            route_intent=route_intent,
        )
        buffer = user_message_buffers.setdefault(chat_id, [])
        buffer.append(transcribed_text)
        user_last_update[chat_id] = update

        # If a job is already scheduled for this user, remove it
        if chat_id in user_job_trackers:
            user_job_trackers[chat_id].schedule_removal()

        # Schedule the processing job
        new_job = context.job_queue.run_once(
            process_buffered_messages,
            when=0.8,  # 0.8-second delay (adjust as needed)
            chat_id=chat_id,
            name=f"process-msg-{chat_id}",
        )
        user_job_trackers[chat_id] = new_job


async def fast_reply_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    *,
    language: str | None = None,
):
    lang = language or context.chat_data.get("language", "en")
    translator = context.application.bot_data["translator"]
    llm_semaphore = context.application.bot_data["llm_semaphore"]

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    draft_id = next(_draft_ids)
    draft_updates: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    draft_task = asyncio.create_task(
        _publish_draft_updates(
            context.bot,
            update.effective_chat.id,
            draft_id,
            draft_updates,
        )
    )
    try:
        provider = context.application.bot_data.get("inference_provider")
        if provider is None:
            raise RuntimeError("Inference provider is not configured")
        async with llm_semaphore:
            request = build_fast_chat_request(query, lang)
            stream_chat = getattr(provider, "stream_chat", None)
            if callable(stream_chat):
                result = None
                streamed_text = ""
                async for event in stream_chat(request):
                    if event.delta:
                        streamed_text += event.delta
                        visible_preview = strip_think(streamed_text)
                        if visible_preview:
                            _queue_latest_draft(draft_updates, visible_preview)
                    elif event.result is not None:
                        result = event.result
                if result is None:
                    raise RuntimeError("Inference stream ended without a final result")
            else:
                result = await provider.chat(request)
            final_answer = result.text
            logger.info(
                "Fast reply completed provider=%s model=%s latency_ms=%.1f",
                result.model.provider,
                result.model.name,
                result.latency_ms,
            )

        final_answer = re.sub(r"<think>.*?</think>", "", final_answer, flags=re.S | re.I).strip()

        if not final_answer:
            await update.message.reply_text(translator.get_string("error_fast_reply_empty", lang))
            return

        latency_badge = f"⚡ {max(result.latency_ms, 0) / 1000:.1f}s"

        feedback_keyboard = None
        if _should_show_feedback_keyboard(context):
            request_id = uuid.uuid4().hex[:10]
            feedback_store.put(
                request_id,
                FeedbackEntry(
                    provider=result.model.provider,
                    model=result.model.name,
                    latency_ms=result.latency_ms,
                    lang=lang,
                    route=_current_route_intent(context).value,
                ),
            )
            feedback_keyboard = get_feedback_keyboard(context, lang, request_id)

        rich_renderer = context.application.bot_data.get("rich_message_renderer")
        rich_sent = False
        if rich_renderer is not None:
            rich_sent = await rich_renderer.send_final(
                context.bot,
                chat_id=update.effective_chat.id,
                answer=final_answer,
                badge=latency_badge,
                reply_markup=feedback_keyboard,
            )
        if not rich_sent:
            fallback_answer = sanitize_untrusted_markdown(final_answer, neutralize_plain_urls=True)
            telegram_text = escape_markdown_v2(f"{fallback_answer}\n\n{latency_badge}")
            await send_long_message(
                update,
                telegram_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=feedback_keyboard,
            )
    except ProviderError as exc:
        logger.warning("Fast reply provider failure code=%s", exc.code.value)
        await update.message.reply_text(translator.get_string("error_generic", lang))
    except Exception as exc:
        logger.error("Fast reply failed type=%s", type(exc).__name__)
        await update.message.reply_text(translator.get_string("error_generic", lang))
    finally:
        if not draft_task.done():
            draft_task.cancel()
        await asyncio.gather(draft_task, return_exceptions=True)


def _display_host(url: str) -> str:
    """Human-readable host label: decode IDNA/punycode, drop a leading ``www.``."""

    host = urlparse(url).hostname or url
    try:
        host = host.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        pass
    if host.startswith("www."):
        host = host[4:]
    return host[:1].upper() + host[1:]


async def grounded_web_reply_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, *, language: str
) -> None:
    """Run the provider-neutral Web ON orchestration and render its citations."""
    translator = context.application.bot_data["translator"]
    gateway = context.application.bot_data.get("search_gateway")
    synthesizer = context.application.bot_data.get("grounded_synthesizer")
    if gateway is None or synthesizer is None:
        await update.message.reply_text(translator.get_string("web_unavailable", language))
        return

    try:
        started_at = time.perf_counter()
        bundle = await gateway.build_bundle(SearchQuery(query=query, language=language))
        if not bundle.items:
            await update.message.reply_text(translator.get_string("web_unavailable", language))
            return
        grounded = await synthesizer.synthesize(query, language, bundle)
        elapsed_s = time.perf_counter() - started_at
        badge = f"🌐 {max(elapsed_s, 0):.1f}s"
        top_citations = grounded.citations[:3]

        # Sanitize the model-authored answer (strip any model links), then attach
        # the app-trusted citation URLs as real MarkdownV2 links. Web ON must use
        # the regular send path — the rich renderer strips links and cannot show a
        # link preview. Telegram renders at most one preview per message, so point
        # it at the top source.
        safe_answer = sanitize_untrusted_markdown(grounded.answer, neutralize_plain_urls=True)
        body = escape_markdown_v2(f"{safe_answer}\n\n{badge}")
        source_lines = [
            f"{index}\\. [{escape_markdown(_display_host(item.canonical_url), version=2)}]"
            f"({escape_markdown(item.canonical_url, version=2, entity_type='text_link')})"
            for index, item in enumerate(top_citations, start=1)
        ]
        message = f"{body}\n\n{chr(10).join(source_lines)}" if source_lines else body
        link_preview = (
            LinkPreviewOptions(
                url=top_citations[0].canonical_url,
                is_disabled=False,
                prefer_small_media=True,
            )
            if top_citations
            else LinkPreviewOptions(is_disabled=True)
        )
        await send_long_message(
            update,
            message,
            parse_mode=ParseMode.MARKDOWN_V2,
            link_preview_options=link_preview,
        )
    except (ProviderError, ValueError) as exc:
        logger.warning("Grounded web reply failed type=%s", type(exc).__name__)
        await update.message.reply_text(translator.get_string("web_unavailable", language))
    except Exception as exc:
        logger.error("Grounded web reply failed type=%s", type(exc).__name__)
        await update.message.reply_text(translator.get_string("web_unavailable", language))


# ---------------------------------------------------------------------------#
#                         Core Logic (Worker)                                #
# ---------------------------------------------------------------------------#
async def _send_worker_error(update: Update, translator, key: str, lang: str) -> None:
    try:
        await update.message.reply_text(translator.get_string(key, lang))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Worker error notification failed type=%s", type(exc).__name__)


async def worker(name: str, queue: StablePriorityQueue, app_data: dict):
    translator = app_data["translator"]
    while True:
        typing_task = None
        queue_item_acquired = False
        update = None
        context = None
        lang = "en"
        try:
            priority, request = await queue.get()
            queue_item_acquired = True

            chat_id = request.chat_id
            update = request.update
            context = request.context
            query = request.query
            lang = request.language
            route_intent = request.route_intent

            llm_semaphore = context.application.bot_data["llm_semaphore"]
            if route_intent is RouteIntent.LOCAL and llm_semaphore.locked():
                await update.message.reply_text(translator.get_string("waiting_in_queue", lang))

            logger.info(
                "Worker %s processing chat=%s priority=%s route=%s",
                name,
                chat_id,
                priority,
                route_intent.value,
            )

            # Keep a typing indicator alive for both routes; the Web ON path can
            # spend tens of seconds in search + synthesis with no draft stream.
            typing_task = asyncio.create_task(send_typing_periodically(context.bot, chat_id))

            chat_locks = app_data["chat_locks"]
            chat_lock = chat_locks.setdefault(chat_id, asyncio.Lock())
            async with chat_lock:
                # Keep worker-level tests and lightweight adapters compatible with
                # contexts that only provide the minimum application data.
                context.application.bot_data.setdefault("translator", translator)
                if route_intent is RouteIntent.WEB:
                    await grounded_web_reply_handler(update, context, query, language=lang)
                else:
                    await fast_reply_handler(update, context, query, language=lang)

        except asyncio.CancelledError:
            raise
        except telegram.error.TimedOut as exc:
            logger.error(
                "Worker %s timed out type=%s",
                name,
                type(exc).__name__,
            )
            if context is not None and update is not None:
                await _send_worker_error(update, translator, "error_timeout", lang)
        except Exception as exc:
            logger.error("Worker %s failed type=%s", name, type(exc).__name__)
            if context is not None and update is not None:
                await _send_worker_error(update, translator, "error_generic", lang)
        finally:
            if typing_task:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("Typing indicator failed type=%s", type(exc).__name__)
            if queue_item_acquired:
                queue.task_done()


# ---------------------------------------------------------------------------#
#                         Message Handling (Gatekeeper)                       #
# ---------------------------------------------------------------------------#
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    message_text = update.message.text
    if not message_text:
        return

    if "language" not in context.chat_data:
        await start(update, context)
        return

    # Add message to buffer and store the latest update object
    _capture_request_snapshot(context, chat_id)
    buffer = user_message_buffers.setdefault(chat_id, [])
    buffer.append(message_text)
    user_last_update[chat_id] = update

    # If a job is already scheduled for this user, remove it
    if chat_id in user_job_trackers:
        user_job_trackers[chat_id].schedule_removal()

    # Schedule the processing job
    new_job = context.job_queue.run_once(
        process_buffered_messages,
        when=0.8,  # 0.8-second delay (adjust as needed)
        chat_id=chat_id,
        name=f"process-msg-{chat_id}",
    )
    user_job_trackers[chat_id] = new_job


# ---------------------------------------------------------------------------#
# Safe split & send for MarkdownV2 (final)
# ---------------------------------------------------------------------------#

# --- Code-as-file helpers (используются в send_long_message) ---
_CODE_BLOCK_RE = re.compile(r"```([A-Za-z0-9_+\-]*)\n([\s\S]*?)\n```", re.M)
_CODE_AS_FILE_THRESHOLD = 2000  # порог, когда код выносить во вложение


def _guess_ext(lang: str) -> str:
    m = {
        "py": "py",
        "python": "py",
        "js": "js",
        "javascript": "js",
        "ts": "ts",
        "typescript": "ts",
        "json": "json",
        "bash": "sh",
        "sh": "sh",
        "shell": "sh",
        "html": "html",
        "css": "css",
        "java": "java",
        "c": "c",
        "cpp": "cpp",
        "c++": "cpp",
        "go": "go",
        "golang": "go",
        "rs": "rs",
        "rust": "rs",
        "rb": "rb",
        "ruby": "rb",
        "php": "php",
        "kt": "kt",
        "kotlin": "kt",
        "swift": "swift",
        "sql": "sql",
        "yaml": "yml",
        "yml": "yml",
        "md": "md",
        "markdown": "md",
        "txt": "txt",
        "text": "txt",
        "": "txt",
    }
    return m.get((lang or "").lower(), "txt")


async def _extract_code_to_files(update, text: str) -> str:
    """
    Находит большие ```lang\n...\n``` блоки, шлёт их как document,
    а в тексте оставляет плейсхолдер 'Код во вложении'.
    """
    out, pos, idx = [], 0, 1
    for m in _CODE_BLOCK_RE.finditer(text):
        lang, code = m.group(1), m.group(2)
        if len(code) < _CODE_AS_FILE_THRESHOLD:
            continue
        out.append(text[pos : m.start()])  # кусок до кода
        ext = _guess_ext(lang)
        bio = io.BytesIO(code.encode("utf-8"))
        bio.name = f"snippet_{idx}.{ext}"
        await update.message.reply_document(InputFile(bio))
        out.append("👆📄📎\n")  # безопасный плейсхолдер
        pos = m.end()
        idx += 1
    out.append(text[pos:])
    return "".join(out)


async def send_long_message(update, text: str, **kwargs):
    """
    Безопасная отправка длинных сообщений для Telegram MarkdownV2:
    • не режет между '\' и следующим символом, внутри **…**, `…` и ```…```;
    • закрывает незакрытые сущности в чанке;
    • при BadRequest на '#'/'.' экранирует их вне кода и повторяет отправку;
    • если всё ещё падает на '-', экранирует дефисы вне кода, сохраняя маркеры '- '.
    """

    MAX = 4096
    if text is None:
        text = ""

    text = await _extract_code_to_files(update, text)

    # ---------- helpers: safe split ----------
    _DBL_STAR_RE = re.compile(r"(?<!\\)\*\*")  # неэкранированные **
    _TRIPLE_RE = re.compile(r"(?<!\\)```")  # неэкранированные ```
    _BACKTICK_RE = re.compile(r"(?<!\\)`")  # неэкранированные `
    _CODE_SPLIT = re.compile(r"(```.*?```|`[^`]*`)", re.S)
    _LINK_RE = re.compile(r"(\[[^\]]+\])\((https?://[^)\s]+)\)")  # [text](url)

    def _is_safe_cut(s: str, idx: int) -> bool:
        if idx <= 0 or idx >= len(s):
            return True
        if s[idx - 1] == "\\":  # не после обратного слэша
            return False
        if s[idx - 1] == "*" and s[idx] == "*":  # не между '**'
            return False
        if s[idx - 1] == "`" and s[idx] == "`":  # не между '``'
            return False
        if len(_TRIPLE_RE.findall(s[:idx])) % 2 == 1:  # не внутри ``` … ```
            return False
        if len(_BACKTICK_RE.findall(s[:idx])) % 2 == 1:  # не внутри ` … `
            return False
        if len(_DBL_STAR_RE.findall(s[:idx])) % 2 == 1:  # не при незакрытом **
            return False
        return True

    def _find_safe_cut(s: str, limit: int) -> int:
        end = min(limit, len(s))
        # сначала ищем перевод строки или пробел
        candidates = [s.rfind("\n", 0, end), s.rfind(" ", 0, end)]
        cut = max([c for c in candidates if c != -1], default=end)
        probe = cut
        while probe > 0 and not _is_safe_cut(s, probe):
            probe -= 1
        return probe if probe > 0 and _is_safe_cut(s, probe) else end

    def _neutralize_unbalanced(chunk: str) -> str:
        # закрыть незакрытый ```/`
        if len(_TRIPLE_RE.findall(chunk)) % 2 == 1:
            chunk += "\n```"
        if len(_BACKTICK_RE.findall(chunk)) % 2 == 1:
            chunk += "`"
        # экранировать последнюю не закрытую '**'
        if len(_DBL_STAR_RE.findall(chunk)) % 2 == 1:
            last = chunk.rfind("**")
            if last != -1 and (last == 0 or chunk[last - 1] != "\\"):
                chunk = chunk[:last] + r"\**" + chunk[last + 2 :]
        # если заканчивается одиночным '\', удваиваем
        if chunk.endswith("\\") and not chunk.endswith("\\\\"):
            chunk += "\\"
        return chunk

    # --- NEW: маленькие помощники для границы чанка ---
    def _avoid_digit_split(left: str, right: str) -> tuple[str, str]:
        """Если слева оканчивается цифрами, а справа начинается цифрой — не резать '10'."""
        if left and right and left[-1].isdigit() and right[0].isdigit():
            j = len(left) - 1
            while j >= 0 and left[j].isdigit():
                j -= 1
            moved = left[j + 1 :]  # хвост цифр, например '10'
            return left[: j + 1], moved + right
        return left, right

    def _fix_boundary_inside_link(left: str, right: str) -> tuple[str, str]:
        """
        Не резать внутри [текст](url).
        Если слева есть '[' без соответствующего ']' — переносим границу к этому '['.
        Если слева есть ']' и затем незакрытая '(' — переносим границу к ']'.
        """
        lb = left.rfind("[")
        rb = left.rfind("]")
        if lb > rb:  # внутри текста ссылки
            cut = lb
            return left[:cut], left[cut:] + right
        lp = left.rfind("(")
        rp = left.rfind(")")
        if rb != -1 and rb < lp > rp:  # внутри (url)
            cut = rb  # порежем перед '('
            return left[:cut], left[cut:] + right
        return left, right

    # ---------- helpers: fallbacks ----------
    def _escape_hash_and_dot_outside_code(s: str) -> str:
        """Экранируем # и . вне кода и ВНЕ URL."""
        PH_L = "\uf101"
        PH_R = "\uf102"  # плейсхолдеры для ( )
        parts = _CODE_SPLIT.split(s)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # прячем ссылки: (url) -> PH_L url PH_R
            seg = _LINK_RE.sub(lambda m: f"{m.group(1)}{PH_L}{m.group(2)}{PH_R}", seg)
            # экранируем
            seg = re.sub(r"(?<!\\)#", r"\#", seg)
            seg = re.sub(r"(?<!\\)\.", r"\.", seg)
            # возвращаем ссылки
            seg = seg.replace(PH_L, "(").replace(PH_R, ")")
            parts[i] = seg
        return "".join(parts)

    def _escape_parens_outside_code(s: str) -> str:
        """Экранируем круглые скобки вне кода и ВНЕ [текст](url)."""
        PH_L = "\uf121"
        PH_R = "\uf122"  # плейсхолдеры для ( )
        parts = _CODE_SPLIT.split(s)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # прячем ссылки: (url) -> PH_L url PH_R
            seg = _LINK_RE.sub(lambda m: f"{m.group(1)}{PH_L}{m.group(2)}{PH_R}", seg)
            # экранируем обычные скобки
            seg = re.sub(r"(?<!\\)\(", r"\(", seg)
            seg = re.sub(r"(?<!\\)\)", r"\)", seg)
            # возвращаем ссылки
            seg = seg.replace(PH_L, "(").replace(PH_R, ")")
            parts[i] = seg
        return "".join(parts)

    def _escape_hyphens_outside_code(s: str) -> str:
        """Экранируем '-' вне кода и ВНЕ URL. Маркеры списков '- ' тоже экранируем."""
        PH_L = "\uf111"
        PH_R = "\uf112"
        parts = _CODE_SPLIT.split(s)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # прячем ссылки
            seg = _LINK_RE.sub(lambda m: f"{m.group(1)}{PH_L}{m.group(2)}{PH_R}", seg)
            # списочные маркеры "- " -> "\- "
            seg = re.sub(
                r"^( *)(-)(\s+)", lambda m: f"{m.group(1)}\\-{m.group(3)}", seg, flags=re.M
            )
            # остальные дефисы
            seg = re.sub(r"(?<!\\)-", r"\-", seg)
            # возвращаем ссылки
            seg = seg.replace(PH_L, "(").replace(PH_R, ")")
            parts[i] = seg
        return "".join(parts)

    # ---------- sending ----------
    if len(text) <= MAX:
        try:
            await update.message.reply_text(text, **kwargs)
        except BadRequest:
            safe = _escape_hash_and_dot_outside_code(text)
            try:
                await update.message.reply_text(safe, **kwargs)
            except BadRequest:
                safer = _escape_hyphens_outside_code(safe)
                try:
                    await update.message.reply_text(safer, **kwargs)
                except BadRequest as e:  # This is the innermost BadRequest
                    logger.warning(
                        f"Failed to send message with MarkdownV2 after all escapes. Sending as plain text. Error: {e}",
                        exc_info=True,
                    )
                    cleaned_final_text = _clean_text_for_plain_send(text)
                    # Send original text, remove parse_mode from kwargs
                    plain_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                    await update.message.reply_text(
                        cleaned_final_text, parse_mode=None, **plain_kwargs
                    )
        return

    rest = text
    # клавиатуру/inline-кнопки показываем только в последнем сообщении
    common_kwargs = {k: v for k, v in kwargs.items() if k != "reply_markup"}
    last_kwargs = kwargs

    while rest:
        if len(rest) <= MAX:
            chunk, rest = rest, ""
        else:
            cut = _find_safe_cut(rest, MAX)
            if cut <= 0:
                cut = MAX  # страховка
            chunk, rest = rest[:cut], rest[cut:]

            # --- NEW: не резать между цифрами (например, '10\. ')
            chunk, rest = _avoid_digit_split(chunk, rest)
            # --- NEW: не резать внутри [текст](url)
            chunk, rest = _fix_boundary_inside_link(chunk, rest)

        chunk = _neutralize_unbalanced(chunk)

        try:
            if rest:
                await update.message.reply_text(chunk, **common_kwargs)
            else:
                await update.message.reply_text(chunk, **last_kwargs)
        except BadRequest:
            # 1-й повтор: экранируем # и . вне кода
            safe_chunk = _escape_hash_and_dot_outside_code(chunk)
            try:
                if rest:
                    await update.message.reply_text(safe_chunk, **common_kwargs)
                else:
                    await update.message.reply_text(safe_chunk, **last_kwargs)
            except BadRequest:
                # 2-й повтор: экранируем '-' вне кода, сохраняя '- ' маркеры
                safer_chunk = _escape_hyphens_outside_code(safe_chunk)
                try:
                    if rest:
                        await update.message.reply_text(safer_chunk, **common_kwargs)
                    else:
                        await update.message.reply_text(safer_chunk, **last_kwargs)
                except BadRequest as e:  # This is the innermost BadRequest
                    logger.warning(
                        f"Failed to send chunk with MarkdownV2 after all escapes. Sending as plain text. Error: {e}",
                        exc_info=True,
                    )
                    cleaned_final_chunk = _clean_text_for_plain_send(chunk)
                    if rest:
                        plain_kwargs = {k: v for k, v in common_kwargs.items() if k != "parse_mode"}
                        await update.message.reply_text(
                            cleaned_final_chunk, parse_mode=None, **plain_kwargs
                        )
                    else:
                        plain_kwargs = {k: v for k, v in last_kwargs.items() if k != "parse_mode"}
                        await update.message.reply_text(
                            cleaned_final_chunk, parse_mode=None, **plain_kwargs
                        )


async def process_buffered_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes the buffered messages for a user after the timeout."""
    chat_id = context.job.chat_id

    # Immediately retrieve and clear the user's data to avoid race conditions
    buffered_messages = user_message_buffers.pop(chat_id, [])
    last_update = user_last_update.pop(chat_id, None)
    snapshot = user_request_snapshots.pop(chat_id, None)
    user_job_trackers.pop(chat_id, None)

    if not buffered_messages or not last_update:
        logger.warning(f"process_buffered_messages called for chat {chat_id} with no data.")
        return

    full_query_text = " ".join(buffered_messages)  # Join messages with a space
    language, route_intent = snapshot or (
        context.chat_data.get("language", "en"),
        _current_route_intent(context),
    )

    logger.info(
        f"Processing buffered messages for chat {chat_id}. Total messages: {len(buffered_messages)}, Combined length: {len(full_query_text)}."
    )

    MAX_MESSAGE_LENGTH = 12000
    if len(full_query_text) > MAX_MESSAGE_LENGTH:
        translator = context.application.bot_data["translator"]
        await last_update.message.reply_text(
            translator.get_string("error_message_too_long", language)
        )
        logger.warning(
            f"Buffered query for chat {chat_id} exceeded max length ({len(full_query_text)} > {MAX_MESSAGE_LENGTH})."
        )
        return

    priority = 1

    # Get the request queue from bot_data
    request_queue = context.application.bot_data["request_queue"]
    request = Request(
        update=last_update,
        context=context,
        chat_id=chat_id,
        query=full_query_text,
        language=language,
        route_intent=route_intent,
    )
    await request_queue.put(priority, request)

    logger.info("Buffered query submitted chat=%s priority=%s", chat_id, priority)


# ---------------------------------------------------------------------------#
#                                   Main                                     #
# ---------------------------------------------------------------------------#


async def _best_effort_cleanup(label: str, operation) -> None:
    try:
        await operation()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Cleanup step %s failed type=%s", label, type(exc).__name__)


async def main_async() -> None:
    config.SETTINGS.validate(require_telegram=True)
    logger.info("Using local Ollama provider")
    translator = Translator("translations.json")
    request_queue = StablePriorityQueue(maxsize=100)
    llm_semaphore = asyncio.Semaphore(1)
    if config.WHISPER_BACKEND == "cpp":
        whisper_transcriber = WhisperCppTranscriber(
            executable=config.WHISPER_CPP_EXECUTABLE,
            model_path=config.WHISPER_CPP_MODEL,
            ffmpeg_executable=config.WHISPER_CPP_FFMPEG,
        )
    else:
        whisper_transcriber = WhisperTranscriber(config.WHISPER_MODEL)
    worker_count = 3

    inference_provider = OllamaProvider(
        base_url=config.OLLAMA_BASE_URL,
        model=config.OLLAMA_MODEL,
        timeout_seconds=config.OLLAMA_TIMEOUT,
        context_window=config.OLLAMA_CONTEXT_TOKENS,
    )

    # Search resources are created once per process and shared by the Web ON
    # gateway. Providers do not perform network I/O during construction.
    search_client: httpx.AsyncClient | None = None
    search_provider: RotatingSearchProvider | None = None
    page_client = None
    page_fetcher = None
    if config.SETTINGS.search_backend == "rotation":
        search_client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0))
        search_provider = build_rotating_provider(config.SETTINGS, search_client)
        if search_provider is None:
            logger.warning("No search provider keys configured; Web ON is unavailable")
        try:
            import aiohttp

            page_client = aiohttp.ClientSession(
                headers={"User-Agent": config.CUSTOM_USER_AGENT},
                raise_for_status=False,
            )
            page_fetcher = PageFetcher(page_client)
        except ImportError:
            logger.warning("Research extra unavailable; Web ON will use search snippets only")

    application = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .read_timeout(1500)
        .write_timeout(1500)
        .connect_timeout(30)
        .job_queue(JobQueue())
        .build()
    )

    application.bot_data["translator"] = translator
    application.bot_data["request_queue"] = request_queue
    application.bot_data["chat_locks"] = {}
    application.bot_data["llm_semaphore"] = llm_semaphore
    application.bot_data["whisper_transcriber"] = whisper_transcriber
    application.bot_data["inference_provider"] = inference_provider
    application.bot_data["search_provider"] = search_provider
    application.bot_data["search_gateway"] = (
        SearchGateway(search_provider, page_loader=page_fetcher.load if page_fetcher else None)
        if search_provider is not None
        else None
    )
    application.bot_data["grounded_synthesizer"] = (
        GroundedSynthesizer(inference_provider) if search_provider is not None else None
    )
    application.bot_data["rich_message_renderer"] = RichMessageRenderer(
        enabled=config.TELEGRAM_RICH_MESSAGES
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    workers = [
        asyncio.create_task(worker(f"Worker-{i + 1}", request_queue, application.bot_data))
        for i in range(worker_count)
    ]

    application_initialized = False
    try:
        await application.initialize()
        application_initialized = True
        await application.start()

        while True:
            try:
                logger.info("Starting bot polling...")
                await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                await asyncio.gather(*workers)
                logger.warning("Workers have finished, which is unexpected. Stopping.")
                break

            except (telegram.error.NetworkError, telegram.error.TimedOut) as exc:
                logger.error(
                    "Bot polling failed type=%s; retrying in 15 seconds",
                    type(exc).__name__,
                )
                if application.updater.running:
                    await application.updater.stop()
                await asyncio.sleep(15)
            except Exception as exc:
                logger.error("Main loop failed type=%s", type(exc).__name__)
                break

    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
    finally:
        logger.info("Shutting down bot...")
        if application.updater.running:
            await _best_effort_cleanup("updater.stop", application.updater.stop)
        for w in workers:
            if not w.done():
                w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if application.running:
            await _best_effort_cleanup("application.stop", application.stop)
        if inference_provider is not None:
            await _best_effort_cleanup("provider.close", inference_provider.aclose)
        if search_provider is not None:
            await _best_effort_cleanup("search_provider.close", search_provider.aclose)
        if search_client is not None:
            await _best_effort_cleanup("search_client.close", search_client.aclose)
        if page_client is not None:
            await _best_effort_cleanup("page_client.close", page_client.close)
        if application_initialized:
            await _best_effort_cleanup("application.shutdown", application.shutdown)
        logger.info("Bot has been shut down.")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Program interrupted by user.")


if __name__ == "__main__":
    main()
