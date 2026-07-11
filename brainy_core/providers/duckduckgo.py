"""Best-effort DuckDuckGo HTML search adapter for the Web ON path.

The adapter deliberately has no import-time I/O.  A caller may inject the
application-lifespan ``httpx.AsyncClient``; otherwise the adapter owns a lazily
created client and must be closed with :meth:`aclose`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from brainy_core.search import SearchQuery, SearchResult
from brainy_core.web_safety import is_safe_public_http_url

_PROVIDER_NAME = "duckduckgo"
_SEARCH_URL = "https://html.duckduckgo.com/html/"
_MAX_RESPONSE_BYTES = 512 * 1024
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class SearchProviderError(RuntimeError):
    """A bounded, content-free search failure suitable for Web ON handling."""


class SearchUnavailableError(SearchProviderError):
    """The provider cannot currently be used (including an open circuit)."""


class SearchResponseError(SearchProviderError):
    """The provider returned an unusable response."""


class _DuckDuckGoHtmlParser(HTMLParser):
    """Extract only result links, titles, and snippets from the HTML endpoint."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, str]] = []
        self._current: dict[str, str] | None = None
        self._result_depth = 0
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._title_depth = 0
        self._snippet_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if self._current is None and "result" in classes:
            self._current = {"href": "", "title": "", "snippet": ""}
            self._result_depth = 1
            return
        if self._current is not None:
            self._result_depth += 1
        if tag == "a" and "result__a" in classes:
            if self._current is not None:
                self._current["href"] = dict(attrs).get("href") or ""
            self._title_parts = []
            self._title_depth = 1
        elif self._title_depth:
            self._title_depth += 1
        if "result__snippet" in classes:
            self._snippet_parts = []
            self._snippet_depth = 1
        elif self._snippet_depth:
            self._snippet_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                if self._current is not None:
                    self._current["title"] = " ".join("".join(self._title_parts).split())
        if self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0 and self._current is not None:
                self._current["snippet"] = " ".join("".join(self._snippet_parts).split())
        if self._current is not None:
            self._result_depth -= 1
            if self._result_depth == 0:
                title = self._current["title"]
                href = self._current["href"]
                snippet = self._current["snippet"]
                if title and href and snippet:
                    self.results.append((title, href, snippet))
                self._current = None

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._snippet_depth:
            self._snippet_parts.append(data)

    def parsed_results(self) -> list[tuple[str, str, str]]:
        return self.results


class DuckDuckGoProvider:
    """Free best-effort search with cache, pacing, retry, and circuit breaking."""

    name = _PROVIDER_NAME

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 8.0,
        cache_ttl_seconds: float = 300.0,
        min_interval_seconds: float = 1.0,
        failure_threshold: int = 3,
        circuit_reset_seconds: float = 30.0,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30.")
        if cache_ttl_seconds < 0 or min_interval_seconds < 0 or circuit_reset_seconds <= 0:
            raise ValueError("cache, interval, and circuit reset values must be non-negative.")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive.")
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self._client = client
        self._owns_client = client is None
        self._cache_ttl_seconds = cache_ttl_seconds
        self._min_interval_seconds = min_interval_seconds
        self._failure_threshold = failure_threshold
        self._circuit_reset_seconds = circuit_reset_seconds
        self._cache: dict[tuple[str, str, int], tuple[float, tuple[SearchResult, ...]]] = {}
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None
        self._closed = False

    async def __aenter__(self) -> "DuckDuckGoProvider":
        self._get_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]:
        if self._closed:
            raise SearchUnavailableError("duckduckgo provider is closed")
        key = (" ".join(request.query.split()), request.language.lower(), request.limit)
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self._cache_ttl_seconds:
                return cached[1]
            if self._circuit_opened_at is not None:
                if now - self._circuit_opened_at < self._circuit_reset_seconds:
                    raise SearchUnavailableError("duckduckgo circuit is open")
                self._circuit_opened_at = None
                self._consecutive_failures = 0
            delay = self._next_request_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + self._min_interval_seconds

        try:
            results = await self._request(request)
        except SearchProviderError:
            async with self._lock:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_threshold:
                    self._circuit_opened_at = time.monotonic()
            raise

        immutable_results = tuple(results)
        async with self._lock:
            self._consecutive_failures = 0
            self._cache[key] = (time.monotonic(), immutable_results)
        return immutable_results

    async def _request(self, request: SearchQuery) -> list[SearchResult]:
        params = {"q": request.query, "kl": _duckduckgo_locale(request.language)}
        for attempt in range(2):
            try:
                response, content = await self._fetch_response(params)
            except httpx.TimeoutException as exc:
                if attempt == 0:
                    continue
                raise SearchUnavailableError("duckduckgo timed out") from exc
            except httpx.RequestError as exc:
                if attempt == 0:
                    continue
                raise SearchUnavailableError("duckduckgo request failed") from exc
            if response.status_code in _TRANSIENT_STATUS_CODES and attempt == 0:
                continue
            break
        else:  # Defensive: every loop branch either breaks or raises.
            raise SearchUnavailableError("duckduckgo request failed")
        if response.status_code >= 400:
            raise SearchUnavailableError(f"duckduckgo returned HTTP {response.status_code}")
        encoding = response.encoding or "utf-8"
        try:
            return _parse_results(content.decode(encoding, errors="replace"), request.limit)
        except (ValueError, UnicodeError) as exc:
            raise SearchResponseError("duckduckgo response could not be parsed") from exc

    async def _fetch_response(self, params: dict[str, str]) -> tuple[httpx.Response, bytes]:
        """Read at most the response cap instead of buffering an arbitrary SERP."""

        async with self._get_client().stream(
            "GET", _SEARCH_URL, params=params, timeout=self._timeout
        ) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise SearchResponseError("duckduckgo response exceeds limit")
            return response, bytes(content)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"User-Agent": "BrainyBot/1.0"})
        return self._client


def _duckduckgo_locale(language: str) -> str:
    """Pass a conservative locale hint while retaining multilingual query text."""

    code = language.strip().lower().split("-", 1)[0]
    return {
        "en": "us-en",
        "ru": "ru-ru",
        "de": "de-de",
        "es": "es-es",
        "pt": "pt-pt",
        "tr": "tr-tr",
        "id": "id-id",
        "fr": "fr-fr",
    }.get(code, "wt-wt")


def _decode_result_url(href: str) -> str:
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlsplit(href)
    if parsed.netloc.lower().endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return href


def _parse_results(html: str, limit: int) -> list[SearchResult]:
    parser = _DuckDuckGoHtmlParser()
    parser.feed(html)
    results: list[SearchResult] = []
    for title, href, snippet in parser.parsed_results():
        url = _decode_result_url(unescape(href))
        if not snippet or not is_safe_public_http_url(url):
            continue
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                rank=len(results) + 1,
                provider=_PROVIDER_NAME,
            )
        )
        if len(results) == limit:
            break
    return results
