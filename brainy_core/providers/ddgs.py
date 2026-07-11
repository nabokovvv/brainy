"""Async adapter for the ``ddgs`` metasearch library.

The backend is selected explicitly.  ``auto`` and the optional DHT mode are not
used because they can change the upstream engine and privacy characteristics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from brainy_core.search import SearchQuery, SearchResult

_PROVIDER_NAME = "ddgs-duckduckgo"


class SearchProviderError(RuntimeError):
    """Content-free internal provider failure."""


class SearchUnavailableError(SearchProviderError):
    """The search provider cannot currently be used."""


class DDGSProvider:
    """Run ddgs' DuckDuckGo backend without blocking the asyncio event loop."""

    name = _PROVIDER_NAME

    def __init__(self, *, timeout_seconds: float = 8.0) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30.")
        self._timeout_seconds = timeout_seconds
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]:
        if self._closed:
            raise SearchUnavailableError("ddgs provider is closed")
        try:
            raw_results = await asyncio.to_thread(
                self._search_sync, request.query, request.language, request.limit
            )
        except SearchProviderError as exc:
            raise SearchUnavailableError("ddgs search failed") from exc
        except Exception as exc:
            raise SearchUnavailableError("ddgs search failed") from exc

        results: list[SearchResult] = []
        for rank, item in enumerate(raw_results, start=1):
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("href", "")).strip()
            snippet = str(item.get("body", "")).strip()
            if not title or not url or not snippet:
                continue
            try:
                results.append(SearchResult(title, url, snippet, rank, self.name))
            except ValueError:
                continue
        return tuple(results)

    def _search_sync(self, query: str, language: str, limit: int) -> Sequence[object]:
        try:
            from ddgs import DDGS

            return DDGS(timeout=self._timeout_seconds).text(
                query,
                region=_ddgs_region(language),
                max_results=limit,
                backend="duckduckgo",
            )
        except Exception as exc:
            raise SearchProviderError("ddgs search failed") from exc


def _ddgs_region(language: str) -> str:
    code = language.strip().casefold().replace("_", "-").split("-", 1)[0]
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
