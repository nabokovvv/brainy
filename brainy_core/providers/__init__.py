"""Inference provider adapters."""

from brainy_core.providers.ddgs import DDGSProvider
from brainy_core.providers.ollama import OllamaProvider

__all__ = ["DDGSProvider", "OllamaProvider"]
