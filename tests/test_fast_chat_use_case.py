from __future__ import annotations

import unittest

from brainy_core.use_cases import build_fast_chat_request


class FastChatUseCaseTests(unittest.TestCase):
    def test_request_is_direct_multilingual_and_bounded(self) -> None:
        request = build_fast_chat_request("Как дела?", "ru", max_output_tokens=256)

        self.assertEqual(request.max_output_tokens, 256)
        self.assertEqual(request.messages[0].role, "system")
        self.assertIn("'ru'", request.messages[0].content)
        self.assertIn("internal modes", request.messages[0].content)
        self.assertEqual(request.messages[1].content, "Как дела?")

    def test_request_rejects_empty_user_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            build_fast_chat_request("   ", "en")


if __name__ == "__main__":
    unittest.main()
