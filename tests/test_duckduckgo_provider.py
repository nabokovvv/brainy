from __future__ import annotations

import unittest

import httpx

from brainy_core.providers.duckduckgo import (
    DuckDuckGoProvider,
    SearchResponseError,
    SearchUnavailableError,
)
from brainy_core.search import SearchQuery


HTML = """
<div class="result results_links"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews%3Fid%3D1">Example news</a><a class="result__snippet">First snippet</a></div>
<div class="result results_links"><a class="result__a" href="http://127.0.0.1/private">Unsafe</a><a class="result__snippet">Must not escape</a></div>
<div class="result results_links"><a class="result__a" href="https://example.org/second">Second result</a><a class="result__snippet">Second snippet</a></div>
"""


class DuckDuckGoProviderTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_normalizes_html_results_and_passes_language_hint(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text=HTML)

        async with self.client(handler) as client:
            provider = DuckDuckGoProvider(client=client, min_interval_seconds=0)
            results = await provider.search(SearchQuery("latest news", "ru", limit=2))

        self.assertEqual([result.rank for result in results], [1, 2])
        self.assertEqual(
            [result.url for result in results],
            ["https://example.com/news?id=1", "https://example.org/second"],
        )
        self.assertEqual(results[0].snippet, "First snippet")
        self.assertEqual(requests[0].url.params["kl"], "ru-ru")

    async def test_success_is_cached_without_a_second_network_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=HTML)

        async with self.client(handler) as client:
            provider = DuckDuckGoProvider(client=client, min_interval_seconds=0)
            request = SearchQuery("latest news", "en", limit=1)
            first = await provider.search(request)
            second = await provider.search(request)

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    async def test_retries_one_transient_failure_then_opens_circuit(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        async with self.client(handler) as client:
            provider = DuckDuckGoProvider(
                client=client, min_interval_seconds=0, failure_threshold=1, circuit_reset_seconds=60
            )
            with self.assertRaises(SearchUnavailableError):
                await provider.search(SearchQuery("latest news", "en"))
            with self.assertRaisesRegex(SearchUnavailableError, "circuit"):
                await provider.search(SearchQuery("another query", "en"))

        self.assertEqual(calls, 2)

    async def test_injected_client_is_not_closed_by_provider(self) -> None:
        async with self.client(lambda request: httpx.Response(200, text=HTML)) as client:
            provider = DuckDuckGoProvider(client=client)
            await provider.aclose()
            self.assertFalse(client.is_closed)

    async def test_response_over_the_cap_is_rejected(self) -> None:
        oversized = b"x" * (512 * 1024 + 1)
        async with self.client(lambda request: httpx.Response(200, content=oversized)) as client:
            provider = DuckDuckGoProvider(client=client, min_interval_seconds=0)
            with self.assertRaisesRegex(SearchResponseError, "exceeds limit"):
                await provider.search(SearchQuery("latest news", "en"))


if __name__ == "__main__":
    unittest.main()
