# Telegram Bot API / PTB Rich Messages Audit

**Date**: 2026-07-11  
**PTB Version**: 22.8  
**Bot API Version**: 10.1 (Rich Messages released 2026-06-11)

---

## Executive Summary

**Bot API 10.1 Rich Messages are NOT yet available in PTB 22.8.**  
No `RichMessage`, `RichText*`, `RichBlock*`, `send_rich_message`, or `send_rich_message_draft` classes/methods exist in PTB 22.8.

---

## Feature Compatibility Matrix

| Feature | Bot API 10.1 | PTB 22.8 | Fallback in Brainy |
|---------|-------------|----------|-------------------|
| `sendRichMessage` | ✅ | ❌ | N/A |
| `sendRichMessageDraft` (streaming) | ✅ | ❌ | Typing + `edit_message_text` chunks |
| `RichTextBold` / `Italic` / `Code` / `Url` | ✅ | ❌ | MarkdownV2 + `MessageEntity` |
| `RichTextMathematicalExpression` | ✅ | ❌ | `$...$` / `$$...$$` (client-side) |
| `RichTextReference` / `RichTextReferenceLink` (citations) | ✅ | ❌ | Manual `[n]` + ref list |
| `RichBlockCode` / `RichBlockPreformatted` | ✅ | ❌ | `\`\`\`lang` fenced blocks |
| `RichBlockBlockQuotation` / `PullQuotation` | ✅ | ❌ | `>` or `BLOCKQUOTE` entity |
| `RichBlockTable` | ✅ | ❌ | Markdown pipe tables |
| `RichBlockDetails` (collapsible) | ✅ | ❌ | No fallback |
| `RichBlockThinking` | ✅ | ❌ | N/A (local model doesn't emit) |
| `message_effect_id` param | ✅ | Param exists | No constants/IDs known |

---

## What Works in PTB 22.8 (Current Brainy Path)

| Feature | Implementation |
|---------|----------------|
| Bold/Italic/Underline/Strikethrough/Spoiler | `parse_mode=MARKDOWN_V2` + `escape_markdown_v2()` |
| Inline code | `\`code\`` via MarkdownV2 |
| Code blocks with language | `\`\`\`python` + `_extract_code_to_files()` for large blocks |
| Blockquotes | `>` prefix or `MessageEntity.BLOCKQUOTE` / `EXPANDABLE_BLOCKQUOTE` |
| Links | `[text](url)` or `TEXT_LINK` entity |
| Math (LaTeX) | `$...$` inline, `$$...$$` display — **client-side only** |
| Tables | Markdown `| a | b |\n|---|---|` |
| Citations | Manual `[1]` in text + `[1]: URL` at bottom (handled by `_SOURCES_LINE`) |
| Streaming | Typing indicator (`send_chat_action`) + periodic `edit_message_text` chunks |

---

## Raw API Required For

- True progressive streaming (`sendRichMessageDraft`)
- Collapsible details (`RichBlockDetails`)
- Native citations with clickable reference links (`RichTextReferenceLink`)
- Message effects (fireworks/confetti — requires Stars, against zero-budget policy)
- Rich block tables with alignment/merging
- Server-side math rendering guarantee

---

## Brainy Stage 1 Decision

**Stick with `MARKDOWN_V2` + current `escape_markdown_v2()` implementation** (`bot.py:153-206`).

Rationale:
- Zero external dependencies
- Works on all Telegram clients (official + third-party)
- No paid features (effects, Stars)
- Progressive delivery achievable via existing typing + edit pattern
- Rich Messages in PTB will arrive in 23.x/24.x — revisit then

---

## Revisit Triggers

- PTB 23.x+ release notes mention `RichMessage`, `send_rich_message`
- Bot API 11.x adds new rich block types
- User demand for collapsible details or native citations exceeds manual workaround pain

---

## Files Referenced

- `bot.py:153-206` — `escape_markdown_v2()` MarkdownV2 sanitizer
- `bot.py:671-842` — `send_long_message()` with code extraction
- `bot.py:348-358` — `send_typing_periodically()` for progressive feel
- `translations.json` — 29 keys × 8 locales, all present