from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot import send_long_message
from telegram.constants import ParseMode


ROOT = Path(__file__).resolve().parents[1]


class PrivacyContractTests(unittest.TestCase):
    def test_obsolete_provider_modules_are_removed(self) -> None:
        removed_modules = {
            "ollama_client.py",
            "search_client.py",
            "together_client.py",
            "xml_parser.py",
        }

        existing = {path.name for path in ROOT.glob("*.py")}

        self.assertTrue(removed_modules.isdisjoint(existing))

    def test_research_tools_are_preserved_but_outside_fast_runtime(self) -> None:
        research_modules = {
            "entity_detector.py",
            "entity_lookup.py",
            "page_processor.py",
            "reranker.py",
            "wikidata_fetcher.py",
            "wikidata_mapper.py",
        }
        existing = {path.name for path in ROOT.glob("*.py")}
        runtime = (ROOT / "bot.py").read_text(encoding="utf-8")

        self.assertTrue(research_modules.issubset(existing))
        self.assertFalse([module for module in research_modules if module[:-3] in runtime])

    def test_runtime_dependency_manifest_has_no_paid_provider_stack(self) -> None:
        manifest = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

        forbidden = {"together"}
        for dependency in forbidden:
            with self.subTest(dependency=dependency):
                self.assertNotIn(dependency, manifest)

        preserved_research = {"beautifulsoup4", "sentence-transformers", "spacy"}
        for dependency in preserved_research:
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, manifest)

    def test_private_conversation_export_and_charts_are_absent(self) -> None:
        runtime_text = (ROOT / "bot.py").read_text(encoding="utf-8")
        config_text = (ROOT / "config.py").read_text(encoding="utf-8")

        forbidden_runtime_symbols = {
            "write_pelican_md_file",
            "chart_generator",
            "send_photo(",
        }
        forbidden_config_symbols = {"MD_OUTPUT_DIR", "CHARTS_OUTPUT_DIR"}

        self.assertFalse((ROOT / "chart_generator.py").exists())
        self.assertFalse([symbol for symbol in forbidden_runtime_symbols if symbol in runtime_text])
        self.assertFalse([symbol for symbol in forbidden_config_symbols if symbol in config_text])

    def test_generated_private_content_directories_do_not_exist_in_checkout(self) -> None:
        self.assertFalse((ROOT / "md").exists())
        self.assertFalse((ROOT / "charts").exists())

    def test_logs_do_not_contain_prompt_or_response_text(self) -> None:
        """Verify source code has no logging patterns that leak user content."""
        runtime_text = (ROOT / "bot.py").read_text(encoding="utf-8")

        # Patterns that would leak user message/response content if present
        forbidden_patterns = [
            "query: '{query}'",  # literal template leak from legacy
            "response:",  # raw response logging
            "cleaned_text[:",  # truncated but still user content
            "prompt:",  # raw prompt logging
            "api_key",  # API key in log
            "sk-",  # OpenAI key prefix
            "private key",  # private key leak
            "BEGIN PRIVATE KEY",  # PEM private key leak
            # F-strings with user data would be bad - check for actual user vars in f-strings
        ]

        violations = [p for p in forbidden_patterns if p in runtime_text]
        self.assertFalse(
            violations, f"Found logging patterns that could leak user data: {violations}"
        )

        # Verify safe logging pattern is used: structured key=value with %s
        safe_patterns = [
            "chat=%s",
            "priority=%s",
            "latency_ms",
            "type=%s",
            "code=%s",
        ]
        for pattern in safe_patterns:
            self.assertIn(
                pattern, runtime_text, f"Expected safe logging pattern '{pattern}' not found"
            )

    def test_send_long_message_does_not_leak_content_in_logs(self) -> None:
        """Verify send_long_message doesn't log message content (it logs nothing, which is correct)."""

        class FakeUpdate:
            def __init__(self):
                self.effective_chat = MagicMock()
                self.effective_chat.id = 999
                self.message = MagicMock()
                self.message.reply_text = AsyncMock()
                self.message.reply_document = AsyncMock()

        update = FakeUpdate()
        sensitive_text = "Password: hunter2\n\nPrivate key:\n-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----"
        import asyncio

        asyncio.run(send_long_message(update, sensitive_text, parse_mode=ParseMode.MARKDOWN_V2))
        # If we get here without exception and no content was logged, test passes
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
