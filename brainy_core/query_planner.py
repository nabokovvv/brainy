"""Turn a raw user message into 1-2 bounded web search queries.

Search APIs are rate-limited and every extra call costs latency, so the planner
is deliberately conservative:

- Short, single-sentence messages are already good search queries: they pass
  through untouched with no LLM call at all.
- Longer messages get exactly one LLM rewrite into a single concise query.
- A second query is allowed only for very long messages (> 500 chars), where a
  single query is likely to miss part of the request.
- Any planner failure falls back to the truncated raw message; the Web path
  must never break because of query planning.
"""

from __future__ import annotations

import re

from brainy_core.inference import ChatMessage, ChatRequest, InferenceProvider

# Messages at or below this length skip the LLM entirely.
PASSTHROUGH_MAX_CHARS = 80
# Only messages longer than this may produce a second query.
SECOND_QUERY_MIN_CHARS = 500
# Hard cap applied to every produced query (and to the fallback).
MAX_QUERY_CHARS = 200

_SENTENCE_BREAK = re.compile(r"[.!?;\n]")
_LINE_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def is_passthrough_query(message: str) -> bool:
    """True when the raw message should be sent to search unchanged."""

    text = message.strip()
    return len(text) <= PASSTHROUGH_MAX_CHARS and not _SENTENCE_BREAK.search(text.rstrip(".!?"))


def fallback_query(message: str) -> str:
    """Deterministic degradation: the raw message, whitespace-normalized and capped."""

    return " ".join(message.split())[:MAX_QUERY_CHARS].strip()


def build_planner_request(message: str, language: str, *, allow_second: bool) -> ChatRequest:
    count_rule = (
        "If the message asks two clearly distinct questions, output two queries, "
        "one per line. Otherwise output exactly one query."
        if allow_second
        else "Output exactly one query."
    )
    system_prompt = (
        "You turn a user's chat message into a web search query. "
        f"Write the query in the language identified by code '{language}' "
        "(keep proper names, product names and code identifiers as written). "
        "Keep it short: the key terms only, no filler words, no quotes, "
        "no explanations. " + count_rule
    )
    return ChatRequest(
        messages=(
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=message),
        ),
        max_output_tokens=80,
        temperature=0.0,
    )


def parse_planned_queries(text: str, message: str, *, allow_second: bool) -> tuple[str, ...]:
    """Extract up to two sane queries from the model output, else fall back."""

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    queries: list[str] = []
    for line in cleaned.splitlines():
        candidate = _LINE_PREFIX.sub("", line).strip().strip('"“”«»')
        if not candidate or len(candidate) > MAX_QUERY_CHARS:
            continue
        if candidate.casefold() in {q.casefold() for q in queries}:
            continue
        queries.append(candidate)
        if len(queries) == (2 if allow_second else 1):
            break
    if not queries:
        return (fallback_query(message),)
    return tuple(queries)


async def plan_search_queries(
    message: str,
    language: str,
    provider: InferenceProvider,
) -> tuple[str, ...]:
    """Produce 1-2 search queries for the message; never raises."""

    if is_passthrough_query(message):
        return (message.strip(),)
    allow_second = len(message) > SECOND_QUERY_MIN_CHARS
    try:
        result = await provider.chat(
            build_planner_request(message, language, allow_second=allow_second)
        )
        return parse_planned_queries(result.text, message, allow_second=allow_second)
    except Exception:
        return (fallback_query(message),)
