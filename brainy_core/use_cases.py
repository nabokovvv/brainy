"""Provider-neutral application use cases."""

from __future__ import annotations

from collections.abc import Sequence

from brainy_core.inference import ChatMessage, ChatRequest
from brainy_core.persona import DEFAULT_PERSONA, with_persona


def build_fast_chat_request(
    query: str,
    language: str,
    *,
    persona: str = DEFAULT_PERSONA,
    history: Sequence[ChatMessage] = (),
    max_output_tokens: int = 400,
) -> ChatRequest:
    """Build the direct local-chat request without routing or provider work.

    ``history`` is a sequence of prior user/assistant turns (whole messages,
    never split) appended after the system prompt and before the current user
    turn. Callers are responsible for bounding its size (see
    ``brainy_core.memory``).
    """

    system_prompt = with_persona(
        "You are Brainy, a fast and helpful multilingual Telegram assistant. "
        "Answer the user's request directly and accurately. "
        "Prefer a concise answer unless the user explicitly asks for detail. "
        f"Reply in the language identified by code '{language}', unless the user explicitly asks "
        "for another language. If you don't know something - just say so! Do not invent facts.",
        persona,
    )
    return ChatRequest(
        messages=(
            ChatMessage(role="system", content=system_prompt),
            *tuple(history),
            ChatMessage(role="user", content=query),
        ),
        max_output_tokens=max_output_tokens,
        temperature=1,
    )
