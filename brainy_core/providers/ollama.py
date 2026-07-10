"""Ollama adapter for its OpenAI-compatible HTTP API."""

from __future__ import annotations

import asyncio
import ipaddress
import json as json_module
import time
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

import httpx

from brainy_core.inference import (
    ChatRequest,
    ChatResult,
    ProviderError,
    ProviderErrorCode,
    ProviderHealth,
    ProviderModel,
    ProviderModelUnavailableError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_PROVIDER_NAME = "ollama"
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_CONCURRENCY = 2
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class OllamaProvider:
    """A single-model Ollama provider with one shared async HTTP client.

    An injected client is borrowed and never closed by the provider. Without one, the
    provider lazily creates a client, reuses it, and owns its lifecycle. Application code
    should create/close this object in its lifespan rather than per request.

    Local generation is serialized by default. ``context_window`` is configured metadata
    only; it does not prove that the model can use 64K within target latency and memory.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 30.0,
        context_window: Optional[int] = None,
        max_concurrency: int = 1,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        allow_remote: bool = False,
    ) -> None:
        self._base_url, is_local = _normalize_base_url(base_url, allow_remote=allow_remote)
        self._model = ProviderModel(
            provider=_PROVIDER_NAME,
            name=model,
            is_local=is_local,
            context_window=context_window,
        )
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0 and 120.")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= _MAX_CONCURRENCY
        ):
            raise ValueError("max_concurrency must be 1 or 2.")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("max_response_bytes must be between 1 and 2097152.")

        self._timeout_seconds = float(timeout_seconds)
        self._timeout = httpx.Timeout(
            self._timeout_seconds,
            connect=min(self._timeout_seconds, 5.0),
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_response_bytes = max_response_bytes
        self._client = client
        self._owns_client = client is None
        self._closed = False

    @property
    def model(self) -> ProviderModel:
        return self._model

    async def __aenter__(self) -> "OllamaProvider":
        self._get_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close an internally owned client; leave an injected shared client open."""

        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def chat(self, request: ChatRequest) -> ChatResult:
        """Run one deadline-bound generation, including queue wait and response parsing."""

        timed_out = False
        result: Optional[ChatResult] = None
        try:
            result = await asyncio.wait_for(
                self._chat_operation(request),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True

        # Raise outside the timeout handler so no underlying task context is retained.
        if timed_out:
            raise ProviderTimeoutError(_PROVIDER_NAME)
        if result is None:  # Defensive: _chat_operation either returns or raises.
            raise ProviderResponseError(_PROVIDER_NAME)
        return result

    async def _chat_operation(self, request: ChatRequest) -> ChatResult:
        payload: Dict[str, Any] = {
            "model": self._model.name,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            # Gemma 4 can emit a long hidden reasoning trace by default. Brainy's
            # local fast path prioritizes the visible answer and has no reasoning UI.
            "reasoning_effort": "none",
            "stream": False,
        }
        started = time.monotonic()
        async with self._semaphore:
            _, content = await self._send("POST", "/chat/completions", json=payload)
            data = _response_json(content)
            return _parse_chat_result(
                data,
                self._model,
                (time.monotonic() - started) * 1000,
            )

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        try:
            model_names = await asyncio.wait_for(
                self._health_operation(),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ProviderHealth(
                model=self._model,
                available=False,
                latency_ms=(time.monotonic() - started) * 1000,
                error_code=ProviderErrorCode.TIMEOUT,
            )
        except ProviderError as exc:
            return ProviderHealth(
                model=self._model,
                available=False,
                latency_ms=(time.monotonic() - started) * 1000,
                error_code=exc.code,
            )

        is_available = self._model.name in model_names
        return ProviderHealth(
            model=self._model,
            available=is_available,
            latency_ms=(time.monotonic() - started) * 1000,
            error_code=None if is_available else ProviderErrorCode.MODEL_UNAVAILABLE,
        )

    async def _health_operation(self) -> Tuple[str, ...]:
        _, content = await self._send("GET", "/models")
        return _parse_model_names(_response_json(content))

    def _get_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise ProviderUnavailableError(_PROVIDER_NAME)
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        if self._client.is_closed:
            raise ProviderUnavailableError(_PROVIDER_NAME)
        return self._client

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[int, bytes]:
        client = self._get_client()
        transport_error: Optional[ProviderErrorCode] = None
        result: Optional[Tuple[int, bytes]] = None
        try:
            async with client.stream(
                method,
                f"{self._base_url}{path}",
                json=json,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                _raise_for_status(response.status_code)
                content = await _read_bounded(response, self._max_response_bytes)
                result = response.status_code, content
        except httpx.TimeoutException:
            transport_error = ProviderErrorCode.TIMEOUT
        except httpx.RequestError:
            transport_error = ProviderErrorCode.UNAVAILABLE

        # Raise outside the httpx handler so errors cannot retain request/response bodies.
        if transport_error is ProviderErrorCode.TIMEOUT:
            raise ProviderTimeoutError(_PROVIDER_NAME)
        if transport_error is ProviderErrorCode.UNAVAILABLE:
            raise ProviderUnavailableError(_PROVIDER_NAME)
        if result is None:
            raise ProviderResponseError(_PROVIDER_NAME)
        return result


def _normalize_base_url(base_url: str, *, allow_remote: bool) -> Tuple[str, bool]:
    if not isinstance(base_url, str):
        raise TypeError("base_url must be text.")
    if not isinstance(allow_remote, bool):
        raise TypeError("allow_remote must be boolean.")
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL.")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("base_url cannot include credentials, query parameters, or fragments.")
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("base_url must include a hostname.")
    try:
        parsed.port
    except ValueError:
        raise ValueError("base_url contains an invalid port.") from None

    is_loopback = _is_loopback_host(hostname)
    if not is_loopback and not allow_remote:
        raise ValueError("Remote Ollama requires explicit allow_remote=True.")
    if not is_loopback and parsed.scheme != "https":
        raise ValueError("Remote Ollama requires HTTPS.")
    return normalized, is_loopback


def _is_loopback_host(hostname: str) -> bool:
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _raise_for_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status in {408, 504}:
        raise ProviderTimeoutError(_PROVIDER_NAME, status)
    if status == 404:
        raise ProviderModelUnavailableError(_PROVIDER_NAME, status)
    if status in {409, 429} or status >= 500:
        raise ProviderUnavailableError(_PROVIDER_NAME, status)
    if 400 <= status < 500:
        raise ProviderRequestError(_PROVIDER_NAME, status)
    raise ProviderResponseError(_PROVIDER_NAME, status)


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            parsed_length = int(declared_length)
        except ValueError:
            raise ProviderResponseError(_PROVIDER_NAME, response.status_code) from None
        if parsed_length < 0 or parsed_length > limit:
            raise ProviderResponseError(_PROVIDER_NAME, response.status_code)

    chunks = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ProviderResponseError(_PROVIDER_NAME, response.status_code)
        chunks.append(chunk)
    return b"".join(chunks)


def _response_json(content: bytes) -> Mapping[str, Any]:
    invalid_json = False
    try:
        data = json_module.loads(content)
    except (TypeError, ValueError, UnicodeError):
        invalid_json = True
        data = None
    if invalid_json or not isinstance(data, Mapping):
        raise ProviderResponseError(_PROVIDER_NAME)
    return data


def _parse_chat_result(
    data: Mapping[str, Any],
    configured_model: ProviderModel,
    latency_ms: float,
) -> ChatResult:
    invalid_response = False
    try:
        choices = data["choices"]
        if not isinstance(choices, list) or not choices:
            raise TypeError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise TypeError
        message = choice["message"]
        if not isinstance(message, Mapping):
            raise TypeError
        text = message["content"]
        if not isinstance(text, str) or not text.strip():
            raise TypeError

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise TypeError

        response_model = data.get("model", configured_model.name)
        if not isinstance(response_model, str) or not response_model.strip():
            raise TypeError

        input_tokens, output_tokens = _parse_usage(data.get("usage"))
    except (KeyError, TypeError, ValueError):
        invalid_response = True

    if invalid_response:
        raise ProviderResponseError(_PROVIDER_NAME)

    model = ProviderModel(
        provider=configured_model.provider,
        name=response_model,
        is_local=configured_model.is_local,
        context_window=configured_model.context_window,
    )
    return ChatResult(
        text=text.strip(),
        model=model,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_usage(usage: object) -> Tuple[Optional[int], Optional[int]]:
    if usage is None:
        return None, None
    if not isinstance(usage, Mapping):
        raise TypeError
    return (
        _optional_non_negative_int(usage.get("prompt_tokens")),
        _optional_non_negative_int(usage.get("completion_tokens")),
    )


def _optional_non_negative_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError
    return value


def _parse_model_names(data: Mapping[str, Any]) -> Tuple[str, ...]:
    models = data.get("data")
    if not isinstance(models, list):
        raise ProviderResponseError(_PROVIDER_NAME)
    names = []
    for model in models:
        if not isinstance(model, Mapping):
            raise ProviderResponseError(_PROVIDER_NAME)
        name = model.get("id")
        if not isinstance(name, str) or not name.strip():
            raise ProviderResponseError(_PROVIDER_NAME)
        names.append(name)
    return tuple(names)
