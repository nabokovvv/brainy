from __future__ import annotations

import threading
import time
import unittest

from tools.benchmark_ollama import BenchmarkResult
from tools.benchmark_ollama_starvation import (
    benchmark_starvation,
    StarvationBenchmarkResult,
    _analyze_fairness,
)


class StarvationBenchmarkTests(unittest.TestCase):
    def test_ten_arrivals_all_complete_with_bounded_queue_wait(self) -> None:
        """10 concurrent arrivals through one slot: all complete, no starvation."""
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_benchmark(**kwargs: object) -> BenchmarkResult:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
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

        results = benchmark_starvation(
            users=10,
            base_url="http://127.0.0.1:11434",
            model="gemma4:e2b",
            prompt="synthetic",
            context_tokens=8_192,
            max_output_tokens=64,
            timeout_seconds=10.0,
            benchmark_fn=fake_benchmark,
        )

        # All 10 arrivals complete
        self.assertEqual(len(results), 10)
        # At most 1 in the slot at any time
        self.assertEqual(max_active, 1)
        # Queue waits are bounded
        max_wait = max(r.queue_wait_ms for r in results)
        self.assertLess(max_wait, 500)

    def test_fairness_ratio_bounded(self) -> None:
        """Fairness ratio (max/min queue wait) should be reasonable for N>3."""
        wait_times: list[float] = []
        lock = threading.Lock()

        def fake_benchmark(**kwargs: object) -> BenchmarkResult:
            with lock:
                idx = len(wait_times)
                wait_times.append(idx * 0.01)
            time.sleep(0.01)
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

        results = benchmark_starvation(
            users=10,
            base_url="http://127.0.0.1:11434",
            model="gemma4:e2b",
            prompt="synthetic",
            context_tokens=8_192,
            max_output_tokens=64,
            timeout_seconds=10.0,
            benchmark_fn=fake_benchmark,
        )

        waits = [r.queue_wait_ms for r in results]
        min_wait = min(waits)
        max_wait = max(waits)

        # With N=10, fairness ratio should be < 10 (ideally ~9 for strict FIFO)
        fairness = max_wait / min_wait if min_wait > 0 else 0
        self.assertLess(fairness, 15, f"Fairness ratio {fairness:.1f}x too high")

    def test_failed_arrival_releases_slot(self) -> None:
        """If one arrival fails, slot releases for waiting requests."""
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
                total_ms=10.0,
                completion_tokens=1,
                generation_tokens_per_second=1.0,
                ollama_rss_kib_before=1,
                ollama_rss_kib_after=1,
                swap_before="0",
                swap_after="0",
            )

        # Expect OSError to propagate
        with self.assertRaises(OSError):
            benchmark_starvation(
                users=3,
                base_url="http://127.0.0.1:11434",
                model="gemma4:e2b",
                prompt="synthetic",
                context_tokens=8_192,
                max_output_tokens=64,
                timeout_seconds=10.0,
                benchmark_fn=failing_once,
            )

        # All 3 should have been attempted (failed one + 2 waiting)
        self.assertEqual(calls, 3)

    def test_analyze_fairness(self) -> None:
        """Test fairness analysis helper."""
        results = tuple(
            StarvationBenchmarkResult(
                case_id=f"user-{i}",
                arrival_idx=i,
                queue_wait_ms=float(i * 10),
                end_to_end_ms=float(i * 10 + 5),
                benchmark=BenchmarkResult(
                    case_id=f"user-{i}",
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
                ),
            )
            for i in range(1, 11)
        )

        fairness = _analyze_fairness(results)
        self.assertEqual(fairness["min_wait_ms"], 10.0)
        self.assertEqual(fairness["max_wait_ms"], 100.0)
        # For 10 items, median is element at index 5 (0-indexed) = 60
        self.assertEqual(fairness["median_wait_ms"], 60.0)
        self.assertAlmostEqual(fairness["p95_wait_ms"], 95.0, delta=5)
        self.assertAlmostEqual(fairness["fairness_ratio"], 10.0, delta=0.1)
        self.assertEqual(fairness["total_arrivals"], 10)


if __name__ == "__main__":
    unittest.main()
