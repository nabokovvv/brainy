from __future__ import annotations

import unittest

from brainy_core.scheduling import StablePriorityQueue


class NonComparablePayload:
    pass


class StablePriorityQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_equal_priorities_are_fifo_without_comparing_payloads(self) -> None:
        queue: StablePriorityQueue[NonComparablePayload] = StablePriorityQueue(maxsize=3)
        first = NonComparablePayload()
        second = NonComparablePayload()

        await queue.put(1, first)
        await queue.put(1, second)

        self.assertEqual(await queue.get(), (1, first))
        queue.task_done()
        self.assertEqual(await queue.get(), (1, second))
        queue.task_done()
        await queue.join()

    async def test_lower_numeric_priority_is_processed_first(self) -> None:
        queue: StablePriorityQueue[str] = StablePriorityQueue(maxsize=3)
        await queue.put(5, "slow")
        await queue.put(1, "fast")

        self.assertEqual(await queue.get(), (1, "fast"))

    async def test_queue_is_bounded(self) -> None:
        queue: StablePriorityQueue[str] = StablePriorityQueue(maxsize=1)
        await queue.put(1, "first")

        self.assertTrue(queue.full())


if __name__ == "__main__":
    unittest.main()
