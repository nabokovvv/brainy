import unittest

from brainy_core.inference import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ProviderHealth,
    ProviderModel,
    ProviderUnavailableError,
)
from brainy_core.providers.fallback import FallbackInferenceProvider


class _Provider:
    def __init__(self, name: str, result: ChatResult | None = None, error=None):
        self._model = ProviderModel(name, f"{name}-model", name == "ollama")
        self.result = result
        self.error = error
        self.calls = 0
        self.closed = False

    @property
    def model(self):
        return self._model

    async def chat(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def health(self):
        return ProviderHealth(self.model, self.error is None, 1)

    async def aclose(self):
        self.closed = True


class FallbackInferenceProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_result_and_actual_model_are_preserved(self):
        actual = ProviderModel("omnirouter", "vendor/actual-model", False)
        primary = _Provider("omnirouter", ChatResult("remote", actual, 10))
        fallback = _Provider(
            "ollama", ChatResult("local", ProviderModel("ollama", "local", True), 20)
        )
        provider = FallbackInferenceProvider(primary, fallback)

        result = await provider.chat(ChatRequest(messages=(ChatMessage("user", "hello"),)))

        self.assertEqual(result.text, "remote")
        self.assertEqual(result.model.name, "vendor/actual-model")
        self.assertEqual(fallback.calls, 0)

    async def test_provider_error_falls_back_to_ollama(self):
        primary = _Provider(
            "omnirouter", error=ProviderUnavailableError("omnirouter", 503)
        )
        local_model = ProviderModel("ollama", "gemma4:e2b", True)
        fallback = _Provider("ollama", ChatResult("local", local_model, 20))
        provider = FallbackInferenceProvider(primary, fallback)

        result = await provider.chat(ChatRequest(messages=(ChatMessage("user", "hello"),)))

        self.assertEqual(result.text, "local")
        self.assertEqual(result.model.provider, "ollama")
        self.assertEqual(fallback.calls, 1)


if __name__ == "__main__":
    unittest.main()
