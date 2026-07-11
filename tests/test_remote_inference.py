from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from brainy_core import ChatMessage, ChatRequest, ProviderRequestError, ProviderUnavailableError
from brainy_core.providers.remote import DailyRequestBudget, OpenAICompatibleRemoteProvider


def _request() -> ChatRequest:
    return ChatRequest(messages=(ChatMessage("user", "private multilingual prompt"),))


class RemoteInferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_retryable_status_without_extra_budget_charge(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
            return httpx.Response(
                200,
                json={
                    "model": "vendor/model",
                    "choices": [{"message": {"content": "Добре."}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                },
                request=request,
            )

        with tempfile.TemporaryDirectory() as tmp:
            budget = DailyRequestBudget(Path(tmp) / "budget.json", provider="nvidia", limit=2)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleRemoteProvider(
                    provider_name="nvidia",
                    base_url="https://integrate.api.nvidia.com/v1",
                    api_key="test-key",
                    model="vendor/model",
                    client=client,
                    budget=budget,
                    sleep=lambda _: _no_sleep(),
                )
                result = await provider.chat(_request())

            self.assertEqual(result.text, "Добре.")
            self.assertEqual(calls, 2)
            self.assertEqual(budget.used, 1)

    async def test_rate_limiter_wait_does_not_consume_request_timeout(self) -> None:
        # Pacing behind the shared RPM limiter is not request latency: a wait
        # longer than timeout_seconds must not fail the request as a timeout.
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "vendor/model",
                    "choices": [{"message": {"content": "Ok."}, "finish_reason": "stop"}],
                },
                request=request,
            )

        class SlowRateBudget:
            async def acquire(self) -> None:
                import asyncio

                await asyncio.sleep(0.3)

        with tempfile.TemporaryDirectory() as tmp:
            budget = DailyRequestBudget(Path(tmp) / "budget.json", provider="nvidia", limit=2)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleRemoteProvider(
                    provider_name="nvidia",
                    base_url="https://integrate.api.nvidia.com/v1",
                    api_key="test-key",
                    model="vendor/model",
                    client=client,
                    budget=budget,
                    rate_budget=SlowRateBudget(),
                    timeout_seconds=0.1,
                )
                result = await provider.chat(_request())

            self.assertEqual(result.text, "Ok.")
            self.assertLess(result.latency_ms, 250)

    async def test_auth_error_is_not_retried(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(401, request=request)

        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleRemoteProvider(
                    provider_name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test-key",
                    model="vendor/model:free",
                    client=client,
                    budget=DailyRequestBudget(
                        Path(tmp) / "budget.json", provider="openrouter", limit=50
                    ),
                )
                with self.assertRaises(ProviderRequestError):
                    await provider.chat(_request())

        self.assertEqual(calls, 1)

    async def test_daily_budget_fails_closed_before_network(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={}, request=request)

        with tempfile.TemporaryDirectory() as tmp:
            budget = DailyRequestBudget(Path(tmp) / "budget.json", provider="openrouter", limit=1)
            self.assertTrue(budget.reserve())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleRemoteProvider(
                    provider_name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test-key",
                    model="vendor/model:free",
                    client=client,
                    budget=budget,
                )
                with self.assertRaises(ProviderUnavailableError):
                    await provider.chat(_request())

        self.assertEqual(calls, 0)

    async def test_corrupt_budget_fails_closed_instead_of_resetting_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            path.write_text("{broken", encoding="utf-8")
            budget = DailyRequestBudget(path, provider="openrouter", limit=50)

            self.assertFalse(budget.reserve())
            self.assertEqual(budget.used, 50)

    async def test_streaming_uses_bearer_auth_and_pinned_model(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                content=(
                    b'data: {"model":"vendor/model:free","choices":[{"delta":{"content":"Hola"}}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"."},"finish_reason":"stop"}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
                request=request,
            )

        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleRemoteProvider(
                    provider_name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="test-key",
                    model="vendor/model:free",
                    client=client,
                    budget=DailyRequestBudget(
                        Path(tmp) / "budget.json", provider="openrouter", limit=50
                    ),
                )
                events = [event async for event in provider.stream_chat(_request())]

        self.assertEqual([event.delta for event in events[:-1]], ["Hola", "."])
        self.assertEqual(events[-1].result.text, "Hola.")
        payload = json.loads(captured[0].content)
        self.assertEqual(payload["model"], "vendor/model:free")
        self.assertEqual(captured[0].headers["authorization"], "Bearer test-key")
        self.assertNotIn("private multilingual prompt", str(events[-1].result.model))


async def _no_sleep() -> None:
    return None


if __name__ == "__main__":
    unittest.main()
