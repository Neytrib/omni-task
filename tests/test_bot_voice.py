"""Voice interaction and private adapter tests use fake Telegram/network transports only."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import File, Update
from aiohttp import ClientConnectionError
from conftest import BOT_KEY
from fastapi import FastAPI
from test_bot import ACTOR, FAKE_TOKEN, OTHER_ACTOR, Harness, TelegramTransport, bot_harness

from bot.app.api import APIClient, APIError, APIUnavailable
from bot.app.config import BotSettings
from bot.app.handlers import create_dispatcher
from bot.app.navigation import Navigation
from bot.app.polling import PollingStatus, run_polling
from bot.app.voice import VoiceAdapter, install_voice_routes

IDENTIFIER = str(uuid4())
LEASE = str(uuid4())
VOICE = {"file_id": "stored-file", "file_unique_id": "unique", "duration": 5, "file_size": 15}
TASK = {
    "id": str(uuid4()),
    "title": "Complete transcript",
    "content": "Complete transcript",
    "status": "pending",
    "version": 1,
    "created_at": "2026-09-30T01:00:00Z",
}


class VoiceAPI:
    def __init__(self, timeline):
        self.timeline = timeline
        self.calls = []
        self.outcome = None
        self.fail_accept = False
        self.context_error = None
        self.checkpoint_fail = False
        self.deleted_after_context = False
        self.context = {
            "telegram_user_id": ACTOR,
            "file_id": "stored-file",
            "file_size": 15,
            "duration_seconds": 5,
            "acknowledgement_message_id": 10000,
            "sent_message_id": None,
            "state": "succeeded",
            "task": TASK,
            "provider_name": "fake",
        }

    async def ensure_user(self, actor, chat):
        self.timeline.append("api:ensure")

    async def create_processing_request(self, *args):
        self.calls.append(args)
        self.timeline.append("api:accept")
        if self.fail_accept:
            raise APIUnavailable()
        return self.outcome or {
            "id": IDENTIFIER,
            "state": "queued",
            "acknowledgement_message_id": args[-1],
        }

    async def get_task(self, actor, identifier):
        self.timeline.append("api:task")
        return TASK

    async def voice_context(self, identifier, lease, kind):
        self.calls.append((identifier, lease, kind))
        self.timeline.append("api:context")
        if self.context_error:
            raise self.context_error
        context = dict(self.context)
        if self.deleted_after_context:
            self.context.update(deleted=True, suppressed=True)
        return context

    async def notification_checkpoint(self, identifier, lease, message_id):
        self.timeline.append("api:checkpoint")
        if self.checkpoint_fail:
            self.checkpoint_fail = False
            raise APIUnavailable()
        self.context["sent_message_id"] = message_id
        return {}


class VoiceTelegram(TelegramTransport):
    def __init__(self, timeline):
        super().__init__(timeline)
        self.chunks = [b"synthetic", b" audio"]
        self.remote_size = 15
        self.path = "voice/file.ogg"
        self.download_error = None
        self.delay = 0
        self.closed_streams = 0
        self.delivery_error = None

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "getFile":
            self.calls.append(method)
            self.timeline.append("telegram:getFile")
            return File(
                file_id=method.file_id,
                file_unique_id="unique",
                file_size=self.remote_size,
                file_path=self.path,
            )
        if self.delivery_error:
            raise self.delivery_error
        return await super().make_request(bot, method, timeout)

    async def stream_content(self, url, **kwargs):
        self.timeline.append("telegram:download")
        assert "/voice/file.ogg" in url
        try:
            if self.download_error:
                raise self.download_error
            if self.delay:
                await asyncio.sleep(self.delay)
            for chunk in self.chunks:
                yield chunk
        finally:
            self.closed_streams += 1


@asynccontextmanager
async def unit_harness(**settings_override):
    timeline = []
    telegram = VoiceTelegram(timeline)
    api = VoiceAPI(timeline)
    settings = BotSettings(bot_api_key=BOT_KEY, **settings_override)
    bot = Bot(FAKE_TOKEN, session=telegram)
    dispatcher = create_dispatcher(api, settings)
    harness = Harness(dispatcher, bot, telegram, SimpleNamespace(calls=[]), timeline)
    adapter = VoiceAdapter(api, bot, settings, dispatcher["navigation"])
    app = FastAPI()
    app.state.configuration, app.state.voice_adapter = settings, adapter
    install_voice_routes(app)
    async with httpx.AsyncClient(
        base_url="http://bot",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {BOT_KEY}"},
    ) as client:
        try:
            yield harness, api, adapter, client
        finally:
            await bot.session.close()


def request_path(operation):
    return f"/internal/voice/{IDENTIFIER}/{operation}"


def test_receipt_precedes_every_api_call_and_carries_complete_source():
    async def scenario():
        async with unit_harness() as (harness, api, _, _):
            start = asyncio.get_running_loop().time()
            await harness.send(voice=VOICE, message_id=91)
            assert harness.timeline == ["telegram:sendMessage", "api:ensure", "api:accept"]
            assert asyncio.get_running_loop().time() - start < 2
            assert api.calls == [(ACTOR, 91, "stored-file", 5, 15, 10000)]
            assert "Queuing" in harness.latest().text

    asyncio.run(scenario())


def test_failed_ack_still_accepts_and_failure_feedback_does_not_confirm_unaccepted_source():
    async def scenario():
        async with unit_harness() as (harness, api, _, _):
            harness.telegram.fail_send_once = True
            await harness.send(voice=VOICE)
            assert api.calls[0][-1] is None
            api.fail_accept = True
            with pytest.raises(APIUnavailable):
                await harness.send(voice=VOICE)
            assert "could not confirm acceptance" in harness.latest().text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,explanation",
    [
        ("voice_user_busy", "already have voice recordings waiting"),
        ("voice_service_busy", "busy right now"),
        ("voice_user_quota", "your voice transcription limit"),
        ("voice_service_quota", "temporarily unavailable"),
    ],
)
@pytest.mark.parametrize("acknowledgement_fails", [False, True])
def test_voice_admission_rejection_resolves_receipt_and_polling_continues(
    code, explanation, acknowledgement_fails, caplog
):
    sentinel = "PRIVATE ADMISSION COUNTS MUST NOT APPEAR"

    async def scenario():
        stop, status = asyncio.Event(), PollingStatus()
        api_calls, offsets = [], []
        updates = [
            Update.model_validate(
                {
                    "update_id": identifier,
                    "message": {
                        "message_id": identifier,
                        "date": 1790726400,
                        "chat": {"id": actor, "type": "private"},
                        "from_user": {"id": actor, "is_bot": False, "first_name": "Synthetic"},
                        **content,
                    },
                }
            )
            for identifier, actor, content in (
                (1, ACTOR, {"voice": VOICE}),
                (2, OTHER_ACTOR, {"text": "/help"}),
                (3, OTHER_ACTOR, {"text": "Synthetic next user's task"}),
            )
        ]

        class PollingTelegram(VoiceTelegram):
            async def make_request(self, bot, method, timeout=None):
                if method.__api_method__ == "getUpdates":
                    offsets.append(method.offset)
                    if len(offsets) == 1:
                        return updates
                    stop.set()
                    return []
                return await super().make_request(bot, method, timeout)

        def respond(request):
            api_calls.append((request.url.path, json.loads(request.content)))
            if request.url.path == "/internal/bot/processing-requests":
                return httpx.Response(429, json={"error": {"code": code, "message": sentinel}})
            if request.url.path == "/internal/bot/users":
                return httpx.Response(200, json={})
            assert request.url.path == "/internal/bot/tasks"
            return httpx.Response(201, json=TASK)

        telegram = PollingTelegram([])
        telegram.fail_send_once = acknowledgement_fails
        bot = Bot(FAKE_TOKEN, session=telegram)
        try:
            async with httpx.AsyncClient(
                base_url="http://api", transport=httpx.MockTransport(respond)
            ) as client:
                dispatcher = create_dispatcher(APIClient(client), BotSettings(bot_api_key=BOT_KEY))
                await asyncio.wait_for(run_polling(bot, dispatcher, stop, status, retry_delay=0), 2)
                assert not dispatcher["interface"].voice_receipts
        finally:
            await bot.session.close()
        assert offsets == [None, 4]
        assert status.state == "stopped"
        voice_calls = [body for path, body in api_calls if path.endswith("processing-requests")]
        assert len(voice_calls) == 1
        assert (voice_calls[0]["acknowledgement_message_id"] is None) == acknowledgement_fails
        text_calls = [body for path, body in api_calls if path.endswith("/tasks")]
        assert text_calls == [
            {
                "telegram_user_id": OTHER_ACTOR,
                "message_id": 3,
                "content": "Synthetic next user's task",
            }
        ]
        replies = [message for (actor, _), message in telegram.messages.items() if actor == ACTOR]
        assert len(replies) == 1
        assert explanation in replies[0].text
        assert "No task was queued. Send text instead or try again later." in replies[0].text
        assert "Queuing" not in replies[0].text and sentinel not in replies[0].text
        other_replies = [
            message.text
            for (actor, _), message in telegram.messages.items()
            if actor == OTHER_ACTOR
        ]
        assert len(other_replies) == 2
        assert "Creating tasks" in other_replies[0]
        assert "Task saved" in other_replies[1]

    asyncio.run(scenario())
    assert sentinel not in caplog.text


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ({"state": "processing"}, "already queued or processing"),
        (
            {"state": "succeeded", "task_id": TASK["id"], "provider_name": "fake"},
            "[Demo transcription]",
        ),
        ({"state": "failed", "error_code": "empty_transcript"}, "No speech text"),
        ({"state": "succeeded", "deleted": True}, "was deleted"),
    ],
)
def test_duplicate_receipt_resolves_existing_outcome(outcome, expected):
    async def scenario():
        async with unit_harness() as (harness, api, _, _):
            api.outcome = {"id": IDENTIFIER, "acknowledgement_message_id": 789, **outcome}
            await harness.send(voice=VOICE)
            assert expected in harness.latest().text
            assert "Queuing" not in harness.latest().text
            assert harness.telegram.calls[-1].__api_method__ == "editMessageText"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "voice", [dict(VOICE, duration=0), dict(VOICE, duration=601), dict(VOICE, file_size=19000001)]
)
def test_voice_metadata_limits_prevent_acceptance(voice):
    async def scenario():
        async with unit_harness() as (harness, api, _, _):
            await harness.send(voice=voice)
            assert not api.calls
            assert "No task was created" in harness.latest().text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Authorization": "Bearer forged"}, 401),
        ({"Authorization": ""}, 401),
        ({"Origin": "http://browser"}, 403),
        ({"Cookie": "session=browser"}, 403),
    ],
)
def test_adapter_auth_before_even_reading_body(headers, expected):
    async def scenario():
        async with unit_harness() as (_, api, _, client):

            async def prohibited_body():
                raise AssertionError("Unauthenticated body must not be consumed")
                yield b"never"

            response = await client.post(
                request_path("audio"), headers=headers, content=prohibited_body()
            )
            assert response.status_code == expected
            assert not api.calls

    asyncio.run(scenario())


def test_adapter_rejects_caller_targets_bad_leases_and_wrong_owner_before_telegram():
    async def scenario():
        async with unit_harness() as (harness, api, _, client):
            for body in (
                {"lease_token": LEASE, "file_id": "forged"},
                {"lease_token": "private-invalid-token"},
            ):
                response = await client.post(request_path("audio"), json=body)
                assert response.status_code == 422
                assert "private-invalid-token" not in response.text
            api.context_error = APIError(409, "invalid_lease")
            response = await client.post(request_path("audio"), json={"lease_token": LEASE})
            assert response.status_code == 409
            assert not harness.telegram.calls

    asyncio.run(scenario())


def test_audio_uses_stored_file_identity_and_bounds_actual_bytes():
    async def scenario():
        async with unit_harness(voice_max_bytes=20) as (harness, api, _, client):
            response = await client.post(request_path("audio"), json={"lease_token": LEASE})
            assert response.status_code == 200 and response.content == b"synthetic audio"
            assert harness.telegram.calls[-1].file_id == "stored-file"
            assert api.calls == [(IDENTIFIER, LEASE, "download")]
            harness.telegram.chunks = [b"a" * 15, b"b" * 15]
            response = await client.post(request_path("audio"), json={"lease_token": LEASE})
            assert response.status_code == 413
            assert response.json()["error"]["code"] == "audio_too_large"
            assert harness.telegram.closed_streams == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,expected",
    [("missing", 404), ("empty", 422), ("timeout", 503), ("metadata", 413), ("unsafe_path", 422)],
)
def test_download_failure_categories(kind, expected):
    async def scenario():
        async with unit_harness() as (harness, api, adapter, client):
            if kind == "missing":
                harness.telegram.path = None
            elif kind == "empty":
                harness.telegram.chunks = []
            elif kind == "timeout":
                # Only the injected test settings bypass the public >=1 second validator.
                adapter.settings = adapter.settings.model_copy(
                    update={"voice_download_timeout_seconds": 0.01}
                )
                harness.telegram.delay = 0.1
            elif kind == "metadata":
                api.context["file_size"] = 19000001
            else:
                harness.telegram.path = "https://forged.example/file"
            response = await client.post(request_path("audio"), json={"lease_token": LEASE})
            assert response.status_code == expected

    asyncio.run(scenario())


def test_success_edits_canonical_receipt_with_bound_controls_and_demo_disclosure():
    async def scenario():
        async with unit_harness() as (harness, api, _, client):
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.json() == {"outcome": "sent", "message_id": 10000}
            confirmation = harness.latest()
            assert confirmation.text.startswith("[Demo transcription]")
            labels = [button.text for button in harness.buttons(confirmation)]
            assert all(
                any(label in text for text in labels)
                for label in ("Pending", "In Progress", "Completed")
            )
            button = harness.button(confirmation, "Delete task")
            assert button.style == "danger" and "Open task" not in labels
            action = harness.dispatcher["navigation"].resolve(
                button.callback_data, ACTOR, ACTOR, 10000
            )
            assert action.kind == "delete" and action.version == TASK["version"]
            assert action.task_id == TASK["id"]
            assert not harness.dispatcher["navigation"].resolve(
                button.callback_data, ACTOR + 1, ACTOR, 10000
            )
            assert api.timeline.count("telegram:sendMessage") == 0

    asyncio.run(scenario())


def test_notification_failure_is_safe_and_deleted_content_is_suppressed():
    async def scenario():
        async with unit_harness() as (harness, api, _, client):
            api.context.update(state="failed", error_code="provider_timeout-secret-provider-url")
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.status_code == 200
            assert "No task was created" in harness.latest().text
            assert "secret" not in harness.latest().text
            api.context.update(deleted=True, suppressed=True)
            count = len(harness.telegram.calls)
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.json() == {"outcome": "suppressed", "message_id": None}
            assert len(harness.telegram.calls) == count

    asyncio.run(scenario())


def test_missing_receipt_fallback_is_checkpointed_before_done_and_retries_edit_it():
    async def scenario():
        async with unit_harness() as (harness, api, _, client):
            harness.telegram.fail_edit_once = "Bad Request: message to edit not found"
            api.checkpoint_fail = True
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.status_code == 503
            assert api.timeline[-2:] == ["telegram:sendMessage", "api:checkpoint"]
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.status_code == 200
            assert api.timeline.count("telegram:sendMessage") == 1
            assert api.context["sent_message_id"] == response.json()["message_id"]
            assert api.timeline[-2:] == ["api:checkpoint", "telegram:editMessageText"]

    asyncio.run(scenario())


def test_fallback_rechecks_deletion_and_unchanged_is_delivered():
    async def scenario():
        async with unit_harness() as (harness, api, _, client):
            harness.telegram.fail_edit_once = "Bad Request: message is not modified"
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.status_code == 200
            harness.telegram.fail_edit_once = "Bad Request: message can't be edited"
            api.deleted_after_context = True
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.json()["outcome"] == "suppressed"
            assert "telegram:sendMessage" not in api.timeline

    asyncio.run(scenario())


def test_concurrent_missing_ack_notifications_send_once():
    async def scenario():
        async with unit_harness() as (harness, api, _, client):
            api.context["acknowledgement_message_id"] = None
            responses = await asyncio.gather(
                *[
                    client.post(request_path("notify"), json={"lease_token": LEASE})
                    for _ in range(4)
                ]
            )
            assert all(response.status_code == 200 for response in responses)
            assert api.timeline.count("telegram:sendMessage") == 1
            assert api.timeline.count("api:checkpoint") == 1

    asyncio.run(scenario())


def test_api_client_voice_contract_has_no_credentials_in_urls():
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            base_url="http://api", transport=httpx.MockTransport(handler)
        ) as client:
            api = APIClient(client)
            await api.create_processing_request(ACTOR, 42, "file", 5, None, 90)
            await api.voice_context(IDENTIFIER, LEASE, "download")
            await api.voice_context(IDENTIFIER, LEASE, "notification")
            await api.notification_checkpoint(IDENTIFIER, LEASE, 91)
        assert all(request.method == "POST" and not request.url.query for request in calls)
        assert all(LEASE not in str(request.url) for request in calls)

    asyncio.run(scenario())


def test_real_voice_intake_preserves_canonical_receipt_on_redelivery(app, api, bot_headers):
    async def scenario():
        async with bot_harness(app) as harness:
            await harness.send(voice=VOICE, message_id=87)
            first = harness.latest()
            await harness.send(voice=VOICE, message_id=87)
            assert "already queued or processing" in harness.latest().text
            result = api.post(
                "/internal/bot/processing-requests",
                headers=bot_headers,
                json={
                    "telegram_user_id": ACTOR,
                    "message_id": 87,
                    "file_id": "stored-file",
                    "duration_seconds": 5,
                    "file_size": 15,
                    "acknowledgement_message_id": 999,
                },
            )
            assert result.status_code in (200, 201)
            assert result.json()["acknowledgement_message_id"] == first.message_id

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["network", "rate_limit", "download_connection"])
def test_transport_errors_are_retryable_and_do_not_expose_credentials(kind, caplog):
    async def scenario():
        async with unit_harness() as (harness, _, _, client):
            secret = "bot-token-sensitive-transcript"
            method = SendMessage(chat_id=ACTOR, text="Synthetic")
            if kind == "network":
                harness.telegram.delivery_error = TelegramNetworkError(
                    method=method, message=secret
                )
            elif kind == "rate_limit":
                harness.telegram.delivery_error = TelegramRetryAfter(
                    method=method, message=secret, retry_after=17
                )
            else:
                harness.telegram.download_error = ClientConnectionError(secret)
            response = await client.post(
                request_path("audio" if kind == "download_connection" else "notify"),
                json={"lease_token": LEASE},
            )
            assert response.status_code == 503
            assert secret not in response.text and secret not in caplog.text
            if kind == "rate_limit":
                assert response.headers["Retry-After"] == "17"

    asyncio.run(scenario())


def test_real_api_notification_checkpoint_restart_latest_status_and_deletion(
    app,
    api,
    bot_headers,
    settings,
):
    from uuid import UUID

    from app import voice
    from test_voice_domain import complete

    async def scenario():
        async with bot_harness(app) as harness:
            await harness.send(voice=VOICE, message_id=199)
            result = api.post(
                "/internal/bot/processing-requests",
                headers=bot_headers,
                json={
                    "telegram_user_id": ACTOR,
                    "message_id": 199,
                    "file_id": "stored-file",
                    "duration_seconds": 5,
                    "file_size": 15,
                    "acknowledgement_message_id": 99,
                },
            )
            request_id = UUID(result.json()["id"])
            completed = complete(app, settings, request_id)
            task_path = f"/internal/bot/tasks/{completed['task_id']}"
            mutation = api.patch(
                task_path,
                headers={**bot_headers, "If-Match": "1"},
                params={"telegram_user_id": ACTOR},
                json={"status": "completed"},
            )
            assert mutation.status_code == 200
            with app.state.session_factory.begin() as db:
                delivery = voice.claim_notification(
                    db, UUID(completed["notification_id"]), settings
                )
            interface = harness.dispatcher["interface"]
            adapter = VoiceAdapter(
                interface.api, harness.bot, interface.settings, interface.navigation
            )
            harness.telegram.fail_edit_once = "Bad Request: message to edit not found"
            delivered = await adapter.notify(str(request_id), delivery["lease_token"])
            assert delivered["outcome"] == "sent"
            assert "✅ Completed" in harness.latest().text
            send_count = sum(
                call.__api_method__ == "sendMessage" for call in harness.telegram.calls
            )
            fresh_navigation = Navigation()
            restarted = VoiceAdapter(
                interface.api, harness.bot, interface.settings, fresh_navigation
            )
            again = await restarted.notify(str(request_id), delivery["lease_token"])
            assert again == delivered
            assert (
                sum(call.__api_method__ == "sendMessage" for call in harness.telegram.calls)
                == send_count
            )
            response = api.delete(
                task_path,
                headers={**bot_headers, "If-Match": "2"},
                params={"telegram_user_id": ACTOR},
            )
            assert response.status_code == 204
            count = len(harness.telegram.calls)
            assert (await restarted.notify(str(request_id), delivery["lease_token"]))[
                "outcome"
            ] == "suppressed"
            assert len(harness.telegram.calls) == count
            await harness.send(voice=VOICE, message_id=199)
            assert "was deleted" in harness.latest().text

    asyncio.run(scenario())


def test_acceptance_retries_reuse_receipt_without_spamming_the_chat():
    async def scenario():
        async with unit_harness() as (harness, api, _, _):
            api.fail_accept = True
            for _ in range(3):
                with pytest.raises(APIUnavailable):
                    await harness.send(voice=VOICE, message_id=499)
            assert harness.timeline.count("telegram:sendMessage") == 1
            api.fail_accept = False
            await harness.send(voice=VOICE, message_id=499)
            assert harness.timeline.count("telegram:sendMessage") == 1
            assert all(call[-1] == 10000 for call in api.calls)
            assert not harness.dispatcher["interface"].voice_receipts

    asyncio.run(scenario())


def test_download_incomplete_length_is_retryable_even_when_decoder_might_accept_it():
    async def scenario():
        async with unit_harness() as (harness, _, _, client):
            harness.telegram.chunks = [b"short"]
            response = await client.post(request_path("audio"), json={"lease_token": LEASE})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "download_incomplete"

    asyncio.run(scenario())


def test_fallback_notification_uses_rechecked_task_status_and_version():
    async def scenario():
        async with unit_harness() as (harness, api, adapter, client):
            original = api.voice_context
            reads = 0

            async def changed_context(*args):
                nonlocal reads
                reads += 1
                if reads == 2:
                    api.context["task"] = {**TASK, "status": "completed", "version": 2}
                return await original(*args)

            api.voice_context = changed_context
            harness.telegram.fail_edit_once = "Bad Request: message can't be edited"
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.status_code == 200
            message = harness.latest()
            assert "Completed" in message.text
            assert "Pending" not in message.text
            assert "[Demo transcription]" in message.text
            control = harness.button(message, "Completed").callback_data
            action = adapter.navigation.resolve(control, ACTOR, ACTOR, message.message_id)
            assert action.version == 2
            assert reads == 2
            assert api.timeline.count("telegram:sendMessage") == 1

    asyncio.run(scenario())


def test_voice_adapter_logs_validated_request_correlation_not_lease_or_body(caplog):
    from omni_logging import current_correlation_id

    async def scenario():
        async with unit_harness() as (_, api, _, client):
            observed = []
            original = api.voice_context

            async def context(*args):
                observed.append(current_correlation_id())
                return await original(*args)

            api.voice_context = context
            api.context_error = APIUnavailable()
            response = await client.post(request_path("notify"), json={"lease_token": LEASE})
            assert response.status_code == 503
            assert observed == [IDENTIFIER]
            records = [
                record.omni_event
                for record in caplog.records
                if hasattr(record, "omni_event")
                and record.omni_event["event"] == "bot_voice_transport_failed"
            ]
            assert records[-1] == {
                "event": "bot_voice_transport_failed",
                "correlation_id": IDENTIFIER,
                "request_id": IDENTIFIER,
                "http_status": 503,
                "error_code": "api_unavailable",
                "outcome": "failed",
            }
            assert LEASE not in repr(records)
            assert TASK["content"] not in repr(records)

    asyncio.run(scenario())
