"""Fail-soft Telegram Bot API 10.1 rich final-message adapter."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

logger = logging.getLogger(__name__)

_RICH_MESSAGE_LIMIT = 32_768
_CODE_SEGMENTS = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REFERENCE_DEFINITION = re.compile(r"(?mi)^\s*\[[^\]\n]+\]:\s*https?://\S+\s*$")
_PLAIN_URL = re.compile(r"(?i)https?://[^\s<>()]+")


def _escape_rich_html(text: str) -> str:
    """Neutralize raw HTML without breaking Markdown blockquote markers."""

    return text.replace("&", "&amp;").replace("<", "&lt;")


def _delimiter_pairs(text: str, opening: str, closing: str) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == opening:
            stack.append(index)
        elif char == closing and stack:
            pairs[stack.pop()] = index
    return pairs


def _strip_markdown_links(text: str) -> str:
    """Remove inline-link and image destinations, including nested parentheses."""

    output: list[str] = []
    bracket_pairs = _delimiter_pairs(text, "[", "]")
    parenthesis_pairs = _delimiter_pairs(text, "(", ")")
    index = 0
    while index < len(text):
        label_start = index + 1 if text.startswith("![", index) else index
        if text.startswith("[", label_start):
            label_end = bracket_pairs.get(label_start)
            if label_end is not None and label_end + 1 < len(text) and text[label_end + 1] == "(":
                target_end = parenthesis_pairs.get(label_end + 1)
                if target_end is not None:
                    output.append(text[label_start + 1 : label_end])
                    index = target_end + 1
                    continue
        output.append(text[index])
        index += 1
    return "".join(output)


def sanitize_untrusted_markdown(answer: str, *, neutralize_plain_urls: bool = False) -> str:
    """Remove model-controlled links/media while preserving code and useful structure."""

    parts = _CODE_SEGMENTS.split(answer)
    for index, part in enumerate(parts):
        if not part or index % 2 == 1:
            continue
        part = _REFERENCE_DEFINITION.sub("", part)
        part = _strip_markdown_links(part)
        if neutralize_plain_urls:
            part = _PLAIN_URL.sub(lambda match: f"`{match.group(0)}`", part)
        parts[index] = part
    return "".join(parts).strip()


def build_safe_rich_markdown(answer: str, badge: str) -> str:
    """Keep useful Markdown while blocking model-authored links, HTML, and remote media."""

    parts = _CODE_SEGMENTS.split(sanitize_untrusted_markdown(answer))
    for index, part in enumerate(parts):
        if not part:
            continue
        parts[index] = _escape_rich_html(part)
    safe_answer = "".join(parts).strip()
    safe_badge = _escape_rich_html(badge.strip())
    return f"{safe_answer}\n\n<footer>{safe_badge}</footer>"


class RichMessageRenderer:
    """Try Bot API 10.1 once, then let the caller use its stable fallback.

    Transport failures are ambiguous because Bot API sends have no idempotency key.
    Brainy is delivery-first: the regular fallback may rarely duplicate a rich message
    that Telegram accepted immediately before the connection failed.
    """

    def __init__(self, *, enabled: bool, max_chars: int = _RICH_MESSAGE_LIMIT) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self._enabled = enabled
        self._max_chars = min(max_chars, _RICH_MESSAGE_LIMIT)
        self._supported = True

    async def send_final(
        self,
        bot: Any,
        *,
        chat_id: int,
        answer: str,
        badge: str,
    ) -> bool:
        """Return False when the regular persistent-message fallback should run."""

        if not self._enabled or not self._supported:
            return False
        rich_markdown = build_safe_rich_markdown(answer, badge)
        if len(rich_markdown) > self._max_chars:
            return False
        do_api_request = getattr(bot, "do_api_request", None)
        if not callable(do_api_request):
            self._supported = False
            return False

        payload: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": {
                "markdown": rich_markdown,
                "skip_entity_detection": True,
            },
        }
        try:
            await asyncio.wait_for(
                do_api_request("sendRichMessage", api_kwargs=payload),
                timeout=5,
            )
            return True
        except BadRequest:
            self._supported = False
            logger.info("Telegram rich message unsupported; circuit opened for this process")
            return False
        except (TimeoutError, NetworkError, RetryAfter, TimedOut):
            logger.info("Telegram rich message transiently unavailable; using regular fallback")
            return False
        except TelegramError:
            self._supported = False
            logger.info("Telegram rich message unsupported; circuit opened for this process")
            return False
        except Exception as exc:
            self._supported = False
            logger.warning(
                "Telegram rich renderer failed type=%s; using regular fallback",
                type(exc).__name__,
            )
            return False
