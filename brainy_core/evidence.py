"""Evidence packing and grounded synthesis for the explicit Web ON path."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlparse

from brainy_core.inference import ChatMessage, ChatRequest, InferenceProvider
from brainy_core.search import SearchProvider, SearchQuery


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    text: str
    canonical_url: str
    provenance: str
    rank: int
    trust: str = "search_snippet"


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    items: tuple[Evidence, ...]
    token_budget: int

    def __post_init__(self) -> None:
        if self.token_budget < 1:
            raise ValueError("token_budget must be positive")
        if len({item.evidence_id for item in self.items}) != len(self.items):
            raise ValueError("evidence IDs must be unique")

    @property
    def by_id(self) -> dict[str, Evidence]:
        return {item.evidence_id: item for item in self.items}

    @property
    def estimated_tokens(self) -> int:
        return sum(_estimate_tokens(item.text) for item in self.items)


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    answer: str
    citation_ids: tuple[str, ...]
    citations: tuple[Evidence, ...]


class SearchGateway:
    """Search, normalize, deduplicate, and pack results deterministically."""

    def __init__(
        self,
        provider: SearchProvider,
        *,
        token_budget: int = 1200,
        page_loader: Callable[[Sequence[str]], Awaitable[Sequence[object]]] | None = None,
        fallback_provider: SearchProvider | None = None,
    ) -> None:
        if token_budget < 40:
            raise ValueError("token_budget must be at least 40")
        self._provider = provider
        self._token_budget = token_budget
        self._page_loader = page_loader
        self._fallback_provider = fallback_provider

    async def build_bundle(self, request: SearchQuery) -> EvidenceBundle:
        try:
            results = await self._provider.search(request)
        except Exception:
            if self._fallback_provider is None:
                raise
            results = await self._fallback_provider.search(request)
        items: list[Evidence] = []
        seen_urls: set[str] = set()
        for result in sorted(results, key=lambda item: (item.rank, item.canonical_url)):
            url = result.canonical_url
            if url in seen_urls:
                continue
            text = " ".join(f"{result.title}: {result.snippet}".split())
            if not text:
                continue
            if (
                _estimate_tokens(text) + sum(_estimate_tokens(item.text) for item in items)
                > self._token_budget
            ):
                break
            digest = hashlib.sha256(f"{url}\n{text}".encode()).hexdigest()[:12]
            items.append(
                Evidence(f"E{len(items) + 1}-{digest}", text, url, result.provider, result.rank)
            )
            seen_urls.add(url)
        if self._page_loader is not None and seen_urls:
            chunks = await self._page_loader(tuple(sorted(seen_urls)))
            query_terms = _terms(request.query)
            ranked = sorted(
                enumerate(chunks),
                key=lambda pair: (
                    -len(query_terms & _terms(str(getattr(pair[1], "text", "")))),
                    pair[0],
                ),
            )
            selected: list[tuple[str, str]] = []
            host_counts: dict[str, int] = {}
            for _, chunk in ranked:
                url = getattr(chunk, "source_url", "")
                text = " ".join(str(getattr(chunk, "text", "")).split())
                host = (urlparse(url).hostname or "").lower()
                if not url or not text or host_counts.get(host, 0) >= 2:
                    continue
                if any(_near_duplicate(text, existing) for _, existing in selected):
                    continue
                selected.append((url, text))
                host_counts[host] = host_counts.get(host, 0) + 1
            for index, (url, text) in enumerate(selected):
                if (
                    _estimate_tokens(text) + sum(_estimate_tokens(item.text) for item in items)
                    > self._token_budget
                ):
                    continue
                digest = hashlib.sha256(f"{url}\n{text}".encode()).hexdigest()[:12]
                items.append(
                    Evidence(
                        f"E{len(items) + 1}-{digest}",
                        text,
                        url,
                        "page_chunk",
                        len(items) + index + 1,
                        "page_content",
                    )
                )
        return EvidenceBundle(tuple(items), self._token_budget)


class GroundedSynthesizer:
    """Answer a question from clean web context, letting the app attach citations.

    The model receives only the evidence *text* — no IDs, no URLs — and replies
    in plain prose, exactly like the local fast path. That lets the answer stream
    token-by-token and removes any chance of the model echoing an evidence ID into
    the text. Citations are the top-ranked retrieved sources, attached by the app.
    """

    def __init__(self, provider: InferenceProvider) -> None:
        self._provider = provider

    def build_request(self, query: str, language: str, bundle: EvidenceBundle) -> ChatRequest:
        context = "\n\n".join(item.text for item in bundle.items)
        system_prompt = (
            "You are Brainy, a helpful multilingual Telegram assistant with web access. "
            "Answer the question using only the web context below. If the context does not "
            "contain the answer, say so briefly instead of guessing. Reply in the language "
            f"identified by code '{language}'. Write plain prose only — no citation markers, "
            "no bracketed IDs, and no URLs; sources are shown separately by the app."
        )
        user_prompt = f"Question: {query}\n\nWeb context:\n{context}"
        return ChatRequest(
            messages=(
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ),
            max_output_tokens=500,
            temperature=0.0,
        )

    @staticmethod
    def select_citations(bundle: EvidenceBundle, limit: int = 3) -> tuple[Evidence, ...]:
        ordered = sorted(bundle.items, key=lambda item: item.rank)
        return tuple(ordered[:limit])

    async def synthesize(self, query: str, language: str, bundle: EvidenceBundle) -> GroundedAnswer:
        """Non-streaming convenience: providers without stream_chat use this."""

        result = await self._provider.chat(self.build_request(query, language, bundle))
        answer = re.sub(r"<think>.*?</think>", "", result.text, flags=re.S | re.I).strip()
        if not answer:
            raise ValueError("synthesis returned an empty answer")
        citations = self.select_citations(bundle)
        return GroundedAnswer(answer, tuple(c.evidence_id for c in citations), citations)


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _terms(text: str) -> set[str]:
    return {word.casefold() for word in text.split() if len(word) > 2}


def _near_duplicate(left: str, right: str) -> bool:
    left_terms, right_terms = _terms(left), _terms(right)
    if not left_terms or not right_terms:
        return left.casefold() == right.casefold()
    return len(left_terms & right_terms) / len(left_terms | right_terms) >= 0.85
