"""Primary/fallback inference routing without provider-specific bot logic."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from typing import cast

from brainy_core.inference import (
    ChatRequest,
    ChatResult,
    ChatStreamEvent,
    InferenceProvider,
    ProviderError,
    ProviderHealth,
    ProviderModel,
)

logger = logging.getLogger(__name__)


class FallbackInferenceProvider:
    """Use the primary provider and fall back locally on provider failures."""

    def __init__(self, primary: InferenceProvider, fallback: InferenceProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def model(self) -> ProviderModel:
        return self._primary.model

    async def chat(self, request: ChatRequest) -> ChatResult:
        try:
            return await self._primary.chat(request)
        except ProviderError as exc:
            logger.warning(
                "Primary inference failed provider=%s code=%s; using fallback=%s",
                exc.provider,
                exc.code.value,
                self._fallback.model.provider,
            )
            return await self._fallback.chat(request)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        primary_stream = getattr(self._primary, "stream_chat", None)
        try:
            if not callable(primary_stream):
                yield ChatStreamEvent(result=await self._primary.chat(request))
                return
            stream = cast(AsyncIterator[ChatStreamEvent], primary_stream(request))
            async for event in stream:
                yield event
        except ProviderError as exc:
            logger.warning(
                "Primary inference stream failed provider=%s code=%s; using fallback=%s",
                exc.provider,
                exc.code.value,
                self._fallback.model.provider,
            )
            fallback_stream = getattr(self._fallback, "stream_chat", None)
            if callable(fallback_stream):
                stream = cast(AsyncIterator[ChatStreamEvent], fallback_stream(request))
                async for event in stream:
                    yield event
            else:
                yield ChatStreamEvent(result=await self._fallback.chat(request))

    async def health(self) -> ProviderHealth:
        primary = await self._primary.health()
        if primary.available:
            return primary
        return await self._fallback.health()

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()
