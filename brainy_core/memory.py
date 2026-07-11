"""In-memory rolling conversation memory (char-budget, whole-message).

Holds the recent user/assistant turns per chat so the next request can include
short multi-turn context. It is intentionally in-memory only: the content of
private conversations is never written to disk (see AGENTS.md). Only the
per-chat budget *setting* is persisted by ``storage``.
"""

from __future__ import annotations

from brainy_core.inference import ChatMessage

# Selectable budgets in characters: no memory / ~one turn / ~ten turns.
MEMORY_BUDGET_OPTIONS: tuple[int, ...] = (0, 1000, 10000)

# Hard cap on stored messages per chat so a very long session cannot grow the
# in-memory buffer without bound. The sent context is still bounded by budget.
MAX_STORED_MESSAGES = 100

# Map a budget value to its translation key for UI labels.
BUDGET_LABEL_KEY: dict[int, str] = {
    0: "memory_off",
    1000: "memory_small",
    10000: "memory_large",
}

_user_history: dict[int, list[ChatMessage]] = {}


def is_valid_budget(budget: int) -> bool:
    """Return True for a known memory-budget value."""

    return budget in MEMORY_BUDGET_OPTIONS


def add_turn(chat_id: int, user_message: ChatMessage, assistant_message: ChatMessage) -> None:
    """Append one completed (user, assistant) turn to the rolling buffer."""

    history = _user_history.setdefault(chat_id, [])
    history.append(user_message)
    history.append(assistant_message)
    if len(history) > MAX_STORED_MESSAGES:
        del history[: len(history) - MAX_STORED_MESSAGES]


def get_history(chat_id: int, budget: int) -> tuple[ChatMessage, ...]:
    """Return prior turns bounded to ``budget`` characters, whole messages only.

    Messages are taken from the most recent end and never split. A non-positive
    budget returns no history (the 0-turn baseline).
    """

    if budget <= 0:
        return ()
    history = _user_history.get(chat_id)
    if not history:
        return ()

    selected: list[ChatMessage] = []
    total = 0
    for message in reversed(history):
        size = len(message.content)
        if selected and total + size > budget:
            break
        selected.append(message)
        total += size
    selected.reverse()
    return tuple(selected)


def clear(chat_id: int) -> None:
    """Drop any stored history for a chat (e.g. on memory budget set to 0)."""

    _user_history.pop(chat_id, None)


def _reset_for_testing() -> None:
    """Clear all in-memory history. Test helper only."""

    _user_history.clear()


__all__ = [
    "BUDGET_LABEL_KEY",
    "MEMORY_BUDGET_OPTIONS",
    "add_turn",
    "clear",
    "get_history",
    "is_valid_budget",
]
