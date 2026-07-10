#!/usr/bin/env python3
"""Validate the non-personal multilingual local-chat evaluation fixture only."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


REQUIRED_LANGUAGES = frozenset({"en", "es", "ru", "pt", "fr", "de", "tr", "id"})
EXPECTED_CASE_COUNT = 15
DEFAULT_CASES_PATH = Path(__file__).parents[1] / "tests/data/local_quality_eval.json"


def load_and_validate(path: Path = DEFAULT_CASES_PATH) -> Counter[str]:
    raw_cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list) or len(raw_cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"Expected exactly {EXPECTED_CASE_COUNT} evaluation cases")
    identifiers: list[str] = []
    languages: Counter[str] = Counter()
    for case in raw_cases:
        if not isinstance(case, dict):
            raise ValueError("Every evaluation case must be an object")
        fields = (case.get("id"), case.get("language"), case.get("prompt"), case.get("rubric"))
        if not all(isinstance(field, str) and field.strip() for field in fields):
            raise ValueError("Each evaluation case needs non-empty text fields")
        case_id, language = case["id"], case["language"]
        if not case_id.startswith(f"{language}-"):
            raise ValueError("Case id must start with its language code")
        identifiers.append(case_id)
        languages[language] += 1
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Evaluation case ids must be unique")
    if set(languages) != REQUIRED_LANGUAGES:
        raise ValueError("Evaluation cases must cover every supported language")
    return languages


def main() -> None:
    print(
        json.dumps(
            {"case_count": EXPECTED_CASE_COUNT, "languages": load_and_validate()}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
