from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from brainy_core.model_catalog import (
    CatalogPolicy,
    CatalogUnavailableError,
    ModelLifecycle,
    MultilingualCanaryResult,
    OpenRouterCatalog,
    OpenRouterModel,
    activate_curated_models,
    evaluate_openrouter_model,
)


def _model(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "vendor/multilingual:free",
        "name": "Multilingual (free)",
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "modality": "text->text",
        },
        "context_length": 32768,
        "top_provider": {"max_completion_tokens": 4096},
        "pricing": {
            "prompt": "0",
            "completion": "0.000000",
            "request": "0",
            "image": "0",
        },
        "supported_parameters": ["temperature", "max_tokens"],
        "expiration_date": None,
    }
    data.update(overrides)
    return data


class OpenRouterEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CatalogPolicy(min_context_tokens=16_384, min_output_tokens=512)

    def test_accepts_only_zero_priced_free_text_variant(self) -> None:
        candidate = OpenRouterModel.from_api(_model())

        decision = evaluate_openrouter_model(candidate, self.policy)

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.lifecycle, ModelLifecycle.ELIGIBLE)

    def test_rejects_paid_dimension_even_when_suffix_is_free(self) -> None:
        candidate = OpenRouterModel.from_api(
            _model(pricing={"prompt": "0", "completion": "0", "request": "0.01"})
        )

        decision = evaluate_openrouter_model(candidate, self.policy)

        self.assertFalse(decision.eligible)
        self.assertIn("non_zero_price", decision.reasons)

    def test_rejects_free_named_model_without_exact_variant(self) -> None:
        candidate = OpenRouterModel.from_api(_model(id="vendor/free-looking-model"))
        decision = evaluate_openrouter_model(candidate, self.policy)
        self.assertIn("not_free_variant", decision.reasons)

    def test_rejects_schema_drift_conservatively(self) -> None:
        with self.assertRaises(ValueError):
            OpenRouterModel.from_api(_model(pricing={"prompt": "free"}))

    def test_rejects_expired_or_non_text_model(self) -> None:
        expired = OpenRouterModel.from_api(_model(expiration_date="2025-01-01"))
        image = OpenRouterModel.from_api(
            _model(
                architecture={
                    "input_modalities": ["image"],
                    "output_modalities": ["image"],
                    "modality": "image->image",
                }
            )
        )

        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        self.assertIn("expired", evaluate_openrouter_model(expired, self.policy, now=now).reasons)
        self.assertIn(
            "text_capability",
            evaluate_openrouter_model(image, self.policy, now=now).reasons,
        )

    def test_activation_requires_curated_allowlist_and_all_language_canary(self) -> None:
        models = (OpenRouterModel.from_api(_model()),)
        passed = MultilingualCanaryResult(
            model_id=models[0].model_id,
            languages=("de", "en", "es", "fr", "id", "pt", "ru", "tr"),
            passed_languages=("de", "en", "es", "fr", "id", "pt", "ru", "tr"),
            median_latency_ms=900,
        )

        active = activate_curated_models(
            models,
            decisions={models[0].model_id: evaluate_openrouter_model(models[0], self.policy)},
            canaries={models[0].model_id: passed},
            curated_ids={models[0].model_id},
        )
        self.assertEqual(active, (models[0].model_id,))

        partial = MultilingualCanaryResult(
            model_id=models[0].model_id,
            languages=passed.languages,
            passed_languages=passed.languages[:-1],
            median_latency_ms=900,
        )
        self.assertEqual(
            activate_curated_models(
                models,
                decisions={models[0].model_id: evaluate_openrouter_model(models[0], self.policy)},
                canaries={models[0].model_id: partial},
                curated_ids={models[0].model_id},
            ),
            (),
        )


class OpenRouterCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_drifted_refresh_preserves_last_known_good(self) -> None:
        responses = [
            httpx.Response(200, json={"data": [_model()]}),
            httpx.Response(200, json={"data": []}),
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            response = responses.pop(0)
            response.request = request
            return response

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                catalog = OpenRouterCatalog(client=client, snapshot_path=path, ttl_seconds=0)
                first = await catalog.get_models()
                second = await catalog.get_models()

            self.assertEqual(second, first)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["models"][0]["id"], "vendor/multilingual:free")

    async def test_concurrent_refresh_is_single_flight(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"data": [_model()]}, request=request)

        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                catalog = OpenRouterCatalog(
                    client=client,
                    snapshot_path=Path(tmp) / "catalog.json",
                    ttl_seconds=900,
                )
                results = await __import__("asyncio").gather(
                    catalog.get_models(), catalog.get_models(), catalog.get_models()
                )

        self.assertEqual(calls, 1)
        self.assertEqual(results[0], results[1])

    async def test_one_drifted_entry_does_not_hide_valid_catalog_entries(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [_model(id="broken", pricing={"prompt": "free"}), _model()]},
                request=request,
            )

        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                catalog = OpenRouterCatalog(
                    client=client,
                    snapshot_path=Path(tmp) / "catalog.json",
                    refresh_jitter_seconds=0,
                )
                models = await catalog.get_models()

        self.assertEqual(tuple(model.model_id for model in models), ("vendor/multilingual:free",))

    async def test_error_without_fresh_enough_lkg_fails_closed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                catalog = OpenRouterCatalog(
                    client=client,
                    snapshot_path=Path(tmp) / "missing.json",
                )
                with self.assertRaises(CatalogUnavailableError):
                    await catalog.get_models()


if __name__ == "__main__":
    unittest.main()
