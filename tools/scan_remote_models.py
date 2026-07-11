#!/usr/bin/env python3
"""Discover and canary zero-cost multilingual remote model candidates.

The command prints only model IDs, lifecycle state, pass/fail language codes, latency,
and counters. It never prints prompts, responses, or API keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import httpx

from brainy_core.model_catalog import CatalogPolicy, OpenRouterCatalog, evaluate_openrouter_model
from brainy_core.multilingual_canary import run_multilingual_canary
from brainy_core.providers.remote import (
    NVIDIA_BASE_URL,
    OPENROUTER_BASE_URL,
    DailyRequestBudget,
    MinuteRateBudget,
    OpenAICompatibleRemoteProvider,
)

NVIDIA_MODELS_URL = f"{NVIDIA_BASE_URL}/models"
NVIDIA_MULTILINGUAL_CANDIDATES = (
    "google/gemma-3-12b-it",
    "google/gemma-4-31b-it",
    "meta/llama-3.3-70b-instruct",
    "mistralai/mistral-small-4-119b-2603",
    "qwen/qwen3-next-80b-a3b-instruct",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("nvidia", "openrouter"), required=True)
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--max-models", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument(
        "--catalog-path",
        default=os.environ.get(
            "OPENROUTER_CATALOG_PATH", "~/.local/state/brainy/openrouter_catalog.json"
        ),
    )
    parser.add_argument(
        "--budget-path",
        default=os.environ.get(
            "REMOTE_INFERENCE_BUDGET_PATH",
            "~/.local/state/brainy/remote_inference_budget.json",
        ),
    )
    parser.add_argument(
        "--openrouter-daily-limit",
        type=int,
        default=_env_int("OPENROUTER_DAILY_LIMIT", 50),
    )
    parser.add_argument(
        "--nvidia-daily-limit", type=int, default=_env_int("NVIDIA_DAILY_LIMIT", 40)
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Per-request timeout in seconds; free-tier models routinely need more than 30.",
    )
    return parser


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None


async def main() -> int:
    args = _parser().parse_args()
    async with httpx.AsyncClient() as client:
        if args.provider == "openrouter":
            candidates = await _openrouter_candidates(client, args.catalog_path)
        else:
            candidates = await _nvidia_candidates(client)

        selected = tuple(args.model or candidates[: args.max_models])
        if args.canary and not args.model:
            print(json.dumps({"error": "explicit_model_required_for_canary"}))
            return 2
        unknown = sorted(set(selected) - set(candidates))
        if unknown:
            print(json.dumps({"error": "models_not_eligible", "model_ids": unknown}))
            return 2
        if not args.canary:
            print(
                json.dumps(
                    {
                        "provider": args.provider,
                        "lifecycle": "eligible",
                        "multilingual_canary_required": True,
                        "model_ids": list(candidates),
                    },
                    sort_keys=True,
                )
            )
            return 0

        key_name = "NVIDIA_API_KEY" if args.provider == "nvidia" else "OPENROUTER_API_KEY"
        api_key = os.environ.get(key_name, "").strip()
        if not api_key:
            print(json.dumps({"error": "missing_api_key", "provider": args.provider}))
            return 2
        limit = (
            args.nvidia_daily_limit if args.provider == "nvidia" else args.openrouter_daily_limit
        )
        base_url = NVIDIA_BASE_URL if args.provider == "nvidia" else OPENROUTER_BASE_URL
        reports = []
        rate_budget = MinuteRateBudget(20)
        for model_id in selected:
            provider = OpenAICompatibleRemoteProvider(
                provider_name=args.provider,
                base_url=base_url,
                api_key=api_key,
                model=model_id,
                client=client,
                budget=DailyRequestBudget(
                    args.budget_path,
                    provider=args.provider,
                    limit=limit,
                ),
                rate_budget=rate_budget,
                timeout_seconds=args.timeout,
            )
            result = await run_multilingual_canary(provider)
            reports.append(
                {
                    "model_id": model_id,
                    "lifecycle": "canary" if result.passed else "quarantine",
                    "passed_languages": list(result.passed_languages),
                    "completed_languages": list(result.completed_languages),
                    "error_languages": sorted(
                        set(result.languages) - set(result.completed_languages)
                    ),
                    "error_codes": [
                        {"language": language, "code": code}
                        for language, code in result.error_codes
                    ],
                    "required_languages": list(result.languages),
                    "median_latency_ms": round(result.median_latency_ms, 1),
                }
            )
        print(json.dumps({"provider": args.provider, "results": reports}, sort_keys=True))
        return 0


async def _openrouter_candidates(client: httpx.AsyncClient, path: str) -> tuple[str, ...]:
    catalog = OpenRouterCatalog(client=client, snapshot_path=path, ttl_seconds=0)
    models = await catalog.get_models()
    policy = CatalogPolicy()
    return tuple(
        model.model_id for model in models if evaluate_openrouter_model(model, policy).eligible
    )


async def _nvidia_candidates(client: httpx.AsyncClient) -> tuple[str, ...]:
    response = await client.get(
        NVIDIA_MODELS_URL,
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(10, connect=5),
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    available = {
        entry.get("id") for entry in entries if isinstance(entry, dict) and entry.get("id")
    }
    return tuple(model_id for model_id in NVIDIA_MULTILINGUAL_CANDIDATES if model_id in available)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
