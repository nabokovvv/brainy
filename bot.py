import logging
import re
import asyncio
from collections import namedtuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
import telegram.error
import os
import textwrap
import whisper
import torch
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import py3langid
import config

import io
from telegram import InputFile

# ---------------------------------------------------------------------------#
#                                 Logging                                    #
# ---------------------------------------------------------------------------#
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Conditional LLM client import
if config.LLM_CLIENT == "together":
    import together_client as llm_client
    logger.info("Using Together AI client")
else:
    import ollama_client as llm_client
    logger.info("Using Ollama client")


import page_processor
import reranker
import search_client
import xml_parser
from localization import Translator
from utils import _filter_duplicate_chunks, detect_language, strip_think, translate_string
import entity_lookup
import chart_generator
import json
import os
from datetime import datetime
import hashlib
import tempfile
from urllib.parse import unquote

# ---------------------------------------------------------------------------#
#                           Language Detection                               #
# ---------------------------------------------------------------------------#


# ---------------------------------------------------------------------------#
#                               Constants                                  #
# ---------------------------------------------------------------------------#
ACTION_CHANGE_MODE = "ACTION_CHANGE_MODE"
ACTION_SHOW_LANGUAGES = "ACTION_SHOW_LANGUAGES"
ACTION_SET_LANGUAGE = "ACTION_SET_LANGUAGE"

# ---------------------------------------------------------------------------#
#                         State and Request Queue                            #
# ---------------------------------------------------------------------------#
user_message_buffers: dict[int, list[str]] = {}
user_job_trackers: dict[int, "Job"] = {}
user_last_update: dict[int, Update] = {}

Request = namedtuple("Request", ["update", "context", "chat_id", "query"])

# ---------------------------------------------------------------------------#
# Markdown V2 Escaping (final)
# ---------------------------------------------------------------------------#

_SPECIAL       = re.compile(r'([\\_\[\]\(\)~>#+\-=|{}\.!])')          # что экранируем всегда
_SINGLE_STAR   = re.compile(r'(?<!\*)\*(?!\*)')                        # одиночная *
_LIST_MARKER   = re.compile(r'^( *)([-+*])(\s+)', re.MULTILINE)        # "- ", "+ ", "* "
_QUOTE_MARKER  = re.compile(r'^( *)(>+)(\s+)',   re.MULTILINE)        # "> ", ">> ", …
_NUMERIC_MARK  = re.compile(r'^( *\d+)(\.)(\s+)', re.MULTILINE)        # "1. "
_CODE_SPLIT    = re.compile(r'(```.*?```|`[^`]*`)', re.S)              # тройной/инлайн код
_HEADING_LINE  = re.compile(r'^(?:\s*#+\s*)+(?P<txt>\S[^\n]*)\s*$', re.MULTILINE)
_URL_IN_PARENS = re.compile(r'\((https?://[^)\s]+)\)')
_LINK_RE    = re.compile(r'(\[[^\]]+\])\((https?://[^)\s]+)\)')  # [text](url)
_UNINDENT = re.compile(r'(?m)^(?![ \t]*(?:[-+*]|\d+\.|>))\s{2,}(?=\S)')
_UNINDENT_MARKERS = re.compile(r'(?m)^[ \t]+(?=(?:[-+*]\s|\d+\\\.\s|>))')

# жирный: **…** и *…* (не захватываем "* " маркер списка)
_DBL_BOLD      = re.compile(r'(?<!\\)\*\*([^*\n]+?)\*\*')
_BOLD_PAIR     = re.compile(r'(?<!\\)\*(?!\s)([^*\n]+?)\*')

# строки "1. https://..." (источники)
_SOURCES_LINE  = re.compile(r'^\s*(\d+)\.\s+(https?://\S+)\s*$', re.M)

# плейсхолдеры
PH_MINUS = '\uFFF1'; PH_PLUS = '\uFFF2'; PH_STAR = '\uFFF3'; PH_QUOTE = '\uFFF4'; PH_DOT = '\uFFF5'
PH_BOPEN = '\uFFF6'; PH_BCLOSE = '\uFFF7'
PH_LB = '\uFFCA'; PH_RB = '\uFFCB'; PH_LP = '\uFFCC'; PH_RP = '\uFFCD'  # [ ] ( ) в ссылках

def strip_think(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.S | re.I).strip()

def normalize(text: str) -> str:
    if not text: return text
    return (text.replace('\u00A0',' ').replace('\u202F',' ').replace('\u2009',' ')
                .replace('\u2011','-'))

def _headings_to_bold(seg: str) -> str:
    seg = _HEADING_LINE.sub(lambda m: f"*{m.group('txt')}*\n\n", seg)
    # не даём накапливаться лишним переносам
    return re.sub(r'\n{3,}', '\n\n', seg)

_BULLET_PH = {'-': PH_MINUS, '+': PH_PLUS, '*': PH_STAR}

def _hide_markers(seg: str) -> str:
    def repl_list(m):
        return f"{m.group(1)}{_BULLET_PH[m.group(2)]}{m.group(3)}"
    seg = _LIST_MARKER.sub(repl_list, seg)
    seg = _QUOTE_MARKER.sub(lambda m: f"{m.group(1)}{PH_QUOTE*len(m.group(2))}{m.group(3)}", seg)
    seg = _NUMERIC_MARK.sub(lambda m: f"{m.group(1)}{PH_DOT}{m.group(3)}", seg)
    return seg

def _restore_markers(seg: str) -> str:
    # точку в нумсписке возвращаем экранированной (1\. )
    return (seg.replace(PH_MINUS,'-').replace(PH_PLUS,'+').replace(PH_STAR,'*')
              .replace(PH_QUOTE,'>').replace(PH_DOT,'\\.'))

