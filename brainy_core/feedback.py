"""Bounded, in-memory correlation store for the 👍/👎 feedback slice.

Design: docs/FEEDBACK_DESIGN.md. Holds only non-content metadata
(provider/model/latency/lang/route) keyed by a short-lived request_id.
Never stores dialogue text; nothing here is persisted to disk.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

DEFAULT_MAXSIZE = 500
DEFAULT_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class FeedbackEntry:
    provider: str
    model: str
    latency_ms: float
    lang: str
    route: str


class FeedbackStore:
    """FIFO-evicting, TTL-expiring map of request_id -> FeedbackEntry."""

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_MAXSIZE,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, FeedbackEntry]] = OrderedDict()

    def put(self, request_id: str, entry: FeedbackEntry, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        self._entries[request_id] = (timestamp, entry)
        self._entries.move_to_end(request_id)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def pop(self, request_id: str, *, now: float | None = None) -> FeedbackEntry | None:
        """Remove and return the entry, or None if missing/expired (idempotent vote)."""
        timestamp = time.monotonic() if now is None else now
        stored = self._entries.pop(request_id, None)
        if stored is None:
            return None
        created_at, entry = stored
        if timestamp - created_at > self._ttl_seconds:
            return None
        return entry

    def __len__(self) -> int:
        return len(self._entries)
