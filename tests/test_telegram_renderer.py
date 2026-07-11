from __future__ import annotations

import asyncio
import unittest

from telegram.error import BadRequest, NetworkError

from telegram_renderer import (
    RichMessageRenderer,
    build_safe_rich_markdown,
    sanitize_untrusted_markdown,
)


class _RawBot:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = failures or []
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def do_api_request(self, endpoint: str, api_kwargs: dict[str, object]) -> bool:
        self.calls.append((endpoint, api_kwargs))
        if self.failures:
            raise self.failures.pop(0)
        return True


class _CancellingBot:
    async def do_api_request(self, endpoint: str, api_kwargs: dict[str, object]) -> bool:
        raise asyncio.CancelledError


class TelegramRendererTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_rich_markdown_preserves_structure_but_blocks_untrusted_media_and_links(
        self,
    ) -> None:
        rendered = build_safe_rich_markdown(
            "# Answer\n\n![track](https://tracker.invalid/pixel)\n"
            '[invented](https://invented.invalid/a_(b) "title") and https://plain.invalid\n\n'
            '![multiline](https://tracker.invalid/p.jpg\n"caption")\n'
            "[invented\nlabel](https://invented.invalid/multiline)\n\n"
            "```python\nprint('<safe>')\n```\n\n$$x^2$$",
            "⚡ 0.4s",
        )

        self.assertIn("# Answer", rendered)
        self.assertNotIn("tracker.invalid", rendered)
        self.assertNotIn("invented.invalid", rendered)
        self.assertIn("https://plain.invalid", rendered)
        self.assertIn("```python", rendered)
        self.assertIn("&lt;safe>", rendered)
        self.assertIn("$$x^2$$", rendered)
        self.assertIn("<footer>⚡ 0.4s</footer>", rendered)

    def test_regular_fallback_neutralizes_plain_urls_without_touching_code(self) -> None:
        rendered = sanitize_untrusted_markdown(
            "See https://invented.invalid and `https://example.test/code`",
            neutralize_plain_urls=True,
        )

        self.assertEqual(
            rendered,
            "See `https://invented.invalid` and `https://example.test/code`",
        )

    async def test_rich_final_uses_public_raw_api_without_paid_or_media_fields(self) -> None:
        bot = _RawBot()
        renderer = RichMessageRenderer(enabled=True)

        sent = await renderer.send_final(bot, chat_id=42, answer="# Fast", badge="⚡ 0.4s")

        self.assertTrue(sent)
        self.assertEqual(bot.calls[0][0], "sendRichMessage")
        payload = bot.calls[0][1]
        self.assertEqual(payload["chat_id"], 42)
        self.assertEqual(
            payload["rich_message"],
            {
                "markdown": "# Fast\n\n<footer>⚡ 0.4s</footer>",
                "skip_entity_detection": True,
            },
        )
        self.assertNotIn("allow_paid_broadcast", payload)
        self.assertNotIn("message_effect_id", payload)

    async def test_rich_final_forwards_reply_markup_when_given(self) -> None:
        bot = _RawBot()
        renderer = RichMessageRenderer(enabled=True)
        markup = {"inline_keyboard": [[{"text": "👍", "callback_data": "x"}]]}

        sent = await renderer.send_final(
            bot, chat_id=42, answer="# Fast", badge="⚡ 0.4s", reply_markup=markup
        )

        self.assertTrue(sent)
        self.assertEqual(bot.calls[0][1]["reply_markup"], markup)

    async def test_rich_final_omits_reply_markup_when_not_given(self) -> None:
        bot = _RawBot()
        renderer = RichMessageRenderer(enabled=True)

        await renderer.send_final(bot, chat_id=42, answer="# Fast", badge="⚡ 0.4s")

        self.assertNotIn("reply_markup", bot.calls[0][1])

    async def test_unsupported_rich_api_opens_circuit_and_falls_back_without_retry(self) -> None:
        bot = _RawBot([BadRequest("method not found")])
        renderer = RichMessageRenderer(enabled=True)

        first = await renderer.send_final(bot, chat_id=42, answer="Answer", badge="⚡ 1.0s")
        second = await renderer.send_final(bot, chat_id=42, answer="Again", badge="⚡ 1.0s")

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(len(bot.calls), 1)

    async def test_transient_network_error_falls_back_without_disabling_future_rich_messages(
        self,
    ) -> None:
        bot = _RawBot([NetworkError("temporary")])
        renderer = RichMessageRenderer(enabled=True)

        self.assertFalse(
            await renderer.send_final(bot, chat_id=42, answer="Answer", badge="⚡ 1.0s")
        )
        self.assertTrue(await renderer.send_final(bot, chat_id=42, answer="Again", badge="⚡ 1.0s"))
        self.assertEqual(len(bot.calls), 2)

    async def test_unexpected_adapter_error_opens_circuit_and_uses_regular_fallback(self) -> None:
        bot = _RawBot([TypeError("wrapper changed")])
        renderer = RichMessageRenderer(enabled=True)

        self.assertFalse(
            await renderer.send_final(bot, chat_id=42, answer="Answer", badge="⚡ 1.0s")
        )
        self.assertFalse(
            await renderer.send_final(bot, chat_id=42, answer="Again", badge="⚡ 1.0s")
        )
        self.assertEqual(len(bot.calls), 1)

    async def test_disabled_or_oversized_rich_path_falls_back_without_api_call(self) -> None:
        bot = _RawBot()

        self.assertFalse(
            await RichMessageRenderer(enabled=False).send_final(
                bot, chat_id=42, answer="Answer", badge="⚡ 1.0s"
            )
        )
        self.assertFalse(
            await RichMessageRenderer(enabled=True, max_chars=10).send_final(
                bot, chat_id=42, answer="A long answer", badge="⚡ 1.0s"
            )
        )
        self.assertEqual(bot.calls, [])

    async def test_cancellation_is_not_converted_into_a_duplicate_fallback(self) -> None:
        renderer = RichMessageRenderer(enabled=True)

        with self.assertRaises(asyncio.CancelledError):
            await renderer.send_final(
                _CancellingBot(), chat_id=42, answer="Answer", badge="⚡ 1.0s"
            )


if __name__ == "__main__":
    unittest.main()
