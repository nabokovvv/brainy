from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brainy_core import RouteIntent
from brainy_core.scheduling import StablePriorityQueue


class _DummyTelegramObject:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _DummyFilter:
    def __and__(self, other: object) -> "_DummyFilter":
        return self

    def __invert__(self) -> "_DummyFilter":
        return self


class _TelegramError(Exception):
    pass


def _load_bot_with_telegram_stub():
    telegram = types.ModuleType("telegram")
    telegram.__path__ = []
    telegram.InlineKeyboardButton = _DummyTelegramObject
    telegram.InlineKeyboardMarkup = _DummyTelegramObject
    telegram.InputFile = _DummyTelegramObject
    telegram.Update = type("Update", (), {"ALL_TYPES": object()})

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.Application = _DummyTelegramObject
    telegram_ext.CallbackQueryHandler = _DummyTelegramObject
    telegram_ext.CommandHandler = _DummyTelegramObject
    telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    telegram_ext.Job = _DummyTelegramObject
    telegram_ext.JobQueue = _DummyTelegramObject
    telegram_ext.MessageHandler = _DummyTelegramObject
    telegram_ext.filters = SimpleNamespace(
        TEXT=_DummyFilter(),
        COMMAND=_DummyFilter(),
        VOICE=_DummyFilter(),
    )

    telegram_constants = types.ModuleType("telegram.constants")
    telegram_constants.ChatAction = SimpleNamespace(TYPING="typing")
    telegram_constants.ParseMode = SimpleNamespace(MARKDOWN_V2="MarkdownV2")

    telegram_error = types.ModuleType("telegram.error")
    telegram_error.BadRequest = _TelegramError
    telegram_error.NetworkError = _TelegramError
    telegram_error.TimedOut = _TelegramError
    telegram.error = telegram_error

    modules = {
        "telegram": telegram,
        "telegram.constants": telegram_constants,
        "telegram.error": telegram_error,
        "telegram.ext": telegram_ext,
    }
    with patch.dict(sys.modules, modules):
        sys.modules.pop("bot", None)
        return importlib.import_module("bot")


bot = _load_bot_with_telegram_stub()


class _Translator:
    def get_string(self, key: str, lang: str, **kwargs: object) -> str:
        return f"{key}:{lang}"


class _Message:
    def __init__(self, voice: object | None = None) -> None:
        self.voice = voice
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class _CallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[tuple[str, object | None]] = []
        self.answered = False

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, *, text: str, reply_markup: object | None = None) -> None:
        self.edits.append((text, reply_markup))


class _VoiceFile:
    async def download_to_drive(self, path: str) -> None:
        return None


class _Voice:
    async def get_file(self) -> _VoiceFile:
        return _VoiceFile()


class _Bot:
    def __init__(self, *, delete_fails: bool = False) -> None:
        self.delete_fails = delete_fails
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.deleted: list[tuple[object, ...]] = []

    async def send_message(self, *args: object, **kwargs: object) -> object:
        self.sent.append((args, kwargs))
        return SimpleNamespace(message_id=42)

    async def delete_message(self, *args: object, **kwargs: object) -> None:
        self.deleted.append(args)
        if self.delete_fails:
            raise RuntimeError("status cleanup failed")


class _FailingTranscriber:
    async def transcribe(self, path: str, *, language: str) -> str:
        raise RuntimeError("transcription failed")


class _SuccessfulTranscriber:
    async def transcribe(self, path: str, *, language: str) -> str:
        return "voice text"


class _StateMutatingTranscriber:
    def __init__(self, chat_data: dict[str, object]) -> None:
        self.chat_data = chat_data

    async def transcribe(self, path: str, *, language: str) -> str:
        self.chat_data.update(language="en", web_enabled=True)
        return "voice text"


