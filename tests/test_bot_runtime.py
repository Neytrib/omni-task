"""Polling delivery semantics and safe HTTP transport behavior without external calls."""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates
from conftest import BOT_KEY
from pydantic import ValidationError

from bot.app.api import APIClient, APIError, APIUnavailable
from bot.app.config import BotSettings
from bot.app.polling import PollingStatus, configure_commands, run_polling


class PollingBot:
    def __init__(self, stop, responses):
        self.stop = stop
        self.responses = iter(responses)
        self.calls = []

    async def get_updates(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses, None)
        if isinstance(response, Exception):
            raise response
        if response is None:
            self.stop.set()
            return []
        return response


def test_polling_processes_in_order_and_advances_offset_only_after_success():
    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        bot = PollingBot(stop, [[SimpleNamespace(update_id=10), SimpleNamespace(update_id=11)]])
        processed = []

        class Dispatcher:
            async def feed_update(self, bot, update):
                processed.append(update.update_id)
                assert len(bot.calls) == 1

        await asyncio.wait_for(run_polling(bot, Dispatcher(), stop, status, retry_delay=0), 2)
        assert processed == [10, 11]
        assert [call["offset"] for call in bot.calls] == [None, 12]
        assert bot.calls[0]["allowed_updates"] == ["message", "callback_query"]
        assert status.state == "stopped"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [APIUnavailable(), RuntimeError("synthetic handler failure")])
def test_failed_handler_retries_same_update_before_new_poll(failure):
    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        update = SimpleNamespace(update_id=20)
        bot = PollingBot(stop, [[update]])
        processed = []

        class Dispatcher:
            async def feed_update(self, bot, received):
                assert received is update
                processed.append(received.update_id)
                assert len(bot.calls) == 1
                if len(processed) == 1:
                    raise failure

        await asyncio.wait_for(run_polling(bot, Dispatcher(), stop, status, retry_delay=0), 2)
        assert processed == [20, 20]
        assert [call["offset"] for call in bot.calls] == [None, 21]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_type", [TelegramUnauthorizedError, TelegramConflictError])
def test_fatal_polling_configuration_stops_without_exposing_exception(failure_type, caplog):
    caplog.set_level(logging.INFO)
    sentinel = "SYNTHETIC_PRIVATE_TELEGRAM_TOKEN_MUST_NOT_LOG"

    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        error = failure_type(method=GetUpdates(), message=sentinel)
        bot = PollingBot(stop, [error])
        await asyncio.wait_for(run_polling(bot, None, stop, status, retry_delay=0), 2)
        assert status.state == "error" and status.error_code == "telegram_configuration"
        assert len(bot.calls) == 1

    asyncio.run(scenario())
    assert sentinel not in caplog.text


def test_transport_retry_keeps_offset_and_never_logs_tokens(caplog):
    caplog.set_level(logging.INFO)
    sentinel = "SYNTHETIC_SENSITIVE_DOWNLOAD_URL"

    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        bot = PollingBot(stop, [TelegramNetworkError(method=GetUpdates(), message=sentinel), []])
        await asyncio.wait_for(run_polling(bot, None, stop, status, retry_delay=0), 2)
        assert [call["offset"] for call in bot.calls] == [None, None, None]

    asyncio.run(scenario())
    assert sentinel not in caplog.text


def test_stop_while_api_is_unavailable_does_not_confirm_failed_update():
    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        bot = PollingBot(stop, [[SimpleNamespace(update_id=40)]])

        class Dispatcher:
            async def feed_update(self, bot, update):
                stop.set()
                raise APIUnavailable()

        await asyncio.wait_for(run_polling(bot, Dispatcher(), stop, status, retry_delay=0), 2)
        assert len(bot.calls) == 1 and bot.calls[0]["offset"] is None
        assert status.state == "stopped"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [429, 500, 503])
def test_api_transient_status_is_retryable_without_echoing_bodies(status):
    async def scenario():
        def response(request):
            return httpx.Response(
                status, json={"error": {"code": "private_body", "message": "PRIVATE"}}
            )

        async with httpx.AsyncClient(
            base_url="http://api", transport=httpx.MockTransport(response)
        ) as client:
            with pytest.raises(APIUnavailable) as error:
                await APIClient(client).create_task(1, 1, "PRIVATE CONTENT")
            assert "PRIVATE" not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,path,status,body",
    [
        ("POST", "/internal/bot/tasks", 429, {"error": {"code": "voice_user_quota"}}),
        (
            "GET",
            "/internal/bot/processing-requests",
            429,
            {"error": {"code": "voice_user_quota"}},
        ),
        (
            "POST",
            "/internal/bot/processing-requests",
            503,
            {"error": {"code": "voice_user_quota"}},
        ),
        (
            "POST",
            "/internal/bot/processing-requests",
            429,
            {"error": {"code": "unknown_limit", "message": "PRIVATE"}},
        ),
        ("POST", "/internal/bot/processing-requests", 429, ["PRIVATE"]),
    ],
)
def test_only_voice_admission_rejections_are_terminal(method, path, status, body):
    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://api",
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
        ) as client:
            with pytest.raises(APIUnavailable) as error:
                await APIClient(client)._request(method, path)
            assert error.value.status == status
            assert "PRIVATE" not in str(error.value)

    asyncio.run(scenario())


