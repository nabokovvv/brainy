#!/usr/bin/env python3
"""Starvation benchmark: N concurrent arrivals through one generation slot.

Measures queue wait fairness and ensures no dropped requests under load.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from tools.benchmark_ollama import BenchmarkResult, benchmark


@dataclass(frozen=True)
class StarvationBenchmarkResult:
    case_id: str
    arrival_idx: int
    queue_wait_ms: float
    end_to_end_ms: float
    benchmark: BenchmarkResult


def benchmark_starvation(
    *,
    users: int,
    base_url: str,
    model: str,
    prompt: str,
    context_tokens: int,
    max_output_tokens: int,
    timeout_seconds: float,
    benchmark_fn: Callable[..., BenchmarkResult] | None = None,
) -> tuple[StarvationBenchmarkResult, ...]:
    """
    Run N concurrent arrivals through a single generation slot.

    Returns results sorted by case_id. All arrivals complete or exception raised.
    """
    if users < 1:
        raise ValueError("users must be positive")

    # Use shared benchmark function or default
    fn = benchmark_fn or benchmark

    # Synchronization: barrier for simultaneous start, semaphore for slot
    start_barrier = threading.Barrier(users)
    slot = threading.Semaphore(1)
    arrivals: list[float] = []
    arrivals_lock = threading.Lock()

    def run(user_number: int) -> StarvationBenchmarkResult:
        case_id = f"starvation-user-{user_number:03d}"

        # Wait for all users to be ready, then proceed
        start_barrier.wait()
        arrived = time.perf_counter()
        with arrivals_lock:
            arrivals.append(arrived)

        # Wait for slot (simulates queue)
        with slot:
            acquired = time.perf_counter()
            result = fn(
                base_url=base_url,
                model=model,
                prompt=prompt,
                case_id=case_id,
                context_tokens=context_tokens,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        finished = time.perf_counter()

        return StarvationBenchmarkResult(
            case_id=case_id,
            arrival_idx=user_number,
            queue_wait_ms=round((acquired - arrived) * 1_000, 1),
            end_to_end_ms=round((finished - arrived) * 1_000, 1),
            benchmark=result,
        )

    with ThreadPoolExecutor(max_workers=users) as executor:
        results = tuple(executor.map(run, range(1, users + 1)))

    # Sort by arrival index
    return tuple(sorted(results, key=lambda r: r.arrival_idx))


def _analyze_fairness(results: tuple[StarvationBenchmarkResult, ...]) -> dict[str, Any]:
    """Analyze queue wait fairness metrics."""
    if not results:
        return {}

    waits = [r.queue_wait_ms for r in results]
    waits_sorted = sorted(waits)
    n = len(waits)

    return {
        "min_wait_ms": waits_sorted[0],
        "max_wait_ms": waits_sorted[-1],
        "median_wait_ms": waits_sorted[n // 2],
        "p95_wait_ms": waits_sorted[int(n * 0.95)],
        "fairness_ratio": waits_sorted[-1] / waits_sorted[0] if waits_sorted[0] > 0 else 0,
        "total_arrivals": n,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--context-tokens", type=int, default=8_192)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    args = parser.parse_args()

    results = benchmark_starvation(
        users=args.users,
        base_url=args.base_url,
        model=args.model,
        prompt=args.prompt,
        context_tokens=args.context_tokens,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
    )

    output = {
        "results": [asdict(r) for r in results],
        "fairness": _analyze_fairness(results),
    }
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