def escape_markdown_v2(text: str) -> str:
    if not text:
        return text
    text = strip_think(normalize(text))
    parts = _CODE_SPLIT.split(text)  # [non-code, code, non-code, ...]
    for i in range(0, len(parts), 2):
        seg = parts[i]

        # источники "1. https://..." -> читаемая ссылка
        def _src_repl(m):
            n, url = m.group(1), m.group(2)
            link_target = url.replace(')', r'\)').replace('(', r'\(')
            link_text = unquote(url)
            return f"{PH_LB}{link_text}{PH_RB}{PH_LP}{link_target}{PH_RP}"
        seg = _SOURCES_LINE.sub(_src_repl, seg)

        seg = _headings_to_bold(seg)  # # Заголовки -> *жирный*

        # прячем жирный
        seg = _DBL_BOLD.sub(lambda m: f"{PH_BOPEN}{m.group(1)}{PH_BCLOSE}", seg)
        seg = _BOLD_PAIR.sub(lambda m: f"{PH_BOPEN}{m.group(1)}{PH_BCLOSE}", seg)

        # прячем маркеры, экранируем спецсимволы, возвращаем маркеры
        seg = _hide_markers(seg)
        seg = _SPECIAL.sub(r'\\\1', seg)
        seg = _SINGLE_STAR.sub(r'\\*', seg)
        seg = _restore_markers(seg)
        
        # убрать ведущие пробелы перед маркерами списков/нумерации/цитат
        seg = _UNINDENT_MARKERS.sub('', seg)

        # ← вот это новенькое: убираем лишние отступы в начале строк, кроме настоящих маркеров
        seg = _UNINDENT.sub('', seg)

        # возвращаем жирный и синтаксис ссылок
        seg = seg.replace(PH_BOPEN, '*').replace(PH_BCLOSE, '*')
        seg = (seg.replace(PH_LB,'[').replace(PH_RB,']')
                  .replace(PH_LP,'(').replace(PH_RP,')'))
        
        # гарантируем пустую строку ПЕРЕД строками-заголовками вида *...*\n\n
        # (если 0 или 1 перенос — делаем два; если уже два, не трогаем)
        seg = re.sub(r'(?<!\n)\n?(\*[^*\n]+\*\n\n)', r'\n\n\1', seg)
        # не даём накапливаться лишним переносам
        seg = re.sub(r'\n{3,}', '\n\n', seg)

        # снять экранирование внутри URL
        seg = _URL_IN_PARENS.sub(lambda m: f"({m.group(1).replace(r'', '')})", seg)

        # если маркеры цитаты/нумерации встретились не в начале строки — перенос
        seg = re.sub(r'(?<!^)(?<![\n\r])((?:\d+\\\.|>))(?=\s)', r'\n\1', seg)

        parts[i] = seg

    return ''.join(parts)
# ---------------------------------------------------------------------------#
#                           Keyboard Generators                              #
# ---------------------------------------------------------------------------#
def get_mode_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data['translator']
    mode_key = context.chat_data.get('mode', 'fast_reply')
    mode_name = translator.get_string(f"mode_{mode_key}", lang)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(translator.get_string("current_mode_button", lang, mode_name=mode_name), callback_data="noop"),
                InlineKeyboardButton(translator.get_string("change_mode_button_text", lang), callback_data=ACTION_CHANGE_MODE),
            ]
        ]
    )

def get_full_mode_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data['translator']
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(translator.get_string("mode_web", lang), callback_data="web"),
                InlineKeyboardButton(translator.get_string("mode_deep_research", lang), callback_data="deep_research"),
            ],
            [
                InlineKeyboardButton(translator.get_string("mode_fast_reply", lang), callback_data="fast_reply"),
                InlineKeyboardButton(translator.get_string("mode_deep_search", lang), callback_data="deep_search"),
            ],
            [
                InlineKeyboardButton(translator.get_string("mode_deepseek_r1", lang), callback_data="deepseek_r1"),
            ]
        ]
    )

def get_language_keyboard(context: ContextTypes.DEFAULT_TYPE, lang: str) -> InlineKeyboardMarkup:
    translator = context.application.bot_data['translator']
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(translator.get_string("keep_language_button", lang), callback_data=f"{ACTION_SET_LANGUAGE}_{lang}"),
                InlineKeyboardButton(translator.get_string("change_language_button", lang), callback_data=ACTION_SHOW_LANGUAGES),
            ]
        ]
    )

def get_all_languages_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    translator = context.application.bot_data['translator']
    keyboard = [
        [InlineKeyboardButton(lang.upper(), callback_data=f"{ACTION_SET_LANGUAGE}_{lang}")]
        for lang in translator.supported_languages
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------------------------#
#                               Commands                                     #
# ---------------------------------------------------------------------------#
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_lang = context.chat_data.get('language')
    translator = context.application.bot_data['translator']

    if not user_lang:
        detected_lang = update.effective_user.language_code
        user_lang = detected_lang if detected_lang in translator.supported_languages else 'en'
        context.chat_data['language'] = user_lang
        
        text = translator.get_string("welcome_new_user", user_lang)
        keyboard = get_language_keyboard(context, user_lang)
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    else:
        await show_mode_menu(context, chat_id)

async def show_mode_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    lang = context.chat_data.get('language', 'en')
    translator = context.application.bot_data['translator']
    text = translator.get_string("choose_your_mode", lang)
    keyboard = get_mode_keyboard(context, chat_id, lang)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

# ---------------------------------------------------------------------------#
#                       Button Callback Handler                              #
# ---------------------------------------------------------------------------#
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    translator = context.application.bot_data['translator']
    action = query.data

    lang = context.chat_data.get('language', 'en')

    if action == ACTION_CHANGE_MODE:
        try:
            await query.edit_message_reply_markup(get_full_mode_keyboard(context, lang))
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                pass  # Ignore if the keyboard is already what we want it to be.
            else:
                raise # Re-raise other bad request errors.
    
    elif action in ["web", "deep_research", "fast_reply", "deep_search", "deepseek_r1"]:
        context.chat_data['mode'] = action
        await query.edit_message_reply_markup(get_mode_keyboard(context, chat_id, lang))

    elif action == ACTION_SHOW_LANGUAGES:
        text = translator.get_string("language_selection_prompt", lang)
        await query.edit_message_text(text=text, reply_markup=get_all_languages_keyboard(context))

    elif action.startswith(f"{ACTION_SET_LANGUAGE}_"):
        new_lang = action.replace(f"{ACTION_SET_LANGUAGE}_", "")
        context.chat_data['language'] = new_lang
        text = translator.get_string("choose_your_mode", new_lang)
        keyboard = get_mode_keyboard(context, chat_id, new_lang)
        await query.edit_message_text(text=text, reply_markup=keyboard)

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
                await asyncio.sleep(15) # Wait longer before retrying
    except asyncio.CancelledError:
        pass # Task was cancelled, expected behavior

def _remove_emojis(text: str) -> str:
    """Removes emoji characters from a string."""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs (includes brain emoji)
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\u2600-\u26FF"          # miscellaneous symbols
        "\u2700-\u27BF"          # dingbats
        "\uFE0F"                # variation selector
        "\u200d"                # zero width joiner
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r'', text).strip()

