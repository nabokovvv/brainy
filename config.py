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
    whisper_backend: str
    whisper_cpp_executable: str
    whisper_cpp_model: str
    whisper_cpp_ffmpeg: str
    web_enabled_default: bool
    search_backend: str
    search_fallback_backend: str
    search_quota_state_path: str
    brave_search_api_key: str | None
    brave_search_monthly_limit: int
    tavily_api_key: str | None
    tavily_monthly_limit: int
    serpapi_api_key: str | None
    serpapi_monthly_limit: int
    user_settings_path: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            telegram_token=_optional_env(source.get("TELEGRAM_TOKEN")),
            llm_client=source.get("LLM_CLIENT", "ollama").strip().lower(),
            ollama_base_url=source.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            .strip()
            .rstrip("/"),
            # Exact tag confirmed on the target Mac mini.
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
            # The deployed Mac mini has a locally managed whisper.cpp large-v3 model.
            # Keep the Python implementation as an explicit development override so a
            # bot started without a dotenv loader does not silently select it.
            whisper_backend=source.get("WHISPER_BACKEND", "cpp").strip().lower(),
            whisper_cpp_executable=source.get(
                "WHISPER_CPP_EXECUTABLE", "/opt/homebrew/bin/whisper-cli"
            ).strip(),
            whisper_cpp_model=source.get(
                "WHISPER_CPP_MODEL",
                "~/Library/Application Support/Brainy/models/whisper/ggml-large-v3.bin",
            ).strip(),
            whisper_cpp_ffmpeg=source.get("WHISPER_CPP_FFMPEG", "/opt/homebrew/bin/ffmpeg").strip(),
            web_enabled_default=_env_bool(source.get("WEB_ENABLED_DEFAULT"), default=False),
            search_backend=source.get("SEARCH_BACKEND", "disabled").strip().lower(),
            search_fallback_backend=source.get("SEARCH_FALLBACK_BACKEND", "disabled")
            .strip()
            .lower(),
            search_quota_state_path=source.get(
                "SEARCH_QUOTA_STATE_PATH", "~/.local/state/brainy/search_quota.json"
            ).strip(),
            brave_search_api_key=_optional_env(source.get("BRAVE_SEARCH_API_KEY")),
            brave_search_monthly_limit=_env_int(
                source.get("BRAVE_SEARCH_MONTHLY_LIMIT"),
                default=900,
                name="BRAVE_SEARCH_MONTHLY_LIMIT",
            ),
            tavily_api_key=_optional_env(source.get("TAVILY_API_KEY")),
            tavily_monthly_limit=_env_int(
                source.get("TAVILY_MONTHLY_LIMIT"), default=900, name="TAVILY_MONTHLY_LIMIT"
            ),
            serpapi_api_key=_optional_env(source.get("SERPAPI_API_KEY")),
            serpapi_monthly_limit=_env_int(
                source.get("SERPAPI_MONTHLY_LIMIT"), default=200, name="SERPAPI_MONTHLY_LIMIT"
            ),
            user_settings_path=source.get(
                "USER_SETTINGS_PATH", "~/.local/state/brainy/settings.sqlite3"
            ).strip(),
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
        if not self.user_settings_path.strip():
            errors.append("USER_SETTINGS_PATH must be non-empty")
        if self.whisper_backend not in {"python", "cpp"}:
            errors.append("WHISPER_BACKEND must be 'python' or 'cpp'")
        if self.whisper_backend == "cpp":
            if not self.whisper_cpp_executable:
                errors.append("WHISPER_CPP_EXECUTABLE must be non-empty")
            if not self.whisper_cpp_model:
                errors.append("WHISPER_CPP_MODEL must be non-empty")
            if not self.whisper_cpp_ffmpeg:
                errors.append("WHISPER_CPP_FFMPEG must be non-empty")

        web_required = self.web_enabled_default if require_web is None else require_web
        if self.search_backend not in {"disabled", "rotation"}:
            errors.append("SEARCH_BACKEND must be 'disabled' or 'rotation'")
        if self.search_fallback_backend not in {"disabled"}:
            errors.append("SEARCH_FALLBACK_BACKEND must be 'disabled'")
        if (
            self.search_fallback_backend == self.search_backend
            and self.search_backend != "disabled"
        ):
            errors.append("SEARCH_FALLBACK_BACKEND must differ from SEARCH_BACKEND")
        limits = (
            ("BRAVE_SEARCH_MONTHLY_LIMIT", self.brave_search_monthly_limit),
            ("TAVILY_MONTHLY_LIMIT", self.tavily_monthly_limit),
            ("SERPAPI_MONTHLY_LIMIT", self.serpapi_monthly_limit),
        )
        for name, limit in limits:
            if limit < 1:
                errors.append(f"{name} must be positive")
        if web_required and self.search_backend != "rotation":
            errors.append("Web search requires SEARCH_BACKEND=rotation")
        if web_required and not any(
            (self.brave_search_api_key, self.tavily_api_key, self.serpapi_api_key)
        ):
            errors.append("Web search requires at least one search provider API key")

        if errors:
            raise ConfigurationError("; ".join(errors))


SETTINGS = Settings.from_env()

# Runtime constants consumed by the Telegram adapter.
TELEGRAM_TOKEN = SETTINGS.telegram_token
OLLAMA_BASE_URL = SETTINGS.ollama_base_url
OLLAMA_MODEL = SETTINGS.ollama_model
OLLAMA_TIMEOUT = SETTINGS.ollama_timeout_seconds
OLLAMA_CONTEXT_TOKENS = SETTINGS.ollama_context_tokens
WHISPER_MODEL = SETTINGS.whisper_model
WHISPER_BACKEND = SETTINGS.whisper_backend
WHISPER_CPP_EXECUTABLE = SETTINGS.whisper_cpp_executable
WHISPER_CPP_MODEL = SETTINGS.whisper_cpp_model
WHISPER_CPP_FFMPEG = SETTINGS.whisper_cpp_ffmpeg

# Dormant research defaults. These utilities are not imported by the fast runtime;
# Stage 2 will replace module globals with injected adapter settings.
CUSTOM_USER_AGENT = "BrainyBot/1.0 (https://askbrainy.com)"
P279_MAX_DEPTH = 1
HIGH_PRIORITY_WEIGHT = 1000
MEDIUM_PRIORITY_WEIGHT = 100
LOW_PRIORITY_WEIGHT = 10
SCIENTIFIC_TERM_BOOST = 1.2
ENTITY_SEARCH_LIMIT = 10
MIN_SITELINKS_LOW_PRIORITY = 5
MIN_SITELINKS_THRESHOLD = 3
