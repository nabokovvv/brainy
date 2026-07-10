from __future__ import annotations

import unittest
from pathlib import Path


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
        self.assertFalse(
            [symbol for symbol in forbidden_runtime_symbols if symbol in runtime_text]
        )
        self.assertFalse(
            [symbol for symbol in forbidden_config_symbols if symbol in config_text]
        )

    def test_generated_private_content_directories_do_not_exist_in_checkout(self) -> None:
        self.assertFalse((ROOT / "md").exists())
        self.assertFalse((ROOT / "charts").exists())


if __name__ == "__main__":
    unittest.main()
