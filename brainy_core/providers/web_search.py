"""HTTP search adapters and monthly quota rotation for Web ON."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from brainy_core.search import SearchProvider, SearchQuery, SearchResult


class WebSearchError(RuntimeError):
    """Content-free provider error suitable for rotation and user fallback."""


class TransientSearchError(WebSearchError):
    """A temporary provider failure (timeout, rate limit, 5xx, bad payload).

    The provider should be skipped for this request but stays eligible next time —
    a transient blip must not disable a provider for the rest of the month.
    """


class ProviderAuthError(WebSearchError):
    """A permanent provider failure (bad or expired key). Disable for the month."""


class SearchUnavailableError(WebSearchError):
    """No configured search provider can currently serve the request."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    api_key: str
    monthly_limit: int

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.api_key.strip():
            raise ValueError("provider name and api key must be non-empty")
        if self.monthly_limit < 1:
            raise ValueError("monthly_limit must be positive")


class MonthlyQuotaLedger:
    """Persistent UTC calendar-month request ledger with fail-closed state."""

    def __init__(self, path: str | os.PathLike[str], providers: Sequence[ProviderConfig]) -> None:
        self._path = Path(path).expanduser()
        self._providers = {provider.name: provider for provider in providers}
        self._lock = asyncio.Lock()

    async def reserve(self, provider_name: str) -> bool:
        async with self._lock:
            state = self._load()
            self._roll_month(state)
            if state["global_disabled"] or provider_name not in self._providers:
                return False
            record = state["providers"].setdefault(provider_name, {"used": 0, "failed": False})
            provider = self._providers[provider_name]
            if record["failed"] or record["used"] >= provider.monthly_limit:
                return False
            record["used"] += 1
            self._save(state)
            return True

    async def mark_failed(self, provider_name: str) -> None:
        async with self._lock:
            state = self._load()
            self._roll_month(state)
            if provider_name in state["providers"]:
                state["providers"][provider_name]["failed"] = True
            if self._all_unavailable(state):
                state["global_disabled"] = True
            self._save(state)

    async def is_globally_disabled(self) -> bool:
        async with self._lock:
            state = self._load()
            self._roll_month(state)
            self._save(state)
            return bool(state["global_disabled"])

    async def disable_global(self) -> None:
        async with self._lock:
            state = self._load()
            self._roll_month(state)
            state["global_disabled"] = True
            self._save(state)

    async def disable_if_exhausted(self) -> None:
        """Latch Web ON off only when every provider is exhausted or failed."""
        async with self._lock:
            state = self._load()
            self._roll_month(state)
            if self._all_unavailable(state):
                state["global_disabled"] = True
            self._save(state)

    def _load(self) -> dict[str, Any]:
        month = _current_month()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("month", month)
        payload.setdefault("global_disabled", False)
        payload.setdefault("providers", {})
        if not isinstance(payload["providers"], dict):
            payload["providers"] = {}
        return payload

    def _roll_month(self, state: dict[str, Any]) -> None:
        month = _current_month()
        if state.get("month") == month:
            return
        state["month"] = month
        state["global_disabled"] = False
        state["providers"] = {}

    def _all_unavailable(self, state: dict[str, Any]) -> bool:
        for name, provider in self._providers.items():
            record = state["providers"].setdefault(name, {"used": 0, "failed": False})
            if not record["failed"] and record["used"] < provider.monthly_limit:
                return False
        return bool(self._providers)

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="quota-", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class BraveSearchProvider:
    name = "brave"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]:
        response = await _request(
            self._client,
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": self._api_key},
            params={
                "q": request.query,
                "count": request.limit,
                "search_lang": _language(request.language),
            },
        )
        payload = _json_object(response)
        results = payload.get("web", {}).get("results", [])
        return _results(results, "title", "url", "description", self.name)

    async def aclose(self) -> None:
        return None


class TavilySearchProvider:
    name = "tavily"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]:
        response = await _request(
            self._client,
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "query": request.query,
                "search_depth": "basic",
                "topic": "general",
                "max_results": request.limit,
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        payload = _json_object(response)
        return _results(payload.get("results", []), "title", "url", "content", self.name)

    async def aclose(self) -> None:
        return None


class SerpApiSearchProvider:
    name = "serpapi"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]:
        response = await _request(
            self._client,
            "https://serpapi.com/search",
            params={
                "engine": "google",
                "q": request.query,
                "num": request.limit,
                "hl": _language(request.language),
                "api_key": self._api_key,
            },
        )
        payload = _json_object(response)
        return _results(payload.get("organic_results", []), "title", "link", "snippet", self.name)

    async def aclose(self) -> None:
        return None


