"""Stable bounded priority queue primitives for request scheduling."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Generic, TypeVar


PayloadT = TypeVar("PayloadT")


@dataclass(order=True, frozen=True)
class _QueueEntry(Generic[PayloadT]):
    priority: int
    sequence: int
    payload: PayloadT = field(compare=False)


class StablePriorityQueue(Generic[PayloadT]):
    """Bounded priority queue with FIFO ordering for equal priorities."""

    def __init__(self, *, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be greater than zero")
        self._queue: asyncio.PriorityQueue[_QueueEntry[PayloadT]] = asyncio.PriorityQueue(
            maxsize=maxsize
        )
        self._sequence = itertools.count()

    async def put(self, priority: int, payload: PayloadT) -> None:
        await self._queue.put(
            _QueueEntry(priority=priority, sequence=next(self._sequence), payload=payload)
        )

    async def get(self) -> tuple[int, PayloadT]:
        entry = await self._queue.get()
        return entry.priority, entry.payload

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def qsize(self) -> int:
        return self._queue.qsize()

    def full(self) -> bool:
        return self._queue.full()
