"""Core contracts and provider adapters for Brainy."""

from brainy_core.inference import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ChatStreamEvent,
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
    StreamingInferenceProvider,
)
from brainy_core.routing import RouteIntent
from brainy_core.search import SearchProvider, SearchQuery, SearchResult
from brainy_core.evidence import (
    Evidence,
    EvidenceBundle,
    GroundedAnswer,
    GroundedSynthesizer,
    SearchGateway,
)
from brainy_core.use_cases import build_fast_chat_request

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "ChatStreamEvent",
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
    "StreamingInferenceProvider",
    "RouteIntent",
    "SearchProvider",
    "SearchQuery",
    "SearchResult",
    "Evidence",
    "EvidenceBundle",
    "GroundedAnswer",
    "GroundedSynthesizer",
    "SearchGateway",
    "build_fast_chat_request",
]
