from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.benchmark_ollama import (
    BenchmarkResult,
    _chat_payload,
    _is_ollama_process,
    _require_loopback_url,
    _serialize_result,
    _synthetic_context_prompt,
    _update_marker_state,
    _write_result,
)


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

    def test_synthetic_context_has_markers_separated_by_requested_filler(self) -> None:
        prompt, markers = _synthetic_context_prompt(1_024)

        self.assertTrue(all(marker in prompt for marker in markers))
        self.assertGreater(prompt.count(" filler"), 900)
        self.assertLess(prompt.index(markers[0]), prompt.index(markers[1]))

    def test_serialized_result_contains_metrics_but_no_prompt_or_response(self) -> None:
        result = BenchmarkResult(
            case_id="full-context-32k",
            model="gemma4:e2b",
            context_tokens=32_768,
            max_output_tokens=32,
            ttft_ms=1.0,
            total_ms=2.0,
            completion_tokens=2,
            generation_tokens_per_second=3.0,
            ollama_rss_kib_before=4,
            ollama_rss_kib_after=5,
            swap_before="6",
            swap_after="6",
            prompt_tokens=32_000,
            prompt_tokens_per_second=7.0,
            expected_markers_seen=True,
        )

        serialized = _serialize_result(result)

        self.assertIn('"prompt_tokens": 32000', serialized)
        self.assertNotIn('"prompt":', serialized)
        self.assertNotIn('"response":', serialized)

    def test_result_file_is_created_in_a_new_directory(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "result.json"

            _write_result(output, '{"safe": true}\n')

            self.assertEqual(output.read_text(encoding="utf-8"), '{"safe": true}\n')

    def test_marker_tracker_detects_marker_split_across_chunks(self) -> None:
        markers = ("ALPHA-314159",)
        seen, tail = _update_marker_state(
            seen=set(), tail="", content="answer ALPHA-", markers=markers
        )
        seen, tail = _update_marker_state(
            seen=seen, tail=tail, content="314159 done", markers=markers
        )

        self.assertEqual(seen, set(markers))
        self.assertLessEqual(len(tail), len(markers[0]) - 1)
