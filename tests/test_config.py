from __future__ import annotations

import unittest
import os
import subprocess
import sys

from config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_local_settings_need_no_external_keys(self) -> None:
        settings = Settings.from_env({})

        settings.validate()

        self.assertEqual(settings.llm_client, "ollama")
        self.assertEqual(settings.search_backend, "disabled")
        self.assertFalse(settings.web_enabled_default)

    def test_telegram_token_is_checked_only_for_bot_runtime(self) -> None:
        settings = Settings.from_env({})

        with self.assertRaisesRegex(ConfigurationError, "TELEGRAM_TOKEN"):
            settings.validate(require_telegram=True)

    def test_whitespace_token_is_not_accepted(self) -> None:
        settings = Settings.from_env({"TELEGRAM_TOKEN": "   "})

        with self.assertRaisesRegex(ConfigurationError, "TELEGRAM_TOKEN"):
            settings.validate(require_telegram=True)

    def test_empty_ollama_model_is_rejected(self) -> None:
        settings = Settings.from_env({"OLLAMA_MODEL": "   "})

        with self.assertRaisesRegex(ConfigurationError, "OLLAMA_MODEL"):
            settings.validate()

    def test_whisper_model_name_is_normalized(self) -> None:
        settings = Settings.from_env({"WHISPER_MODEL": "  base  "})

        settings.validate()

        self.assertEqual(settings.whisper_model, "base")

    def test_cpp_whisper_requires_explicit_paths(self) -> None:
        settings = Settings.from_env(
            {
                "WHISPER_BACKEND": "cpp",
                "WHISPER_CPP_EXECUTABLE": " ",
                "WHISPER_CPP_MODEL": " ",
                "WHISPER_CPP_FFMPEG": " ",
            }
        )

        with self.assertRaisesRegex(ConfigurationError, "WHISPER_CPP"):
            settings.validate()

    def test_remote_provider_is_fail_closed_until_free_only_routing_exists(self) -> None:
        settings = Settings.from_env(
            {"LLM_CLIENT": "together", "TOGETHER_AI_API_KEY": "not-a-real-key"}
        )

        with self.assertRaisesRegex(ConfigurationError, "Stage 3"):
            settings.validate()

    def test_legacy_yandex_backend_is_removed(self) -> None:
        settings = Settings.from_env({"SEARCH_BACKEND": "yandex"})

        with self.assertRaisesRegex(ConfigurationError, "SEARCH_BACKEND.*Stage 2"):
            settings.validate(require_web=False)

    def test_context_supports_owner_confirmed_64k_limit(self) -> None:
        settings = Settings.from_env({"OLLAMA_CONTEXT_TOKENS": "65536"})
        settings.validate()

        self.assertEqual(settings.ollama_context_tokens, 65_536)

    def test_context_above_model_limit_is_rejected(self) -> None:
        settings = Settings.from_env({"OLLAMA_CONTEXT_TOKENS": "65537"})

        with self.assertRaisesRegex(ConfigurationError, "65536"):
            settings.validate()

    def test_invalid_numeric_environment_values_are_configuration_errors(self) -> None:
        cases = (
            ("OLLAMA_TIMEOUT", "soon", "must be a number"),
            ("OLLAMA_CONTEXT_TOKENS", "lots", "must be an integer"),
        )
        for name, value, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, message):
                    Settings.from_env({name: value})

    def test_timeout_matches_provider_maximum(self) -> None:
        settings = Settings.from_env({"OLLAMA_TIMEOUT": "121"})

        with self.assertRaisesRegex(ConfigurationError, "120"):
            settings.validate()

    def test_unknown_search_backend_is_rejected_even_when_web_is_off(self) -> None:
        settings = Settings.from_env({"SEARCH_BACKEND": "surprise"})

        with self.assertRaisesRegex(ConfigurationError, "SEARCH_BACKEND"):
            settings.validate()

    def test_future_duckduckgo_backend_cannot_be_enabled_before_implementation(self) -> None:
        settings = Settings.from_env(
            {"SEARCH_BACKEND": "duckduckgo", "WEB_ENABLED_DEFAULT": "true"}
        )

        with self.assertRaisesRegex(ConfigurationError, "SEARCH_BACKEND.*Stage 2"):
            settings.validate()

    def test_removed_legacy_environment_knobs_do_not_break_local_import(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "WEB_ENABLED_DEFAULT": "false",
                "RERANK_THRESHOLD": "not-a-number",
                "ENTITY_SEARCH_LIMIT": "not-a-number",
            }
        )

        result = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
