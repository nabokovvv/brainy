from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import _close_dangling_code_fence, _first_http_url, _plain_fallback, send_rich


class FirstHttpUrlTests(unittest.TestCase):
    def test_first_http_url_prefers_markdown_link_target(self):
        self.assertEqual(
            _first_http_url("Read [this](https://example.com/story?q=1) first."),
            "https://example.com/story?q=1",
        )

    def test_first_http_url_falls_back_to_plain_url(self):
        self.assertEqual(
            _first_http_url("see https://example.com/x for more"),
            "https://example.com/x",
        )

    def test_first_http_url_none_when_absent(self):
        self.assertIsNone(_first_http_url("no links here"))


class PlainFallbackTests(unittest.TestCase):
    def test_strips_bold_and_fences(self):
        out = _plain_fallback("**bold** and ```py\nx=1\n```")
        self.assertNotIn("**", out)
        self.assertNotIn("```", out)
        self.assertIn("bold", out)
        self.assertIn("x=1", out)

    def test_link_becomes_text_and_url(self):
        out = _plain_fallback("See [site](https://ex.com) now")
        self.assertIn("site (https://ex.com)", out)

    def test_collapses_blank_lines(self):
        self.assertEqual(_plain_fallback("a\n\n\n\nb"), "a\n\nb")


class CloseDanglingCodeFenceTests(unittest.TestCase):
    def test_closes_unbalanced_fence(self):
        self.assertEqual(_close_dangling_code_fence("```python\nx = 1"), "```python\nx = 1\n```")

    def test_leaves_balanced_fence_untouched(self):
        text = "```python\nx = 1\n```"
        self.assertEqual(_close_dangling_code_fence(text), text)

    def test_no_fence_untouched(self):
        self.assertEqual(_close_dangling_code_fence("plain answer"), "plain answer")


class BadgeSurvivesTruncatedCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_badge_is_separate_text_when_model_truncates_code(self):
        # Long code block with a missing closing fence (token-truncated reply).
        code = "\n".join(f"line_{i} = {i}" for i in range(40))
        answer = f"Here:\n\n```python\n{code}"
        badge = "⚡ 14.6s"
        upd = FakeUpdate()
        await send_rich(upd, f"{_close_dangling_code_fence(answer)}\n\n{badge}")
        # Code goes to a file; the badge must arrive as its own text message,
        # not swallowed into the file.
        upd.message.reply_document.assert_called_once()
        text_calls = [c.args[0] for c in upd.message.reply_text.call_args_list]
        self.assertTrue(any(badge in t for t in text_calls))
        doc = upd.message.reply_document.call_args
        sent_bytes = doc.args[0].input_file_content
        self.assertNotIn(b"14.6s", sent_bytes)


class FakeUpdate:
    def __init__(self):
        self.effective_message = None
        self.message = MagicMock()
        self.message.reply_text = AsyncMock()
        self.message.reply_document = AsyncMock()


class SendRichTests(unittest.IsolatedAsyncioTestCase):
    def _make_update(self):
        return FakeUpdate()

    async def test_empty_text_sends_nothing(self):
        upd = self._make_update()
        await send_rich(upd, "")
        upd.message.reply_text.assert_not_called()
        upd.message.reply_document.assert_not_called()

    async def test_plain_message_sent_once_without_parse_mode(self):
        upd = self._make_update()
        await send_rich(upd, "Just a short answer.")
        upd.message.reply_text.assert_called_once()
        upd.message.reply_document.assert_not_called()
        # Entity-based delivery never sets parse_mode.
        self.assertNotIn("parse_mode", upd.message.reply_text.call_args.kwargs)

    async def test_short_code_block_stays_inline_with_pre_entity(self):
        upd = self._make_update()
        await send_rich(upd, "Here:\n\n```python\ndef add(x, y):\n    return x + y\n```")
        upd.message.reply_document.assert_not_called()
        upd.message.reply_text.assert_called_once()
        entities = upd.message.reply_text.call_args.kwargs.get("entities") or []
        self.assertTrue(any(getattr(e, "type", None) == "pre" for e in entities))

    async def test_long_code_block_extracted_as_file(self):
        upd = self._make_update()
        code = "\n".join(f"line_{i} = {i}" for i in range(40))  # >= 30 lines
        await send_rich(upd, f"Script:\n\n```python\n{code}\n```")
        upd.message.reply_document.assert_called_once()

    async def test_reply_markup_only_on_final_message(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("OK", callback_data="ok")]])
        upd = self._make_update()
        text = "word " * 1400  # forces splitting across several messages
        await send_rich(upd, text, reply_markup=kb)
        calls = upd.message.reply_text.call_args_list
        self.assertGreater(len(calls), 1)
        for call in calls[:-1]:
            self.assertNotIn("reply_markup", call.kwargs)
        self.assertIs(calls[-1].kwargs["reply_markup"], kb)

    async def test_link_preview_options_only_on_final_message(self):
        from telegram import LinkPreviewOptions

        upd = self._make_update()
        options = LinkPreviewOptions(url="https://example.com/story", is_disabled=False)
        text = "word " * 1400 + "\n\n[source](https://example.com/story)"
        await send_rich(upd, text, link_preview_options=options)
        calls = upd.message.reply_text.call_args_list
        self.assertGreater(len(calls), 1)
        for call in calls[:-1]:
            self.assertNotIn("link_preview_options", call.kwargs)
        self.assertIs(calls[-1].kwargs["link_preview_options"], options)

    async def test_plain_fallback_on_telegramify_failure(self):
        upd = self._make_update()
        # None content makes telegramify raise; send_rich must still deliver a reply.
        with patch("bot.telegramify", side_effect=RuntimeError("boom")):
            await send_rich(upd, "**hello**")
        upd.message.reply_text.assert_called_once()
        self.assertNotIn("parse_mode", upd.message.reply_text.call_args.kwargs)

    async def test_transient_network_error_is_retried_then_succeeds(self):
        from telegram.error import NetworkError

        upd = self._make_update()
        # First send raises a transient NetworkError, second succeeds.
        upd.message.reply_text = AsyncMock(side_effect=[NetworkError("blip"), None])
        with patch("bot._SEND_RETRY_BACKOFF", (0,)):
            await send_rich(upd, "Just a short answer.")
        self.assertEqual(upd.message.reply_text.call_count, 2)


class SendWithRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_request_is_not_retried(self):
        from bot import _send_with_retry
        from telegram.error import BadRequest

        send = AsyncMock(side_effect=BadRequest("bad"))
        with self.assertRaises(BadRequest):
            await _send_with_retry(send)
        send.assert_called_once()

    async def test_network_error_retries_then_reraises_when_exhausted(self):
        from bot import _send_with_retry
        from telegram.error import NetworkError

        send = AsyncMock(side_effect=NetworkError("down"))
        with patch("bot._SEND_RETRY_BACKOFF", (0, 0, 0)):
            with self.assertRaises(NetworkError):
                await _send_with_retry(send)
        self.assertEqual(send.call_count, 4)  # initial + 3 retries


if __name__ == "__main__":
    unittest.main()
