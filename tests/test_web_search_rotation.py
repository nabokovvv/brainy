from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from brainy_core.providers.web_search import (
    BraveSearchProvider,
    MonthlyQuotaLedger,
    ProviderConfig,
    RotatingSearchProvider,
    SearchUnavailableError,
    SerpApiSearchProvider,
    TavilySearchProvider,
)
from brainy_core.search import SearchQuery, SearchResult


class WebProviderMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_mappings_preserve_snippets(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.search.brave.com":
                return httpx.Response(
                    200,
                    json={
                        "web": {
                            "results": [
                                {"title": "B", "url": "https://b.example", "description": "brave"}
                            ]
                        }
                    },
                )
            if request.url.host == "api.tavily.com":
                return httpx.Response(
                    200,
                    json={
                        "results": [{"title": "T", "url": "https://t.example", "content": "tavily"}]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "organic_results": [
                        {"title": "S", "link": "https://s.example", "snippet": "serp"}
                    ]
                },
            )

        query = SearchQuery("question", "en", limit=1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results = await asyncio.gather(
                BraveSearchProvider(client, "b").search(query),
                TavilySearchProvider(client, "t").search(query),
                SerpApiSearchProvider(client, "s").search(query),
            )

        self.assertEqual([result[0].snippet for result in results], ["brave", "tavily", "serp"])


class RotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_uses_one_provider_before_falling_back(self) -> None:
        class FakeProvider:
            def __init__(self, name: str, calls: list[str]) -> None:
                self.name = name
                self.calls = calls

            async def search(self, request: SearchQuery) -> tuple[SearchResult, ...]:
                self.calls.append(self.name)
                await asyncio.sleep(0.03)
                return (
                    SearchResult(
                        self.name, f"https://{self.name}.example", self.name, 1, self.name
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            configs = tuple(
                ProviderConfig(name, "key", 2) for name in ("tavily", "brave", "serpapi")
            )
            calls: list[str] = []
            ledger = MonthlyQuotaLedger(Path(directory) / "quota.json", configs)
            provider = RotatingSearchProvider(
                tuple((config, FakeProvider(config.name, calls)) for config in configs), ledger
            )
            started = time.monotonic()
            results = await provider.search(SearchQuery("question", "en"))

        self.assertLess(time.monotonic() - started, 0.08)
        self.assertEqual(calls, ["tavily"])
        self.assertEqual({result.provider for result in results}, {"tavily"})

    async def test_rotation_falls_back_only_after_provider_failure(self) -> None:
        class FakeProvider:
            def __init__(self, name: str, failing: bool) -> None:
                self.name = name
                self.failing = failing

            async def search(self, request: SearchQuery) -> tuple[SearchResult, ...]:
                if self.failing:
                    raise RuntimeError("provider unavailable")
                return (
                    SearchResult(
                        self.name, f"https://{self.name}.example", self.name, 1, self.name
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            configs = tuple(
                ProviderConfig(name, "key", 2) for name in ("tavily", "brave", "serpapi")
            )
            ledger = MonthlyQuotaLedger(Path(directory) / "quota.json", configs)
            provider = RotatingSearchProvider(
                (
                    (configs[0], FakeProvider("tavily", True)),
                    (configs[1], FakeProvider("brave", False)),
                    (configs[2], FakeProvider("serpapi", False)),
                ),
                ledger,
            )
            results = await provider.search(SearchQuery("question", "en"))

        self.assertEqual({result.provider for result in results}, {"brave"})

    async def test_all_failures_disable_web_until_next_month(self) -> None:
        class FailingProvider:
            async def search(self, request: SearchQuery) -> tuple[SearchResult, ...]:
                raise RuntimeError("upstream error")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            configs = (ProviderConfig("brave", "key", 2),)
            ledger = MonthlyQuotaLedger(path, configs)
            provider = RotatingSearchProvider(((configs[0], FailingProvider()),), ledger)
            with self.assertRaises(SearchUnavailableError):
                await provider.search(SearchQuery("question", "en"))
            with self.assertRaises(SearchUnavailableError):
                await provider.search(SearchQuery("question", "en"))
            state = json.loads(path.read_text())

        self.assertTrue(state["global_disabled"])


if __name__ == "__main__":
    unittest.main()
