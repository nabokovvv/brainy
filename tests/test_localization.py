from __future__ import annotations

import json
import string
import unittest
from pathlib import Path

from utils import TRANSLATIONS as CONTENT_TRANSLATIONS


TRANSLATIONS_PATH = Path(__file__).resolve().parents[1] / "translations.json"
EXPECTED_LANGUAGES = {"de", "en", "es", "fr", "id", "pt", "ru", "tr"}
EXPECTED_KEYS = {
    "change_language_button",
    "change_mode_button_text",
    "choose_your_mode",
    "current_mode_button",
    "deep_research_start_message",
    "deep_search_start_message",
    "error_fast_reply_chinese",
    "error_fast_reply_empty",
    "error_generic",
    "error_message_too_long",
    "error_no_context",
    "error_no_steps",
    "error_timeout",
    "explore_sources_button",
    "explore_sources_expired",
    "explore_sources_started",
    "feedback_expired",
    "feedback_recorded_down",
    "feedback_recorded_up",
    "feedback_thumbs_down_button",
    "feedback_thumbs_up_button",
    "keep_language_button",
    "language_selection_prompt",
    "language_updated",
    "mode_deep_research",
    "mode_deep_search",
    "mode_deepseek_r1",
    "mode_fast_reply",
    "mode_switched_to",
    "mode_web",
    "sources_label",
    "trying_fast_reply",
    "waiting_in_queue",
    "web_progress_searching",
    "web_progress_synthesizing",
    "web_status_off",
    "web_status_on",
    "web_unavailable",
    "welcome_new_user",
    "welcome_back",
    "settings_title",
    "settings_language_button",
    "settings_persona_button",
    "settings_memory_button",
    "back_button",
    "memory_short_off",
    "new_chat_confirm",
    "persona_prompt",
    "persona_assistant",
    "persona_assistant_desc",
    "persona_kawaii",
    "persona_kawaii_desc",
    "persona_bro",
    "persona_bro_desc",
    "persona_sarcastic",
    "persona_sarcastic_desc",
    "persona_invalid",
    "persona_set",
    "memory_prompt",
    "memory_off",
    "memory_small",
    "memory_large",
    "memory_invalid",
    "memory_set",
}
EXPECTED_CONTENT_LANGUAGES = {"de", "en", "es", "id", "pt", "ru", "tr"}
EXPECTED_CONTENT_KEYS = {
    "Author_Title",
    "Chunks Analyzed:",
    "Research Statistics:",
    "Total Characters Read:",
    "Websites Visited:",
}


class TranslationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.translations = json.loads(TRANSLATIONS_PATH.read_text(encoding="utf-8"))

    def test_all_locales_keep_the_fixed_language_and_key_baseline(self) -> None:
        self.assertEqual(set(self.translations), EXPECTED_LANGUAGES)
        self.assertEqual(set(self.translations["en"]), EXPECTED_KEYS)

        mismatches = {
            language: {
                "missing": sorted(EXPECTED_KEYS - set(messages)),
                "extra": sorted(set(messages) - EXPECTED_KEYS),
            }
            for language, messages in self.translations.items()
            if set(messages) != EXPECTED_KEYS
        }
        self.assertFalse(mismatches, mismatches)

    def test_existing_content_strategy_translations_are_preserved(self) -> None:
        self.assertEqual(set(CONTENT_TRANSLATIONS), EXPECTED_CONTENT_LANGUAGES)
        for language, messages in CONTENT_TRANSLATIONS.items():
            with self.subTest(language=language):
                self.assertEqual(set(messages), EXPECTED_CONTENT_KEYS)
                self.assertTrue(all(value.strip() for value in messages.values()))

    def test_translations_are_non_empty_and_keep_format_placeholders(self) -> None:
        formatter = string.Formatter()
        expected_placeholders = {
            key: {field_name for _, field_name, _, _ in formatter.parse(value) if field_name}
            for key, value in self.translations["en"].items()
        }

        errors: dict[str, dict[str, object]] = {}
        for language, messages in self.translations.items():
            for key, value in messages.items():
                actual_placeholders = {
                    field_name for _, field_name, _, _ in formatter.parse(value) if field_name
                }
                if not value.strip() or actual_placeholders != expected_placeholders[key]:
                    errors[f"{language}.{key}"] = {
                        "empty": not value.strip(),
                        "expected_placeholders": sorted(expected_placeholders[key]),
                        "actual_placeholders": sorted(actual_placeholders),
                    }

        self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()
