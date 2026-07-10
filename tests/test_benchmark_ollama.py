from __future__ import annotations

import unittest

from tools.benchmark_ollama import _chat_payload, _is_ollama_process, _require_loopback_url


class OllamaBenchmarkTests(unittest.TestCase):
    def test_rss_filter_ignores_benchmark_process_names(self) -> None:
        self.assertTrue(_is_ollama_process("/Applications/Ollama.app/Contents/MacOS/Ollama"))
        self.assertTrue(_is_ollama_process("/usr/local/bin/ollama"))
        self.assertTrue(_is_ollama_process("/usr/local/bin/ollama serve"))
        self.assertFalse(_is_ollama_process("python /tmp/brainy-benchmark-ollama.py"))

    def test_payload_disables_hidden_thinking_and_bounds_generation(self) -> None:
        payload = _chat_payload(
            model="gemma4:e2b",
            prompt="synthetic prompt",
            context_tokens=8_192,
            max_output_tokens=128,
        )

        self.assertTrue(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"], {"num_ctx": 8_192, "num_predict": 128})

    def test_accepts_only_loopback_http_endpoints(self) -> None:
        self.assertEqual(
            _require_loopback_url("http://127.0.0.1:11434/"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            _require_loopback_url("http://localhost:11434"),
            "http://localhost:11434",
        )

        for url in ("https://localhost:11434", "http://10.0.0.1:11434", "not a url"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    _require_loopback_url(url)
