"""Tests for the in-memory char-budget rolling conversation memory."""

from __future__ import annotations

import unittest

from brainy_core.inference import ChatMessage
from brainy_core.memory import (
    BUDGET_LABEL_KEY,
    MEMORY_BUDGET_OPTIONS,
    add_turn,
    clear,
    get_history,
    is_valid_budget,
)


def _u(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def _a(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


class MemoryBudgetTests(unittest.TestCase):
    def test_valid_budgets(self) -> None:
        self.assertEqual(MEMORY_BUDGET_OPTIONS, (0, 1000, 10000))
        for budget in MEMORY_BUDGET_OPTIONS:
            self.assertTrue(is_valid_budget(budget))
        self.assertFalse(is_valid_budget(500))
        self.assertFalse(is_valid_budget(-1))

    def test_label_keys_cover_all_budgets(self) -> None:
        self.assertEqual(set(BUDGET_LABEL_KEY), set(MEMORY_BUDGET_OPTIONS))


class MemoryHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        clear(7)

    def tearDown(self) -> None:
        clear(7)

    def test_zero_budget_returns_no_history(self) -> None:
        add_turn(7, _u("hello"), _a("hi"))
        self.assertEqual(get_history(7, 0), ())

    def test_history_is_whole_messages_from_the_tail(self) -> None:
        add_turn(7, _u("first question"), _a("first answer"))
        add_turn(7, _u("second question"), _a("second answer"))

        history = get_history(7, 10_000)
        self.assertEqual(
            [m.content for m in history],
            ["first question", "first answer", "second question", "second answer"],
        )

    def test_history_drops_oldest_whole_messages_when_over_budget(self) -> None:
        # Each turn is ~16 chars; a 30-char budget keeps only the last turn.
        add_turn(7, _u("aaaa aaaa aaaa"), _a("bbbb bbbb bbbb"))
        add_turn(7, _u("cccc cccc cccc"), _a("dddd dddd dddd"))

        history = get_history(7, 30)
        contents = [m.content for m in history]
        self.assertNotIn("aaaa aaaa aaaa", contents)
        self.assertIn("cccc cccc cccc", contents)
        self.assertIn("dddd dddd dddd", contents)

    def test_history_never_splits_a_message(self) -> None:
        # A 40-char message must be kept whole, never truncated to fit budget=20.
        add_turn(7, _u("x" * 40), _a("y" * 40))
        history = get_history(7, 20)
        self.assertEqual([m.content for m in history], ["y" * 40])

    def test_empty_history_when_no_turns(self) -> None:
        self.assertEqual(get_history(7, 1000), ())

    def test_clear_removes_stored_history(self) -> None:
        add_turn(7, _u("q"), _a("a"))
        clear(7)
        self.assertEqual(get_history(7, 1000), ())


if __name__ == "__main__":
    unittest.main()
