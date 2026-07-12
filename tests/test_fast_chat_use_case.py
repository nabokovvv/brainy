from __future__ import annotations

import unittest

from brainy_core.inference import ChatMessage
from brainy_core.use_cases import build_fast_chat_request


class FastChatUseCaseTests(unittest.TestCase):
    def test_request_is_direct_multilingual_and_bounded(self) -> None:
        request = build_fast_chat_request("Как дела?", "ru", max_output_tokens=256)

        self.assertEqual(request.max_output_tokens, 256)
        self.assertEqual(request.messages[0].role, "system")
        self.assertIn("'ru'", request.messages[0].content)
        self.assertIn("Do not invent facts", request.messages[0].content)
        self.assertEqual(request.messages[1].content, "Как дела?")

    def test_request_rejects_empty_user_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            build_fast_chat_request("   ", "en")

    def test_images_attach_to_the_current_turn_only(self) -> None:
        history = (
            ChatMessage(role="user", content="prev question"),
            ChatMessage(role="assistant", content="prev answer"),
        )
        request = build_fast_chat_request(
            "What is in this photo?", "en", history=history, images=("b64data",)
        )

        self.assertEqual(request.messages[-1].images, ("b64data",))
        self.assertEqual(request.messages[1].images, ())
        self.assertEqual(request.messages[2].images, ())

    def test_history_is_inserted_between_system_and_current_turn(self) -> None:
        history = (
            ChatMessage(role="user", content="prev question"),
            ChatMessage(role="assistant", content="prev answer"),
        )
        request = build_fast_chat_request("new question", "en", history=history)

        roles = [m.role for m in request.messages]
        contents = [m.content for m in request.messages]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(contents[1:], ["prev question", "prev answer", "new question"])


if __name__ == "__main__":
    unittest.main()
