from __future__ import annotations

import unittest

from brainy_core.source_exploration import SourceExploration, SourceExplorationStore


class SourceExplorationStoreTests(unittest.TestCase):
    def test_entry_is_one_shot_and_bound_to_chat(self) -> None:
        store = SourceExplorationStore()
        entry = SourceExploration(chat_id=7, query="private question", language="en")
        store.put("token", entry, now=10)

        self.assertIsNone(store.pop("token", chat_id=8, now=11))
        self.assertIsNone(store.pop("token", chat_id=7, now=11))

    def test_entry_expires_and_store_is_bounded(self) -> None:
        store = SourceExplorationStore(maxsize=1, ttl_seconds=5)
        first = SourceExploration(chat_id=1, query="first", language="en")
        second = SourceExploration(chat_id=1, query="second", language="en")
        store.put("first", first, now=1)
        store.put("second", second, now=2)

        self.assertIsNone(store.pop("first", chat_id=1, now=2))
        self.assertIsNone(store.pop("second", chat_id=1, now=8))


if __name__ == "__main__":
    unittest.main()
