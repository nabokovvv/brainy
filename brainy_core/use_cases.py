"""Provider-neutral application use cases."""

from __future__ import annotations

from brainy_core.inference import ChatMessage, ChatRequest


def build_fast_chat_request(
    query: str,
    language: str,
    *,
    max_output_tokens: int = 400,
) -> ChatRequest:
    """Build the direct local-chat request without routing or provider work."""

    system_prompt = (
        "You are Brainy, a fast and helpful multilingual Telegram assistant. "
        "Answer the user's request directly, accurately, and without mentioning internal modes. "
        "Prefer a concise answer unless the user explicitly asks for detail. "
        f"Reply in the language identified by code '{language}', unless the user explicitly asks "
        "for another language. Do not invent current facts when web access is disabled."
    )
    return ChatRequest(
        messages=(
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=query),
        ),
        max_output_tokens=max_output_tokens,
        temperature=0.3,
    )
