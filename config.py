"""Environment-backed configuration without import-time validation or I/O."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when an explicitly enabled runtime feature is misconfigured."""


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean value: {value!r}")


def _env_float(value: str | None, *, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _env_int(value: str | None, *, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _optional_env(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True)
class Settings:
    telegram_token: str | None
    llm_client: str
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    ollama_context_tokens: int
    whisper_model: str
    web_enabled_default: bool
    search_backend: str
    yandex_api_key: str | None
    yandex_folder_id: str | None
    together_api_key: str | None
    wikidata_access_token: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            telegram_token=_optional_env(source.get("TELEGRAM_TOKEN")),
            llm_client=source.get("LLM_CLIENT", "ollama").strip().lower(),
            ollama_base_url=source.get(
                "OLLAMA_BASE_URL", "http://localhost:11434/v1"
            ).strip().rstrip("/"),
            # Owner-provided provisional name; confirm the exact Ollama tag on the target Mac.
            ollama_model=source.get(
                "OLLAMA_MODEL", source.get("FAST_REPLY_MODEL", "gemma4:e2b")
            ).strip(),
            ollama_timeout_seconds=_env_float(
                source.get("OLLAMA_TIMEOUT"), default=120.0, name="OLLAMA_TIMEOUT"
            ),
            ollama_context_tokens=_env_int(
                source.get("OLLAMA_CONTEXT_TOKENS"),
                default=65_536,
                name="OLLAMA_CONTEXT_TOKENS",
            ),
            whisper_model=source.get("WHISPER_MODEL", "base").strip(),
            web_enabled_default=_env_bool(source.get("WEB_ENABLED_DEFAULT"), default=False),
            search_backend=source.get("SEARCH_BACKEND", "disabled").strip().lower(),
            yandex_api_key=_optional_env(source.get("YANDEX_API_KEY")),
            yandex_folder_id=_optional_env(source.get("YANDEX_FOLDER_ID")),
            together_api_key=_optional_env(source.get("TOGETHER_AI_API_KEY")),
            wikidata_access_token=_optional_env(source.get("WIKIDATA_ACCESS_TOKEN")),
        )

    def validate(self, *, require_telegram: bool = False, require_web: bool | None = None) -> None:
        """Validate only features that will actually be used by this process."""

        errors: list[str] = []
        if require_telegram and not self.telegram_token:
            errors.append("TELEGRAM_TOKEN is required to run the Telegram bot")

        if self.llm_client != "ollama":
            errors.append(
                "LLM_CLIENT must remain 'ollama' until Stage 3 enforces free-only remote routing"
            )

        if not self.ollama_base_url:
            errors.append("OLLAMA_BASE_URL must be non-empty")
        if not self.ollama_model:
            errors.append("OLLAMA_MODEL must be non-empty")

        if not 0 < self.ollama_timeout_seconds <= 120:
            errors.append("OLLAMA_TIMEOUT must be between 0 and 120 seconds")
        if not 1 <= self.ollama_context_tokens <= 65536:
            errors.append("OLLAMA_CONTEXT_TOKENS must be between 1 and 65536")
        if not self.whisper_model.strip():
            errors.append("WHISPER_MODEL must be non-empty")

        web_required = self.web_enabled_default if require_web is None else require_web
        if self.search_backend not in {"disabled", "yandex"}:
            errors.append("SEARCH_BACKEND must be 'disabled' or the legacy 'yandex'")
        if web_required:
            errors.append("Web search cannot be enabled until the Stage 2 zero-cost adapter lands")

        if errors:
            raise ConfigurationError("; ".join(errors))


SETTINGS = Settings.from_env()

# Compatibility constants for the legacy modules. They remain side-effect free and
# will disappear as each module moves to dependency-injected settings.
TELEGRAM_TOKEN = SETTINGS.telegram_token
LLM_CLIENT = SETTINGS.llm_client

OLLAMA_BASE_URL = SETTINGS.ollama_base_url
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", f"{OLLAMA_BASE_URL}/completions")
OLLAMA_CHAT_ENDPOINT = os.getenv("OLLAMA_CHAT_ENDPOINT", f"{OLLAMA_BASE_URL}/chat/completions")
OLLAMA_MODEL = SETTINGS.ollama_model
FAST_REPLY_MODEL = os.getenv("FAST_REPLY_MODEL", OLLAMA_MODEL)
OLLAMA_TIMEOUT = SETTINGS.ollama_timeout_seconds
OLLAMA_CONTEXT_TOKENS = SETTINGS.ollama_context_tokens
WHISPER_MODEL = SETTINGS.whisper_model

FACTUAL_PARAMS = {
    "temperature": 0.3,
    "top_k": 50,
    "top_p": 0.9,
    "frequency_penalty": 0.2,
    "max_tokens": 1024,
    "repetition_penalty": 1.1,
}
FACTUAL_PARAMS_2 = {"temperature": 0.3}
CREATIVE_PARAMS = {"temperature": 0.7, "top_p": 0.9}
DEEP_SEARCH_STEP_ONE_MODEL = os.getenv("DEEP_SEARCH_STEP_ONE_MODEL", OLLAMA_MODEL)
DEEP_SEARCH_STEP_SIX_MODEL = os.getenv("DEEP_SEARCH_STEP_SIX_MODEL", OLLAMA_MODEL)
DEEP_SEARCH_STEP_FINAL_MODEL = os.getenv("DEEP_SEARCH_STEP_FINAL_MODEL", OLLAMA_MODEL)

TOGETHER_AI_API_KEY = SETTINGS.together_api_key
TOGETHER_MODEL = os.getenv("TOGETHER_MODEL", "ServiceNow-AI/Apriel-1.6-15b-Thinker")
TOGETHER_DEEPSEEK = os.getenv("TOGETHER_DEEPSEEK", "ServiceNow-AI/Apriel-1.5-15b-Thinker")
TOGETHER_WEB_SEARCH = os.getenv("TOGETHER_WEB_SEARCH", TOGETHER_MODEL)
TOGETHER_FAST = os.getenv("TOGETHER_FAST", TOGETHER_MODEL)
TOGETHER_SUMMARY = os.getenv("TOGETHER_SUMMARY", TOGETHER_MODEL)
TOGETHER_QUERIES = os.getenv("TOGETHER_QUERIES", TOGETHER_MODEL)

WEB_ENABLED_DEFAULT = SETTINGS.web_enabled_default
SEARCH_BACKEND = SETTINGS.search_backend
YANDEX_API_KEY = SETTINGS.yandex_api_key
YANDEX_FOLDER_ID = SETTINGS.yandex_folder_id


def _legacy_int(name: str, default: int) -> int:
    """Ignore dormant legacy knobs while the web path is fail-closed."""

    if not WEB_ENABLED_DEFAULT:
        return default
    return _env_int(os.getenv(name), default=default, name=name)


def _legacy_float(name: str, default: float) -> float:
    """Ignore dormant legacy knobs while the web path is fail-closed."""

    if not WEB_ENABLED_DEFAULT:
        return default
    return _env_float(os.getenv(name), default=default, name=name)

RERANK_MODEL = os.getenv("RERANK_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
RERANK_THRESHOLD = _legacy_float("RERANK_THRESHOLD", 0.69)
TOP_N = _legacy_int("TOP_N", 5)

WIKIDATA_ACCESS_TOKEN = SETTINGS.wikidata_access_token
CUSTOM_USER_AGENT = os.getenv("CUSTOM_USER_AGENT", "BrainyBot/1.0 (https://askbrainy.com)")

MIN_SITELINKS_THRESHOLD = _legacy_int("MIN_SITELINKS_THRESHOLD", 3)
MIN_SITELINKS_LOW_PRIORITY = _legacy_int("MIN_SITELINKS_LOW_PRIORITY", 5)
ENTITY_SEARCH_LIMIT = _legacy_int("ENTITY_SEARCH_LIMIT", 10)
P279_MAX_DEPTH = _legacy_int("P279_MAX_DEPTH", 1)
HIGH_PRIORITY_WEIGHT = _legacy_int("HIGH_PRIORITY_WEIGHT", 1000)
MEDIUM_PRIORITY_WEIGHT = _legacy_int("MEDIUM_PRIORITY_WEIGHT", 100)
LOW_PRIORITY_WEIGHT = _legacy_int("LOW_PRIORITY_WEIGHT", 10)
SCIENTIFIC_TERM_BOOST = _legacy_float("SCIENTIFIC_TERM_BOOST", 1.2)
