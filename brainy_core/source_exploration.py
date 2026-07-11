"""Short-lived private state for the bounded "Explore sources" action."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceExploration:
    chat_id: int
    query: str
    language: str


class SourceExplorationStore:
    """One-shot FIFO/TTL store; content is kept in memory and never persisted."""

    def __init__(self, *, maxsize: int = 200, ttl_seconds: float = 30 * 60) -> None:
        if maxsize <= 0 or ttl_seconds <= 0:
            raise ValueError("maxsize and ttl_seconds must be positive")
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, SourceExploration]] = OrderedDict()

    def put(self, token: str, entry: SourceExploration, *, now: float | None = None) -> None:
        created_at = time.monotonic() if now is None else now
        self._entries[token] = (created_at, entry)
        self._entries.move_to_end(token)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def pop(
        self, token: str, *, chat_id: int, now: float | None = None
    ) -> SourceExploration | None:
        current_time = time.monotonic() if now is None else now
        stored = self._entries.pop(token, None)
        if stored is None:
            return None
        created_at, entry = stored
        if current_time - created_at > self._ttl_seconds or entry.chat_id != chat_id:
            return None
        return entry

    def __len__(self) -> int:
        return len(self._entries)
