from __future__ import annotations

import threading
import time
import unittest

from tools.benchmark_ollama import BenchmarkResult
from tools.benchmark_ollama_concurrency import benchmark_serialized_arrivals


class OllamaConcurrencyBenchmarkTests(unittest.TestCase):
    def test_three_arrivals_use_one_generation_slot(self) -> None:
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def fake_benchmark(**kwargs: object) -> BenchmarkResult:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return BenchmarkResult(
                case_id=str(kwargs["case_id"]),
                model="gemma4:e2b",
                context_tokens=8_192,
                max_output_tokens=64,
                ttft_ms=1.0,
                total_ms=10.0,
                completion_tokens=1,
                generation_tokens_per_second=1.0,
                ollama_rss_kib_before=1,
                ollama_rss_kib_after=1,
                swap_before="0",
                swap_after="0",
            )

        results = benchmark_serialized_arrivals(
            users=3,
            base_url="http://127.0.0.1:11434",
            model="gemma4:e2b",
            prompt="synthetic",
            context_tokens=8_192,
            max_output_tokens=64,
            timeout_seconds=10.0,
            benchmark_fn=fake_benchmark,
        )

        self.assertEqual(maximum_active, 1)
        self.assertEqual(
            {result.case_id for result in results}, {f"concurrent-user-{n}" for n in range(1, 4)}
        )
        self.assertTrue(all(result.end_to_end_ms >= result.queue_wait_ms for result in results))

    def test_rejects_zero_users(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            benchmark_serialized_arrivals(
                users=0,
                base_url="http://127.0.0.1:11434",
                model="gemma4:e2b",
                prompt="synthetic",
                context_tokens=8_192,
                max_output_tokens=64,
                timeout_seconds=10.0,
            )

    def test_failed_arrival_releases_slot_for_waiting_requests(self) -> None:
        calls = 0
        lock = threading.Lock()

        def failing_once(**kwargs: object) -> BenchmarkResult:
            nonlocal calls
            with lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                raise OSError("synthetic failure")
            return BenchmarkResult(
                case_id=str(kwargs["case_id"]),
                model="gemma4:e2b",
                context_tokens=8_192,
                max_output_tokens=64,
                ttft_ms=1.0,
                total_ms=1.0,
                completion_tokens=1,
                generation_tokens_per_second=1.0,
                ollama_rss_kib_before=1,
                ollama_rss_kib_after=1,
                swap_before="0",
                swap_after="0",
            )

        with self.assertRaises(OSError):
            benchmark_serialized_arrivals(
                users=3,
                base_url="http://127.0.0.1:11434",
                model="gemma4:e2b",
                prompt="synthetic",
                context_tokens=8_192,
                max_output_tokens=64,
                timeout_seconds=10.0,
                benchmark_fn=failing_once,
            )

        self.assertEqual(calls, 3)
