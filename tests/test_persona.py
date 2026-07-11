"""Tests for persona selection (tone-only system prompt prefix)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from brainy_core.persona import (
    ALL_PERSONAS,
    DEFAULT_PERSONA,
    PERSONA_ASSISTANT,
    PERSONA_BRO,
    PERSONA_KAWAII,
    PERSONA_SARCASTIC,
    get_persona_prefix,
    is_valid_persona,
    with_persona,
)
from brainy_core.use_cases import build_fast_chat_request
from brainy_core.evidence import GroundedSynthesizer
from brainy_core.inference import ChatMessage

ROOT = Path(__file__).resolve().parents[1]
TRANSLATIONS = json.loads((ROOT / "translations.json").read_text(encoding="utf-8"))

PERSONA_KEYS = (
    [
        "persona_prompt",
        "persona_invalid",
        "persona_set",
    ]
    + [f"persona_{name}" for name in ALL_PERSONAS]
    + [f"persona_{name}_desc" for name in ALL_PERSONAS]
)


class PersonaModuleTests(unittest.TestCase):
    def test_all_four_personas_are_defined(self) -> None:
        self.assertEqual(
            set(ALL_PERSONAS),
            {PERSONA_ASSISTANT, PERSONA_KAWAII, PERSONA_BRO, PERSONA_SARCASTIC},
        )

    def test_default_persona_has_no_prefix(self) -> None:
        self.assertEqual(get_persona_prefix(PERSONA_ASSISTANT), "")
        self.assertEqual(get_persona_prefix(DEFAULT_PERSONA), "")

    def test_non_default_personas_have_a_prefix(self) -> None:
        for name in (PERSONA_KAWAII, PERSONA_BRO, PERSONA_SARCASTIC):
            self.assertTrue(get_persona_prefix(name))

    def test_unknown_persona_falls_back_to_empty_prefix(self) -> None:
        self.assertEqual(get_persona_prefix("does_not_exist"), "")
        self.assertFalse(is_valid_persona("does_not_exist"))

    def test_with_persona_appends_prefix(self) -> None:
        base = "You are Brainy."
        result = with_persona(base, PERSONA_KAWAII)
        self.assertTrue(result.startswith(base))
        self.assertIn(get_persona_prefix(PERSONA_KAWAII), result)

    def test_with_persona_default_is_unchanged(self) -> None:
        base = "You are Brainy."
        self.assertEqual(with_persona(base, PERSONA_ASSISTANT), base)


class PersonaToneTests(unittest.TestCase):
    def test_fast_request_includes_persona_prefix(self) -> None:
        request = build_fast_chat_request("hello", "en", persona=PERSONA_BRO)
        system = request.messages[0]
        self.assertIsInstance(system, ChatMessage)
        self.assertIn(get_persona_prefix(PERSONA_BRO), system.content)
        self.assertNotIn(get_persona_prefix(PERSONA_KAWAII), system.content)

    def test_fast_request_default_has_no_persona_prefix(self) -> None:
        request = build_fast_chat_request("hello", "en")
        self.assertEqual(
            request.messages[0].content,
            build_fast_chat_request("hello", "en", persona=DEFAULT_PERSONA).messages[0].content,
        )

    def test_web_synthesis_request_includes_persona_prefix(self) -> None:
        # Build a synthetic bundle without any network/provider dependency.
        from brainy_core.evidence import Evidence, EvidenceBundle

        bundle = EvidenceBundle(
            items=(
                Evidence(
                    evidence_id="e1",
                    text="fact",
                    canonical_url="https://example.com",
                    provenance="page_chunk",
                    rank=1,
                ),
            ),
            token_budget=1200,
        )
        synth = GroundedSynthesizer(provider=_StubProvider())
        request = synth.build_request("q", "en", bundle, persona=PERSONA_SARCASTIC)
        self.assertIn(get_persona_prefix(PERSONA_SARCASTIC), request.messages[0].content)


class _StubProvider:
    """Minimal InferenceProvider stand-in; only build_request is exercised."""

    @property
    def model(self):
        return None


class LocaleParityTests(unittest.TestCase):
    def test_persona_keys_exist_in_every_locale(self) -> None:
        for lang, strings in TRANSLATIONS.items():
            missing = [key for key in PERSONA_KEYS if key not in strings]
            self.assertFalse(
                missing,
                msg=f"Locale '{lang}' missing persona keys: {missing}",
            )

    def test_persona_set_uses_persona_name_placeholder(self) -> None:
        for lang, strings in TRANSLATIONS.items():
            self.assertIn(
                "{persona_name}",
                strings["persona_set"],
                msg=f"Locale '{lang}' persona_set must accept {{persona_name}}",
            )


if __name__ == "__main__":
    unittest.main()
