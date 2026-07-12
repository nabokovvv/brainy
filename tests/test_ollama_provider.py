"""Offline contract tests for the Ollama OpenAI-compatible adapter."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import AsyncIterator, Awaitable, Callable

import httpx

from brainy_core import (
    ChatMessage,
    ChatRequest,
    ProviderErrorCode,
    ProviderModelUnavailableError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from brainy_core.providers import OllamaProvider

MockHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class OllamaProviderTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(
        self,
        handler: MockHandler,
        *,
        model: str = "gemma-test:latest",
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 1024 * 1024,
    ) -> tuple[OllamaProvider, httpx.AsyncClient]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434/v1",
            model=model,
            client=client,
            timeout_seconds=timeout_seconds,
            context_window=65_536,
            max_response_bytes=max_response_bytes,
        )
        return provider, client

    async def test_chat_normalizes_success_and_reuses_injected_client(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "gemma-test:latest",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "  Fast answer.  "},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                },
            )

        provider, client = self.make_provider(handler)
        request = ChatRequest(
            messages=(
                ChatMessage(role="system", content="Answer briefly."),
                ChatMessage(role="user", content="Question"),
            ),
            max_output_tokens=123,
            temperature=0.1,
            request_id="test-request",
        )

        first = await provider.chat(request)
        second = await provider.chat(request)

        self.assertEqual(first.text, "Fast answer.")
        self.assertEqual(first.model.name, "gemma-test:latest")
        self.assertTrue(first.model.is_local)
        self.assertEqual(first.model.context_window, 65_536)
        self.assertEqual(first.input_tokens, 11)
        self.assertEqual(first.output_tokens, 3)
        self.assertEqual(first.finish_reason, "stop")
        self.assertGreaterEqual(first.latency_ms, 0)
        self.assertEqual(second.text, first.text)
        self.assertEqual(len(requests), 2)

        sent = json.loads(requests[0].content)
        self.assertEqual(
            requests[0].url,
            httpx.URL("http://127.0.0.1:11434/v1/chat/completions"),
        )
        self.assertEqual(sent["model"], "gemma-test:latest")
        self.assertEqual(sent["max_tokens"], 123)
        self.assertEqual(sent["messages"][1], {"role": "user", "content": "Question"})
        self.assertEqual(sent["reasoning_effort"], "none")
        self.assertFalse(sent["stream"])
        self.assertNotIn("api_key", sent)

        await provider.aclose()
        self.assertFalse(client.is_closed, "The provider must not close a borrowed shared client.")

    async def test_chat_sends_multimodal_content_when_message_has_images(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "gemma-test:latest",
                    "choices": [{"message": {"content": "Red"}, "finish_reason": "stop"}],
                },
            )

        provider, _ = self.make_provider(handler)
        request = ChatRequest(
            messages=(
                ChatMessage(role="user", content="What color is this?", images=("YmFzZTY0",)),
            )
        )

        result = await provider.chat(request)

        self.assertEqual(result.text, "Red")
        sent = json.loads(requests[0].content)
        self.assertEqual(
            sent["messages"][0],
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,YmFzZTY0"},
                    },
                ],
            },
        )

    async def test_chat_omits_text_part_when_image_message_has_no_caption(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Red"}}]},
            )

        provider, _ = self.make_provider(handler)
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="", images=("YmFzZTY0",)),)
        )

        await provider.chat(request)

        sent = json.loads(requests[0].content)
        self.assertEqual(
            sent["messages"][0]["content"],
            [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YmFzZTY0"}}],
        )

    async def test_stream_chat_yields_deltas_and_normalized_final_result(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"model":"gemma-test:latest","choices":[{"delta":{"role":"assistant"}}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"Fast "}}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"answer."},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":11,"completion_tokens":3}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                request=request,
            )

        provider, _ = self.make_provider(handler)

        events = [event async for event in provider.stream_chat(_request_with_private_text())]

        self.assertEqual([event.delta for event in events[:-1]], ["Fast ", "answer."])
        self.assertIsNotNone(events[-1].result)
        result = events[-1].result
        assert result is not None
        self.assertEqual(result.text, "Fast answer.")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.input_tokens, 11)
        self.assertEqual(result.output_tokens, 3)
        sent = json.loads(requests[0].content)
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertEqual(sent["reasoning_effort"], "none")

    async def test_stream_response_is_capped_before_an_unterminated_line_is_decoded(self) -> None:
        class OversizedStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b'data: {"choices":[{"delta":{"content":"'
                yield b"x" * 64

            async def aclose(self) -> None:
                return None

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=OversizedStream(), request=request)

        provider, _ = self.make_provider(handler, max_response_bytes=48)

        with self.assertRaises(ProviderResponseError):
            _ = [event async for event in provider.stream_chat(_request_with_private_text())]

    async def test_timeout_has_safe_typed_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("transport details", request=request)

        provider, _ = self.make_provider(handler)

        with self.assertRaises(ProviderTimeoutError) as raised:
            await provider.chat(_request_with_private_text())

        self.assertEqual(raised.exception.code, ProviderErrorCode.TIMEOUT)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("private prompt", str(raised.exception))
        self.assertNotIn("transport details", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    async def test_whole_request_has_a_wall_clock_deadline(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200, json={}, request=request)

        provider, _ = self.make_provider(handler, timeout_seconds=0.01)

        with self.assertRaises(ProviderTimeoutError) as raised:
            await provider.chat(_request_with_private_text())

        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    async def test_connect_failure_maps_to_unavailable(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("socket details", request=request)

        provider, _ = self.make_provider(handler)

        with self.assertRaises(ProviderUnavailableError) as raised:
            await provider.chat(_request_with_private_text())

        self.assertEqual(raised.exception.code, ProviderErrorCode.UNAVAILABLE)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("socket details", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    async def test_default_concurrency_allows_only_one_in_flight_chat(self) -> None:
        active = 0
        max_active = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            first_started.set()
            await release.wait()
            active -= 1
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "answer"}}]},
                request=request,
            )

        provider, _ = self.make_provider(handler)
        first = asyncio.create_task(provider.chat(_request_with_private_text()))
        await first_started.wait()
        others = [
            asyncio.create_task(provider.chat(_request_with_private_text())) for _ in range(2)
        ]
        await asyncio.sleep(0.01)

        self.assertEqual(active, 1)
        self.assertEqual(max_active, 1)
        release.set()
        await asyncio.gather(first, *others)
        self.assertEqual(max_active, 1)

    async def test_malformed_json_maps_to_invalid_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{not-json", request=request)

        provider, _ = self.make_provider(handler)

        with self.assertRaises(ProviderResponseError) as raised:
            await provider.chat(_request_with_private_text())

        self.assertEqual(raised.exception.code, ProviderErrorCode.INVALID_RESPONSE)
        self.assertNotIn("private prompt", str(raised.exception))
        self.assertNotIn("not-json", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    async def test_response_body_size_is_bounded(self) -> None:
        class OversizedStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b"x" * 16
                yield b"y" * 17

            async def aclose(self) -> None:
                return None

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=OversizedStream(), request=request)

        provider, _ = self.make_provider(handler, max_response_bytes=32)

        with self.assertRaises(ProviderResponseError):
            await provider.chat(_request_with_private_text())

    async def test_unavailable_status_does_not_expose_response_body(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                text="daemon failed while processing private prompt",
                request=request,
            )

        provider, _ = self.make_provider(handler)

        with self.assertRaises(ProviderUnavailableError) as raised:
            await provider.chat(_request_with_private_text())

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("private prompt", str(raised.exception))

    async def test_404_is_non_retryable_model_unavailable(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, request=request)

        provider, _ = self.make_provider(handler)

        with self.assertRaises(ProviderModelUnavailableError) as raised:
            await provider.chat(_request_with_private_text())

        self.assertEqual(raised.exception.code, ProviderErrorCode.MODEL_UNAVAILABLE)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_remote_endpoint_requires_explicit_https_opt_in(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow_remote"):
            OllamaProvider(base_url="https://ollama.example/v1", model="gemma")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            OllamaProvider(
                base_url="http://ollama.example/v1",
                model="gemma",
                allow_remote=True,
            )

        provider = OllamaProvider(
            base_url="https://ollama.example/v1",
            model="gemma",
            allow_remote=True,
        )

        self.assertFalse(provider.model.is_local)
        await provider.aclose()

    async def test_health_reports_configured_model_without_raising(self) -> None:
        async def healthy_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "gemma-test:latest"}]},
                request=request,
            )

        provider, _ = self.make_provider(healthy_handler)

        health = await provider.health()

        self.assertTrue(health.available)
        self.assertIsNone(health.error_code)
        self.assertEqual(health.model.name, "gemma-test:latest")

    async def test_health_marks_missing_model_unavailable(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "another-model"}]},
                request=request,
            )

        provider, _ = self.make_provider(handler)

        health = await provider.health()

        self.assertFalse(health.available)
        self.assertEqual(health.error_code, ProviderErrorCode.MODEL_UNAVAILABLE)


def _request_with_private_text() -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role="user", content="private prompt"),))


if __name__ == "__main__":
    unittest.main()
