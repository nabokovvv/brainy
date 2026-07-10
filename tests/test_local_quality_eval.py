from __future__ import annotations

import unittest

from tools.local_quality_eval import EXPECTED_CASE_COUNT, REQUIRED_LANGUAGES, load_and_validate


class LocalQualityEvalTests(unittest.TestCase):
    def test_fixture_has_fifteen_cases_in_every_supported_language(self) -> None:
        languages = load_and_validate()

        self.assertEqual(sum(languages.values()), EXPECTED_CASE_COUNT)
        self.assertEqual(set(languages), REQUIRED_LANGUAGES)