def _clean_text_for_plain_send(text: str) -> str:
    # Rule 1: Remove all backslashes and all asterisks, except for newlines.
    cleaned_text = text.replace('\\', '').replace('*', '')

    # Rule 2: Detect and remove ONLY URLs in (...) including "(",")" themselves.
    # Use the existing _URL_IN_PARENS regex.
    cleaned_text = _URL_IN_PARENS.sub('', cleaned_text)

    # Rule 3: If there is a line that equals "---" (ignoring whitespace) remove this line
    lines = cleaned_text.split('\n')
    filtered_lines = [line for line in lines if line.strip() != '---']
    cleaned_text = '\n'.join(filtered_lines)

    # Rule 4: Check for empty lines, no more than 2 empty lines (\n\n)
    cleaned_text = re.sub(r'\n{3,}', '\n\n', cleaned_text)

    return cleaned_text


async def write_pelican_md_file(query: str, llm_response: str, lang: str, mode: str, translator, stats_data: dict = None, chart_path: str = None, chart_title: str = None):
    md_dir = config.MD_OUTPUT_DIR # User specified path
    os.makedirs(md_dir, exist_ok=True)

    # 1. Strip <think> tags from the response
    llm_response = re.sub(r'<think>.*?</think>', '', llm_response, flags=re.S | re.I).strip()

    # Separate main content from sources
    sources_text = ""
    main_content = llm_response
    try:
        sources_label = translator.get_string('sources_label', lang)
        sources_separator = f"\n\n## {sources_label}:\n"
        if sources_separator in main_content:
            # Split content and sources
            content_parts = main_content.split(sources_separator, 1)
            main_content = content_parts[0]
            sources_text = f"\n\n## {sources_label}:\n{content_parts[1]}"
    except Exception:
        # If sources label not found or any other error, treat the whole response as main content
        pass

    # --- Chart Injection ---
    body_with_chart = main_content
    if chart_path and chart_title:
        # Make chart path relative to the md file's location
        try:
            relative_chart_path = os.path.relpath(chart_path, md_dir)
            # Ensure unix-style separators for markdown URL
            relative_chart_path = relative_chart_path.replace(os.path.sep, '/')
        except ValueError:
            # This can happen on Windows if the paths are on different drives.
            # In a container/unix environment, this is less likely.
            # Fallback to the absolute path.
            relative_chart_path = chart_path

        chart_markdown = f"\n\n![{chart_title}]({relative_chart_path})\n\n"
        
        # Find the end of the first paragraph (double newline)
        paragraphs = main_content.split('\n\n', 1)
        if len(paragraphs) > 1:
            first_paragraph = paragraphs[0]
            rest_of_content = paragraphs[1]
            body_with_chart = f"{first_paragraph}{chart_markdown}{rest_of_content}"
        else:
            # If only one paragraph, append after it
            body_with_chart = f"{main_content}{chart_markdown}"


    # Generate a unique filename
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    query_hash = hashlib.sha256(query.encode('utf-8')).hexdigest()[:8]
    filename = f"{timestamp}-{query_hash}-{mode}-{lang}.md"
    file_path = os.path.join(md_dir, filename)

    # 2. Slug generation removed, placeholder inserted
    slug_placeholder = "[INSERT SLUG HERE]"

    # 5. Append sources where we have them
    body = f"{body_with_chart}{sources_text}"

    # Append statistics if provided
    if stats_data:
        stats_block = f"""## {translate_string('Research Statistics:', lang)}\n
"""
        if "websites_visited" in stats_data:
            stats_block += f"- {translate_string('Websites Visited:', lang)} {stats_data['websites_visited']}\n"
        if "chunks_analyzed" in stats_data:
            stats_block += f"- {translate_string('Chunks Analyzed:', lang)} {stats_data['chunks_analyzed']:,}\n"
        if "total_chars_read" in stats_data:
            stats_block += f"- {translate_string('Total Characters Read:', lang)} {stats_data['total_chars_read']:,}\n"
        # Format the stats block as a markdown comment that is not rendered in HTML
        body += f"\n\n{stats_block.strip()}"

    # 3. Tags: only 1 tag - used mode (translated and cleaned)
    translated_mode = translator.get_string(f"mode_{mode}", lang)
    cleaned_tag = _remove_emojis(translated_mode)

    content = f'''Title: [GENERATE SEO TITLE]
Date: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Category: [INSERT CATEGORY HERE]
Tags: {cleaned_tag}
Slug: {slug_placeholder}
Lang: {lang}
Author: Brainy
Author_Title: {translate_string("Author_Title", lang)}
Question: {query}
Author_Bio: [GENERATE AUTHOR BIO]

{body}
'''
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Successfully wrote Pelican MD file: {file_path}")
    except Exception as e:
        logger.error(f"Failed to write Pelican MD file {file_path}: {e}")


