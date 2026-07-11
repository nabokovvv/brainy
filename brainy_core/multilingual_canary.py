"""Small, deterministic canary for Brainy's eight supported languages."""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from brainy_core.inference import ChatMessage, ChatRequest, InferenceProvider, ProviderError
from brainy_core.model_catalog import BRAINY_LANGUAGES, MultilingualCanaryResult


@dataclass(frozen=True)
class CanaryCase:
    language: str
    prompt: str
    required_groups: tuple[tuple[str, ...], ...]


CASES: tuple[CanaryCase, ...] = (
    CanaryCase(
        "de",
        "Antworte auf Deutsch: Was ist die Hauptstadt Deutschlands?",
        (("berlin",), ("hauptstadt", "deutschland")),
    ),
    CanaryCase(
        "en",
        "Answer in English: What is the capital of Ireland?",
        (("dublin",), ("capital", "ireland")),
    ),
    CanaryCase(
        "es",
        "Responde en español: ¿Cuál es la capital del Perú?",
        (("lima",), ("capital", "peru")),
    ),
    CanaryCase(
        "fr",
        "Réponds en français : Quelle est la capitale du Maroc ?",
        (("rabat",), ("capitale", "maroc")),
    ),
    CanaryCase(
        "id",
        "Jawab dalam bahasa Indonesia: Apa ibu kota Jepang?",
        (("tokyo", "tokio"), ("ibu kota", "jepang")),
    ),
    CanaryCase(
        "pt",
        "Responda em português: Qual é a capital do Brasil?",
        (("brasilia",), ("capital", "brasil")),
    ),
    CanaryCase(
        "ru",
        "Ответь по-русски: какова столица Казахстана?",
        (("астана",), ("столица", "казахстан")),
    ),
    CanaryCase(
        "tr",
        "Türkçe yanıtla: Türkiye'nin başkenti neresidir?",
        (("ankara",), ("baskent", "turkiye")),
    ),
)


async def run_multilingual_canary(
    provider: InferenceProvider,
    *,
    cases: Sequence[CanaryCase] = CASES,
) -> MultilingualCanaryResult:
    """Run bounded requests without retaining response text in the result."""

    passed: list[str] = []
    completed: list[str] = []
    errors: list[tuple[str, str]] = []
    latencies: list[float] = []
    for case in cases:
        request = ChatRequest(
            messages=(
                ChatMessage(
                    "system",
                    "Give one short sentence in the requested language. Do not add explanations.",
                ),
                ChatMessage("user", case.prompt),
            ),
            max_output_tokens=48,
            temperature=0,
        )
        try:
            result = await provider.chat(request)
        except ProviderError as exc:
            errors.append((case.language, exc.code.value))
            continue
        except Exception:
            errors.append((case.language, "unexpected"))
            continue
        completed.append(case.language)
        latencies.append(max(result.latency_ms, 0))
        normalized = _normalize(result.text)
        # The first group holds the factual answer and is always required. The
        # remaining groups are language-echo markers: a terse reply like
        # "Berlin." has no room for them, so they only apply to full sentences.
        answer_group, *echo_groups = case.required_groups
        answer_ok = any(_normalize(term) in normalized for term in answer_group)
        terse_reply = len(normalized.split()) <= 5
        echo_ok = terse_reply or all(
            any(_normalize(term) in normalized for term in alternatives)
            for alternatives in echo_groups
        )
        if answer_ok and echo_ok:
            passed.append(case.language)
    return MultilingualCanaryResult(
        model_id=provider.model.name,
        languages=tuple(case.language for case in cases),
        passed_languages=tuple(passed),
        median_latency_ms=statistics.median(latencies) if latencies else 0,
        completed_languages=tuple(completed),
        error_codes=tuple(errors),
    )


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


assert tuple(case.language for case in CASES) == BRAINY_LANGUAGES
