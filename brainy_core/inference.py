"""Provider-independent inference contracts.

The types in this module intentionally contain no provider configuration or prompt
construction. Application use cases build the messages; adapters only transport them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Literal, Optional, Protocol, Tuple, runtime_checkable

MessageRole = Literal["system", "user", "assistant"]

# Bounds one message's image payload so a single Telegram photo message cannot
# balloon into an unbounded multimodal request.
MAX_IMAGES_PER_MESSAGE = 4


class ProviderErrorCode(str, Enum):
    """Stable, content-free error codes safe to map to localized user messages."""

    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    REQUEST_REJECTED = "request_rejected"
    INVALID_RESPONSE = "invalid_response"
    MODEL_UNAVAILABLE = "model_unavailable"


@dataclass(frozen=True)
class ChatMessage:
    """A chat message understood by every inference provider.

    ``images`` holds raw base64-encoded image bytes (no ``data:`` URI prefix -
    providers add whatever wrapper their transport needs). Providers that
    cannot accept images should ignore this field or reject the request;
    ``ChatMessage`` itself only enforces the bound on how many a single
    message may carry.
    """

    role: MessageRole
    content: str
    images: Tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("Unsupported chat message role.")
        if not isinstance(self.content, str):
            raise ValueError("Chat message content must be text.")
        images = tuple(self.images)
        if len(images) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"A message may carry at most {MAX_IMAGES_PER_MESSAGE} images.")
        if not all(isinstance(image, str) and image.strip() for image in images):
            raise ValueError("images must be non-empty base64 strings.")
        if not self.content.strip() and not images:
            raise ValueError("Chat message content must be non-empty text.")
        object.__setattr__(self, "images", images)


@dataclass(frozen=True)
class ChatRequest:
    """A bounded, provider-neutral chat generation request."""

    messages: Tuple[ChatMessage, ...]
    max_output_tokens: int = 400
    temperature: float = 0.3
    request_id: Optional[str] = None

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        if not messages:
            raise ValueError("At least one chat message is required.")
        if not all(isinstance(message, ChatMessage) for message in messages):
            raise TypeError("All messages must be ChatMessage instances.")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 8192
        ):
            raise ValueError("max_output_tokens must be between 1 and 8192.")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0.0 <= self.temperature <= 2.0
        ):
            raise ValueError("temperature must be between 0 and 2.")
        object.__setattr__(self, "messages", messages)


@dataclass(frozen=True)
class ProviderModel:
    """Identity and configured capabilities of one provider model.

    ``context_window`` is operator-provided metadata, not a runtime claim. In particular,
    65,536 means that the model is expected to support 64K context; actual usable context,
    latency, and memory pressure still require a benchmark on the target machine.
    """

    provider: str
    name: str
    is_local: bool
    context_window: Optional[int] = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not self.provider.strip()
            or not isinstance(self.name, str)
            or not self.name.strip()
        ):
            raise ValueError("Provider and model names must be non-empty.")
        if not isinstance(self.is_local, bool):
            raise TypeError("is_local must be boolean.")
        if self.context_window is not None and (
            isinstance(self.context_window, bool)
            or not isinstance(self.context_window, int)
            or self.context_window <= 0
        ):
            raise ValueError("context_window must be positive when provided.")


@dataclass(frozen=True)
class ChatResult:
    """Normalized result returned by an inference provider."""

    text: str
    model: ProviderModel
    latency_ms: float
    finish_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass(frozen=True)
class ChatStreamEvent:
    """One visible delta or the normalized final result of a streamed chat."""

    delta: str = ""
    result: Optional[ChatResult] = None

    def __post_init__(self) -> None:
        if not isinstance(self.delta, str):
            raise TypeError("delta must be text.")
        if bool(self.delta) == (self.result is not None):
            raise ValueError("A stream event must contain either a delta or a final result.")


@dataclass(frozen=True)
class ProviderHealth:
    """Readiness of the provider and its configured model."""

    model: ProviderModel
    available: bool
    latency_ms: float
    error_code: Optional[ProviderErrorCode] = None


class ProviderError(RuntimeError):
    """Base class for safe, bounded inference errors."""

    code = ProviderErrorCode.UNAVAILABLE
    retryable = False
    safe_message = "Inference provider error."

    def __init__(self, provider: str, status_code: Optional[int] = None) -> None:
        super().__init__(self.safe_message)
        self.provider = provider
        self.status_code = status_code


class ProviderTimeoutError(ProviderError):
    code = ProviderErrorCode.TIMEOUT
    retryable = True
    safe_message = "Inference provider timed out."


class ProviderUnavailableError(ProviderError):
    code = ProviderErrorCode.UNAVAILABLE
    retryable = True
    safe_message = "Inference provider is unavailable."


class ProviderModelUnavailableError(ProviderError):
    code = ProviderErrorCode.MODEL_UNAVAILABLE
    safe_message = "Configured inference model is unavailable."


class ProviderRequestError(ProviderError):
    code = ProviderErrorCode.REQUEST_REJECTED
    safe_message = "Inference request was rejected."


class ProviderResponseError(ProviderError):
    code = ProviderErrorCode.INVALID_RESPONSE
    safe_message = "Inference provider returned an invalid response."


@runtime_checkable
class InferenceProvider(Protocol):
    """Async interface implemented by all inference providers."""

    @property
    def model(self) -> ProviderModel: ...

    async def chat(self, request: ChatRequest) -> ChatResult: ...

    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class StreamingInferenceProvider(InferenceProvider, Protocol):
    """Optional provider capability for progressive delivery."""

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]: ...
