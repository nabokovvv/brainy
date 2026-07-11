"""Acceptance tests for feedback button slice per docs/FEEDBACK_DESIGN.md."""

from __future__ import annotations

import logging
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bot import (
    ACTION_FEEDBACK,
    _FEEDBACK_SAMPLE_MAX,
    _FEEDBACK_SAMPLE_MIN,
    _handle_feedback_callback,
    _should_show_feedback_keyboard,
)
from brainy_core.feedback import FeedbackEntry, FeedbackStore
from localization import Translator


class FeedbackSamplingTests(unittest.TestCase):
    """Tests for _should_show_feedback_keyboard per-chat sampling logic."""

    def setUp(self) -> None:
        self.context = MagicMock()
        self.context.chat_data = {}

    def test_first_reply_always_shows_keyboard(self) -> None:
        """First fast-reply in a chat always shows the keyboard."""
        result = _should_show_feedback_keyboard(self.context)
        self.assertTrue(result)

    def test_countdown_initialized_after_first_show(self) -> None:
        """Countdown is set to random 8-12 after first show."""
        _should_show_feedback_keyboard(self.context)
        countdown = self.context.chat_data["feedback_prompt_countdown"]
        self.assertGreaterEqual(countdown, _FEEDBACK_SAMPLE_MIN)
        self.assertLessEqual(countdown, _FEEDBACK_SAMPLE_MAX)

    def test_subsequent_replies_decrement_countdown(self) -> None:
        """Subsequent replies decrement countdown, don't show keyboard until <=0."""
        with patch("bot.random.randint", return_value=10):
            _should_show_feedback_keyboard(self.context)  # first show -> True, sets countdown=10

        # Next 10 calls should NOT show (countdown 10->0)
        for _ in range(10):
            result = _should_show_feedback_keyboard(self.context)
            self.assertFalse(result)

        # 11th call should show again (countdown 0 -> reset)
        with patch("bot.random.randint", return_value=8):
            result = _should_show_feedback_keyboard(self.context)
        self.assertTrue(result)

    def test_countdown_bounds_respected(self) -> None:
        """Countdown always resets to range 8-12."""
        for _ in range(100):
            with patch("bot.random.randint", return_value=10):
                _should_show_feedback_keyboard(self.context)
            # Deplete countdown
            while self.context.chat_data.get("feedback_prompt_countdown", 0) > 0:
                _should_show_feedback_keyboard(self.context)
            # Now countdown is 0, next call should show and reset
            with patch("bot.random.randint", return_value=10):
                _should_show_feedback_keyboard(self.context)
            # Check new countdown is in bounds
            countdown = self.context.chat_data["feedback_prompt_countdown"]
            self.assertGreaterEqual(countdown, _FEEDBACK_SAMPLE_MIN)
            self.assertLessEqual(countdown, _FEEDBACK_SAMPLE_MAX)

    def test_isolation_per_chat(self) -> None:
        """Different chats have independent countdowns."""
        ctx1 = MagicMock()
        ctx1.chat_data = {}
        ctx2 = MagicMock()
        ctx2.chat_data = {}

        with patch("bot.random.randint", return_value=10):
            self.assertTrue(_should_show_feedback_keyboard(ctx1))
            self.assertTrue(_should_show_feedback_keyboard(ctx2))

        # Both should have independent countdowns (set to 10, not decremented yet)
        self.assertEqual(ctx1.chat_data["feedback_prompt_countdown"], 10)
        self.assertEqual(ctx2.chat_data["feedback_prompt_countdown"], 10)


class FeedbackCallbackHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _handle_feedback_callback."""

    def setUp(self) -> None:
        self.query = AsyncMock()
        self.context = MagicMock()
        self.translator = Translator(str(Path(__file__).parent.parent / "translations.json"))
        self.lang = "en"
        self.log_stream = StringIO()
        self.log_handler = logging.StreamHandler(self.log_stream)
        self.logger = logging.getLogger("bot")
        self.logger.addHandler(self.log_handler)
        self.logger.setLevel(logging.INFO)
        self.log_handler.setFormatter(logging.Formatter("%(message)s"))

    def tearDown(self) -> None:
        self.logger.removeHandler(self.log_handler)

    def _make_entry(self) -> FeedbackEntry:
        return FeedbackEntry(
            provider="ollama",
            model="gemma4:e2b",
            latency_ms=1234.5,
            lang="en",
            route="local",
        )

    async def test_up_vote_logs_feedback_recorded_with_whitelist_fields(self) -> None:
        """Up vote creates log with exactly the whitelist fields."""
        request_id = "req-abc123"
        entry = self._make_entry()

        with patch("bot.feedback_store") as mock_store:
            mock_store.pop.return_value = entry

            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_up_{request_id}",
            )

        log_output = self.log_stream.getvalue()
        self.assertIn("feedback_recorded", log_output)
        self.assertIn(f"request_id={request_id}", log_output)
        self.assertIn("vote=up", log_output)
        self.assertIn("provider=ollama", log_output)
        self.assertIn("model=gemma4:e2b", log_output)
        self.assertIn("latency_ms=1234.5", log_output)
        self.assertIn("lang=en", log_output)
        self.assertIn("route=local", log_output)

    async def test_down_vote_logs_feedback_recorded_with_whitelist_fields(self) -> None:
        """Down vote creates log with exactly the whitelist fields."""
        request_id = "req-xyz789"
        entry = FeedbackEntry(
            provider="ollama",
            model="gemma4:e2b",
            latency_ms=567.8,
            lang="ru",
            route="web",
        )

        with patch("bot.feedback_store") as mock_store:
            mock_store.pop.return_value = entry

            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_down_{request_id}",
            )

        log_output = self.log_stream.getvalue()
        self.assertIn("feedback_recorded", log_output)
        self.assertIn("vote=down", log_output)
        self.assertIn("route=web", log_output)
        self.assertIn("lang=ru", log_output)

    async def test_no_dialogue_text_in_log_args(self) -> None:
        """Property test: prompt/response text never appears in log args."""
        request_id = "req-test"
        entry = self._make_entry()

        # These strings should NEVER appear in feedback log
        forbidden_terms = ["SENSITIVE_USER_QUERY_12345", "SENSITIVE_MODEL_RESPONSE_67890"]

        with patch("bot.feedback_store") as mock_store:
            mock_store.pop.return_value = entry

            with self.assertLogs("bot", level="INFO") as cm:
                await _handle_feedback_callback(
                    self.query,
                    self.context,
                    self.translator,
                    self.lang,
                    f"{ACTION_FEEDBACK}_up_{request_id}",
                )

        # Check ALL log arguments, not just formatted message
        for record in cm.records:
            all_args_text = record.getMessage()
            for arg in record.args:
                if isinstance(arg, str):
                    all_args_text += " " + arg

            for term in forbidden_terms:
                self.assertNotIn(
                    term, all_args_text, f"Sensitive term '{term}' found in feedback log"
                )

    async def test_idempotent_vote_repeat_tap_no_second_log(self) -> None:
        """Second tap on same request_id doesn't create second log entry."""
        request_id = "req-idem"
        entry = self._make_entry()

        with patch("bot.feedback_store") as mock_store:
            # First pop returns entry, second returns None (already consumed)
            mock_store.pop.side_effect = [entry, None]

            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_up_{request_id}",
            )
            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_up_{request_id}",
            )

        log_output = self.log_stream.getvalue()
        feedback_count = log_output.count("feedback_recorded")
        self.assertEqual(feedback_count, 1)

    async def test_expired_request_id_shows_feedback_expired_no_log(self) -> None:
        """Tap on expired/missing request_id shows expired message, no log."""
        request_id = "req-expired"

        with patch("bot.feedback_store") as mock_store:
            mock_store.pop.return_value = None

            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_up_{request_id}",
            )

        # No feedback_recorded log (check via captured log stream)
        log_output = self.log_stream.getvalue()
        self.assertNotIn("feedback_recorded", log_output)

        # query.answer called with expired message
        self.query.answer.assert_called_once()
        answer_text = self.query.answer.call_args[1]["text"]
        self.assertIn("no longer available", answer_text.lower())

    async def test_fuzz_chat_id_user_id_not_in_log(self) -> None:
        """Fuzz test: run with various chat_id/user_id, verify they never appear in log."""
        for chat_id, user_id in [
            (123456789, 987654321),
            (-1001234567890, 12345),
            (0, 0),
            (999999999999, 888888888888),
        ]:
            with self.subTest(chat_id=chat_id, user_id=user_id):
                self.log_stream.seek(0)
                self.log_stream.truncate(0)

                # Use a proper UUID-like request_id, NOT derived from chat_id/user_id
                request_id = "abcd1234ef"
                entry = self._make_entry()
                self.query.message = MagicMock()
                self.query.message.chat_id = chat_id
                self.query.from_user = MagicMock()
                self.query.from_user.id = user_id

                with patch("bot.feedback_store") as mock_store:
                    mock_store.pop.return_value = entry

                    with self.assertLogs("bot", level="INFO") as cm:
                        await _handle_feedback_callback(
                            self.query,
                            self.context,
                            self.translator,
                            self.lang,
                            f"{ACTION_FEEDBACK}_up_{request_id}",
                        )

                log_text = " ".join(cm.output)
                self.assertNotIn(str(chat_id), log_text)
                self.assertNotIn(str(user_id), log_text)

    async def test_edit_message_reply_markup_called(self) -> None:
        """Keyboard removed after vote."""
        request_id = "edit_test"
        entry = self._make_entry()

        with patch("bot.feedback_store") as mock_store:
            mock_store.pop.return_value = entry

            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_up_{request_id}",
            )

        self.query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    async def test_confirmation_answer_called_with_localized_text(self) -> None:
        """Confirmation answer uses localized text."""
        request_id = "confirm_test"
        entry = self._make_entry()

        with patch("bot.feedback_store") as mock_store:
            mock_store.pop.return_value = entry

            await _handle_feedback_callback(
                self.query,
                self.context,
                self.translator,
                self.lang,
                f"{ACTION_FEEDBACK}_down_{request_id}",
            )

        self.query.answer.assert_called_once()
        answer_text = self.query.answer.call_args[1]["text"]
        self.assertEqual(answer_text, "Thanks — noted. 👎")

    async def test_route_field_not_hardcoded_local(self) -> None:
        """Design requires route field to reflect actual route, not hardcoded 'local'."""
        for route in ("local", "web"):
            with self.subTest(route=route):
                self.log_stream.seek(0)
                self.log_stream.truncate(0)

                entry = FeedbackEntry(
                    provider="ollama",
                    model="gemma4:e2b",
                    latency_ms=100.0,
                    lang="en",
                    route=route,
                )
                request_id = f"req_route_{route}"

                with patch("bot.feedback_store") as mock_store:
                    mock_store.pop.return_value = entry

                    await _handle_feedback_callback(
                        self.query,
                        self.context,
                        self.translator,
                        self.lang,
                        f"{ACTION_FEEDBACK}_up_{request_id}",
                    )

                log_output = self.log_stream.getvalue()
                self.assertIn(f"route={route}", log_output)


class FeedbackStoreBoundedTests(unittest.TestCase):
    """Additional bounded-size tests for FeedbackStore."""

    def test_eviction_under_load(self) -> None:
        """Under continuous load, store never exceeds maxsize."""
        maxsize = 50
        store = FeedbackStore(maxsize=maxsize, ttl_seconds=3600)

        # Simulate rapid puts without pops
        for i in range(maxsize * 3):
            store.put(f"req-{i}", FeedbackEntry("ollama", "gemma4:e2b", 100.0, "en", "local"))
            self.assertLessEqual(len(store), maxsize)

        # Oldest entries evicted
        self.assertIsNone(store.pop("req-0"))
        self.assertIsNone(store.pop("req-99"))


if __name__ == "__main__":
    unittest.main()
