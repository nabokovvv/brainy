"""Provider-neutral contracts for the explicit Web ON path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """A bounded user search request detached from any backend format."""

    query: str
    language: str
    limit: int = 5

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("Search query must be non-empty.")
        if not self.language.strip():
            raise ValueError("Search language must be non-empty.")
        if not 1 <= self.limit <= 10:
            raise ValueError("Search result limit must be between 1 and 10.")


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Normalized result returned by every search backend."""

    title: str
    url: str
    snippet: str
    rank: int
    provider: str
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.snippet.strip():
            raise ValueError("Search result title and snippet must be non-empty.")
        if self.rank < 1:
            raise ValueError("Search result rank must be positive.")
        if not self.provider.strip():
            raise ValueError("Search result provider must be non-empty.")
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("Search result URL must be an absolute HTTP(S) URL.")
        if parts.username or parts.password:
            raise ValueError("Search result URL must not contain credentials.")

    @property
    def canonical_url(self) -> str:
        """Return a stable URL without fragments or an accidental trailing slash."""

        parts = urlsplit(self.url)
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


class SearchProvider(Protocol):
    """Async adapter contract used by the Web ON orchestration layer."""

    name: str

    async def search(self, request: SearchQuery) -> Sequence[SearchResult]: ...
