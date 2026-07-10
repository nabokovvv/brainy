"""Lazy, dependency-injected Whisper transcription service."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any, Callable, Protocol


class WhisperModel(Protocol):
    def transcribe(self, audio_path: str, **options: Any) -> dict[str, Any]:
        ...


class WhisperTranscriptionError(RuntimeError):
    """Safe error raised when Whisper returns an unusable transcription."""


def _load_whisper_model(model_name: str) -> WhisperModel:
    import torch
    import whisper

    torch.set_num_threads(4)
    return whisper.load_model(model_name)


async def _run_blocking_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Delay cancellation until the underlying thread has actually stopped using inputs."""

    loop = asyncio.get_running_loop()
    thread_future = loop.run_in_executor(None, partial(function, *args, **kwargs))
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(thread_future)
            break
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            if cancellation_requested:
                raise asyncio.CancelledError() from None
            raise

    if cancellation_requested:
        raise asyncio.CancelledError()
    return result


class WhisperTranscriber:
    """Load Whisper once, on the first voice message, and reuse it safely."""

    def __init__(
        self,
        model_name: str = "base",
        *,
        loader: Callable[[str], WhisperModel] = _load_whisper_model,
    ) -> None:
        if not model_name.strip():
            raise ValueError("Whisper model name must be non-empty.")
        self._model_name = model_name
        self._loader = loader
        self._model: WhisperModel | None = None
        self._load_lock = asyncio.Lock()
        self._transcribe_lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def prefetch(self) -> None:
        await self._get_model()

    async def transcribe(self, audio_path: str | Path, *, language: str) -> str:
        path = str(audio_path)
        if not path:
            raise ValueError("Audio path must be non-empty.")
        model = await self._get_model()
        async with self._transcribe_lock:
            result = await _run_blocking_call(
                model.transcribe,
                path,
                language=language,
                beam_size=3,
                temperature=0.0,
                condition_on_previous_text=True,
            )
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise WhisperTranscriptionError("Whisper returned an empty transcription.")
        return text.strip()

    async def _get_model(self) -> WhisperModel:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                await _run_blocking_call(self._load_and_store_model)
        return self._model

    def _load_and_store_model(self) -> WhisperModel:
        model = self._loader(self._model_name)
        self._model = model
        return model
