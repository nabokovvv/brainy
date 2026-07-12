# Telegram Bot API / PTB Rich Messages — Compatibility Audit (2026-07-11)

## Environment
- **PTB version**: 22.8
- **Bot API version**: 10.1 (June 11, 2026 release)
- **Python**: 3.12

---

## Bot API 10.1 Rich Messages — What Exists vs. What PTB 22.8 Exposes

| Feature | Bot API 10.1 | PTB 22.8 | Status |
|---------|--------------|----------|--------|
| `sendRichMessage` | ✅ | ❌ (no wrapper) | **Raw API only** |
| `sendRichMessageDraft` (streaming) | ✅ | ❌ | **Raw API only** |
| `RichMessage` / `InputRichMessage` types | ✅ | ❌ | **Raw API only** |
| `RichText*` inline formatting (bold, italic, code, math, spoiler, etc.) | ✅ | ❌ | **Raw API only** |
| `RichBlock*` structures (paragraph, heading, code, quote, table, collage, etc.) | ✅ | ❌ | **Raw API only** |
| `RichTextMathematicalExpression` (`$$...$$`) | ✅ | ❌ | **Raw API only** |
| `RichTextCode` / `RichBlockPreformatted` | ✅ | ❌ | **Raw API only** |
| `RichBlockBlockQuotation` / `RichBlockPullQuotation` | ✅ | ❌ | **Raw API only** |
| `RichTextReference` / `RichTextReferenceLink` (citations) | ✅ | ❌ | **Raw API only** |
| `MessageEntity` types | 21 types | 21 types | **Full parity** |
| `ParseMode.MARKDOWN_V2` | ✅ | ✅ | **Full parity** |
| `ParseMode.HTML` | ✅ | ✅ | **Full parity** |
| `message_effect_id` | ✅ | ✅ (param) | **Full parity** |
| `allow_paid_broadcast` | ✅ | ✅ (param) | **Full parity** (but budget = $0) |

---

## Current PTB 22.8 Capabilities (No Rich API)

### Available via `sendMessage`
| Feature | MarkdownV2 | HTML |
|---------|------------|------|
| Bold (`**text**`) | ✅ | ✅ |
| Italic (`_text_`) | ✅ | ✅ |
| Inline code (`` `code` ``) | ✅ | ✅ |
| Code blocks (```lang\ncode\n```) | ✅ | ✅ |
| Underline (`__text__`) | ✅ | ✅ |
| Strikethrough (`~~text~~`) | ✅ | ✅ |
| Spoiler (`||text||`) | ✅ | ✅ |
| Blockquote (`> text`) | ✅ | ✅ |
| Expandable blockquote (`>! text`) | ✅ | ✅ |
| Links (`[text](url)`) | ✅ | ✅ |
| Mentions (`@user`) | ✅ | ✅ |
| Hashtags/Cashtags | ✅ | ✅ |
| Custom emoji | ✅ (entity) | ✅ (entity) |

### NOT Available in PTB 22.8 (Require Bot API 10.1 Raw Calls)
| Feature | Workaround |
|---------|------------|
| Mathematical expressions (`$$x^2$$`) | Plain text or image |
| Structured citations (`[^1]` → reference link) | Manual footnote text |
| Tables | Monospace code block |
| Collages / Slideshows | Multiple messages |
| Streaming partial rich updates | Draft messages (`sendMessageDraft`) |
| Rich block quotes with attribution | `> quote\n> — author` |

---

## Current Implementation (2026-07-12 update)

The raw `sendRichMessage` path described above was scaffolded (`telegram_renderer.RichMessageRenderer`,
registered in `bot_data`) but never actually called by a real handler, and was
removed. Brainy instead delivers formatting through `bot.send_rich()`, which
converts the model's markdown into `MessageEntity` lists via
`telegramify-markdown` and sends them with `entities=...` — no `parse_mode`,
no manual escaping. See `docs/RICH_MESSAGES_AUDIT.md` for the current design
and rationale; this file is kept for its Bot API 10.1 / PTB 22.8 compatibility
matrix above, which is still accurate.

**Do NOT**:
- Reintroduce hand-written MarkdownV2 escaping regexes — that's exactly what
  this migration removed after it caused hard-to-debug `BadRequest` fallbacks.
- Wait for PTB to add Rich Message wrappers before revisiting raw `sendRichMessage`
  (could be months) unless a concrete feature need (tables, math, collapsible
  sections) can't be expressed with entities.
- Use paid features (Stars, paid broadcast, effects) — budget = $0.

**DO**:
- Keep `send_rich()`'s plain-text fallback tested and fast.
- If `telegramify-markdown` mis-renders some model output, fix/report it
  upstream or adjust the call site — not by re-adding manual escaping.