async def fast_web_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    lang = context.chat_data.get('language', 'en')
    translator = context.application.bot_data['translator']
    llm_semaphore = context.application.bot_data["llm_semaphore"]
    ranker = context.application.bot_data["reranker"] # Use shared reranker
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        searcher = search_client.SearchClient(
            config.SEARCH_BACKEND, config.YANDEX_API_KEY
        )
        xml_results = await searcher.search(query, num_results=30)
        yandex_raw = await asyncio.to_thread(xml_parser.parse_yandex_xml, xml_results)

        if not yandex_raw:
            # Fallback to fast reply if no snippets
            async with llm_semaphore:
                final_answer = await llm_client.prompt_without_context(query, lang)
            
            # First, write the clean markdown to the file
            await write_pelican_md_file(query, final_answer, lang, "web", translator)

            final_answer = re.sub(r"<think>.*?</think>", "", final_answer, flags=re.S | re.I).strip()
            telegram_text = escape_markdown_v2(final_answer)
            await send_long_message(update, telegram_text, parse_mode=ParseMode.MARKDOWN_V2)
            await show_mode_menu(context, update.effective_chat.id)
            return

        yandex_chunks = [
            page_processor.TextChunk(text=c.text, source_url=c.url, index=i)
            for i, c in enumerate(yandex_raw)
        ]

        top_chunks = await asyncio.to_thread(
            ranker.rerank, # Use shared ranker
            query,
            yandex_chunks,
            top_n=config.TOP_N,
            threshold=config.RERANK_THRESHOLD,
        )

        if not top_chunks:
            # Fallback to fast reply if no snippets
            async with llm_semaphore:
                final_answer = await llm_client.prompt_without_context(query, lang)
            
            await write_pelican_md_file(query, final_answer, lang, "web", translator)

            final_answer = re.sub(r"<think>.*?</think>", "", final_answer, flags=re.S | re.I).strip()
            telegram_text = escape_markdown_v2(final_answer)
            await send_long_message(update, telegram_text, parse_mode=ParseMode.MARKDOWN_V2)
            await show_mode_menu(context, update.effective_chat.id)
            return

        entities_info = await entity_lookup.get_entity_info(query, lang)
        logger.info(f"Discovered entities: {entities_info}")

        async with llm_semaphore:
            final_answer = await llm_client.generate_answer_from_serp(query, top_chunks, lang, translator, entities_info)

        stats_data = {
            "websites_visited": len(set(c.url for c in yandex_raw)),
            "chunks_analyzed": len(top_chunks),
            "total_chars_read": sum(len(chunk.text) for chunk in top_chunks)
        }
        await write_pelican_md_file(query, final_answer, lang, "web", translator, stats_data)

        final_answer = re.sub(r"<think>.*?</think>", "", final_answer, flags=re.S | re.I).strip()
        telegram_text = escape_markdown_v2(final_answer)
        await send_long_message(update, telegram_text, parse_mode=ParseMode.MARKDOWN_V2)
        await show_mode_menu(context, update.effective_chat.id)

    except Exception as e:
        logger.error("Error in Fast Web mode:", exc_info=True)
        await update.message.reply_text(translator.get_string("error_generic", lang))
        await show_mode_menu(context, update.effective_chat.id)