class _JobQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_once(self, callback, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(schedule_removal=lambda: None)


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_modes_are_not_runtime_handlers(self) -> None:
        removed_handlers = {
            "deep_research_handler",
            "deep_search_handler",
            "deepseek_r1_handler",
            "fast_web_handler",
        }

        self.assertFalse([name for name in removed_handlers if hasattr(bot, name)])

    async def test_cancelling_idle_worker_does_not_over_acknowledge_queue(self) -> None:
        queue: StablePriorityQueue[object] = StablePriorityQueue(maxsize=1)
        task = asyncio.create_task(
            bot.worker(
                "test",
                queue,
                {"translator": _Translator(), "chat_locks": {}},
            )
        )
        await asyncio.sleep(0)

        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]

        self.assertIsInstance(result, asyncio.CancelledError)
        await queue.put(1, "still-usable")
        self.assertEqual(await queue.get(), (1, "still-usable"))
        queue.task_done()
        await queue.join()

    async def test_worker_passes_snapshotted_language_to_handler(self) -> None:
        queue = StablePriorityQueue(maxsize=1)
        called = asyncio.Event()
        captured: list[str | None] = []
        message = _Message()
        application = SimpleNamespace(bot_data={"llm_semaphore": asyncio.Semaphore(1)})
        context = SimpleNamespace(
            application=application,
            bot=object(),
            chat_data={"language": "en"},
        )
        update = SimpleNamespace(message=message)
        request = bot.Request(update, context, 7, "question", "ru", RouteIntent.LOCAL)
        await queue.put(1, request)

        async def fake_handler(update, context, query, *, language=None) -> None:
            captured.append(language)
            called.set()

        async def fake_typing(bot_instance, chat_id) -> None:
            await asyncio.Event().wait()

        with (
            patch.object(bot, "fast_reply_handler", fake_handler),
            patch.object(bot, "send_typing_periodically", fake_typing),
        ):
            task = asyncio.create_task(
                bot.worker(
                    "test",
                    queue,
                    {"translator": _Translator(), "chat_locks": {}},
                )
            )
            await asyncio.wait_for(called.wait(), timeout=1)
            await asyncio.wait_for(queue.join(), timeout=1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(captured, ["ru"])

    async def test_worker_fails_closed_for_web_snapshot_without_calling_local_model(self) -> None:
        queue = StablePriorityQueue(maxsize=1)
        message = _Message()
        application = SimpleNamespace(bot_data={"llm_semaphore": asyncio.Semaphore(1)})
        context = SimpleNamespace(
            application=application,
            bot=object(),
            chat_data={"language": "en", "web_enabled": False},
        )
        update = SimpleNamespace(message=message)
        await queue.put(1, bot.Request(update, context, 7, "latest news", "en", RouteIntent.WEB))

        async def forbidden_local_handler(*args: object, **kwargs: object) -> None:
            raise AssertionError("Web requests must not silently use local inference")

        async def fake_typing(bot_instance, chat_id) -> None:
            await asyncio.Event().wait()

        with (
            patch.object(bot, "fast_reply_handler", forbidden_local_handler),
            patch.object(bot, "send_typing_periodically", fake_typing),
        ):
            task = asyncio.create_task(
                bot.worker(
                    "test",
                    queue,
                    {"translator": _Translator(), "chat_locks": {}},
                )
            )
            await asyncio.wait_for(queue.join(), timeout=1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(message.replies, ["web_unavailable:en"])

    async def test_buffer_keeps_route_and_language_from_first_message(self) -> None:
        chat_id = 21
        queue = StablePriorityQueue(maxsize=1)
        message = _Message()
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(
            job=SimpleNamespace(chat_id=chat_id),
            chat_data={"language": "ru", "web_enabled": False},
            application=SimpleNamespace(
                bot_data={"request_queue": queue, "translator": _Translator()}
            ),
        )
        bot.user_message_buffers[chat_id] = ["first"]
        bot.user_last_update[chat_id] = update
        bot._capture_request_snapshot(context, chat_id)
        context.chat_data.update(language="en", web_enabled=True)

        try:
            await bot.process_buffered_messages(context)
            _, request = await queue.get()

            self.assertEqual(request.language, "ru")
            self.assertIs(request.route_intent, RouteIntent.LOCAL)
        finally:
            queue.task_done()
            bot.user_message_buffers.pop(chat_id, None)
            bot.user_last_update.pop(chat_id, None)
            bot.user_job_trackers.pop(chat_id, None)
            bot.user_request_snapshots.pop(chat_id, None)

    async def test_web_toggle_updates_chat_state_without_running_search(self) -> None:
        callback = _CallbackQuery(bot.ACTION_TOGGLE_WEB)
        context = SimpleNamespace(
            chat_data={"language": "en", "web_enabled": False},
            application=SimpleNamespace(bot_data={"translator": _Translator()}),
        )

        await bot.button(SimpleNamespace(callback_query=callback), context)

        self.assertTrue(callback.answered)
        self.assertTrue(context.chat_data["web_enabled"])
        self.assertEqual(callback.edits[0][0], "web_status_on:en")

    async def test_voice_failure_replies_and_cleans_status(self) -> None:
        chat_id = 11
        telegram_bot = _Bot()
        message = _Message(_Voice())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=message)
        context = SimpleNamespace(
            bot=telegram_bot,
            chat_data={"language": "ru"},
            application=SimpleNamespace(
                bot_data={
                    "translator": _Translator(),
                    "whisper_transcriber": _FailingTranscriber(),
                }
            ),
        )

        await bot.handle_voice_message(update, context)

        self.assertEqual(message.replies, ["error_generic:ru"])
        self.assertEqual(telegram_bot.deleted, [(chat_id, 42)])

    async def test_voice_status_cleanup_failure_does_not_hide_transcript(self) -> None:
        chat_id = 12
        telegram_bot = _Bot(delete_fails=True)
        message = _Message(_Voice())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=message)
        job_queue = _JobQueue()
        context = SimpleNamespace(
            bot=telegram_bot,
            chat_data={"language": "en"},
            job_queue=job_queue,
            application=SimpleNamespace(
                bot_data={
                    "translator": _Translator(),
                    "whisper_transcriber": _SuccessfulTranscriber(),
                }
            ),
        )
        bot.user_message_buffers.pop(chat_id, None)
        bot.user_last_update.pop(chat_id, None)
        bot.user_job_trackers.pop(chat_id, None)
        bot.user_request_snapshots.pop(chat_id, None)

        try:
            await bot.handle_voice_message(update, context)

            self.assertEqual(telegram_bot.sent[1][0], (chat_id, "voice text"))
            self.assertEqual(bot.user_message_buffers[chat_id], ["voice text"])
            self.assertEqual(len(job_queue.calls), 1)
        finally:
            bot.user_message_buffers.pop(chat_id, None)
            bot.user_last_update.pop(chat_id, None)
            bot.user_job_trackers.pop(chat_id, None)
            bot.user_request_snapshots.pop(chat_id, None)

    async def test_voice_keeps_route_and_language_from_before_transcription(self) -> None:
        chat_id = 13
        telegram_bot = _Bot()
        message = _Message(_Voice())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=message)
        job_queue = _JobQueue()
        chat_data: dict[str, object] = {"language": "ru", "web_enabled": False}
        context = SimpleNamespace(
            bot=telegram_bot,
            chat_data=chat_data,
            job_queue=job_queue,
            application=SimpleNamespace(
                bot_data={
                    "translator": _Translator(),
                    "whisper_transcriber": _StateMutatingTranscriber(chat_data),
                }
            ),
        )

        try:
            await bot.handle_voice_message(update, context)

            self.assertEqual(
                bot.user_request_snapshots[chat_id],
                ("ru", RouteIntent.LOCAL),
            )
            self.assertEqual(chat_data, {"language": "en", "web_enabled": True})
        finally:
            bot.user_message_buffers.pop(chat_id, None)
            bot.user_last_update.pop(chat_id, None)
            bot.user_job_trackers.pop(chat_id, None)
            bot.user_request_snapshots.pop(chat_id, None)


if __name__ == "__main__":
    unittest.main()
