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
    RetryPolicy,
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
    parser.add_argument("--provider", action="append", choices=("nvidia", "openrouter"), required=True)
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
        default=120,
        help="Per-request timeout in seconds; free-tier models routinely run close to the cap.",
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=20,
        help="Requests per minute; free tiers often throttle well below 20.",
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
        candidates_by_provider = {}
        for prov in args.provider:
            if prov == "openrouter":
                candidates_by_provider["openrouter"] = await _openrouter_candidates(
                    client, args.catalog_path
                )
            else:
                candidates_by_provider["nvidia"] = await _nvidia_candidates(client)

        if not args.canary:
            results = {}
            for prov in args.provider:
                results[prov] = {
                    "lifecycle": "eligible",
                    "multilingual_canary_required": True,
                    "model_ids": list(candidates_by_provider[prov]),
                }
            print(json.dumps(results, sort_keys=True))
            return 0

        if not args.model:
            print(json.dumps({"error": "explicit_model_required_for_canary"}))
            return 2

        async def canary_provider(prov: str) -> dict:
            if prov == "nvidia":
                api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
                base_url = NVIDIA_BASE_URL
                limit = args.nvidia_daily_limit
            else:
                api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
                base_url = OPENROUTER_BASE_URL
                limit = args.openrouter_daily_limit
            if not api_key:
                return {"provider": prov, "error": "missing_api_key"}
            candidates = candidates_by_provider[prov]
            selected = tuple(m for m in args.model if m in candidates)
            if not selected:
                return {"provider": prov, "results": []}
            reports: list[dict] = []
            rate_budget = MinuteRateBudget(args.rpm)
            for model_id in selected:
                provider = OpenAICompatibleRemoteProvider(
                    provider_name=prov,
                    base_url=base_url,
                    api_key=api_key,
                    model=model_id,
                    client=client,
                    budget=DailyRequestBudget(
                        _prov_budget_path(args.budget_path, prov),
                        provider=prov,
                        limit=limit,
                    ),
                    rate_budget=rate_budget,
                    timeout_seconds=args.timeout,
                    retry_policy=RetryPolicy(max_attempts=3, max_delay_seconds=20),
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
            return {"provider": prov, "results": reports}

        results = await asyncio.gather(*[canary_provider(prov) for prov in args.provider])
        print(json.dumps({"results": results}, sort_keys=True))
        return 0


def _prov_budget_path(base: str, prov: str) -> str:
    p = os.path.expanduser(base)
    root, ext = os.path.splitext(p)
    return f"{root}-{prov}{ext}"


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
