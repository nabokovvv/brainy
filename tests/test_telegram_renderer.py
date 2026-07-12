from __future__ import annotations

import unittest

from telegram_renderer import sanitize_untrusted_markdown


class SanitizeUntrustedMarkdownTests(unittest.TestCase):
    def test_strips_model_authored_links_and_images_but_keeps_labels(self) -> None:
        rendered = sanitize_untrusted_markdown(
            "![track](https://tracker.invalid/pixel)\n"
            '[invented](https://invented.invalid/a_(b) "title")\n'
            "[invented\nlabel](https://invented.invalid/multiline)"
        )

        self.assertNotIn("tracker.invalid", rendered)
        self.assertNotIn("invented.invalid", rendered)
        self.assertIn("invented", rendered)

    def test_preserves_code_segments_untouched(self) -> None:
        rendered = sanitize_untrusted_markdown(
            "text [x](https://invented.invalid) `code [keep](https://keep.test)`"
        )

        self.assertNotIn("invented.invalid", rendered)
        # A link inside inline code is content, not a real link: it must survive.
        self.assertIn("`code [keep](https://keep.test)`", rendered)

    def test_neutralizes_plain_urls_without_touching_code(self) -> None:
        rendered = sanitize_untrusted_markdown(
            "See https://invented.invalid and `https://example.test/code`",
            neutralize_plain_urls=True,
        )

        self.assertEqual(
            rendered,
            "See `https://invented.invalid` and `https://example.test/code`",
        )


if __name__ == "__main__":
    unittest.main()
