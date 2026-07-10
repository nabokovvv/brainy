#!/usr/bin/env python3
"""Validate the non-personal multilingual local-chat evaluation fixture only."""

from __future__ import annotations

import json
import time
from collections import Counter
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


REQUIRED_LANGUAGES = frozenset({"en", "es", "ru", "pt", "fr", "de", "tr", "id"})
EXPECTED_CASE_COUNT = 15
DEFAULT_CASES_PATH = Path(__file__).parents[1] / "tests/data/local_quality_eval.json"


def _require_loopback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Ollama URL must be an http URL with a host")
    try:
        is_loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname.lower() == "localhost"
    if not is_loopback:
        raise ValueError("Evaluation only permits a loopback Ollama URL")
    return url.rstrip("/")


def load_and_validate(path: Path = DEFAULT_CASES_PATH) -> Counter[str]:
    raw_cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list) or len(raw_cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"Expected exactly {EXPECTED_CASE_COUNT} evaluation cases")
    identifiers: list[str] = []
    languages: Counter[str] = Counter()
    for case in raw_cases:
        if not isinstance(case, dict):
            raise ValueError("Every evaluation case must be an object")
        fields = (case.get("id"), case.get("language"), case.get("prompt"), case.get("rubric"))
        if not all(isinstance(field, str) and field.strip() for field in fields):
            raise ValueError("Each evaluation case needs non-empty text fields")
        case_id, language = case["id"], case["language"]
        if not case_id.startswith(f"{language}-"):
            raise ValueError("Case id must start with its language code")
        identifiers.append(case_id)
        languages[language] += 1
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Evaluation case ids must be unique")
    if set(languages) != REQUIRED_LANGUAGES:
        raise ValueError("Evaluation cases must cover every supported language")
    return languages


def run_cases(
    *,
    base_url: str,
    model: str,
    show_responses: bool,
    request_fn: Callable[..., Any] = urlopen,
) -> list[dict[str, object]]:
    """Run synthetic cases; callers may explicitly save their non-personal results."""
    if not model.strip():
        raise ValueError("Model must be non-empty")
    _require_loopback_url(base_url)
    raw_cases = json.loads(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
    load_and_validate()
    results: list[dict[str, object]] = []
    for case in raw_cases:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Reply concisely in the user's language."},
                {"role": "user", "content": case["prompt"]},
            ],
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {"num_ctx": 8_192, "num_predict": 128},
        }
        request = Request(
            f"{_require_loopback_url(base_url)}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        with request_fn(request, timeout=90) as response:
            answer = json.load(response).get("message", {}).get("content", "").strip()
        result = {
            "id": case["id"],
            "language": case["language"],
            "prompt": case["prompt"],
            "rubric": case["rubric"],
            "response": answer,
            "nonempty": bool(answer),
            "latency_ms": round((time.monotonic() - started) * 1_000),
        }
        results.append(result)
        if show_responses:
            print(f"[{case['id']}] {answer}")
    return results


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--model", default="gemma4:e2b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--show-responses", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        print(
            json.dumps(
                {"case_count": EXPECTED_CASE_COUNT, "languages": load_and_validate()},
                sort_keys=True,
            )
        )
        return
    results = run_cases(
        base_url=args.base_url,
        model=args.model,
        show_responses=args.show_responses,
    )
    if args.output:
        args.output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    latencies = [result["latency_ms"] for result in results]
    print(
        json.dumps(
            {
                "cases": len(results),
                "nonempty": sum(result["nonempty"] for result in results),
                "latency_ms_max": max(latencies),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
