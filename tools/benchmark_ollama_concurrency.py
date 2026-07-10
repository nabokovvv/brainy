#!/usr/bin/env python3
"""Measure three concurrent arrivals through one serialized local Ollama slot."""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Callable

from tools.benchmark_ollama import BenchmarkResult, benchmark


@dataclass(frozen=True)
class QueuedBenchmarkResult:
    case_id: str
    queue_wait_ms: float
    end_to_end_ms: float
    benchmark: BenchmarkResult


def benchmark_serialized_arrivals(
    *,
    users: int,
    base_url: str,
    model: str,
    prompt: str,
    context_tokens: int,
    max_output_tokens: int,
    timeout_seconds: float,
    benchmark_fn: Callable[..., BenchmarkResult] = benchmark,
) -> tuple[QueuedBenchmarkResult, ...]:
    if users < 1:
        raise ValueError("users must be positive")
    slot = threading.Semaphore(1)
    ready = threading.Barrier(users)

    def run(user_number: int) -> QueuedBenchmarkResult:
        case_id = f"concurrent-user-{user_number}"
        ready.wait()
        arrived = time.perf_counter()
        with slot:
            acquired = time.perf_counter()
            result = benchmark_fn(
                base_url=base_url,
                model=model,
                prompt=prompt,
                case_id=case_id,
                context_tokens=context_tokens,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        finished = time.perf_counter()
        return QueuedBenchmarkResult(
            case_id=case_id,
            queue_wait_ms=round((acquired - arrived) * 1_000, 1),
            end_to_end_ms=round((finished - arrived) * 1_000, 1),
            benchmark=result,
        )

    with ThreadPoolExecutor(max_workers=users) as executor:
        results = tuple(executor.map(run, range(1, users + 1)))
    return tuple(sorted(results, key=lambda result: result.case_id))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--users", type=int, default=3)
    parser.add_argument("--context-tokens", type=int, default=8_192)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    args = parser.parse_args()
    results = benchmark_serialized_arrivals(
        users=args.users,
        base_url=args.base_url,
        model=args.model,
        prompt=args.prompt,
        context_tokens=args.context_tokens,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            [asdict(result) for result in results],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