class RotatingSearchProvider:
    """Try quota-eligible providers in order, spending one slot per attempt."""

    name = "rotation"

    def __init__(
        self, providers: Sequence[tuple[ProviderConfig, SearchProvider]], ledger: MonthlyQuotaLedger
    ) -> None:
        self._providers = tuple(providers)
        self._ledger = ledger

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]:
        if await self._ledger.is_globally_disabled():
            raise SearchUnavailableError("web search disabled until next month")
        attempted = False
        had_success = False
        for config, provider in self._providers:
            if not await self._ledger.reserve(config.name):
                continue
            attempted = True
            try:
                results = tuple(await provider.search(request))
            except ProviderAuthError:
                # Permanent failure: stop spending on this provider this month.
                await self._ledger.mark_failed(config.name)
                continue
            except Exception:
                # Transient failure: skip for this request; the provider stays
                # eligible so a blip cannot disable Web ON for the month.
                continue
            had_success = True
            if results:
                return results
        if had_success:
            return ()
        # Only latch Web ON off when every provider is genuinely out of quota or
        # permanently failed — never because a single request hit transient errors.
        await self._ledger.disable_if_exhausted()
        if not attempted:
            raise SearchUnavailableError("web search quota unavailable")
        raise SearchUnavailableError("all web search providers failed")

    async def aclose(self) -> None:
        for _, provider in self._providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


def build_rotating_provider(
    settings: Any, client: httpx.AsyncClient
) -> RotatingSearchProvider | None:
    """Build configured adapters without exposing provider secret names to handlers."""

    provider_configs = tuple(
        provider
        for provider in (
            ProviderConfig("tavily", settings.tavily_api_key, settings.tavily_monthly_limit)
            if settings.tavily_api_key
            else None,
            ProviderConfig(
                "brave", settings.brave_search_api_key, settings.brave_search_monthly_limit
            )
            if settings.brave_search_api_key
            else None,
            ProviderConfig("serpapi", settings.serpapi_api_key, settings.serpapi_monthly_limit)
            if settings.serpapi_api_key
            else None,
        )
        if provider is not None
    )
    if not provider_configs:
        return None
    adapters = {
        "brave": BraveSearchProvider(client, settings.brave_search_api_key or ""),
        "tavily": TavilySearchProvider(client, settings.tavily_api_key or ""),
        "serpapi": SerpApiSearchProvider(client, settings.serpapi_api_key or ""),
    }
    return RotatingSearchProvider(
        tuple((item, adapters[item.name]) for item in provider_configs),
        MonthlyQuotaLedger(settings.search_quota_state_path, provider_configs),
    )


async def _request(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    try:
        response = await client.request(
            "POST" if "json" in kwargs else "GET", url, timeout=8, **kwargs
        )
        if response.status_code in (401, 403):
            # Bad or expired key: retrying this month will not help.
            raise ProviderAuthError(f"search api auth {response.status_code}")
        if response.status_code >= 400:
            # Rate limits (429), server errors (5xx), and other 4xx are treated
            # as transient so one blip cannot kill the provider for the month.
            raise TransientSearchError(f"search api status {response.status_code}")
        return response
    except WebSearchError:
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        raise TransientSearchError("search api request failed") from exc


def _json_object(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise TransientSearchError("search api returned invalid json") from exc
    if not isinstance(payload, Mapping):
        raise TransientSearchError("search api returned invalid payload")
    return payload


def _results(
    values: object, title_key: str, url_key: str, snippet_key: str, provider: str
) -> tuple[SearchResult, ...]:
    if not isinstance(values, list):
        return ()
    results: list[SearchResult] = []
    for rank, item in enumerate(values, start=1):
        if not isinstance(item, Mapping):
            continue
        title = str(item.get(title_key, "")).strip()
        url = str(item.get(url_key, "")).strip()
        snippet = str(item.get(snippet_key, "")).strip()
        if not title or not url or not snippet:
            continue
        try:
            results.append(SearchResult(title, url, snippet, rank, provider))
        except ValueError:
            continue
    return tuple(results)


def _language(language: str) -> str:
    return language.strip().casefold().split("-", 1)[0] or "en"


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")
