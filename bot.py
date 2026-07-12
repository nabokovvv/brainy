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
from pathlib import Path
from urllib.parse import urlparse

import httpx
import telegram.error
from telegramify_markdown import ContentType, telegramify
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    MessageEntity,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
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
from brainy_core.inference import ChatMessage
from brainy_core.persona import (
    ALL_PERSONAS,
    DEFAULT_PERSONA,
    is_valid_persona,
)
from brainy_core.memory import (
    BUDGET_LABEL_KEY,
    MEMORY_BUDGET_OPTIONS,
    add_turn,
    clear,
    get_history,
    is_valid_budget,
)
from brainy_core.feedback import FeedbackEntry, FeedbackStore
from brainy_core.providers import OllamaProvider
from brainy_core.query_planner import plan_search_queries
from brainy_core.providers.web_search import RotatingSearchProvider, build_rotating_provider
from brainy_core.scheduling import StablePriorityQueue
from brainy_core.source_exploration import SourceExploration, SourceExplorationStore
from brainy_core.voice import WhisperCppTranscriber, WhisperTranscriber
from localization import Translator
from page_processor import PageFetcher
from telegram_renderer import sanitize_untrusted_markdown
from storage import AsyncUserSettingsRepo, DEFAULT_MEMORY_BUDGET, SQLiteUserSettingsRepo
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
ACTION_OPEN_SETTINGS = "ACTION_OPEN_SETTINGS"
ACTION_SHOW_LANGUAGES = "ACTION_SHOW_LANGUAGES"
ACTION_SET_LANGUAGE = "ACTION_SET_LANGUAGE"
ACTION_TOGGLE_WEB = "ACTION_TOGGLE_WEB"
ACTION_FEEDBACK = "ACTION_FEEDBACK"
ACTION_EXPLORE_SOURCES = "ACTION_EXPLORE_SOURCES"
ACTION_SHOW_PERSONAS = "ACTION_SHOW_PERSONAS"
ACTION_SET_PERSONA = "ACTION_SET_PERSONA"
ACTION_SHOW_MEMORY = "ACTION_SHOW_MEMORY"
ACTION_SET_MEMORY = "ACTION_SET_MEMORY"

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
source_exploration_store = SourceExplorationStore()


@dataclass(frozen=True)
class Request:
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    chat_id: int
    query: str
    language: str
    route_intent: RouteIntent


# ---------------------------------------------------------------------------#
# Link detection (used for link previews)
# ---------------------------------------------------------------------------#

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HTTP_URL = re.compile(r"https?://[^\s<>\]\)]+")


def _first_http_url(text: str) -> str | None:
    """Return the first HTTP(S) URL suitable for Telegram's link preview."""

    markdown_link = _MD_LINK.search(text)
    if markdown_link is not None:
        return markdown_link.group(2)
    plain_url = _HTTP_URL.search(text)
    return plain_url.group(0) if plain_url is not None else None


def _visible_link_preview(url: str | None = None) -> LinkPreviewOptions:
    """Ask Telegram for a clearly visible preview of a URL in this message."""

    return LinkPreviewOptions(
        url=url,
        is_disabled=False,
        prefer_large_media=True,
        show_above_text=True,
    )


# Native-name language labels for the settings hub and the language submenu.
LANGUAGE_DISPLAY: dict[str, str] = {
    "en": "🇬🇧 English",
    "es": "🇪🇸 Español",
    "ru": "🇷🇺 Русский",
    "pt": "🇵🇹 Português",
    "fr": "🇫🇷 Français",
    "de": "🇩🇪 Deutsch",
    "tr": "🇹🇷 Türkçe",
    "id": "🇮🇩 Indonesia",
}

# Compact, language-neutral labels for memory budgets (footer + hub button).
_MEMORY_SHORT: dict[int, str | None] = {0: None, 1000: "~1k", 10000: "~10k"}


def _memory_short_label(translator, lang: str, budget: int) -> str:
    return _MEMORY_SHORT.get(budget) or translator.get_string("memory_short_off", lang)