async def deep_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    lang = context.chat_data.get('language', 'en')
    translator = context.application.bot_data['translator']
    llm_semaphore = context.application.bot_data["llm_semaphore"]
    ranker = context.application.bot_data["reranker"] # Use shared reranker

    await update.message.reply_text(translator.get_string("deep_search_start_message", lang))
    context.chat_data['mode'] = 'fast_reply'
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        async with llm_semaphore:
            entities_info = await entity_lookup.get_entity_info(query, lang)
            logger.info(f"Discovered entities for deep search: {entities_info}")
            sub_queries = await llm_client.get_sub_queries(query, lang)
        searcher = search_client.SearchClient(
            config.SEARCH_BACKEND, config.YANDEX_API_KEY
        )

        all_reranked_chunks_by_query = []
        all_processed_urls = set()

        for sub_query in sub_queries:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            xml_results = await searcher.search(sub_query)
            yandex_raw = await asyncio.to_thread(xml_parser.parse_yandex_xml, xml_results)

            if not yandex_raw:
                continue

            yandex_chunks = [
                page_processor.TextChunk(text=c.text, source_url=c.url, index=i)
                for i, c in enumerate(yandex_raw)
            ]
            
            # Fetch content from new URLs
            urls_to_fetch = {c.url for c in yandex_raw} - all_processed_urls
            web_page_chunks = await page_processor.fetch_and_process_pages(list(urls_to_fetch))
            all_processed_urls.update(urls_to_fetch)

            all_chunks_for_sub_query = yandex_chunks + web_page_chunks

            top_chunks_for_sub_query = await asyncio.to_thread(
                ranker.rerank, # Use shared ranker
                sub_query, # Rerank against the specific sub-query
                all_chunks_for_sub_query,
                top_n=config.TOP_N, # Keep a good number of chunks per sub-query
                threshold=config.RERANK_THRESHOLD,
            )

            if top_chunks_for_sub_query:
                all_reranked_chunks_by_query.append({
                    "query": sub_query,
                    "snippets": top_chunks_for_sub_query
                })

        if not all_reranked_chunks_by_query:
            # Fallback to fast reply if no snippets found for any sub-query
            await update.message.reply_text(
                translator.get_string("error_no_context", lang) + " " + translator.get_string("trying_fast_reply", lang)
            )
            async with llm_semaphore:
                final_answer = await llm_client.prompt_without_context(
                    query,
                    lang,
                    model=config.DEEP_SEARCH_STEP_SIX_MODEL,
                    params=config.CREATIVE_PARAMS,
                )
            top_sources = []
        else:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            async with llm_semaphore:
                final_answer = await llm_client.synthesize_answer(query, all_reranked_chunks_by_query, lang, entities_info)

            # Get top 5 unique source URLs from all reranked chunks
            top_sources = []
            seen_urls = set()
            for result in all_reranked_chunks_by_query:
                for chunk in result['snippets']:
                    if chunk.source_url not in seen_urls:
                        top_sources.append(chunk.source_url)
                        seen_urls.add(chunk.source_url)
                        if len(top_sources) >= 5:
                            break
                if len(top_sources) >= 5:
                    break
        
        # Append sources if available
        if top_sources:
            sources_label = translator.get_string("sources_label", lang)
            sources_text = "\n".join([f"{i+1}. {unquote(url)}" for i, url in enumerate(top_sources)])
            final_answer += f"\n\n{sources_label}:\n{sources_text}"

        # --- Chart Generation ---
        chart_info = None
        try:
            chart_info = await chart_generator.generate_chart(
                final_answer, 
                llm_client.client,
                config.CHARTS_OUTPUT_DIR
            )
        except Exception as e:
            logger.error(f"Chart generation failed: {e}", exc_info=True)

        final_answer = re.sub(r"<think>.*?</think>", "", final_answer, flags=re.S).strip()

        # Collect stats for MD file
        stats_data = {
            "websites_visited": len(all_processed_urls),
            "chunks_analyzed": sum(len(result['snippets']) for result in all_reranked_chunks_by_query),
            "total_chars_read": sum(len(chunk.text) for result in all_reranked_chunks_by_query for chunk in result['snippets'])
        }

        # First, write the clean markdown to the file
        chart_path, chart_title = chart_info if chart_info else (None, None)
        await write_pelican_md_file(query, final_answer, lang, "deep_search", translator, stats_data, chart_path=chart_path, chart_title=chart_title)
        
        # --- Send Chart if available ---
        if chart_path:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=open(chart_path, 'rb'),
                    caption=chart_title
                )
            except Exception as e:
                logger.error(f"Failed to send chart photo: {e}", exc_info=True)

        telegram_text = escape_markdown_v2(final_answer)

        await send_long_message(
            update,
            telegram_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        context.chat_data['mode'] = 'fast_reply'
        await show_mode_menu(context, update.effective_chat.id)
    except Exception as e:
        logger.error("Error in Deep Search:", exc_info=True)
        await update.message.reply_text(translator.get_string("error_generic", lang))
        await show_mode_menu(context, update.effective_chat.id)

async def deep_research_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    lang = context.chat_data.get('language', 'en')
    translator = context.application.bot_data['translator']
    llm_semaphore = context.application.bot_data["llm_semaphore"]
    ranker = context.application.bot_data["reranker"] # Use shared reranker
    
    await update.message.reply_text(translator.get_string("deep_research_start_message", lang))
    context.chat_data['mode'] = 'fast_reply'
    await show_mode_menu(context, update.effective_chat.id) # Show keyboard immediately
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        async with llm_semaphore:
            entities_info = await entity_lookup.get_entity_info(query, lang)
            logger.info(f"Discovered entities for initial query: {entities_info}")
            steps = await llm_client.get_research_steps(query, lang, entities_info)
        if not steps:
            await update.message.reply_text(translator.get_string("error_no_steps", lang))
            return

        research_data: dict[str, str] = {}
        all_top_sources = set() # Initialize a set to collect all unique sources
        total_websites_visited = set()
        total_chunks_analyzed = 0
        total_chars_read = 0

        for step in steps:
            await context.bot.send_chat_action(
                update.effective_chat.id, ChatAction.TYPING
            )

            async with llm_semaphore:
                sub_queries = await llm_client.get_sub_queries(step, lang)
            searcher = search_client.SearchClient(
                search_backend=config.SEARCH_BACKEND,
                api_key=config.YANDEX_API_KEY,
            )

            xml_results = []
            for q in sub_queries:
                xml_result = await searcher.search(q)
                xml_results.append(xml_result)

            yandex_raw_chunks = []
            for xml in xml_results:
                parsed_chunks = await asyncio.to_thread(
                    xml_parser.parse_yandex_xml, xml
                )
                yandex_raw_chunks.extend(parsed_chunks)

            yandex_chunks = [
                page_processor.TextChunk(text=c.text, source_url=c.url, index=i)
                for i, c in enumerate(yandex_raw_chunks)
            ]
            urls = list({c.url for c in yandex_raw_chunks})
            web_page_chunks = await page_processor.fetch_and_process_pages(urls)
            all_chunks = yandex_chunks + web_page_chunks

            # Update stats for this step
            total_websites_visited.update(url for chunk in all_chunks for url in [chunk.source_url])
            total_chunks_analyzed += len(all_chunks)
            total_chars_read += sum(len(chunk.text) for chunk in all_chunks)

            top_chunks = await asyncio.to_thread(
                ranker.rerank, # Use shared ranker
                step,
                all_chunks,
                top_n=config.TOP_N,
                threshold=config.RERANK_THRESHOLD,
            )

            # Filter out duplicate chunks
            unique_top_chunks = _filter_duplicate_chunks(top_chunks)

            # Collect sources for the final output
            for chunk in unique_top_chunks:
                all_top_sources.add(chunk.source_url)

            if unique_top_chunks:
                # Discover entities for the current research step
                entities_info_step = await entity_lookup.get_entity_info(step, lang)
                logger.info(f"Discovered entities for step '{step}': {entities_info_step}")

                # Generate summary for the current research step
                async with llm_semaphore:
                    summary = await llm_client.generate_summary_from_chunks(
                        step, unique_top_chunks, lang, translator, entities_info_step
                    )
                research_data[step] = summary

        if not research_data:
            await update.message.reply_text(translator.get_string("error_no_context", lang))
            return

        # Join all research items and their content together
        joined_research_items = ""
        for step, summary in research_data.items():
            joined_research_items += f"\n\n## {step}\n"
            joined_research_items += summary

        # --- New Map-Reduce Workflow ---
        # 1. Split the combined research data into manageable chunks
        chunk_size = 6000
        chunks = textwrap.wrap(joined_research_items, chunk_size, break_long_words=False, replace_whitespace=False)
        logger.info(f"Split research data into {len(chunks)} chunks for summarization.")

        # 2. Summarize each chunk sequentially to respect rate limits (Map step)
        valid_summaries = []
        for chunk in chunks:
            summary = await llm_client.summarize_research_chunk(chunk, query, lang)
            if summary:
                valid_summaries.append(summary)
        logger.info(f"Generated {len(valid_summaries)} summaries from chunks.")

        # 3. Synthesize the final answer from the summaries (Reduce step)
        async with llm_semaphore:
            final_answer = await llm_client.polish_research_answer("\n\n".join(valid_summaries), query, lang, translator)
        
        # --- Chart Generation ---
        chart_info = None
        try:
            chart_info = await chart_generator.generate_chart(
                final_answer, 
                llm_client.client,
                config.CHARTS_OUTPUT_DIR
            )
        except Exception as e:
            logger.error(f"Chart generation failed: {e}", exc_info=True)

        # Collect stats for MD file
        stats_data = {
            "websites_visited": len(total_websites_visited),
            "chunks_analyzed": total_chunks_analyzed,
            "total_chars_read": total_chars_read
        }
        # First, write the clean markdown to the file
        chart_path, chart_title = chart_info if chart_info else (None, None)
        await write_pelican_md_file(query, final_answer, lang, "deep_research", translator, stats_data, chart_path=chart_path, chart_title=chart_title)
        
        # --- Send Chart if available ---
        if chart_path:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=open(chart_path, 'rb'),
                    caption=chart_title
                )
            except Exception as e:
                logger.error(f"Failed to send chart photo: {e}", exc_info=True)

        telegram_text = escape_markdown_v2(final_answer)

        await send_long_message(
            update,
            telegram_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        context.chat_data['mode'] = 'fast_reply'
        await show_mode_menu(context, update.effective_chat.id)
    except Exception as e:
        logger.error("Error in Deep Research:", exc_info=True)
        await update.message.reply_text(translator.get_string("error_generic", lang))
        await show_mode_menu(context, update.effective_chat.id)

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if 'language' not in context.chat_data:
        await start(update, context)
        return

    # Send an animated hourglass emoji as a status indicator
    status_message = await context.bot.send_message(chat_id, "⏳")

    try:
        voice = update.message.voice
        with tempfile.NamedTemporaryFile(suffix=".oga") as temp_audio_file:
            voice_file = await voice.get_file()
            await voice_file.download_to_drive(temp_audio_file.name)
            
            whisper_model = context.application.bot_data["whisper_model"]
            lang = context.chat_data.get('language', 'en')
            result = await asyncio.to_thread(whisper_model.transcribe, temp_audio_file.name, language=lang, beam_size=3, temperature=0.0, condition_on_previous_text=True)
            transcribed_text = result["text"]
    finally:
        # Delete the hourglass message once transcription is done
        await context.bot.delete_message(chat_id, status_message.message_id)

    if transcribed_text:
        # Send the transcribed text back to the user
        await context.bot.send_message(chat_id, transcribed_text)

        # Add message to buffer and store the latest update object
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
            name=f"process-msg-{chat_id}"
        )
        user_job_trackers[chat_id] = new_job

