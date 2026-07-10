from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PrivacyContractTests(unittest.TestCase):
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