def test_api_client_preserves_complete_content_actor_and_source_message():
    async def scenario():
        content = "  Full source\nՀայերեն 🧪 "
        calls = []

        def respond(request):
            import json

            calls.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "synthetic-task"})

        async with httpx.AsyncClient(
            base_url="http://api", transport=httpx.MockTransport(respond)
        ) as client:
            await APIClient(client).create_task(111, 222, content)
        assert calls == [{"telegram_user_id": 111, "message_id": 222, "content": content}]

    asyncio.run(scenario())


def test_api_client_rejects_forged_task_paths_before_http():
    async def scenario():
        def forbidden(request):
            raise AssertionError("Invalid task identifier must not reach HTTP.")

        async with httpx.AsyncClient(
            base_url="http://api", transport=httpx.MockTransport(forbidden)
        ) as client:
            with pytest.raises(APIError) as error:
                await APIClient(client).get_task(1, "../../login-links")
            assert error.value.status == 422

    asyncio.run(scenario())


def test_no_token_configuration_is_explicitly_disabled_and_secret_repr_is_redacted():
    settings = BotSettings(_env_file=None, bot_api_key=BOT_KEY, telegram_bot_token="")
    assert settings.telegram_bot_token is None
    assert BOT_KEY not in repr(settings)
    with pytest.raises(ValidationError) as error:
        BotSettings(_env_file=None, bot_api_key=BOT_KEY, telegram_bot_token="PRIVATE_BROKEN_TOKEN")
    assert "PRIVATE_BROKEN_TOKEN" not in str(error.value)


@pytest.mark.parametrize("failure_type", [TelegramForbiddenError, TelegramBadRequest])
def test_terminal_reply_error_does_not_block_later_updates(failure_type, caplog):
    sentinel = "SYNTHETIC_PRIVATE_REPLY_REJECTION"

    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        bot = PollingBot(stop, [[SimpleNamespace(update_id=50), SimpleNamespace(update_id=51)]])
        processed = []

        class Dispatcher:
            async def feed_update(self, bot, update):
                processed.append(update.update_id)
                if update.update_id == 50:
                    raise failure_type(method=GetUpdates(), message=sentinel)

        await asyncio.wait_for(run_polling(bot, Dispatcher(), stop, status, retry_delay=0), 2)
        assert processed == [50, 51]
        assert [call["offset"] for call in bot.calls] == [None, 52]

    asyncio.run(scenario())
    assert sentinel not in caplog.text


def test_private_command_setup_retries_safely_without_discarding_updates(caplog):
    sentinel = "SYNTHETIC_SECRET_SETUP_ERROR"

    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()

        class Bot:
            def __init__(self):
                self.calls = []

            async def set_my_commands(self, commands, **kwargs):
                self.calls.append((commands, kwargs))
                if len(self.calls) == 1:
                    raise TelegramNetworkError(method=GetUpdates(), message=sentinel)
                return True

        bot = Bot()
        assert await configure_commands(bot, stop, status, retry_delay=0)
        commands, kwargs = bot.calls[-1]
        assert [command.command for command in commands] == ["start", "help", "list", "profile"]
        assert kwargs["scope"].type == "all_private_chats"
        assert len(bot.calls) == 2

    asyncio.run(scenario())
    assert sentinel not in caplog.text


def test_polling_correlates_retries_and_success_without_logging_update_or_content(caplog):
    from uuid import UUID

    from omni_logging import current_correlation_id

    caplog.set_level(logging.INFO, logger="omni_task.bot")

    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        bot = PollingBot(stop, [[SimpleNamespace(update_id=70), SimpleNamespace(update_id=71)]])
        attempts = []

        class Dispatcher:
            async def feed_update(self, bot, update):
                attempts.append((update.update_id, current_correlation_id()))
                if len(attempts) == 1:
                    raise APIUnavailable()

        await asyncio.wait_for(run_polling(bot, Dispatcher(), stop, status, retry_delay=0), 2)
        assert [identifier for identifier, _ in attempts] == [70, 70, 71]
        assert attempts[0][1] == attempts[1][1]
        assert attempts[2][1] != attempts[0][1]
        for _, correlation in attempts:
            assert str(UUID(correlation)) == correlation
        events = [record.omni_event for record in caplog.records if hasattr(record, "omni_event")]
        retry = next(event for event in events if event["event"] == "bot_update_retry")
        handled = [event for event in events if event["event"] == "bot_update_handled"]
        assert retry["correlation_id"] == handled[0]["correlation_id"] == attempts[0][1]
        assert handled[1]["correlation_id"] == attempts[2][1]
        assert set(retry) == {"event", "correlation_id", "error_code", "outcome"}

    asyncio.run(scenario())


def test_bot_http_propagates_current_correlation_and_keeps_version_headers():
    from uuid import uuid4

    from omni_logging import correlation_context

    async def scenario():
        seen = []
        correlation, task_id = str(uuid4()), str(uuid4())

        def response(request):
            seen.append(request)
            return httpx.Response(200, json={"id": task_id})

        async with httpx.AsyncClient(
            base_url="http://api", transport=httpx.MockTransport(response)
        ) as client:
            api = APIClient(client)
            with correlation_context(correlation):
                await api.ensure_user(100, 100)
                await api.change_status(100, task_id, "completed", 3)
        assert all(request.headers["X-Correlation-ID"] == correlation for request in seen)
        assert seen[1].headers["If-Match"] == "3"
        assert all(correlation not in str(request.url) for request in seen)

    asyncio.run(scenario())
