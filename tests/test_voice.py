from __future__ import annotations

import asyncio
import threading
import time
import unittest

from brainy_core.voice import WhisperTranscriber, WhisperTranscriptionError


class FakeWhisperModel:
    def __init__(self, text: str = "  voice text  ") -> None:
        self.text = text
        self.calls: list[tuple[str, dict[str, object]]] = []

    def transcribe(self, audio_path: str, **options: object) -> dict[str, str]:
        self.calls.append((audio_path, options))
        return {"text": self.text}


class ConcurrencyTrackingModel(FakeWhisperModel):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, audio_path: str, **options: object) -> dict[str, str]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return super().transcribe(audio_path, **options)
        finally:
            with self._lock:
                self.active -= 1


class BlockingWhisperModel(ConcurrencyTrackingModel):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio_path: str, **options: object) -> dict[str, str]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if not self.release.wait(timeout=2):
                raise TimeoutError("test did not release transcription")
            return FakeWhisperModel.transcribe(self, audio_path, **options)
        finally:
            with self._lock:
                self.active -= 1


class BlockingLoader:
    def __init__(self, model: FakeWhisperModel) -> None:
        self.model = model
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, model_name: str) -> FakeWhisperModel:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if not self.release.wait(timeout=2):
                raise TimeoutError("test did not release model load")
            return self.model
        finally:
            with self._lock:
                self.active -= 1


class WhisperTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_existing_transcription_options_and_reuses_model(self) -> None:
        model = FakeWhisperModel()
        loads: list[str] = []

        def loader(model_name: str) -> FakeWhisperModel:
            loads.append(model_name)
            return model

        transcriber = WhisperTranscriber("base", loader=loader)

        first, second = await asyncio.gather(
            transcriber.transcribe("first.oga", language="ru"),
            transcriber.transcribe("second.oga", language="ru"),
        )

        self.assertEqual((first, second), ("voice text", "voice text"))
        self.assertEqual(loads, ["base"])
        self.assertTrue(transcriber.is_loaded)
        self.assertEqual(model.calls[0][1]["beam_size"], 3)
        self.assertEqual(model.calls[0][1]["temperature"], 0.0)
        self.assertTrue(model.calls[0][1]["condition_on_previous_text"])

    async def test_empty_transcription_is_a_safe_error(self) -> None:
        transcriber = WhisperTranscriber("base", loader=lambda _: FakeWhisperModel("  "))

        with self.assertRaises(WhisperTranscriptionError):
            await transcriber.transcribe("voice.oga", language="en")

    async def test_transcriptions_are_serialized_for_local_resource_safety(self) -> None:
        model = ConcurrencyTrackingModel()
        transcriber = WhisperTranscriber("base", loader=lambda _: model)

        await asyncio.gather(
            transcriber.transcribe("first.oga", language="en"),
            transcriber.transcribe("second.oga", language="en"),
        )

        self.assertEqual(model.max_active, 1)

    async def test_cancel_waits_for_thread_before_next_transcription(self) -> None:
        model = BlockingWhisperModel()
        transcriber = WhisperTranscriber("base", loader=lambda _: model)

        first = asyncio.create_task(transcriber.transcribe("first.oga", language="en"))
        entered = await asyncio.to_thread(model.entered.wait, 1)
        self.assertTrue(entered)
        first.cancel()
        second = asyncio.create_task(transcriber.transcribe("second.oga", language="en"))
        await asyncio.sleep(0.02)

        self.assertEqual(model.max_active, 1)
        model.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(results[1], "voice text")
        self.assertEqual(model.max_active, 1)

    async def test_cancelled_prefetch_remains_single_flight(self) -> None:
        loader = BlockingLoader(FakeWhisperModel())
        transcriber = WhisperTranscriber("base", loader=loader)

        first = asyncio.create_task(transcriber.prefetch())
        entered = await asyncio.to_thread(loader.entered.wait, 1)
        self.assertTrue(entered)
        first.cancel()
        second = asyncio.create_task(transcriber.prefetch())
        await asyncio.sleep(0.02)

        self.assertEqual(loader.calls, 1)
        self.assertEqual(loader.max_active, 1)
        loader.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIsNone(results[1])
        self.assertEqual(loader.calls, 1)
        self.assertTrue(transcriber.is_loaded)


if __name__ == "__main__":
    unittest.main()
