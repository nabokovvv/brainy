"""Primary/fallback inference routing without provider-specific bot logic."""

from __future__ import annotations

import logging

from brainy_core.inference import (
    ChatRequest,
    ChatResult,
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

    async def health(self) -> ProviderHealth:
        primary = await self._primary.health()
        if primary.available:
            return primary
        return await self._fallback.health()

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()
