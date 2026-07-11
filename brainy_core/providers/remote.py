"""Bounded OpenAI-compatible adapters for zero-cost remote inference."""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from brainy_core.inference import (
    ChatRequest,
    ChatResult,
    ChatStreamEvent,
    ProviderErrorCode,
    ProviderHealth,
    ProviderModel,
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_RETRYABLE_STATUSES = {429, 502, 503, 504}


class DailyRequestBudget:
    """Content-free UTC-day counter persisted atomically with mode 0600."""

    def __init__(self, path: str | os.PathLike[str], *, provider: str, limit: int) -> None:
        if not provider.strip() or isinstance(limit, bool) or limit < 1:
            raise ValueError("provider and a positive daily limit are required")
        self._path = Path(path).expanduser()
        self._provider = provider
        self._limit = limit
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._read_current()

    def reserve(self) -> bool:
        with self._lock:
            used = self._read_current()
            if used >= self._limit:
                return False
            self._write_current(used + 1)
            return True

    def _read_current(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, TypeError, AttributeError):
            return self._limit
        try:
            entry = data.get(self._provider, {})
            if entry.get("date") != today:
                return 0
            used = entry.get("used", 0)
            if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
                return used
            return self._limit
        except (TypeError, AttributeError):
            return self._limit

    def _write_current(self, used: int) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError, TypeError):
            data = {}
        data[self._provider] = {"date": today, "used": used, "limit": self._limit}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class MinuteRateBudget:
    """Process-local rolling RPM limiter shared by configured provider routes."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(requests_per_minute, bool) or not 1 <= requests_per_minute <= 120:
            raise ValueError("requests_per_minute must be between 1 and 120")
        self._limit = requests_per_minute
        self._sleep = sleep
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._limit:
                    self._timestamps.append(now)
                    return
                await self._sleep(max(60 - (now - self._timestamps[0]), 0))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    max_delay_seconds: float = 2.0


class OpenAICompatibleRemoteProvider:
    """A pinned single-model route with a shared client and one logical-request budget."""

    def __init__(
        self,
        *,
        provider_name: str,
        base_url: str,
        api_key: str,
        model: str,
        budget: DailyRequestBudget,
        rate_budget: MinuteRateBudget | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        context_window: int | None = None,
        max_concurrency: int = 1,
        retry_policy: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("remote inference base_url must be a clean HTTPS URL")
        if not provider_name.strip() or not api_key.strip() or not model.strip():
            raise ValueError("provider, API key, and model must be non-empty")
        if not 0 < timeout_seconds <= 120 or max_concurrency not in {1, 2}:
            raise ValueError("remote inference bounds are invalid")
        if retry_policy.max_attempts not in {1, 2, 3}:
            raise ValueError("remote retry attempts must be between 1 and 3")
        self._model = ProviderModel(provider_name, model, False, context_window)
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._budget = budget
        self._rate_budget = rate_budget or MinuteRateBudget(20)
        self._client = client
        self._owns_client = client is None
        self._timeout_seconds = float(timeout_seconds)
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._retry = retry_policy
        self._sleep = sleep
        self._jitter = jitter
        self._closed = False

    @property
    def model(self) -> ProviderModel:
        return self._model

    async def chat(self, request: ChatRequest) -> ChatResult:
        self._reserve()
        payload = self._payload(request, stream=False)
        # Waiting out the shared RPM limiter is expected pacing, not request
        # latency, so it must not consume the timeout window or the latency.
        await self._rate_budget.acquire()
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._semaphore:
                    response = await self._request_with_retry(payload, stream=False)
        except TimeoutError:
            raise ProviderTimeoutError(self._model.provider) from None
        data = _json_object(response)
        return _chat_result(data, self._model, (time.monotonic() - started) * 1000)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        self._reserve()
        text_parts: list[str] = []
        finish_reason: str | None = None
        response_model = self._model.name
        await self._rate_budget.acquire()
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._semaphore:
                    response = await self._request_with_retry(
                        self._payload(request, stream=True), stream=True
                    )
                    try:
                        size = 0
                        async for line in response.aiter_lines():
                            size += len(line.encode("utf-8")) + 1
                            if size > 2 * 1024 * 1024:
                                raise ProviderResponseError(self._model.provider)
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data:"):
                                raise ProviderResponseError(self._model.provider)
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                chunk = json.loads(raw)
                            except ValueError:
                                raise ProviderResponseError(self._model.provider) from None
                            delta, chunk_finish, chunk_model = _stream_fields(chunk)
                            response_model = chunk_model or response_model
                            finish_reason = chunk_finish or finish_reason
                            if delta:
                                text_parts.append(delta)
                                yield ChatStreamEvent(delta=delta)
                    finally:
                        await response.aclose()
        except TimeoutError:
            raise ProviderTimeoutError(self._model.provider) from None
        text = "".join(text_parts).strip()
        if not text:
            raise ProviderResponseError(self._model.provider)
        yield ChatStreamEvent(
            result=ChatResult(
                text=text,
                model=ProviderModel(
                    self._model.provider,
                    response_model,
                    False,
                    self._model.context_window,
                ),
                latency_ms=(time.monotonic() - started) * 1000,
                finish_reason=finish_reason,
            )
        )

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        try:
            response = await self._client_or_create().get(
                f"{self._base_url}/models",
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
            _raise_status(self._model.provider, response)
            payload = _json_object(response)
            entries = payload.get("data")
            if not isinstance(entries, list):
                raise ProviderResponseError(self._model.provider)
            names = {entry.get("id") for entry in entries if isinstance(entry, Mapping)}
            available = self._model.name in names
            return ProviderHealth(
                self._model,
                available,
                (time.monotonic() - started) * 1000,
                None if available else ProviderErrorCode.MODEL_UNAVAILABLE,
            )
        except Exception:
            return ProviderHealth(
                self._model,
                False,
                (time.monotonic() - started) * 1000,
                ProviderErrorCode.UNAVAILABLE,
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _reserve(self) -> None:
        if not self._budget.reserve():
            raise ProviderUnavailableError(self._model.provider, 429)

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model.name,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _request_with_retry(
        self, payload: Mapping[str, Any], *, stream: bool
    ) -> httpx.Response:
        for attempt in range(self._retry.max_attempts):
            try:
                client = self._client_or_create()
                request = client.build_request(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=payload,
                    timeout=self._timeout,
                )
                response = await client.send(
                    request,
                    stream=stream,
                    follow_redirects=False,
                )
            except httpx.ConnectTimeout:
                if attempt + 1 >= self._retry.max_attempts:
                    raise ProviderTimeoutError(self._model.provider) from None
                await self._sleep(self._retry_delay(None, attempt))
                continue
            except httpx.TimeoutException:
                raise ProviderTimeoutError(self._model.provider) from None
            except httpx.RequestError:
                raise ProviderUnavailableError(self._model.provider) from None
            if response.status_code not in _RETRYABLE_STATUSES:
                try:
                    _raise_status(self._model.provider, response)
                except Exception:
                    await response.aclose()
                    raise
                return response
            if attempt + 1 >= self._retry.max_attempts:
                await response.aclose()
                raise ProviderUnavailableError(self._model.provider, response.status_code)
            await response.aclose()
            await self._sleep(self._retry_delay(response, attempt))
        raise ProviderUnavailableError(self._model.provider)

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            value = response.headers.get("Retry-After")
            try:
                if value is not None:
                    return min(max(float(value), 0.0), self._retry.max_delay_seconds)
            except ValueError:
                pass
        cap = min(0.25 * (2**attempt), self._retry.max_delay_seconds)
        return self._jitter(0.0, cap)

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("provider is closed")
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client


def _raise_status(provider: str, response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in {400, 401, 403, 404, 409, 422}:
        raise ProviderRequestError(provider, status)
    if status == 429 or status >= 500:
        raise ProviderUnavailableError(provider, status)
    raise ProviderResponseError(provider, status)


def _json_object(response: httpx.Response) -> Mapping[str, Any]:
    if len(response.content) > 2 * 1024 * 1024:
        raise ProviderResponseError("remote")
    try:
        data = response.json()
    except ValueError:
        raise ProviderResponseError("remote") from None
    if not isinstance(data, Mapping):
        raise ProviderResponseError("remote")
    return data


def _chat_result(data: Mapping[str, Any], model: ProviderModel, latency_ms: float) -> ChatResult:
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderResponseError(model.provider) from None
    if not text:
        raise ProviderResponseError(model.provider)
    usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
    response_model = data.get("model", model.name)
    if not isinstance(response_model, str) or not response_model:
        raise ProviderResponseError(model.provider)
    return ChatResult(
        text=text,
        model=ProviderModel(model.provider, response_model, False, model.context_window),
        latency_ms=latency_ms,
        finish_reason=choice.get("finish_reason") if isinstance(choice, Mapping) else None,
        input_tokens=usage.get("prompt_tokens") if isinstance(usage, Mapping) else None,
        output_tokens=usage.get("completion_tokens") if isinstance(usage, Mapping) else None,
    )


def _stream_fields(data: object) -> tuple[str, str | None, str | None]:
    if not isinstance(data, Mapping):
        raise ProviderResponseError("remote")
    try:
        choice = data["choices"][0]
        delta = choice.get("delta", {}).get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderResponseError("remote") from None
    if not isinstance(delta, str):
        raise ProviderResponseError("remote")
    finish = choice.get("finish_reason") if isinstance(choice, Mapping) else None
    response_model = data.get("model")
    return (
        delta,
        finish if isinstance(finish, str) else None,
        response_model if isinstance(response_model, str) else None,
    )
