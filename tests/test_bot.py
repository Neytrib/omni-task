"""Real aiogram dispatch and real PostgreSQL, with HTTP/Telegram transports isolated."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from itertools import count

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat, Message, Update, User
from conftest import BOT_KEY
from sqlalchemy import func, select

ACTOR = 810001
OTHER_ACTOR = 810002
FAKE_TOKEN = "123456:synthetic-test-token-never-sent-to-telegram"


class TelegramTransport(BaseSession):
    def __init__(self, timeline):
        super().__init__()
        self.calls = []
        self.messages = {}
        self.timeline = timeline
        self.identifiers = count(10000)
        self.fail_edit_once = None
        self.fail_send_once = False

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("No Telegram download is permitted in task-interface tests.")
        yield b""  # pragma: no cover

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls.append(method)
        self.timeline.append("telegram:" + name)
        if name == "getMe":
            return User(
                id=bot.id, is_bot=True, first_name="Synthetic Bot", username="synthetic_bot"
            )
        if name == "answerCallbackQuery":
            return True
        if name not in {"sendMessage", "editMessageText", "editMessageReplyMarkup"}:
            raise AssertionError(f"Unexpected external method {name}.")
        if name == "sendMessage" and self.fail_send_once:
            self.fail_send_once = False
            raise TelegramForbiddenError(method=method, message="Synthetic blocked bot")
        if name == "editMessageText" and self.fail_edit_once:
            error, self.fail_edit_once = self.fail_edit_once, None
            raise TelegramBadRequest(method=method, message=error)
        chat_id = int(method.chat_id)
        message_id = next(self.identifiers) if name == "sendMessage" else method.message_id
        previous = self.messages.get((chat_id, message_id))
        text = getattr(method, "text", None) or (previous.text if previous else "")
        if name != "editMessageReplyMarkup":
            assert len(text.encode("utf-16-le")) // 2 <= 4096
            assert self.prepare_value(method.parse_mode, bot=bot, files={}) is None
            preview = self.prepare_value(method.link_preview_options, bot=bot, files={})
            if isinstance(preview, str):
                preview = json.loads(preview)
            assert preview and preview["is_disabled"] is True
        markup = method.reply_markup
        if markup:
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data:
                        assert len(button.callback_data.encode()) <= 64
        message = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="Synthetic Bot"),
            text=text,
            reply_markup=markup,
        )
        self.messages[(chat_id, message_id)] = message
        return message.as_(bot)


class APITransport(httpx.AsyncBaseTransport):
    def __init__(self, app, timeline):
        self.transport = httpx.ASGITransport(app=app)
        self.calls = []
        self.timeline = timeline
        self.failure = None

    async def handle_async_request(self, request):
        self.calls.append(request)
        self.timeline.append(f"api:{request.method}:{request.url.path}")
        assert request.headers["authorization"] == f"Bearer {BOT_KEY}"
        if self.failure and request.url.path == self.failure[0]:
            _, status, code = self.failure
            self.failure = None
            if status is None:
                raise httpx.ConnectError("synthetic sensitive transport failure", request=request)
            return httpx.Response(status, json={"error": {"code": code, "message": "Synthetic"}})
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


class Harness:
    def __init__(self, dispatcher, bot, telegram, api_transport, timeline):
        self.dispatcher = dispatcher
        self.bot = bot
        self.telegram = telegram
        self.api_transport = api_transport
        self.timeline = timeline
        self.updates = count(1)
        self.messages = count(1)

    async def send(
        self,
        text=None,
        *,
        actor=ACTOR,
        message_id=None,
        kind="message",
        chat_type="private",
        **content,
    ):
        fields = {
            "message_id": next(self.messages) if message_id is None else message_id,
            "date": datetime.now(UTC),
            "chat": {"id": actor if chat_type == "private" else -123, "type": chat_type},
            "from_user": {"id": actor, "is_bot": False, "first_name": "Synthetic User"},
            **content,
        }
        if text is not None:
            fields["text"] = text
            if text.startswith("/"):
                fields["entities"] = [
                    {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
                ]
        update = Update(update_id=next(self.updates), **{kind: Message.model_validate(fields)})
        await self.dispatcher.feed_update(self.bot, update)

    async def press(
        self, message, label=None, *, actor=ACTOR, data=None, chat_id=None, message_id=None
    ):
        if data is None:
            data = self.button(message, label).callback_data
        callback_message = message.model_copy(
            update={
                "chat": Chat(id=actor if chat_id is None else chat_id, type="private"),
                "message_id": message.message_id if message_id is None else message_id,
            }
        )
        update = Update.model_validate(
            {
                "update_id": next(self.updates),
                "callback_query": {
                    "id": "synthetic-callback-" + str(next(self.updates)),
                    "from_user": {"id": actor, "is_bot": False, "first_name": "Synthetic User"},
                    "chat_instance": "synthetic-chat-instance",
                    "message": callback_message,
                    "data": data,
                },
            }
        )
        await self.dispatcher.feed_update(self.bot, update)

    def latest(self, actor=ACTOR):
        messages = [value for (chat, _), value in self.telegram.messages.items() if chat == actor]
        assert messages, "Expected a Telegram response."
        return messages[-1]

    def refreshed(self, message):
        return self.telegram.messages[(message.chat.id, message.message_id)]

    @staticmethod
    def buttons(message):
        return (
            [button for row in message.reply_markup.inline_keyboard for button in row]
            if message.reply_markup
            else []
        )

    @classmethod
    def button(cls, message, label):
        for button in cls.buttons(message):
            if label.lower() in button.text.lower():
                return button
        raise AssertionError(f"Missing requested button {label!r}.")


@asynccontextmanager
async def bot_harness(app, **overrides):
    from bot.app.api import APIClient
    from bot.app.config import BotSettings
    from bot.app.handlers import create_dispatcher

    timeline = []
    telegram = TelegramTransport(timeline)
    transport = APITransport(app, timeline)
    settings = BotSettings(
        _env_file=None, api_base_url="http://api", bot_api_key=BOT_KEY, **overrides
    )
    async with httpx.AsyncClient(
        base_url="http://api", transport=transport, headers={"Authorization": f"Bearer {BOT_KEY}"}
    ) as client:
        bot = Bot(FAKE_TOKEN, session=telegram)
        dispatcher = create_dispatcher(APIClient(client), settings)
        try:
            yield Harness(dispatcher, bot, telegram, transport, timeline)
        finally:
            await bot.session.close()


def owned_tasks(api, bot_headers, actor=ACTOR):
    response = api.get(
        "/internal/bot/tasks", headers=bot_headers, params={"telegram_user_id": actor}
    )
    assert response.status_code == 200
    return response.json()["items"]


def test_start_help_and_profile_do_not_create_tasks_or_accept_dashboard(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            expected_start = (
                "Welcome to Omni Task — your private task manager.\n\n"
                "Send one text message to create one Pending task. "
                "Your full message is preserved; buttons change its status.\n\n"
                "/help — Inputs and commands"
            )
            for _ in range(2):
                await bot.send("/start")
                assert bot.latest().text == expected_start
            await bot.send("/help")
            help_text = bot.latest().text.lower()
            assert all(command in help_text for command in ("/start", "/list", "/profile", "/help"))
            assert all(
                explanation in help_text
                for explanation in (
                    "voice",
                    "background",
                    "complete transcript",
                    "previous",
                    "next",
                    "back to tasks",
                    "read full text",
                    "in progress",
                    "completed",
                    "confirm delete",
                    "cancel",
                    "private chats",
                    "short-lived",
                    "no registration or login code",
                )
            )
            assert "welcome to omni task" not in help_text
            assert "send one text message to create one pending task" not in help_text
            assert "configured voice limits" not in help_text and "bytes" not in help_text
            assert "19,000,000" not in help_text
            assert "/dashboard" not in help_text
            await bot.send("/profile")
            profile = bot.latest()
            button = bot.button(profile, "dashboard")
            assert button.url and "/login#token=" in button.url
            assert "Request /profile again" in profile.text
            calls_before = len(bot.api_transport.calls)
            await bot.send("/dashboard")
            unknown = bot.latest()
            assert "Unknown command" in unknown.text
            assert "/dashboard" not in unknown.text
            assert not any(button.url for button in bot.buttons(unknown))
            assert len(bot.api_transport.calls) == calls_before
            assert owned_tasks(api, bot_headers) == []

    asyncio.run(scenario())


def test_text_preserves_content_and_duplicate_update_is_one_task(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            content = "  Keep <b>literal markup</b>\nՀայերեն 🧪 with https://example.test  "
            await bot.send(content, message_id=77)
            confirmation = bot.latest()
            for label in ("Pending", "In Progress", "Completed", "Delete task"):
                assert bot.button(confirmation, label).callback_data
            assert bot.button(confirmation, "Delete task").style == "danger"
            assert all("Open task" != button.text for button in bot.buttons(confirmation))
            assert all(
                button.style is None
                for button in bot.buttons(confirmation)
                if button.text != "Delete task"
            )
            await bot.send(content, message_id=77)
            tasks = owned_tasks(api, bot_headers)
            assert len(tasks) == 1 and tasks[0]["content"] == content
            await bot.send(content, message_id=78)
            assert len(owned_tasks(api, bot_headers)) == 2

    asyncio.run(scenario())


def test_details_status_noop_delete_cancel_confirm_and_redelivery(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Complete detail body\nSecond line", message_id=55)
            await bot.send("/list")
            listing = bot.latest()
            before = len(bot.timeline)
            await bot.press(listing, "Complete detail body")
            assert bot.timeline[before] == "telegram:answerCallbackQuery"
            details = bot.refreshed(listing)
            assert "Complete detail body\nSecond line" in details.text
            await bot.press(details, "In Progress")
            assert owned_tasks(api, bot_headers)[0]["status"] == "in_progress"
            details = bot.refreshed(details)
            version = owned_tasks(api, bot_headers)[0]["version"]
            await bot.press(details, "In Progress")
            assert owned_tasks(api, bot_headers)[0]["version"] == version
            details = bot.refreshed(details)
            assert bot.button(details, "Delete").style == "danger"
            await bot.press(details, "Delete")
            assert len(owned_tasks(api, bot_headers)) == 1
            confirmation = bot.refreshed(details)
            assert bot.button(confirmation, "Confirm").style == "danger"
            assert bot.button(confirmation, "Cancel").style is None
            cancelled_confirmation = bot.button(confirmation, "Confirm").callback_data
            await bot.press(confirmation, "Cancel")
            assert len(owned_tasks(api, bot_headers)) == 1
            await bot.press(confirmation, data=cancelled_confirmation)
            assert len(owned_tasks(api, bot_headers)) == 1
            details = bot.refreshed(details)
            await bot.press(details, "Delete")
            confirmation = bot.refreshed(details)
            confirm_data = bot.button(confirmation, "Confirm").callback_data
            await bot.press(confirmation, data=confirm_data)
            assert owned_tasks(api, bot_headers) == []
            await bot.press(confirmation, data=confirm_data)
            await bot.send("Complete detail body\nSecond line", message_id=55)
            assert owned_tasks(api, bot_headers) == []

    asyncio.run(scenario())


def test_creation_delete_confirms_current_version_and_rejects_stale_confirmation(
    app, api, bot_headers
):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("Delete only after reviewing the current task")
            saved = bot.latest()
            deletion = bot.button(saved, "Delete task")
            assert deletion.model_dump(exclude_none=True)["style"] == "danger"
            original = owned_tasks(api, bot_headers)[0]

            def change_status(status, version):
                response = api.patch(
                    f"/internal/bot/tasks/{original['id']}",
                    params={"telegram_user_id": ACTOR},
                    headers={**bot_headers, "If-Match": str(version)},
                    json={"status": status},
                )
                assert response.status_code == 200
                return response.json()

            changed = change_status("in_progress", original["version"])
            await bot.press(saved, data=deletion.callback_data)
            prompt = bot.refreshed(saved)
            assert "Delete this task?" in prompt.text
            assert len(owned_tasks(api, bot_headers)) == 1
            assert not any(request.method == "DELETE" for request in bot.api_transport.calls)
            confirm = bot.button(prompt, "Confirm delete")
            action = bot.dispatcher["navigation"].resolve(
                confirm.callback_data, ACTOR, ACTOR, prompt.message_id
            )
            assert action.version == changed["version"]
            assert confirm.style == "danger" and bot.button(prompt, "Cancel").style is None
            await bot.press(prompt, "Cancel")
            assert original["content"] in bot.refreshed(prompt).text
            calls = len(bot.api_transport.calls)
            await bot.press(prompt, data=confirm.callback_data)
            assert len(bot.api_transport.calls) == calls

            await bot.press(saved, data=deletion.callback_data)
            prompt = bot.refreshed(saved)
            stale_confirm = bot.button(prompt, "Confirm delete").callback_data
            newest = change_status("completed", changed["version"])
            await bot.press(prompt, data=stale_confirm)
            assert owned_tasks(api, bot_headers)[0] == newest
            current = bot.refreshed(prompt)
            assert "changed" in current.text.lower()
            calls = len(bot.api_transport.calls)
            await bot.press(prompt, data=stale_confirm)
            assert len(bot.api_transport.calls) == calls
            await bot.press(current, "Delete")
            prompt = bot.refreshed(current)
            await bot.press(prompt, "Confirm delete")
            assert owned_tasks(api, bot_headers) == []
            calls = len(bot.api_transport.calls)
            await bot.press(prompt, "Confirm delete")
            assert len(bot.api_transport.calls) == calls

    asyncio.run(scenario())


def test_twelve_tasks_have_previous_next_and_back(app, api, bot_headers, make_task):
    for number in range(12):
        make_task(content=f"Pagination item {number:02d}")

    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/list")
            first = bot.latest()
            assert (
                len([button for button in bot.buttons(first) if "Pagination item" in button.text])
                == 5
            )
            await bot.press(first, "Next")
            second = bot.refreshed(first)
            second_text = second.text
            await bot.press(second, "Pagination item")
            await bot.press(bot.refreshed(second), "Back")
            assert bot.refreshed(second).text == second_text
            await bot.press(bot.refreshed(second), "Next")
            third = bot.refreshed(second)
            assert (
                len([button for button in bot.buttons(third) if "Pagination item" in button.text])
                == 2
            )
            assert not any("Next" in button.text for button in bot.buttons(third))
            await bot.press(third, "Previous")
            assert bot.refreshed(third).text == second_text
            assert len(owned_tasks(api, bot_headers)) == 12

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,chat_type",
    [("message", "group"), ("message", "supergroup"), ("edited_message", "private")],
)
def test_group_and_edited_messages_are_ignored(app, db_engine, db_tables, kind, chat_type):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("Ignored message", kind=kind, chat_type=chat_type)
            assert bot.api_transport.calls == []
            assert bot.telegram.calls == []

    asyncio.run(scenario())
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db_tables["tasks"])) == 0


@pytest.mark.parametrize(
    "content",
    [
        {
            "photo": [
                {
                    "file_id": "synthetic_photo",
                    "file_unique_id": "synthetic_unique",
                    "width": 20,
                    "height": 20,
                }
            ],
            "caption": "Do not create from captions",
        },
        {
            "document": {
                "file_id": "synthetic_doc",
                "file_unique_id": "synthetic_unique",
                "file_name": "sample.txt",
            }
        },
        {
            "voice": {
                "file_id": "synthetic_voice",
                "file_unique_id": "synthetic_unique",
                "duration": 999999,
                "file_size": 999999999,
            }
        },
    ],
    ids=["photo", "document", "oversized-voice"],
)
def test_unsupported_media_and_voice_do_not_promise_processing(app, api, bot_headers, content):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send(**content)
            response = bot.latest().text.lower()
            assert "queuing" not in response and "transcribing" not in response
            assert owned_tasks(api, bot_headers) == []
            assert not any(
                "processing-requests" in request.url.path for request in bot.api_transport.calls
            )

    asyncio.run(scenario())


def test_unknown_commands_never_become_tasks(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("/unknown do something")
            assert "/help" in bot.latest().text
            assert owned_tasks(api, bot_headers) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("forgery", ["actor", "message", "handle"])
def test_forged_callback_cannot_read_or_mutate_other_users(app, api, bot_headers, forgery):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Owner-only sensitive synthetic body")
            message = bot.latest()
            before = len(bot.api_transport.calls)
            if forgery == "actor":
                await bot.press(message, "Delete task", actor=OTHER_ACTOR)
            elif forgery == "message":
                await bot.press(message, "Delete task", message_id=message.message_id + 100)
            else:
                await bot.press(message, data="forged:open:00000000")
            assert len(bot.api_transport.calls) == before
            assert owned_tasks(api, bot_headers)[0]["status"] == "pending"
            assert bot.telegram.calls[-1].__api_method__ == "answerCallbackQuery"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content",
    ["", " \n\t", "invalid\x00body", "x" * 50001],
    ids=["empty", "spaces", "nul", "oversized"],
)
def test_invalid_text_never_creates_or_shortens_task(app, api, bot_headers, content):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send(content)
            assert "not" in bot.latest().text.lower()
            assert owned_tasks(api, bot_headers) == []

    asyncio.run(scenario())


def test_long_non_bmp_details_are_safe_preview_with_dashboard_access(
    app, api, bot_headers, make_task
):
    content = "🧪" * 3000 + "\nFull ending retained only in storage"
    make_task(content=content)

    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/list")
            message = bot.latest()
            task_button = next(button for button in bot.buttons(message) if "🧪" in button.text)
            await bot.press(message, data=task_button.callback_data)
            detail = bot.refreshed(message)
            assert "preview" in detail.text.lower()
            assert len(detail.text.encode("utf-16-le")) // 2 <= 4096
            assert owned_tasks(api, bot_headers)[0]["content"] == content
            await bot.press(detail, "Read full text")
            first_page = bot.refreshed(detail)
            complete = first_page.text.split("\n\n", 1)[1]
            await bot.press(first_page, "Next text")
            second_page = bot.refreshed(first_page)
            complete += second_page.text.split("\n\n", 1)[1]
            assert complete == content
            await bot.press(second_page, "Back to task")
            detail = bot.refreshed(second_page)
            await bot.press(detail, "Dashboard")
            dashboard = bot.refreshed(detail)
            assert bot.button(dashboard, "Dashboard").url

    asyncio.run(scenario())


def test_stale_status_control_refreshes_current_task_without_overwrite(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Concurrent dashboard update")
            message = bot.latest()
            original = owned_tasks(api, bot_headers)[0]
            changed = api.patch(
                f"/internal/bot/tasks/{original['id']}",
                params={"telegram_user_id": ACTOR},
                headers={**bot_headers, "If-Match": str(original["version"])},
                json={"status": "completed"},
            )
            assert changed.status_code == 200
            await bot.press(message, "In Progress")
            assert owned_tasks(api, bot_headers)[0]["status"] == "completed"
            assert "changed" in bot.refreshed(message).text.lower()

    asyncio.run(scenario())


def test_expired_and_restarted_controls_make_no_api_requests(app):
    from bot.app.handlers import create_dispatcher

    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Navigation expiry")
            message = bot.latest()
            before = len(bot.api_transport.calls)
            for control in bot.dispatcher["navigation"].controls.values():
                control.expires_at = 0
            await bot.press(message, "Delete task")
            assert len(bot.api_transport.calls) == before
            interface = bot.dispatcher["interface"]
            bot.dispatcher = create_dispatcher(interface.api, interface.settings)
            await bot.press(message, "Delete task")
            assert len(bot.api_transport.calls) == before

    asyncio.run(scenario())


def test_backend_ownership_still_rejects_foreign_task_from_navigation(
    app, api, bot_headers, make_task
):
    from bot.app.navigation import Action

    foreign = make_task(user=1, content="Other owner's private task")

    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Own task")
            message = bot.latest()
            navigation = bot.dispatcher["navigation"]
            key = navigation.add(ACTOR, ACTOR, Action(kind="open", task_id=foreign["id"]))
            navigation.bind([key], ACTOR, ACTOR, message.message_id)
            await bot.press(message, data=key)
            assert "unavailable" in bot.refreshed(message).text.lower()
            assert foreign["content"] not in bot.refreshed(message).text
            assert len(owned_tasks(api, bot_headers)) == 1
            assert owned_tasks(api, bot_headers, OTHER_ACTOR)[0]["status"] == "pending"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [None, 503, 429])
def test_transient_creation_failure_is_honest_and_retries_original_source(
    app, api, bot_headers, status
):
    from bot.app.api import APIUnavailable

    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            bot.api_transport.failure = ("/internal/bot/tasks", status, "api_unavailable")
            with pytest.raises(APIUnavailable):
                await bot.send("Retried source body", message_id=505)
            assert "retry" in bot.latest().text.lower()
            assert owned_tasks(api, bot_headers) == []
            await bot.send("Retried source body", message_id=505)
            assert len(owned_tasks(api, bot_headers)) == 1

    asyncio.run(scenario())


def test_api_authentication_failure_never_reports_creation(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            bot.api_transport.failure = ("/internal/bot/tasks", 401, "bot_unauthenticated")
            await bot.send("Must fail safely")
            assert "cannot authenticate" in bot.latest().text.lower()
            assert owned_tasks(api, bot_headers) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("error", ["message is not modified", "message to edit not found"])
def test_telegram_edit_recovery_is_safe(app, api, bot_headers, error):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Resilient detail control")
            await bot.send("/list")
            message = bot.latest()
            bot.telegram.fail_edit_once = error
            await bot.press(message, "Resilient detail control")
            assert len(owned_tasks(api, bot_headers)) == 1
            if error == "message to edit not found":
                replacement = bot.latest()
                assert replacement.message_id != message.message_id
                await bot.press(replacement, "Completed")
                assert owned_tasks(api, bot_headers)[0]["status"] == "completed"

    asyncio.run(scenario())


def test_leading_space_command_is_preserved_as_task_content(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            content = "  /help is part of my actual task"
            await bot.send(content)
            assert owned_tasks(api, bot_headers)[0]["content"] == content

    asyncio.run(scenario())


def test_failed_retry_notice_keeps_api_failure_retryable(app, api, bot_headers):
    from bot.app.api import APIUnavailable

    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            bot.api_transport.failure = ("/internal/bot/tasks", 503, "api_unavailable")
            bot.telegram.fail_send_once = True
            with pytest.raises(APIUnavailable):
                await bot.send("Not accepted yet", message_id=71)
            assert owned_tasks(api, bot_headers) == []
            await bot.send("Not accepted yet", message_id=71)
            assert len(owned_tasks(api, bot_headers)) == 1

    asyncio.run(scenario())


def test_undeliverable_confirmation_does_not_undo_durable_task(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            bot.telegram.fail_send_once = True
            with pytest.raises(TelegramForbiddenError):
                await bot.send("Accepted before reply", message_id=72)
            assert len(owned_tasks(api, bot_headers)) == 1
            await bot.send("Accepted before reply", message_id=72)
            assert len(owned_tasks(api, bot_headers)) == 1

    asyncio.run(scenario())


def test_cancel_fallback_to_new_message_revokes_old_confirmation(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as bot:
            await bot.send("/start")
            await bot.send("Cancel should remain final")
            message = bot.latest()
            await bot.press(message, "Delete task")
            confirmation = bot.refreshed(message)
            confirm_data = bot.button(confirmation, "Confirm").callback_data
            bot.telegram.fail_edit_once = "message to edit not found"
            await bot.press(confirmation, "Cancel")
            assert bot.latest().message_id != confirmation.message_id
            await bot.press(confirmation, data=confirm_data)
            assert len(owned_tasks(api, bot_headers)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["Wrong HTTP URL", "BUTTON_URL_INVALID"])
def test_profile_url_rejection_replies_without_leaking_link(app, caplog, reason):
    async def scenario():
        async with bot_harness(app) as bot:
            original = bot.telegram.make_request
            rejected_links = []

            async def reject_url_once(tg_bot, method, timeout=None):
                markup = getattr(method, "reply_markup", None)
                urls = [
                    button.url
                    for row in (markup.inline_keyboard if markup else [])
                    for button in row
                    if button.url
                ]
                if urls and not rejected_links:
                    rejected_links.extend(urls)
                    raise TelegramBadRequest(method=method, message=f"{reason}: {urls[0]}")
                return await original(tg_bot, method, timeout)

            bot.telegram.make_request = reject_url_once
            await bot.send("/profile")
            assert rejected_links
            assert "temporarily unavailable" in bot.latest().text
            assert "/profile" in bot.latest().text
            assert not bot.latest().reply_markup
            assert "dashboard_url_rejected" in caplog.text
            assert all(url not in caplog.text for url in rejected_links)
            assert "token=" not in caplog.text

    asyncio.run(scenario())
