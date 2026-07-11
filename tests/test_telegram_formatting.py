from __future__ import annotations

import asyncio
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import (
    _clean_text_for_plain_send,
    _extract_code_to_files,
    send_long_message,
)
from telegram.constants import ParseMode


class FakeUpdate:
    def __init__(self):
        self.effective_chat = MagicMock()
        self.effective_chat.id = 12345
        self.message = MagicMock()
        self.message.reply_text = AsyncMock()
        self.message.reply_document = AsyncMock()


class SendLongMessageTests(unittest.IsolatedAsyncioTestCase):
    MAX = 4096

    def _make_update(self):
        return FakeUpdate()

    # --- Code block extraction ---
    async def test_small_code_block_stays_inline(self):
        upd = self._make_update()
        text = "```python\nprint('hi')\n```"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        upd.message.reply_text.assert_called_once()
        upd.message.reply_document.assert_not_called()

    async def test_large_code_block_extracted_as_file(self):
        upd = self._make_update()
        code = "x = 1\n" * 500  # > 2000 chars
        text = f"```python\n{code}\n```"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        upd.message.reply_document.assert_called_once()
        call_text = upd.message.reply_text.call_args[0][0]
        self.assertIn("👆📄📎", call_text)

    async def test_multiple_large_code_blocks_each_get_file(self):
        upd = self._make_update()
        code1 = "a = 1\n" * 500
        code2 = "b = 2\n" * 500
        text = f"```python\n{code1}\n```\n\n```js\n{code2}\n```"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        self.assertEqual(upd.message.reply_document.call_count, 2)

    # --- Safe splitting ---
    async def test_no_split_under_limit(self):
        upd = self._make_update()
        text = "short message"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        upd.message.reply_text.assert_called_once()

    async def test_split_at_newline_not_mid_entity(self):
        upd = self._make_update()
        # Bold spans across potential split point
        text = "x " * 2000 + "**bold text**\n" + "y " * 2000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        self.assertEqual(upd.message.reply_text.call_count, 2)
        # Both calls should have balanced ** in each chunk
        for call in upd.message.reply_text.call_args_list:
            chunk = call[0][0]
            self.assertEqual(chunk.count("**") % 2, 0)

    async def test_avoid_digit_split(self):
        upd = self._make_update()
        # "10. text" should not be split as "1" + "0. text"
        text = "a " * 2040 + "10. item\n" + "b " * 2040
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        calls = upd.message.reply_text.call_args_list
        # Verify no chunk ends with lone digit before ". "
        for call in calls:
            chunk = call[0][0]
            self.assertNotRegex(chunk, r"\d$")

    async def test_no_split_inside_link(self):
        upd = self._make_update()
        link = "[text](https://example.com/very/long/url/path/here)"
        text = "x " * 2000 + link + " y " * 2000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        calls = upd.message.reply_text.call_args_list
        # Link should stay intact in one chunk
        all_chunks = "".join(c[0][0] for c in calls)
        self.assertIn(link, all_chunks)

    # --- Fallback chain ---
    async def test_fallback_hash_dot_then_hyphen_then_plain(self):
        upd = self._make_update()
        # Craft text that fails MARKDOWN_V2: starts with # (heading)
        text = "# Heading\n" + "x " * 4000
        # First call fails, second escapes #, third escapes -, fourth goes plain
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        # Should eventually send as plain text
        self.assertTrue(upd.message.reply_text.called)

    async def test_fallback_preserves_content(self):
        upd = self._make_update()
        text = "### Title\n\n**bold** and `code` and [link](https://ex.com)\n\n" + "x " * 4000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        # All reply_text calls made
        self.assertGreater(upd.message.reply_text.call_count, 1)

    # --- Entity balancing ---
    async def test_unbalanced_bold_closed_in_chunk(self):
        upd = self._make_update()
        text = "**bold start\n" + "x " * 2000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        # Find chunk with the bold marker and verify it's balanced
        for chunk, _ in upd.message.reply_text.call_args_list:
            if "**bold start" in chunk:
                self.assertEqual(chunk.count("**") % 2, 0, f"Bold should be balanced in chunk: {repr(chunk[-50:])}")
                return
        self.fail("No chunk contained the bold marker")

    async def test_unbalanced_code_fence_closed_in_chunk(self):
        upd = self._make_update()
        text = "```python\ncode continues\n" + "x " * 2000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        for chunk, _ in upd.message.reply_text.call_args_list:
            if "```python" in chunk:
                self.assertTrue(chunk.endswith("```"), f"Code fence should be closed: {repr(chunk[-50:])}")
                return
        self.fail("No chunk contained the code fence")

    async def test_unbalanced_backtick_closed_in_chunk(self):
        upd = self._make_update()
        text = "`inline code\n" + "x " * 2000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        for chunk, _ in upd.message.reply_text.call_args_list:
            if "`inline code" in chunk:
                self.assertTrue(chunk.endswith("`"), f"Backtick should be closed: {repr(chunk[-50:])}")
                return
        self.fail("No chunk contained the backtick")

    async def test_trailing_backslash_doubled(self):
        upd = self._make_update()
        text = "ends with backslash \\\n" + "x " * 2000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        for chunk, _ in upd.message.reply_text.call_args_list:
            if "ends with backslash" in chunk:
                self.assertTrue(chunk.endswith("\\\\"), f"Should end with double backslash: {repr(chunk[-50:])}")
                return
        self.fail("No chunk contained the trailing backslash")

    # --- Reply markup only on last chunk ---
    async def test_reply_markup_only_on_final_chunk(self):
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("OK", callback_data="ok")]])
        upd = self._make_update()
        text = "x " * 5000
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        calls = upd.message.reply_text.call_args_list
        # All but last should not have reply_markup
        for i, call in enumerate(calls[:-1]):
            self.assertNotIn("reply_markup", call.kwargs)
        # Last should have it
        self.assertIn("reply_markup", calls[-1].kwargs)

    # --- _clean_text_for_plain_send ---
    def test_clean_text_for_plain_send_removes_backslashes_asterisks_urls(self):
        text = r"Hello *world* [link](https://ex.com) ---"
        cleaned = _clean_text_for_plain_send(text)
        self.assertNotIn("\\", cleaned)
        self.assertNotIn("*", cleaned)
        self.assertNotIn("(https://ex.com)", cleaned)
        # --- is only removed when on its own line
        self.assertIn("---", cleaned)

    def test_clean_text_for_plain_send_removes_standalone_dashes(self):
        text = "Hello\n---\nWorld"
        cleaned = _clean_text_for_plain_send(text)
        self.assertNotIn("---", cleaned)
        self.assertEqual(cleaned, "Hello\nWorld")

    def test_clean_text_for_plain_send_max_two_empty_lines(self):
        text = "a\n\n\n\nb"
        cleaned = _clean_text_for_plain_send(text)
        self.assertEqual(cleaned.count("\n\n"), 1)  # max two newlines -> one empty line

    # --- _extract_code_to_files edge cases ---
    async def test_code_block_at_boundary(self):
        upd = self._make_update()
        code = "x = 1\n" * 500
        text = f"before\n```python\n{code}\n```\nafter"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        upd.message.reply_document.assert_called_once()
        reply_text = upd.message.reply_text.call_args[0][0]
        self.assertIn("before", reply_text)
        self.assertIn("after", reply_text)

    async def test_empty_code_block_not_extracted(self):
        upd = self._make_update()
        text = "```python\n```\ntext"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        upd.message.reply_document.assert_not_called()

    async def test_inline_code_not_extracted(self):
        upd = self._make_update()
        text = "Use `print()` function"
        await send_long_message(upd, text, parse_mode=ParseMode.MARKDOWN_V2)
        upd.message.reply_document.assert_not_called()


class CleanTextTests(unittest.TestCase):
    def test_strips_markdown(self):
        assert _clean_text_for_plain_send("*bold*") == "bold"
        assert _clean_text_for_plain_send(r"\*escaped\*") == "*escaped*"
        assert _clean_text_for_plain_send("[text](url)") == "text"
        assert _clean_text_for_plain_send("---") == ""

    def test_preserves_newlines_up_to_two(self):
        assert _clean_text_for_plain_send("a\nb") == "a\nb"
        assert _clean_text_for_plain_send("a\n\nb") == "a\n\nb"
        assert _clean_text_for_plain_send("a\n\n\nb") == "a\n\nb"
        assert _clean_text_for_plain_send("a\n\n\n\nb") == "a\n\nb"


if __name__ == "__main__":
    unittest.main()