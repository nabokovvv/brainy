"""Core contracts and provider adapters for Brainy."""

from brainy_core.inference import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    InferenceProvider,
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
from brainy_core.routing import RouteIntent
from brainy_core.use_cases import build_fast_chat_request

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "InferenceProvider",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderHealth",
    "ProviderModel",
    "ProviderModelUnavailableError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "RouteIntent",
    "build_fast_chat_request",
]
