from __future__ import annotations

import unittest

from brainy_core.inference import ChatResult, ProviderHealth, ProviderModel
from brainy_core.multilingual_canary import run_multilingual_canary


class FakeProvider:
    def __init__(self, answers: list[str]) -> None:
        self.model = ProviderModel("fake", "candidate", False, 32_768)
        self.answers = answers

    async def chat(self, request):
        return ChatResult(self.answers.pop(0), self.model, 10)

    async def health(self):
        return ProviderHealth(self.model, True, 1)

    async def aclose(self):
        return None


class MultilingualCanaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_passes_only_when_all_eight_localized_facts_are_correct(self) -> None:
        provider = FakeProvider(
            [
                "Die Hauptstadt Deutschlands ist Berlin.",
                "The capital of Ireland is Dublin.",
                "La capital del Perú es Lima.",
                "La capitale du Maroc est Rabat.",
                "Ibu kota Jepang adalah Tokyo.",
                "A capital do Brasil é Brasília.",
                "Столица Казахстана — Астана.",
                "Türkiye'nin başkenti Ankara'dır.",
            ]
        )

        result = await run_multilingual_canary(provider)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.passed_languages), 8)

    async def test_wrong_language_or_fact_quarantines_candidate(self) -> None:
        provider = FakeProvider(["wrong"] * 8)

        result = await run_multilingual_canary(provider)

        self.assertFalse(result.passed)
        self.assertEqual(result.passed_languages, ())


if __name__ == "__main__":
    unittest.main()