async def fast_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    lang = context.chat_data.get('language', 'en')
    translator = context.application.bot_data['translator']
    llm_semaphore = context.application.bot_data["llm_semaphore"]

    # Get available modes for the prompt, excluding the current mode
    mode_keys = ["web", "deep_search", "deep_research"]
    available_modes = [translator.get_string(f"mode_{key}", lang) for key in mode_keys]

    translated_mode_names = {
        "fast_reply": translator.get_string("mode_fast_reply", lang),
        "web_search": translator.get_string("mode_web", lang),
        "deep_search": translator.get_string("mode_deep_search", lang),
        "deep_research": translator.get_string("mode_deep_research", lang),
    }

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        async with llm_semaphore:
            final_answer = await llm_client.fast_reply(query, lang, available_modes, translated_mode_names)
        
        final_answer = re.sub(r"<think>.*?</think>", "", final_answer, flags=re.S | re.I).strip()
        
        if llm_client.contains_chinese(final_answer):
            await update.message.reply_text(translator.get_string("error_fast_reply_chinese", lang))
            await show_mode_menu(context, update.effective_chat.id)
            return

        if not final_answer:
            await update.message.reply_text(translator.get_string("error_fast_reply_empty", lang))
            return

        # await write_pelican_md_file(query, final_answer, lang, "fast_reply", translator)
        
        telegram_text = escape_markdown_v2(final_answer)

        await send_long_message(update, telegram_text, parse_mode=ParseMode.MARKDOWN_V2)
        await show_mode_menu(context, update.effective_chat.id)
    except Exception as e:
        logger.error("Error in Fast Reply mode:", exc_info=True)
        await update.message.reply_text(translator.get_string("error_generic", lang))
        await show_mode_menu(context, update.effective_chat.id)

async def deepseek_r1_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    lang = context.chat_data.get('language', 'en')
    translator = context.application.bot_data['translator']
    llm_semaphore = context.application.bot_data["llm_semaphore"]

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        async with llm_semaphore:
            final_answer = await llm_client.deepseek_r1_reply(query, lang)
        
        await write_pelican_md_file(query, final_answer, lang, "deepseek_r1", translator)

        if not final_answer:
            await update.message.reply_text(translator.get_string("error_generic", lang))
            return

        telegram_text = escape_markdown_v2(final_answer)
        await send_long_message(update, telegram_text, parse_mode=ParseMode.MARKDOWN_V2)
        await show_mode_menu(context, update.effective_chat.id)
    except Exception as e:
        logger.error("Error in DeepSeek R1 mode:", exc_info=True)
        await update.message.reply_text(translator.get_string("error_generic", lang))
        await show_mode_menu(context, update.effective_chat.id)

# ---------------------------------------------------------------------------#
#                         Core Logic (Worker)                                #
# ---------------------------------------------------------------------------#
async def worker(name: str, queue: asyncio.PriorityQueue, app_data: dict):
    translator = app_data['translator']
    while True:
        typing_task = None
        try:
            priority, request = await queue.get()

            chat_id = request.chat_id
            update = request.update
            context = request.context
            query = request.query
            lang = context.chat_data.get('language', 'en')

            llm_semaphore = context.application.bot_data["llm_semaphore"]
            if llm_semaphore.locked():
                await update.message.reply_text(translator.get_string("waiting_in_queue", lang))

            mode = context.chat_data.get('mode', 'fast_reply')
            logger.info(f"Worker {name} processing query for chat {chat_id} in mode {mode} with priority {priority}.")

            # Start typing indicator
            typing_task = asyncio.create_task(send_typing_periodically(context.bot, chat_id))

            handler_map = {
                "web": fast_web_handler,
                "deep_research": deep_research_handler,
                "deep_search": deep_search_handler,
                "fast_reply": fast_reply_handler, # New handler for Fast Answer
                "deepseek_r1": deepseek_r1_handler,
            }
            handler = handler_map.get(mode)
            if handler:
                await handler(update, context, query)
            else:
                await update.message.reply_text("Mode not implemented yet.")

        except telegram.error.TimedOut as e:
            logger.error(f"Timeout error in worker {name}: {e}", exc_info=True)
            lang = context.chat_data.get('language', 'en')
            await update.message.reply_text(translator.get_string("error_timeout", lang))
        except Exception as e:
            logger.error(f"Error in worker {name}: {e}", exc_info=True)
            lang = context.chat_data.get('language', 'en')
            await update.message.reply_text(translator.get_string("error_generic", lang))
        finally:
            if typing_task:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
            queue.task_done()

