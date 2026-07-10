#!/usr/bin/env python3
"""Measure a local Ollama streaming response without recording generated text."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class BenchmarkResult:
    case_id: str
    model: str
    context_tokens: int
    max_output_tokens: int
    ttft_ms: float | None
    total_ms: float
    completion_tokens: int | None
    generation_tokens_per_second: float | None
    ollama_rss_kib_before: int | None
    ollama_rss_kib_after: int | None
    swap_before: str | None
    swap_after: str | None
    prompt_tokens: int | None = None
    prompt_tokens_per_second: float | None = None
    expected_markers_seen: bool | None = None


def _require_loopback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Ollama URL must be an http URL with a host")
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname.lower() == "localhost"
    if not is_loopback:
        raise ValueError("Benchmark only permits a loopback Ollama URL")
    return url.rstrip("/")


def _is_ollama_process(command: str) -> bool:
    normalized = command.lower()
    executable = normalized.strip().split(maxsplit=1)[0]
    return "/ollama.app/" in normalized or executable.endswith("/ollama")


def _ollama_rss_kib() -> int | None:
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "rss=,command="], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    rss = 0
    found = False
    for line in output.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not _is_ollama_process(fields[1]):
            continue
        try:
            rss += int(fields[0])
            found = True
        except ValueError:
            continue
    return rss if found else None


def _swap_usage() -> str | None:
    try:
        return subprocess.check_output(
            ["sysctl", "-n", "vm.swapusage"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _stream_events(response: Any) -> Iterable[dict[str, Any]]:
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if line:
            yield json.loads(line)


def _chat_payload(
    *, model: str, prompt: str, context_tokens: int, max_output_tokens: int
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "keep_alive": "10m",
        "options": {"num_ctx": context_tokens, "num_predict": max_output_tokens},
    }


def _synthetic_context_prompt(target_tokens: int) -> tuple[str, tuple[str, ...]]:
    if target_tokens < 64:
        raise ValueError("synthetic input target must be at least 64 tokens")
    first_marker = "ALPHA-314159"
    second_marker = "OMEGA-271828"
    prompt = (
        f"Remember this first code: {first_marker}."
        + " filler" * (target_tokens - 48)
        + f" Remember this second code: {second_marker}. "
        "Reply with the first and second remembered codes only."
    )
    return prompt, (first_marker, second_marker)


def _serialize_result(result: BenchmarkResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_result(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _update_marker_state(
    *,
    seen: set[str],
    tail: str,
    content: str,
    markers: tuple[str, ...],
) -> tuple[set[str], str]:
    marker_chunk = tail + content
    seen.update(marker for marker in markers if marker in marker_chunk)
    marker_window = max((len(marker) for marker in markers), default=1)
    return seen, marker_chunk[-(marker_window - 1) :] if marker_window > 1 else ""


def benchmark(
    *,
    base_url: str,
    model: str,
    prompt: str,
    case_id: str,
    context_tokens: int,
    max_output_tokens: int,
    timeout_seconds: float,
    expected_markers: tuple[str, ...] = (),
) -> BenchmarkResult:
    if not model.strip() or not prompt.strip() or not case_id.strip():
        raise ValueError("model, prompt, and case_id must be non-empty")
    if context_tokens < 1 or max_output_tokens < 1:
        raise ValueError("context_tokens and max_output_tokens must be positive")

    endpoint = f"{_require_loopback_url(base_url)}/api/chat"
    payload = _chat_payload(
        model=model,
        prompt=prompt,
        context_tokens=context_tokens,
        max_output_tokens=max_output_tokens,
    )
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    rss_before = _ollama_rss_kib()
    swap_before = _swap_usage()
    started = time.perf_counter()
    first_token_at: float | None = None
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    prompt_eval_duration: int | None = None
    seen_markers: set[str] = set()
    marker_tail = ""

    # The target is restricted to loopback by _require_loopback_url above.
    with urlopen(request, timeout=timeout_seconds) as response:
        for event in _stream_events(response):
            content = event.get("message", {}).get("content", "")
            if content and expected_markers:
                seen_markers, marker_tail = _update_marker_state(
                    seen=seen_markers,
                    tail=marker_tail,
                    content=content,
                    markers=expected_markers,
                )
            if content and first_token_at is None:
                first_token_at = time.perf_counter()
            if event.get("done") and isinstance(event.get("eval_count"), int):
                completion_tokens = event["eval_count"]
                prompt_tokens = event.get("prompt_eval_count")
                prompt_eval_duration = event.get("prompt_eval_duration")

    finished = time.perf_counter()
    generation_seconds = finished - first_token_at if first_token_at is not None else None
    tokens_per_second = (
        completion_tokens / generation_seconds
        if completion_tokens is not None and generation_seconds and generation_seconds > 0
        else None
    )
    prompt_tokens_per_second = (
        prompt_tokens / (prompt_eval_duration / 1_000_000_000)
        if isinstance(prompt_tokens, int)
        and isinstance(prompt_eval_duration, int)
        and prompt_eval_duration > 0
        else None
    )
    return BenchmarkResult(
        case_id=case_id,
        model=model,
        context_tokens=context_tokens,
        max_output_tokens=max_output_tokens,
        ttft_ms=round((first_token_at - started) * 1_000, 1)
        if first_token_at is not None
        else None,
        total_ms=round((finished - started) * 1_000, 1),
        completion_tokens=completion_tokens,
        generation_tokens_per_second=round(tokens_per_second, 2) if tokens_per_second else None,
        ollama_rss_kib_before=rss_before,
        ollama_rss_kib_after=_ollama_rss_kib(),
        swap_before=swap_before,
        swap_after=_swap_usage(),
        prompt_tokens=prompt_tokens,
        prompt_tokens_per_second=round(prompt_tokens_per_second, 2)
        if prompt_tokens_per_second
        else None,
        expected_markers_seen=(
            all(marker in seen_markers for marker in expected_markers) if expected_markers else None
        ),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--synthetic-input-tokens", type=int)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--context-tokens", type=int, default=8_192)
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    expected_markers: tuple[str, ...] = ()
    prompt = args.prompt
    if args.synthetic_input_tokens is not None:
        prompt, expected_markers = _synthetic_context_prompt(args.synthetic_input_tokens)
    result = benchmark(
        base_url=args.base_url,
        model=args.model,
        prompt=prompt,
        case_id=args.case_id,
        context_tokens=args.context_tokens,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
        expected_markers=expected_markers,
    )
    serialized = _serialize_result(result)
    if args.output:
        _write_result(args.output, serialized)
    print(serialized, end="")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2) from exc
