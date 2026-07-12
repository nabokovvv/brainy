"""Sanitization of untrusted (web) model output before Telegram delivery."""

from __future__ import annotations

import re

_CODE_SEGMENTS = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REFERENCE_DEFINITION = re.compile(r"(?mi)^\s*\[[^\]\n]+\]:\s*https?://\S+\s*$")
_PLAIN_URL = re.compile(r"(?i)https?://[^\s<>()]+")


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
