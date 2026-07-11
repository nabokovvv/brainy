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

## Existing Implementation in Repo: `telegram_renderer.py`

A `RichMessageRenderer` class that:
- Calls raw `sendRichMessage` via `bot.do_api_request()`
- Sanitizes markdown: strips tracking pixels, invented URLs, escapes HTML in code blocks
- Circuit-breaker: permanent `BadRequest` → disables rich for process lifetime
- Transient errors (network) → fallback without disabling
- Falls back to regular `sendMessage` on any failure
- **No paid features**: omits `allow_paid_broadcast`, `message_effect_id`

### Test Coverage (5 tests)
- ✅ Sanitization preserves structure, blocks untrusted media/links
- ✅ Successful rich send uses `sendRichMessage` with correct payload
- ✅ Unsupported API → circuit opens, no retry
- ✅ Transient network error → fallback, circuit stays closed
- ✅ Disabled / oversized → immediate fallback, no API call

---

## Compatibility / Fallback Checklist

### For Each Message Sent
- [ ] **Try rich path first** (if enabled and size < limit)
- [ ] **On success**: done
- [ ] **On `BadRequest` / method not found**: disable rich for process, fallback
- [ ] **On transient error** (NetworkError, Timeout, RetryAfter): fallback, keep rich enabled
- [ ] **Fallback**: `sendMessage` with MarkdownV2, same sanitized content
- [ ] **Never include**: `allow_paid_broadcast`, `message_effect_id`, `star_count`

### Sanitization Rules (Applied to Both Paths)
- [ ] Strip `<img>` / `![...](tracking-url)` → tracking pixels
- [ ] Strip invented markdown links `[text](https://invented.invalid)` 
- [ ] Keep plain HTTPS URLs (auto-link)
- [ ] Escape HTML inside code blocks (`<` → `<`)
- [ ] Preserve: headings, code blocks, math (`$$...$$`), blockquotes
- [ ] Add footer badge: `<footer>⚡ X.Xs</footer>`

### Configuration
| Setting | Default | Notes |
|---------|---------|-------|
| `RICH_MESSAGES_ENABLED` | `true` | Can be disabled via env |
| `RICH_MAX_CHARS` | `4096` | Telegram message limit |
| `RICH_CIRCUIT_BREAKER_TTL` | `infinite` | Process lifetime once opened |

---

## Recommendation for Stage 1

**Current approach is correct**: Use `telegram_renderer.RichMessageRenderer` with raw API calls + circuit breaker + MarkdownV2 fallback.

**Do NOT**:
- Wait for PTB to add wrappers (could be months)
- Use paid features (Stars, paid broadcast, effects) — budget = $0
- Assume rich messages work on all clients (older Telegram apps may not render)

**DO**:
- Keep fallback path tested and fast
- Monitor `BadRequest` rate in logs (circuit open = expected on older API servers)
- Add telemetry: rich_sent / rich_failed / fallback_used counters