from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from brainy_core.providers.ddgs import DDGSProvider
from brainy_core.providers.ddgs import SearchUnavailableError
from brainy_core.search import SearchQuery


class DDGSProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_ddgs_body_to_search_snippet_and_selects_backend(self) -> None:
        calls: dict[str, object] = {}

        class FakeDDGS:
            def __init__(self, *, timeout: float) -> None:
                calls["timeout"] = timeout

            def text(self, query: str, **kwargs: object) -> list[dict[str, str]]:
                calls["query"] = query
                calls.update(kwargs)
                return [
                    {"title": "Example", "href": "https://example.com/a", "body": "Snippet"},
                    {"title": "Missing URL", "href": "", "body": "Ignored"},
                ]

        with patch.dict(sys.modules, {"ddgs": types.SimpleNamespace(DDGS=FakeDDGS)}):
            provider = DDGSProvider(timeout_seconds=7)
            results = await provider.search(SearchQuery("latest news", "ru", limit=2))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "Snippet")
        self.assertEqual(calls["query"], "latest news")
        self.assertEqual(calls["region"], "ru-ru")
        self.assertEqual(calls["backend"], "duckduckgo")
        self.assertEqual(calls["max_results"], 2)

    async def test_search_failure_is_content_free_and_bounded(self) -> None:
        class FailingDDGS:
            def __init__(self, *, timeout: float) -> None:
                pass

            def text(self, query: str, **kwargs: object) -> list[dict[str, str]]:
                raise RuntimeError("upstream failure with query content")

        with patch.dict(sys.modules, {"ddgs": types.SimpleNamespace(DDGS=FailingDDGS)}):
            provider = DDGSProvider()
            with self.assertRaisesRegex(SearchUnavailableError, "ddgs search failed"):
                await provider.search(SearchQuery("query", "en"))

    async def test_closed_provider_fails_closed(self) -> None:
        provider = DDGSProvider()
        await provider.aclose()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await provider.search(SearchQuery("query", "en"))


if __name__ == "__main__":
    unittest.main()