# ---------------------------------------------------------------------------#
#                         Message Handling (Gatekeeper)                       #
# ---------------------------------------------------------------------------#
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    message_text = update.message.text
    if not message_text:
        return

    if 'language' not in context.chat_data:
        await start(update, context)
        return

    # Add message to buffer and store the latest update object
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
        name=f"process-msg-{chat_id}"
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
        "py":"py","python":"py","js":"js","javascript":"js","ts":"ts","typescript":"ts",
        "json":"json","bash":"sh","sh":"sh","shell":"sh","html":"html","css":"css",
        "java":"java","c":"c","cpp":"cpp","c++":"cpp","go":"go","golang":"go",
        "rs":"rs","rust":"rs","rb":"rb","ruby":"rb","php":"php","kt":"kt","kotlin":"kt",
        "swift":"swift","sql":"sql","yaml":"yml","yml":"yml","md":"md","markdown":"md",
        "txt":"txt","text":"txt","": "txt"
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
        out.append(text[pos:m.start()])              # кусок до кода
        ext = _guess_ext(lang)
        bio = io.BytesIO(code.encode("utf-8"))
        bio.name = f"snippet_{idx}.{ext}"
        await update.message.reply_document(InputFile(bio))
        out.append("👆📄📎\n")              # безопасный плейсхолдер
        pos = m.end()
        idx += 1
    out.append(text[pos:])
    return "".join(out)

def restore_link_syntax(text: str) -> str:
    return (text.replace(_PH_LB, '[').replace(_PH_RB, ']')
                .replace(_PH_LP, '(').replace(_PH_RP, ')'))

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
    _DBL_STAR_RE  = re.compile(r'(?<!\\)\*\*')   # неэкранированные **
    _TRIPLE_RE    = re.compile(r'(?<!\\)```')    # неэкранированные ```
    _BACKTICK_RE  = re.compile(r'(?<!\\)`')      # неэкранированные `
    _CODE_SPLIT   = re.compile(r'(```.*?```|`[^`]*`)', re.S)
    _LINK_RE    = re.compile(r'(\[[^\]]+\])\((https?://[^)\s]+)\)')  # [text](url)

    def _is_safe_cut(s: str, idx: int) -> bool:
        if idx <= 0 or idx >= len(s):
            return True
        if s[idx - 1] == '\\':                          # не после обратного слэша
            return False
        if s[idx - 1] == '*' and s[idx] == '*':         # не между '**'
            return False
        if s[idx - 1] == '`' and s[idx] == '`':         # не между '``'
            return False
        if len(_TRIPLE_RE.findall(s[:idx])) % 2 == 1:   # не внутри ``` … ```
            return False
        if len(_BACKTICK_RE.findall(s[:idx])) % 2 == 1: # не внутри ` … `
            return False
        if len(_DBL_STAR_RE.findall(s[:idx])) % 2 == 1: # не при незакрытом **
            return False
        return True

    def _find_safe_cut(s: str, limit: int) -> int:
        end = min(limit, len(s))
        # сначала ищем перевод строки или пробел
        candidates = [s.rfind('\n', 0, end), s.rfind(' ', 0, end)]
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
            if last != -1 and (last == 0 or chunk[last - 1] != '\\'):
                chunk = chunk[:last] + r"\**" + chunk[last + 2:]
        # если заканчивается одиночным '\', удваиваем
        if chunk.endswith('\\') and not chunk.endswith('\\\\'):
            chunk += '\\'
        return chunk

    # --- NEW: маленькие помощники для границы чанка ---
    def _avoid_digit_split(left: str, right: str) -> tuple[str, str]:
        """Если слева оканчивается цифрами, а справа начинается цифрой — не резать '10'."""
        if left and right and left[-1].isdigit() and right[0].isdigit():
            j = len(left) - 1
            while j >= 0 and left[j].isdigit():
                j -= 1
            moved = left[j+1:]           # хвост цифр, например '10'
            return left[:j+1], moved + right
        return left, right

    def _fix_boundary_inside_link(left: str, right: str) -> tuple[str, str]:
        """
        Не резать внутри [текст](url).
        Если слева есть '[' без соответствующего ']' — переносим границу к этому '['.
        Если слева есть ']' и затем незакрытая '(' — переносим границу к ']'.
        """
        lb = left.rfind('[')
        rb = left.rfind(']')
        if lb > rb:  # внутри текста ссылки
            cut = lb
            return left[:cut], left[cut:] + right
        lp = left.rfind('(')
        rp = left.rfind(')')
        if rb != -1 and rb < lp > rp:  # внутри (url)
            cut = rb  # порежем перед '('
            return left[:cut], left[cut:] + right
        return left, right

    # ---------- helpers: fallbacks ----------
    def _escape_hash_and_dot_outside_code(s: str) -> str:
        """Экранируем # и . вне кода и ВНЕ URL."""
        PH_L = '\uF101'; PH_R = '\uF102'  # плейсхолдеры для ( )
        parts = _CODE_SPLIT.split(s)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # прячем ссылки: (url) -> PH_L url PH_R
            seg = _LINK_RE.sub(lambda m: f"{m.group(1)}{PH_L}{m.group(2)}{PH_R}", seg)
            # экранируем
            seg = re.sub(r'(?<!\\)#', r'\#', seg)
            seg = re.sub(r'(?<!\\)\.', r'\.', seg)
            # возвращаем ссылки
            seg = seg.replace(PH_L, '(').replace(PH_R, ')')
            parts[i] = seg
        return ''.join(parts)
    
    def _escape_parens_outside_code(s: str) -> str:
        """Экранируем круглые скобки вне кода и ВНЕ [текст](url)."""
        PH_L = '\uF121'; PH_R = '\uF122'  # плейсхолдеры для ( )
        parts = _CODE_SPLIT.split(s)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # прячем ссылки: (url) -> PH_L url PH_R
            seg = _LINK_RE.sub(lambda m: f"{m.group(1)}{PH_L}{m.group(2)}{PH_R}", seg)
            # экранируем обычные скобки
            seg = re.sub(r'(?<!\\)\(', r'\(', seg)
            seg = re.sub(r'(?<!\\)\)', r'\)', seg)
            # возвращаем ссылки
            seg = seg.replace(PH_L, '(').replace(PH_R, ')')
            parts[i] = seg
        return ''.join(parts)

    def _escape_hyphens_outside_code(s: str) -> str:
        """Экранируем '-' вне кода и ВНЕ URL. Маркеры списков '- ' тоже экранируем."""
        PH_L = '\uF111'; PH_R = '\uF112'
        parts = _CODE_SPLIT.split(s)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # прячем ссылки
            seg = _LINK_RE.sub(lambda m: f"{m.group(1)}{PH_L}{m.group(2)}{PH_R}", seg)
            # списочные маркеры "- " -> "\- "
            seg = re.sub(r'^( *)(-)(\s+)', lambda m: f"{m.group(1)}\\-{m.group(3)}", seg, flags=re.M)
            # остальные дефисы
            seg = re.sub(r'(?<!\\)-', r'\-', seg)
            # возвращаем ссылки
            seg = seg.replace(PH_L, '(').replace(PH_R, ')')
            parts[i] = seg
        return ''.join(parts)

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
                except BadRequest as e: # This is the innermost BadRequest
                    logger.warning(f"Failed to send message with MarkdownV2 after all escapes. Sending as plain text. Error: {e}", exc_info=True)
                    cleaned_final_text = _clean_text_for_plain_send(text)
                    # Send original text, remove parse_mode from kwargs
                    plain_kwargs = {k: v for k, v in kwargs.items() if k != 'parse_mode'}
                    await update.message.reply_text(cleaned_final_text, parse_mode=None, **plain_kwargs)
        return

    rest = text
    # клавиатуру/inline-кнопки показываем только в последнем сообщении
    common_kwargs = {k: v for k, v in kwargs.items() if k != 'reply_markup'}
    last_kwargs   = kwargs

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
                except BadRequest as e: # This is the innermost BadRequest
                    logger.warning(f"Failed to send chunk with MarkdownV2 after all escapes. Sending as plain text. Error: {e}", exc_info=True)
                    cleaned_final_chunk = _clean_text_for_plain_send(chunk)
                    if rest:
                        plain_kwargs = {k: v for k, v in common_kwargs.items() if k != 'parse_mode'}
                        await update.message.reply_text(cleaned_final_chunk, parse_mode=None, **plain_kwargs)
                    else:
                        plain_kwargs = {k: v for k, v in last_kwargs.items() if k != 'parse_mode'}
                        await update.message.reply_text(cleaned_final_chunk, parse_mode=None, **plain_kwargs)

