# Telegram Rich Formatting — Current Approach

**Updated**: 2026-07-12
**PTB Version**: 22.8
**Delivery**: entity-based, via `telegramify-markdown`

---

## Executive Summary

Brainy no longer hand-escapes MarkdownV2 or calls the raw `sendRichMessage`
Bot API 10.1 endpoint. Both were replaced by `bot.send_rich()`, which converts
the model's CommonMark output into Telegram `MessageEntity` lists via the
`telegramify-markdown` library and sends them with `entities=...` and no
`parse_mode` at all. Because entities are passed as structured data instead of
a delimited string, there is nothing for Telegram to reject as "can't parse
entities" — the whole class of `BadRequest` failures that motivated the old
three-step MarkdownV2 escaping fallback is gone by construction.

---

## What `send_rich()` Does (`bot.py`)

1. `telegramify(content, max_message_length=4096, min_file_lines=30, render_mermaid=False)`
   splits the model's raw markdown into an ordered list of `Text` / `File`
   boxes.
2. `Text` boxes carry `.text` (plain string, no markdown syntax) plus
   `.entities` (bold, italic, `pre` code blocks, `text_link`, etc.) — sent via
   `reply_text(box.text, entities=box.entities)`.
3. `File` boxes (code fences with 30+ lines) are sent via `reply_document`,
   same threshold decision the old `_extract_code_to_files` made, just
   delegated to the library.
4. `reply_markup` / `link_preview_options` are attached only to the last box
   in the sequence.
5. Any unexpected failure (library exception or `BadRequest` from Telegram)
   falls back to `_plain_fallback()` — a small, code-fence-aware stripper that
   sends a plain-text message so a reply is never dropped.

Untrusted web content is still passed through
`telegram_renderer.sanitize_untrusted_markdown()` before `send_rich()`, which
strips model-echoed links/images (but leaves inline/fenced code untouched) —
that security control is independent of the delivery mechanism and unchanged
by this migration.

---

## What Changed From the Previous Approach

| | Before | Now |
|---|---|---|
| Escaping | ~40 hand-written regexes (`escape_markdown_v2`) | None — entities are structured data |
| Splitting long messages | Custom safe-cut logic guessing at entity boundaries | `telegramify`'s own splitter |
| Long code blocks | `_extract_code_to_files` (2000-char threshold) | `telegramify`'s file boxes (30-line threshold) |
| Failure fallback | 3-step MarkdownV2 re-escape, then a fallback that also stripped `*`/`\` from code | Single plain-text fallback that leaves code content alone |
| Raw `sendRichMessage` (Bot API 10.1) | `telegram_renderer.RichMessageRenderer`, wired into `bot_data` but never actually called from any handler | Removed — dead code |

The raw `sendRichMessage` path (`RichBlockCode`, `RichTextMathematicalExpression`,
etc.) was audited and scaffolded but never reached from a real handler; it added
a circuit breaker and a config flag (`TELEGRAM_RICH_MESSAGES`) with no live
caller. Both were deleted rather than kept as unused surface area.

---

## Revisit Triggers

- `telegramify-markdown` stops covering a formatting case the model produces
  (check its `markdownify`/`telegramify` test suite first).
- PTB ships typed wrappers for Bot API 10.1 Rich Messages and there's a
  concrete feature need (tables, collapsible sections, math) that entities
  can't express.

---

## Files Referenced

- `bot.py` — `send_rich()`, `_plain_fallback()`, `_first_http_url()`,
  `_visible_link_preview()`
- `telegram_renderer.py` — `sanitize_untrusted_markdown()` (untrusted web
  content only)
- `pyproject.toml` — `telegramify-markdown` dependency
