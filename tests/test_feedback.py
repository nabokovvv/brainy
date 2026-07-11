from __future__ import annotations

import unittest

from brainy_core.feedback import FeedbackEntry, FeedbackStore


def _entry(**overrides) -> FeedbackEntry:
    defaults = dict(
        provider="ollama", model="gemma4:e2b", latency_ms=1234.5, lang="en", route="local"
    )
    defaults.update(overrides)
    return FeedbackEntry(**defaults)


class FeedbackStoreTests(unittest.TestCase):
    def test_put_then_pop_returns_entry_once(self) -> None:
        store = FeedbackStore(maxsize=10, ttl_seconds=60)
        store.put("req-1", _entry())

        self.assertEqual(store.pop("req-1"), _entry())
        self.assertIsNone(store.pop("req-1"))

    def test_pop_missing_request_id_returns_none(self) -> None:
        store = FeedbackStore(maxsize=10, ttl_seconds=60)

        self.assertIsNone(store.pop("never-existed"))

    def test_bounded_size_evicts_oldest(self) -> None:
        store = FeedbackStore(maxsize=3, ttl_seconds=60)
        for index in range(10):
            store.put(f"req-{index}", _entry())
            self.assertLessEqual(len(store), 3)

        self.assertIsNone(store.pop("req-0"))
        self.assertIsNone(store.pop("req-6"))
        self.assertIsNotNone(store.pop("req-9"))

    def test_ttl_expiry_treats_stale_entry_as_missing(self) -> None:
        store = FeedbackStore(maxsize=10, ttl_seconds=5)
        store.put("req-1", _entry(), now=0.0)

        self.assertIsNone(store.pop("req-1", now=10.0))

    def test_entry_never_carries_dialogue_text_fields(self) -> None:
        entry = _entry()
        for field in ("provider", "model", "lang", "route"):
            value = getattr(entry, field)
            self.assertNotIn("\n", value)
        self.assertEqual(
            set(entry.__dataclass_fields__),
            {"provider", "model", "latency_ms", "lang", "route"},
        )

    def test_construction_rejects_non_positive_bounds(self) -> None:
        with self.assertRaises(ValueError):
            FeedbackStore(maxsize=0, ttl_seconds=60)
        with self.assertRaises(ValueError):
            FeedbackStore(maxsize=10, ttl_seconds=0)


if __name__ == "__main__":
    unittest.main()