async def process_buffered_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes the buffered messages for a user after the timeout."""
    chat_id = context.job.chat_id

    # Immediately retrieve and clear the user's data to avoid race conditions
    buffered_messages = user_message_buffers.pop(chat_id, [])
    last_update = user_last_update.pop(chat_id, None)
    user_job_trackers.pop(chat_id, None)

    if not buffered_messages or not last_update:
        logger.warning(f"process_buffered_messages called for chat {chat_id} with no data.")
        return

    full_query_text = " ".join(buffered_messages) # Join messages with a space

    logger.info(f"Processing buffered messages for chat {chat_id}. Total messages: {len(buffered_messages)}, Combined length: {len(full_query_text)}.")

    MAX_MESSAGE_LENGTH = 12000
    if len(full_query_text) > MAX_MESSAGE_LENGTH:
        lang = last_update.effective_user.language_code
        translator = context.application.bot_data['translator']
        await last_update.message.reply_text(translator.get_string("error_message_too_long", lang))
        logger.warning(f"Buffered query for chat {chat_id} exceeded max length ({len(full_query_text)} > {MAX_MESSAGE_LENGTH}).")
        return

    # Determine priority based on the mode
    mode = context.chat_data.get('mode', 'fast_reply')
    priorities = {
        "fast_reply": 1,  # Highest priority for fast answers
        "web": 2,
        "deepseek_r1": 3,
        "deep_search": 4,
        "deep_research": 5,  # Lowest priority for the most intensive task
    }
    priority = priorities.get(mode, 3)  # Default to 3

    # Get the request queue from bot_data
    request_queue = context.application.bot_data["request_queue"]
    request = Request(update=last_update, context=context, chat_id=chat_id, query=full_query_text)
    await request_queue.put((priority, request))

    logger.info(f"Buffered query for chat {chat_id} (mode: {mode}, priority: {priority}) submitted to main queue.")

# ---------------------------------------------------------------------------#
#                                   Main                                     #
# ---------------------------------------------------------------------------#

async def main_async() -> None:
    torch.set_num_threads(4)
    translator = Translator('translations.json')
    request_queue = asyncio.PriorityQueue()
    llm_semaphore = asyncio.Semaphore(5)
    # Load the reranker model once at startup
    logger.info("Loading reranker model...")
    reranker_instance = reranker.Reranker(config.RERANK_MODEL)
    logger.info("Reranker model loaded.")
    logger.info("Loading whisper model...")
    whisper_model = whisper.load_model("base")
    logger.info("Whisper model loaded.")
    worker_count = 3

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
    application.bot_data["llm_semaphore"] = llm_semaphore
    application.bot_data["reranker"] = reranker_instance # Add reranker to bot_data
    application.bot_data["whisper_model"] = whisper_model

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    workers = [
        asyncio.create_task(worker(f"Worker-{i+1}", request_queue, application.bot_data))
        for i in range(worker_count)
    ]

    try:
        await application.initialize()
        await application.start()

        while True:
            try:
                logger.info("Starting bot polling...")
                await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                await asyncio.gather(*workers)
                logger.warning("Workers have finished, which is unexpected. Stopping.")
                break

            except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                logger.error(f"Bot polling failed due to network/timeout error: {e}. Retrying in 15 seconds...")
                if application.updater.running:
                    await application.updater.stop()
                await asyncio.sleep(15)
            except Exception as e:
                logger.error(f"An unexpected error occurred in the main loop: {e}", exc_info=True)
                break

    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
    finally:
        logger.info("Shutting down bot...")
        if application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        for w in workers:
            if not w.done():
                w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        logger.info("Bot has been shut down.")

def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Program interrupted by user.")

if __name__ == "__main__":
    main()
