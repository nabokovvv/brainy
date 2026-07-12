"""Unit tests for the provider-neutral inference contracts."""

from __future__ import annotations

import unittest

from brainy_core.inference import MAX_IMAGES_PER_MESSAGE, ChatMessage


class ChatMessageImageTests(unittest.TestCase):
    def test_accepts_text_with_no_images(self) -> None:
        message = ChatMessage(role="user", content="hello")
        self.assertEqual(message.images, ())

    def test_accepts_an_image_with_empty_caption(self) -> None:
        message = ChatMessage(role="user", content="", images=("YmFzZTY0",))
        self.assertEqual(message.content, "")
        self.assertEqual(message.images, ("YmFzZTY0",))

    def test_rejects_empty_content_and_no_images(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            ChatMessage(role="user", content="   ")

    def test_rejects_too_many_images(self) -> None:
        images = tuple(f"img{i}" for i in range(MAX_IMAGES_PER_MESSAGE + 1))
        with self.assertRaisesRegex(ValueError, "at most"):
            ChatMessage(role="user", content="caption", images=images)

    def test_rejects_blank_image_entries(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty base64"):
            ChatMessage(role="user", content="caption", images=("",))

    def test_images_is_normalized_to_a_tuple(self) -> None:
        message = ChatMessage(role="user", content="caption", images=["a", "b"])
        self.assertEqual(message.images, ("a", "b"))


if __name__ == "__main__":
    unittest.main()
