from __future__ import annotations

import unittest
from io import BytesIO

from tools.local_quality_eval import (
    EXPECTED_CASE_COUNT,
    REQUIRED_LANGUAGES,
    load_and_validate,
    run_cases,
)


class FakeResponse(BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LocalQualityEvalTests(unittest.TestCase):
    def test_fixture_has_fifteen_cases_in_every_supported_language(self) -> None:
        languages = load_and_validate()

        self.assertEqual(sum(languages.values()), EXPECTED_CASE_COUNT)
        self.assertEqual(set(languages), REQUIRED_LANGUAGES)

    def test_runner_uses_loopback_and_returns_answer_for_manual_scoring(self) -> None:
        def fake_request(*_: object, **__: object) -> FakeResponse:
            return FakeResponse(b'{"message":{"content":"synthetic answer"}}')

        results = run_cases(
            base_url="http://127.0.0.1:11434",
            model="gemma4:e2b",
            show_responses=False,
            request_fn=fake_request,
        )

        self.assertEqual(len(results), EXPECTED_CASE_COUNT)
        self.assertTrue(all(result["nonempty"] for result in results))
        self.assertTrue(all(result["response"] == "synthetic answer" for result in results))