def _back_row(translator, lang: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            translator.get_string("back_button", lang), callback_data=ACTION_OPEN_SETTINGS
        )
    ]


def get_all_languages_keyboard(
    context: ContextTypes.DEFAULT_TYPE, lang: str
) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    codes = translator.supported_languages
    rows = [
        [
            InlineKeyboardButton(
                ("✓ " if code == lang else "") + LANGUAGE_DISPLAY.get(code, code.upper()),
                callback_data=f"{ACTION_SET_LANGUAGE}_{code}",
            )
            for code in codes[i : i + 2]
        ]
        for i in range(0, len(codes), 2)
    ]
    rows.append(_back_row(translator, lang))
    return InlineKeyboardMarkup(rows)


def _current_route_intent(context: ContextTypes.DEFAULT_TYPE) -> RouteIntent:
    return RouteIntent.WEB if context.chat_data.get("web_enabled", False) else RouteIntent.LOCAL


async def _ensure_settings_loaded(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Hydrate PTB chat_data once without persisting any dialogue content."""

    if "language" in context.chat_data:
        return
    repo = context.application.bot_data.get("settings_repo")
    if repo is not None:
        try:
            settings = await repo.get(chat_id)
        except Exception as exc:
            logger.error("Settings hydration failed type=%s", type(exc).__name__)
            return
        if settings is not None:
            context.chat_data["language"] = settings.language
            context.chat_data["web_enabled"] = settings.web_enabled
            context.chat_data["persona"] = settings.persona
            context.chat_data["memory_budget"] = settings.memory_budget


async def _persist_settings(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    language: str | None = None,
    web_enabled: bool | None = None,
    persona: str | None = None,
    memory_budget: int | None = None,
) -> None:
    repo = context.application.bot_data.get("settings_repo")
    if repo is None:
        return
    try:
        await repo.upsert(
            chat_id,
            language=language,
            web_enabled=web_enabled,
            persona=persona,
            memory_budget=memory_budget,
        )
    except Exception as exc:
        logger.error("Settings persistence failed type=%s", type(exc).__name__)


def _current_persona(context: ContextTypes.DEFAULT_TYPE) -> str:
    persona = context.chat_data.get("persona", DEFAULT_PERSONA)
    return persona if is_valid_persona(persona) else DEFAULT_PERSONA


def _current_memory_budget(context: ContextTypes.DEFAULT_TYPE) -> int:
    budget = context.chat_data.get("memory_budget", DEFAULT_MEMORY_BUDGET)
    return budget if is_valid_budget(budget) else DEFAULT_MEMORY_BUDGET


def get_memory_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    current = _current_memory_budget(context)
    rows = [
        [
            InlineKeyboardButton(
                ("✓ " if budget == current else "")
                + translator.get_string(BUDGET_LABEL_KEY[budget], lang),
                callback_data=f"{ACTION_SET_MEMORY}_{budget}",
            )
        ]
        for budget in MEMORY_BUDGET_OPTIONS
    ]
    rows.append(_back_row(translator, lang))
    return InlineKeyboardMarkup(rows)


def get_persona_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    current = _current_persona(context)
    rows = [
        [
            InlineKeyboardButton(
                ("✓ " if name == current else "")
                + translator.get_string(f"persona_{name}", lang),
                callback_data=f"{ACTION_SET_PERSONA}_{name}",
            )
        ]
        for name in ALL_PERSONAS
    ]
    rows.append(_back_row(translator, lang))
    return InlineKeyboardMarkup(rows)


def _persona_prompt_text(translator, lang: str) -> str:
    lines = [translator.get_string("persona_prompt", lang)]
    for name in ALL_PERSONAS:
        lines.append(
            f"• {translator.get_string(f'persona_{name}', lang)} — "
            f"{translator.get_string(f'persona_{name}_desc', lang)}"
        )
    return "\n".join(lines)


def get_settings_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    """One-message settings hub: every row shows the current value and opens
    either a toggle (web) or a submenu edited in place."""

    translator = context.application.bot_data["translator"]
    web_key = (
        "web_status_on" if _current_route_intent(context) is RouteIntent.WEB else "web_status_off"
    )
    persona_label = translator.get_string(f"persona_{_current_persona(context)}", lang)
    memory_label = _memory_short_label(translator, lang, _current_memory_budget(context))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    translator.get_string(web_key, lang), callback_data=ACTION_TOGGLE_WEB
                )
            ],
            [
                InlineKeyboardButton(
                    translator.get_string(
                        "settings_language_button",
                        lang,
                        value=LANGUAGE_DISPLAY.get(lang, lang.upper()),
                    ),
                    callback_data=ACTION_SHOW_LANGUAGES,
                )
            ],
            [
                InlineKeyboardButton(
                    translator.get_string("settings_persona_button", lang, value=persona_label),
                    callback_data=ACTION_SHOW_PERSONAS,
                )
            ],
            [
                InlineKeyboardButton(
                    translator.get_string("settings_memory_button", lang, value=memory_label),
                    callback_data=ACTION_SHOW_MEMORY,
                )
            ],
        ]
    )


async def _render_settings_hub(query, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    """Edit the settings message back to the hub view (idempotent)."""

    translator = context.application.bot_data["translator"]
    try:
        await query.edit_message_text(
            text=translator.get_string("settings_title", lang),
            reply_markup=get_settings_keyboard(context, lang),
        )
    except BadRequest:
        # "Message is not modified" when nothing changed — safe to ignore.
        pass


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


def get_web_answer_keyboard(
    context: ContextTypes.DEFAULT_TYPE, lang: str, exploration_token: str
) -> InlineKeyboardMarkup:
    translator = context.application.bot_data["translator"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    translator.get_string("explore_sources_button", lang),
                    callback_data=f"{ACTION_EXPLORE_SOURCES}_{exploration_token}",
                )
            ]
        ]
    )


def _short_model_name(name: str) -> str:
    """Compact model label for the footer: drop registry paths, cap length."""

    base = name.split("/")[-1]
    return base if len(base) <= 24 else base[:23] + "…"


def _reply_footer(
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    *,
    icon: str,
    seconds: float,
    model_name: str,
) -> str:
    """One-line meta footer: mode+latency, model, memory budget, language.

    Uses language-neutral tokens and a single leading emoji so it fits one
    line on most phones.
    """

    mem = _MEMORY_SHORT.get(_current_memory_budget(context)) or "off"
    return (
        f"{icon} {max(seconds, 0):.1f}s · {_short_model_name(model_name)}"
        f" · 🧠 {mem} · {lang.upper()}"
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
    """One welcome message with the settings hub attached — no message stack."""

    chat_id = update.effective_chat.id
    await _ensure_settings_loaded(context, chat_id)
    user_lang = context.chat_data.get("language")
    translator = context.application.bot_data["translator"]

    if not user_lang:
        detected_lang = update.effective_user.language_code
        user_lang = detected_lang if detected_lang in translator.supported_languages else "en"
        context.chat_data["language"] = user_lang
        await _persist_settings(
            context,
            chat_id,
            language=user_lang,
            web_enabled=_current_route_intent(context) is RouteIntent.WEB,
        )
        text = translator.get_string("welcome_new_user", user_lang)
    else:
        text = translator.get_string("welcome_back", user_lang)

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=get_settings_keyboard(context, user_lang),
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the single-message settings hub on demand."""

    chat_id = update.effective_chat.id
    await _ensure_settings_loaded(context, chat_id)
    lang = context.chat_data.get("language", "en")
    translator = context.application.bot_data["translator"]
    await context.bot.send_message(
        chat_id=chat_id,
        text=translator.get_string("settings_title", lang),
        reply_markup=get_settings_keyboard(context, lang),
    )


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/new and /reset: drop conversation context, keep all settings."""

    chat_id = update.effective_chat.id
    await _ensure_settings_loaded(context, chat_id)
    lang = context.chat_data.get("language", "en")
    translator = context.application.bot_data["translator"]
    clear(chat_id)
    user_message_buffers.pop(chat_id, None)
    await update.message.reply_text(translator.get_string("new_chat_confirm", lang))


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
    effective_chat = getattr(update, "effective_chat", None)
    chat_id = effective_chat.id if effective_chat is not None else 0
    await _ensure_settings_loaded(context, chat_id)
    translator = context.application.bot_data["translator"]
    action = query.data
    lang = context.chat_data.get("language", "en")

    if action.startswith(f"{ACTION_FEEDBACK}_"):
        await _handle_feedback_callback(query, context, translator, lang, action)
        return

    if action.startswith(f"{ACTION_EXPLORE_SOURCES}_"):
        token = action[len(f"{ACTION_EXPLORE_SOURCES}_") :]
        exploration = source_exploration_store.pop(token, chat_id=chat_id)
        if exploration is None:
            await query.answer(text=translator.get_string("explore_sources_expired", lang))
            return
        await query.answer(text=translator.get_string("explore_sources_started", lang))
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        chat_locks = context.application.bot_data.setdefault("chat_locks", {})
        chat_lock = chat_locks.setdefault(chat_id, asyncio.Lock())
        async with chat_lock:
            await grounded_web_reply_handler(
                update,
                context,
                exploration.query,
                language=exploration.language,
                deep=True,
            )
        return

    if action == ACTION_OPEN_SETTINGS:
        await query.answer()
        await _render_settings_hub(query, context, lang)
        return

    if action == ACTION_SHOW_PERSONAS:
        await query.answer()
        await query.edit_message_text(
            text=_persona_prompt_text(translator, lang),
            reply_markup=get_persona_keyboard(context, lang),
        )
        return

    if action.startswith(f"{ACTION_SET_PERSONA}_"):
        new_persona = action.replace(f"{ACTION_SET_PERSONA}_", "")
        if not is_valid_persona(new_persona):
            await query.answer(text=translator.get_string("persona_invalid", lang))
            return
        context.chat_data["persona"] = new_persona
        await _persist_settings(context, chat_id, persona=new_persona)
        await query.answer(
            text=translator.get_string(
                "persona_set",
                lang,
                persona_name=translator.get_string(f"persona_{new_persona}", lang),
            )
        )
        await _render_settings_hub(query, context, lang)
        return

    if action == ACTION_SHOW_MEMORY:
        await query.answer()
        await query.edit_message_text(
            text=translator.get_string("memory_prompt", lang),
            reply_markup=get_memory_keyboard(context, lang),
        )
        return

    if action.startswith(f"{ACTION_SET_MEMORY}_"):
        raw = action.replace(f"{ACTION_SET_MEMORY}_", "")
        try:
            new_budget = int(raw)
        except ValueError:
            await query.answer(text=translator.get_string("memory_invalid", lang))
            return
        if not is_valid_budget(new_budget):
            await query.answer(text=translator.get_string("memory_invalid", lang))
            return
        context.chat_data["memory_budget"] = new_budget
        if new_budget == 0:
            clear(chat_id)
        await _persist_settings(context, chat_id, memory_budget=new_budget)
        await query.answer(
            text=translator.get_string(
                "memory_set",
                lang,
                memory_label=translator.get_string(BUDGET_LABEL_KEY[new_budget], lang),
            )
        )
        await _render_settings_hub(query, context, lang)
        return

    await query.answer()

    if action == ACTION_SHOW_LANGUAGES:
        text = translator.get_string("language_selection_prompt", lang)
        await query.edit_message_text(
            text=text, reply_markup=get_all_languages_keyboard(context, lang)
        )

    elif action.startswith(f"{ACTION_SET_LANGUAGE}_"):
        new_lang = action.replace(f"{ACTION_SET_LANGUAGE}_", "")
        if new_lang not in translator.supported_languages:
            return
        context.chat_data["language"] = new_lang
        await _persist_settings(context, chat_id, language=new_lang)
        await _render_settings_hub(query, context, new_lang)

    elif action == ACTION_TOGGLE_WEB:
        context.chat_data["web_enabled"] = _current_route_intent(context) is RouteIntent.LOCAL
        await _persist_settings(
            context,
            chat_id,
            web_enabled=context.chat_data["web_enabled"],
        )
        await _render_settings_hub(query, context, lang)

    elif action in {"web", "deep_research", "fast_reply", "deep_search", "deepseek_r1"}:
        # Old inline keyboards may survive a deploy. Preserve their route intent.
        context.chat_data["web_enabled"] = action in {"web", "deep_research", "deep_search"}
        await _persist_settings(
            context,
            chat_id,
            web_enabled=context.chat_data["web_enabled"],
        )
        await _render_settings_hub(query, context, lang)


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


async def _finalize_draft(
    bot,
    chat_id: int,
    draft_id: int,
    draft_task: asyncio.Task,
    final_text: str,
) -> None:
    """Stop the periodic draft loop and publish its true final revision.

    Cancelling the loop alone can race its throttling (one update per
    _DRAFT_UPDATE_INTERVAL_SECONDS): the last *complete* preview may still be
    queued, unsent, when we cancel. Telegram is then left showing a stale,
    mid-stream draft — visually a second, unformatted, truncated copy of the
    answer — until its ~30s ephemeral timeout expires. Sending the complete
    text here directly, bypassing the queue, keeps that leftover bubble in
    sync with the persisted message instead of showing an old fragment.
    """

    if not draft_task.done():
        draft_task.cancel()
    await asyncio.gather(draft_task, return_exceptions=True)
    await _send_progress_draft(bot, chat_id, draft_id=draft_id, text=final_text[:_DRAFT_TEXT_LIMIT])


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


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await _ensure_settings_loaded(context, chat_id)
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
    chat_id = update.effective_chat.id
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
            history = get_history(chat_id, _current_memory_budget(context))
            request = build_fast_chat_request(
                query, lang, persona=_current_persona(context), history=history
            )
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

        await _finalize_draft(context.bot, chat_id, draft_id, draft_task, final_answer)

        add_turn(
            chat_id,
            ChatMessage(role="user", content=query),
            ChatMessage(role="assistant", content=final_answer),
        )

        latency_badge = _reply_footer(
            context,
            lang,
            icon="⚡",
            seconds=result.latency_ms / 1000,
            model_name=result.model.name,
        )

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

        # Fast Reply is the local route: no external injection vector, so render
        # the model's markdown faithfully (bold, headings, lists, clickable links
        # and code blocks). send_rich converts CommonMark to Telegram entities.
        await send_rich(
            update,
            f"{_close_dangling_code_fence(final_answer)}\n\n{latency_badge}",
            reply_markup=feedback_keyboard,
            link_preview_options=_visible_link_preview(_first_http_url(final_answer)),
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    *,
    language: str,
    deep: bool = False,
) -> None:
    """Run the provider-neutral Web ON orchestration and render its citations."""
    reply_target = getattr(update, "effective_message", None) or update.message
    translator = context.application.bot_data["translator"]
    gateway = context.application.bot_data.get("deep_search_gateway" if deep else "search_gateway")
    synthesizer = context.application.bot_data.get("grounded_synthesizer")
    provider = context.application.bot_data.get("inference_provider")
    llm_semaphore = context.application.bot_data["llm_semaphore"]
    if gateway is None or synthesizer is None or provider is None:
        await reply_target.reply_text(translator.get_string("web_unavailable", language))
        return

    chat_id = update.effective_chat.id
    draft_id = next(_draft_ids)
    draft_updates: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    draft_task = asyncio.create_task(
        _publish_draft_updates(context.bot, chat_id, draft_id, draft_updates)
    )
    try:
        started_at = time.perf_counter()
        deep_deadline = asyncio.get_running_loop().time() + 30 if deep else None
        # Phase 1: show a "searching" draft while the gateway runs.
        _queue_latest_draft(
            draft_updates, translator.get_string("web_progress_searching", language)
        )
        # Condense the raw message into 1-2 bounded search queries. Short
        # messages skip the LLM entirely; failures fall back to the raw text,
        # so this can only reduce search-API usage, never block the answer.
        async with llm_semaphore:
            planned_queries = await plan_search_queries(query, language, provider)
        search_requests = tuple(
            SearchQuery(query=planned, language=language, limit=10 if deep else 5)
            for planned in planned_queries
        )
        bundle = await asyncio.wait_for(
            gateway.build_bundle(search_requests[0], search_requests[1:]),
            timeout=30 if deep else 20,
        )
        if not bundle.items:
            await reply_target.reply_text(translator.get_string("web_unavailable", language))
            return

        # Phase 2: synthesis is a plain text-in/text-out chat over the clean web
        # context, so stream it token-by-token into the same draft, exactly like
        # the local fast path. Hold the shared LLM semaphore (single local GPU).
        _queue_latest_draft(
            draft_updates, translator.get_string("web_progress_synthesizing", language)
        )
        request = synthesizer.build_request(
            query,
            language,
            bundle,
            detailed=deep,
            persona=_current_persona(context),
            history=get_history(chat_id, _current_memory_budget(context)),
        )
        remaining = (
            max(0.1, deep_deadline - asyncio.get_running_loop().time())
            if deep_deadline is not None
            else None
        )
        async with asyncio.timeout(remaining):
            async with llm_semaphore:
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
                        raise RuntimeError("Synthesis stream ended without a final result")
                else:
                    result = await provider.chat(request)
        answer_text = re.sub(r"<think>.*?</think>", "", result.text, flags=re.S | re.I).strip()

        if not answer_text:
            await reply_target.reply_text(translator.get_string("web_unavailable", language))
            return

        await _finalize_draft(context.bot, chat_id, draft_id, draft_task, answer_text)

        add_turn(
            chat_id,
            ChatMessage(role="user", content=query),
            ChatMessage(role="assistant", content=answer_text),
        )

        elapsed_s = time.perf_counter() - started_at
        badge = _reply_footer(
            context,
            language,
            icon="🔎" if deep else "🌐",
            seconds=elapsed_s,
            model_name=result.model.name,
        )
        top_citations = synthesizer.select_citations(bundle, 5 if deep else 3)

        # The chunk text is still untrusted web content, so sanitize the model
        # output (strip any links it echoed) before attaching the app-trusted
        # citation URLs as Markdown links. send_rich escapes them safely into
        # Telegram entities. Telegram renders at most one link preview per
        # message, so point it at the top source.
        safe_answer = sanitize_untrusted_markdown(answer_text, neutralize_plain_urls=True)
        source_lines = [
            f"{index}. [{_display_host(item.canonical_url)}]({item.canonical_url})"
            for index, item in enumerate(top_citations, start=1)
        ]
        message = f"{_close_dangling_code_fence(safe_answer)}\n\n{badge}"
        if source_lines:
            message += "\n\n" + "\n".join(source_lines)
        link_preview = (
            _visible_link_preview(top_citations[0].canonical_url)
            if top_citations
            else LinkPreviewOptions(is_disabled=True)
        )
        reply_markup = None
        if not deep:
            exploration_token = uuid.uuid4().hex[:12]
            source_exploration_store.put(
                exploration_token,
                SourceExploration(chat_id=chat_id, query=query, language=language),
            )
            reply_markup = get_web_answer_keyboard(context, language, exploration_token)
        await send_rich(
            update,
            message,
            link_preview_options=link_preview,
            reply_markup=reply_markup,
        )
    except (ProviderError, ValueError) as exc:
        logger.warning("Grounded web reply failed type=%s", type(exc).__name__)
        await reply_target.reply_text(translator.get_string("web_unavailable", language))
    except Exception as exc:
        logger.error("Grounded web reply failed type=%s", type(exc).__name__)
        await reply_target.reply_text(translator.get_string("web_unavailable", language))
    finally:
        if not draft_task.done():
            draft_task.cancel()
        await asyncio.gather(draft_task, return_exceptions=True)


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
    await _ensure_settings_loaded(context, chat_id)
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
# Rich delivery via telegramify-markdown (entity-based, no MarkdownV2 escaping)
# ---------------------------------------------------------------------------#

# Code blocks with at least this many lines are delivered as a file attachment
# instead of an inline ``pre`` block.
_CODE_TO_FILE_MIN_LINES = 30


def _close_dangling_code_fence(answer: str) -> str:
    """Balance an unclosed ``` fence in model output before appending anything.

    Token-truncated replies (and models that simply forget the closing fence)
    leave an odd number of ``` markers. Without a closing fence, everything we
    append afterwards - most visibly the latency badge - is swallowed into the
    open code block and delivered as part of the code file instead of as text.
    """

    if answer.count("```") % 2:
        return f"{answer}\n```"
    return answer


def _plain_fallback(text: str) -> str:
    """Strip Markdown decoration for a last-resort plain-text send."""

    text = re.sub(r"```[A-Za-z0-9_+\-]*\n?", "", text).replace("```", "")
    text = text.replace("**", "").replace("__", "")
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _to_ptb_entities(entities) -> list[MessageEntity] | None:
    """Convert telegramify-markdown entities to PTB ``telegram.MessageEntity``.

    telegramify returns its own MessageEntity class; PTB cannot JSON-serialize
    foreign objects and wraps the resulting ``TypeError`` in ``NetworkError``,
    so passing them through directly kills every send that carries formatting.
    Offsets are already UTF-16 as Telegram requires.
    """

    if not entities:
        return None
    return [
        MessageEntity(
            type=entity.type,
            offset=entity.offset,
            length=entity.length,
            url=entity.url,
            language=entity.language,
            custom_emoji_id=entity.custom_emoji_id,
        )
        for entity in entities
    ]


# Backoff (seconds) between retries of a transient Telegram transport failure.
_SEND_RETRY_BACKOFF = (0.5, 1.0, 2.0)


async def _send_with_retry(send):
    """Run an async Telegram send, retrying transient transport failures.

    ``BadRequest`` is a content problem (retrying won't help) and is re-raised
    at once so the caller can fall back to plain text. ``NetworkError`` /
    ``TimedOut`` / ``RetryAfter`` are transient: a brief connectivity blip must
    not discard a fully generated answer, so we retry with backoff before giving
    up. ``send`` is a zero-arg callable returning a fresh coroutine each call.
    """

    last_exc: Exception | None = None
    for attempt in range(len(_SEND_RETRY_BACKOFF) + 1):
        try:
            return await send()
        except BadRequest:
            raise
        except RetryAfter as exc:
            last_exc = exc
            delay = getattr(exc, "retry_after", 1) or 1
            await asyncio.sleep(float(delay) + 0.5)
        except (NetworkError, TimedOut) as exc:
            last_exc = exc
            if attempt < len(_SEND_RETRY_BACKOFF):
                logger.warning(
                    "Telegram send transient failure type=%s attempt=%d; retrying",
                    type(exc).__name__,
                    attempt + 1,
                )
                await asyncio.sleep(_SEND_RETRY_BACKOFF[attempt])
    assert last_exc is not None
    raise last_exc


async def send_rich(update, text: str, **kwargs):
    """Deliver a model answer to Telegram using entity-based formatting.

    ``telegramify`` converts CommonMark to Telegram ``MessageEntity`` objects
    (bold, inline code, links, ...), so messages go out without ``parse_mode``
    and need no manual MarkdownV2 escaping - which removes the whole class of
    ``BadRequest`` failures on reserved characters. Long code blocks become file
    attachments. ``reply_markup`` / ``link_preview_options`` ride on the final
    message only. On any failure we fall back to a plain-text send so a reply is
    never dropped.
    """

    reply_target = getattr(update, "effective_message", None) or update.message
    if not text:
        return

    tail_keys = ("reply_markup", "link_preview_options")
    tail_kwargs = {k: kwargs[k] for k in tail_keys if k in kwargs}
    base_kwargs = {k: v for k, v in kwargs.items() if k not in tail_keys and k != "parse_mode"}

    try:
        boxes = await telegramify(
            content=text,
            max_message_length=_DRAFT_TEXT_LIMIT,
            min_file_lines=_CODE_TO_FILE_MIN_LINES,
            render_mermaid=False,
        )
    except Exception as exc:
        logger.warning("telegramify failed type=%s; sending plain text", type(exc).__name__)
        await _send_with_retry(
            lambda: reply_target.reply_text(_plain_fallback(text), **base_kwargs, **tail_kwargs)
        )
        return

    for index, box in enumerate(boxes):
        is_last = index == len(boxes) - 1
        send_kwargs = {**base_kwargs, **(tail_kwargs if is_last else {})}
        doc_kwargs = {k: v for k, v in send_kwargs.items() if k != "link_preview_options"}
        try:
            if box.content_type == ContentType.FILE:
                document = InputFile(io.BytesIO(box.file_data), filename=box.file_name)
                await _send_with_retry(
                    lambda box=box, doc_kwargs=doc_kwargs, document=document: (
                        reply_target.reply_document(
                            document,
                            caption=box.caption_text or None,
                            caption_entities=_to_ptb_entities(box.caption_entities),
                            **doc_kwargs,
                        )
                    )
                )
            else:
                await _send_with_retry(
                    lambda box=box, send_kwargs=send_kwargs: reply_target.reply_text(
                        box.text, entities=_to_ptb_entities(box.entities), **send_kwargs
                    )
                )
        except BadRequest as exc:
            logger.warning("Rich send failed type=%s; plain-text fallback", type(exc).__name__)
            source = text if box.content_type == ContentType.FILE else box.text
            await _send_with_retry(
                lambda source=source, doc_kwargs=doc_kwargs: reply_target.reply_text(
                    _plain_fallback(source), **doc_kwargs
                )
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
    settings_path = Path(config.SETTINGS.user_settings_path).expanduser()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_repo = AsyncUserSettingsRepo(SQLiteUserSettingsRepo(str(settings_path)))
    settings_path.chmod(0o600)
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

    # Bounded, coherent HTTP timeouts. read/write cover regular API calls and
    # sends (get_updates long-polling uses its own separate timeout); write is
    # larger to allow document/code-file uploads. pool_timeout avoids spurious
    # PoolTimeout when several workers send concurrently. Transient failures are
    # retried in send_rich (_send_with_retry) rather than absorbed by a huge
    # timeout that would pin a worker for minutes on a stuck connection.
    application = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(60)
        .pool_timeout(30)
        .job_queue(JobQueue())
        .build()
    )

    application.bot_data["translator"] = translator
    application.bot_data["settings_repo"] = settings_repo
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
    application.bot_data["deep_search_gateway"] = (
        SearchGateway(
            search_provider,
            token_budget=2800,
            page_loader=page_fetcher.load if page_fetcher else None,
        )
        if search_provider is not None
        else None
    )
    application.bot_data["grounded_synthesizer"] = (
        GroundedSynthesizer(inference_provider) if search_provider is not None else None
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler(["new", "reset"], new_chat))
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
        # Populate Telegram's "/" command menu. Best-effort: the bot works
        # without it, so a transient API failure must not block startup.
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("new", "New chat — clear the context"),
                    BotCommand("settings", "Web, language, persona, memory"),
                    BotCommand("start", "Restart the bot"),
                ]
            )
        except telegram.error.TelegramError as exc:
            logger.warning("set_my_commands failed type=%s", type(exc).__name__)
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
        await _best_effort_cleanup("settings_repo.close", settings_repo.close)
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
