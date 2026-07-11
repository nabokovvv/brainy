"""Free-only OpenRouter discovery and multilingual promotion policy.

Catalog metadata can establish technical eligibility, but never multilingual quality.
Promotion to ``active`` therefore requires a recorded canary across every Brainy locale
and an explicit curated allowlist.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

import httpx

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
BRAINY_LANGUAGES = ("de", "en", "es", "fr", "id", "pt", "ru", "tr")


class CatalogUnavailableError(RuntimeError):
    """The remote catalog and a sufficiently recent LKG are both unavailable."""


class ModelLifecycle(str, Enum):
    DISCOVERED = "discovered"
    ELIGIBLE = "eligible"
    CANARY = "canary"
    ACTIVE = "active"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class CatalogPolicy:
    min_context_tokens: int = 16_384
    min_output_tokens: int = 512
    required_parameters: tuple[str, ...] = ("max_tokens",)


@dataclass(frozen=True)
class OpenRouterModel:
    model_id: str
    name: str
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    context_tokens: int
    max_output_tokens: int
    pricing: tuple[tuple[str, Decimal], ...]
    supported_parameters: tuple[str, ...]
    expiration_date: datetime | None

    @classmethod
    def from_api(cls, data: object) -> "OpenRouterModel":
        if not isinstance(data, Mapping):
            raise ValueError("model entry must be an object")
        model_id = _required_text(data.get("id"), "id")
        name = _required_text(data.get("name", model_id), "name")
        architecture = _required_mapping(data.get("architecture"), "architecture")
        top_provider = _required_mapping(data.get("top_provider"), "top_provider")
        pricing_data = _required_mapping(data.get("pricing"), "pricing")
        if not pricing_data:
            raise ValueError("pricing must not be empty")

        prices: list[tuple[str, Decimal]] = []
        for dimension, value in pricing_data.items():
            if not isinstance(dimension, str) or not dimension:
                raise ValueError("pricing dimension must be text")
            try:
                price = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ValueError("pricing value must be numeric") from None
            if not price.is_finite() or price < 0:
                raise ValueError("pricing value must be finite and non-negative")
            prices.append((dimension, price))

        context_tokens = _positive_int(data.get("context_length"), "context_length")
        max_output_tokens = _positive_int(
            top_provider.get("max_completion_tokens"), "max_completion_tokens"
        )
        supported = _text_tuple(data.get("supported_parameters"), "supported_parameters")
        expiration = _parse_expiration(data.get("expiration_date"))
        return cls(
            model_id=model_id,
            name=name,
            input_modalities=_text_tuple(architecture.get("input_modalities"), "input_modalities"),
            output_modalities=_text_tuple(
                architecture.get("output_modalities"), "output_modalities"
            ),
            context_tokens=context_tokens,
            max_output_tokens=max_output_tokens,
            pricing=tuple(sorted(prices)),
            supported_parameters=supported,
            expiration_date=expiration,
        )


@dataclass(frozen=True)
class EligibilityDecision:
    model_id: str
    lifecycle: ModelLifecycle
    reasons: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return self.lifecycle is ModelLifecycle.ELIGIBLE


@dataclass(frozen=True)
class MultilingualCanaryResult:
    model_id: str
    languages: tuple[str, ...]
    passed_languages: tuple[str, ...]
    median_latency_ms: float
    completed_languages: tuple[str, ...] = ()
    error_codes: tuple[tuple[str, str], ...] = ()

    @property
    def passed(self) -> bool:
        return (
            set(self.languages) == set(BRAINY_LANGUAGES)
            and set(self.passed_languages) == set(self.languages)
            and set(self.completed_languages or self.passed_languages) == set(self.languages)
            and self.median_latency_ms >= 0
        )


def evaluate_openrouter_model(
    model: OpenRouterModel,
    policy: CatalogPolicy,
    *,
    now: datetime | None = None,
) -> EligibilityDecision:
    reasons: list[str] = []
    if not model.model_id.endswith(":free"):
        reasons.append("not_free_variant")
    if any(price != 0 for _, price in model.pricing):
        reasons.append("non_zero_price")
    if "text" not in model.input_modalities or "text" not in model.output_modalities:
        reasons.append("text_capability")
    if model.context_tokens < policy.min_context_tokens:
        reasons.append("context_limit")
    if model.max_output_tokens < policy.min_output_tokens:
        reasons.append("output_limit")
    if not set(policy.required_parameters).issubset(model.supported_parameters):
        reasons.append("required_parameters")
    current = now or datetime.now(timezone.utc)
    if model.expiration_date is not None and model.expiration_date <= current:
        reasons.append("expired")
    return EligibilityDecision(
        model_id=model.model_id,
        lifecycle=ModelLifecycle.DISCOVERED if reasons else ModelLifecycle.ELIGIBLE,
        reasons=tuple(reasons),
    )


def activate_curated_models(
    models: Sequence[OpenRouterModel],
    *,
    decisions: Mapping[str, EligibilityDecision],
    canaries: Mapping[str, MultilingualCanaryResult],
    curated_ids: set[str],
    max_active: int = 3,
) -> tuple[str, ...]:
    """Return a stable active set; discovery order never promotes a model by itself."""

    active: list[str] = []
    for model in models:
        decision = decisions.get(model.model_id)
        canary = canaries.get(model.model_id)
        if (
            model.model_id in curated_ids
            and decision is not None
            and decision.eligible
            and canary is not None
            and canary.passed
        ):
            active.append(model.model_id)
        if len(active) >= max_active:
            break
    return tuple(active)


class OpenRouterCatalog:
    """TTL cache with single-flight refresh and a persistent stale-if-error LKG."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        snapshot_path: str | os.PathLike[str],
        ttl_seconds: float = 900,
        stale_if_error_seconds: float = 86_400,
        now: Callable[[], datetime] | None = None,
        refresh_jitter_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if (
            ttl_seconds < 0
            or stale_if_error_seconds < ttl_seconds
            or not 0 <= refresh_jitter_seconds <= 5
        ):
            raise ValueError("catalog TTL values are invalid")
        self._client = client
        self._path = Path(snapshot_path).expanduser()
        self._ttl_seconds = ttl_seconds
        self._stale_if_error_seconds = stale_if_error_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._refresh_jitter_seconds = refresh_jitter_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._lock = asyncio.Lock()
        self._cached: tuple[OpenRouterModel, ...] | None = None
        self._fetched_at: datetime | None = None

    async def get_models(self) -> tuple[OpenRouterModel, ...]:
        self._load_snapshot_once()
        if self._is_fresh(self._ttl_seconds):
            assert self._cached is not None
            return self._cached

        async with self._lock:
            if self._is_fresh(self._ttl_seconds):
                assert self._cached is not None
                return self._cached
            try:
                models = await self._refresh()
            except (httpx.HTTPError, ValueError, CatalogUnavailableError):
                if self._is_fresh(self._stale_if_error_seconds):
                    assert self._cached is not None
                    return self._cached
                raise CatalogUnavailableError("OpenRouter model catalog is unavailable.") from None
            self._cached = models
            self._fetched_at = self._now()
            self._write_snapshot(models, self._fetched_at)
            return models

    def _load_snapshot_once(self) -> None:
        if self._cached is not None or self._fetched_at is not None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            models = _parse_models(payload["models"])
            if not models:
                raise ValueError("snapshot contains no valid models")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self._cached = models
        self._fetched_at = fetched_at.astimezone(timezone.utc)

    def _is_fresh(self, age_seconds: float) -> bool:
        if self._cached is None or self._fetched_at is None:
            return False
        age = (self._now() - self._fetched_at).total_seconds()
        return 0 <= age <= age_seconds

    async def _refresh(self) -> tuple[OpenRouterModel, ...]:
        if self._refresh_jitter_seconds:
            await self._sleep(self._jitter(0, self._refresh_jitter_seconds))
        response = await self._client.get(
            OPENROUTER_MODELS_URL,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("catalog response is not JSON") from None
        if not isinstance(payload, Mapping):
            raise ValueError("catalog response must be an object")
        models = _parse_models(payload.get("data"))
        if not models:
            raise CatalogUnavailableError("OpenRouter returned an empty catalog.")
        return models

    def _write_snapshot(self, models: Sequence[OpenRouterModel], fetched_at: datetime) -> None:
        payload = {
            "schema_version": 1,
            "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
            "models": [_model_to_api(model) for model in models],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
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


def _parse_models(value: object) -> tuple[OpenRouterModel, ...]:
    if not isinstance(value, list):
        raise ValueError("models must be a list")
    models: list[OpenRouterModel] = []
    for item in value:
        try:
            models.append(OpenRouterModel.from_api(item))
        except ValueError:
            # One malformed/unsupported entry must not erase hundreds of valid models.
            # If every entry drifts, the empty result is rejected and the LKG survives.
            continue
    return tuple(models)


def _model_to_api(model: OpenRouterModel) -> dict[str, object]:
    return {
        "id": model.model_id,
        "name": model.name,
        "architecture": {
            "input_modalities": list(model.input_modalities),
            "output_modalities": list(model.output_modalities),
        },
        "context_length": model.context_tokens,
        "top_provider": {"max_completion_tokens": model.max_output_tokens},
        "pricing": {key: str(value) for key, value in model.pricing},
        "supported_parameters": list(model.supported_parameters),
        "expiration_date": (
            model.expiration_date.date().isoformat() if model.expiration_date else None
        ),
    }


def _required_mapping(value: object, name: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a non-empty text list")
    return tuple(value)


def _parse_expiration(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expiration_date must be a date or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00+00:00")
        except ValueError:
            raise ValueError("expiration_date is invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
