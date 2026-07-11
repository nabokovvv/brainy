# Telegram Bot API / PTB Rich Messages Audit

**Date**: 2026-07-11  
**PTB Version**: 22.8  
**Bot API Version**: 10.1 (Rich Messages released 2026-06-11)

---

## Executive Summary

PTB 22.8 has no typed wrappers for Bot API 10.1 Rich Messages, but its public
`Bot.do_api_request()` escape hatch supports new raw endpoints. Brainy uses that path
for `sendRichMessage` and retains the wrapped MarkdownV2/plain sender as a mandatory
persistent fallback.

---

## Feature Compatibility Matrix

| Feature | Bot API 10.1 | PTB 22.8 | Fallback in Brainy |
|---------|-------------|----------|-------------------|
| `sendRichMessage` | ✅ | Raw only | `do_api_request` -> MarkdownV2/plain |
| `sendRichMessageDraft` (streaming) | ✅ | Raw only | wrapped `sendMessageDraft` |
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
| Streaming | Same-ID `sendMessageDraft` fed by provider token deltas; typing fallback |

---

## Raw API Required For

- Full rich-draft blocks (`sendRichMessageDraft`); Brainy currently streams plain drafts
- Collapsible details (`RichBlockDetails`)
- Native citations with clickable reference links (`RichTextReferenceLink`)
- Rich message effects; paid broadcasts remain forbidden, ordinary effects are optional
- Rich block tables with alignment/merging
- Server-side math rendering guarantee

---

## Brainy Stage 1 Decision

Use raw `sendRichMessage` through PTB's supported `do_api_request`, guarded by a
process-level circuit breaker and a `TELEGRAM_RICH_MESSAGES` switch. Fall back to the
existing MarkdownV2/plain sender on unsupported, oversized, rejected, or transient
calls.

Rationale:
- Zero external dependencies
- Works on all Telegram clients (official + third-party)
- No paid features (effects, Stars)
- Native headings/lists/code/math are available today without waiting for a wrapper
- Model-authored HTML, links and remote media are removed before either render path
- A transport failure is ambiguous because Telegram has no send idempotency key;
  delivery-first fallback can rarely duplicate a final rather than lose it

---

## Revisit Triggers

- PTB release notes add typed `RichMessage` / `send_rich_message` wrappers
- Bot API 11.x adds new rich block types
- User demand for collapsible details or native citations exceeds manual workaround pain

---

## Files Referenced

- `bot.py:153-206` — `escape_markdown_v2()` MarkdownV2 sanitizer
- `bot.py:671-842` — `send_long_message()` with code extraction
- `telegram_renderer.py` — safe raw rich sender and circuit breaker
- `bot.py` — provider streaming, draft publisher and regular persistent fallback
- `translations.json` — 29 keys × 8 locales, all present
