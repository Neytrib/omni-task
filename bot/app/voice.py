"""Private Telegram transport for leased voice jobs; no database or broker access."""

import asyncio
import hmac
import io
import json
import logging
from collections import OrderedDict
from contextlib import aclosing
from uuid import UUID
from weakref import WeakValueDictionary

from aiogram import Bot
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import LinkPreviewOptions
from aiohttp import ClientError, ClientResponseError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from bot.app.api import APIClient, APIError
from bot.app.config import BotSettings
from bot.app.navigation import Navigation
from bot.app.views import View, Views
from omni_logging import correlation_context, log_event

logger = logging.getLogger("omni_task.bot.voice")


def failure_text(code: str | None) -> str:
    descriptions = {
        "provider_disabled": "Voice transcription is not configured yet. Please send text for now.",
        "provider_not_configured": "Voice transcription is not configured. Please send text.",
        "paid_transcription_not_enabled": "Voice transcription is not enabled. Please send text.",
        "audio_too_long": "The voice recording exceeds the supported duration limit.",
        "audio_unavailable": "The voice recording is no longer available from Telegram.",
        "unsupported_audio": "This recording format could not be read. Please record it again.",
        "audio_too_large": "The voice recording exceeds the supported size limit.",
        "duration_exceeded": "The voice recording exceeds the supported duration limit.",
        "audio_missing": "The voice recording is no longer available from Telegram.",
        "invalid_audio": "The voice recording could not be read. Please record it again.",
        "empty_transcript": "No speech text was returned for this recording.",
        "transcript_too_large": "The full transcript exceeds the supported text limit.",
    }
    description = descriptions.get(code, "This voice recording could not be transcribed.")
    return description + " No task was created. You can send a new recording or a text message."


class DeliveryError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None):
        self.status, self.code, self.retry_after = status, code, retry_after
        super().__init__(code)


class LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise DeliveryError(413, "audio_too_large")
        return super().write(data)


def transport_error(error: TelegramAPIError) -> DeliveryError:
    if isinstance(error, TelegramRetryAfter):
        return DeliveryError(503, "telegram_rate_limited", max(1, int(error.retry_after)))
    if isinstance(error, TelegramForbiddenError):
        return DeliveryError(403, "telegram_delivery_forbidden")
    if isinstance(error, TelegramUnauthorizedError):
        return DeliveryError(401, "telegram_credentials_invalid")
    if isinstance(error, TelegramBadRequest):
        return DeliveryError(422, "telegram_request_rejected")
    return DeliveryError(503, "telegram_unavailable")


class VoiceAdapter:
    def __init__(self, api: APIClient, bot: Bot, settings: BotSettings, navigation: Navigation):
        self.api, self.bot, self.settings, self.navigation = api, bot, settings, navigation
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        # A checkpoint timeout may follow a successful Telegram send. Retain its known ID
        # while this process lives and retry the checkpoint before any further delivery.
        self.pending_checkpoints: OrderedDict[str, int] = OrderedDict()

    def lock(self, identifier: str) -> asyncio.Lock:
        lock = self.locks.get(identifier)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[identifier] = lock
        return lock

    async def audio(self, identifier: str, lease: str) -> bytes:
        context = await self.api.voice_context(identifier, lease, "download")
        if context.get("file_size") and context["file_size"] > self.settings.voice_max_bytes:
            raise DeliveryError(413, "audio_too_large")
        if context.get("duration_seconds", 0) > self.settings.voice_max_duration_seconds:
            raise DeliveryError(422, "duration_exceeded")
        try:
            async with asyncio.timeout(self.settings.voice_download_timeout_seconds):
                remote = await self.bot.get_file(context["file_id"])
                if remote.file_size and remote.file_size > self.settings.voice_max_bytes:
                    raise DeliveryError(413, "audio_too_large")
                path = remote.file_path
                if not path:
                    raise DeliveryError(404, "audio_missing")
                # A Telegram response supplies this relative path, never an HTTP caller.
                if path.startswith(("/", "\\")) or ":" in path or ".." in path.split("/"):
                    raise DeliveryError(422, "invalid_audio_path")
                with LimitedBuffer(self.settings.voice_max_bytes) as destination:
                    url = self.bot.session.api.file_url(self.bot.token, path)
                    # Close the HTTP stream immediately on limit/timeout/cancellation, too.
                    stream = self.bot.session.stream_content(
                        url=url,
                        timeout=self.settings.voice_download_timeout_seconds,
                        chunk_size=64 * 1024,
                        raise_for_status=True,
                    )
                    async with aclosing(stream):
                        async for chunk in stream:
                            destination.write(chunk)
                    data = destination.getvalue()
                if not data:
                    raise DeliveryError(422, "invalid_audio")
                for expected in (remote.file_size, context.get("file_size")):
                    if expected is not None and len(data) != expected:
                        raise DeliveryError(503, "download_incomplete")
                return data
        except TimeoutError:
            raise DeliveryError(503, "telegram_download_timeout") from None
        except TelegramBadRequest:
            raise DeliveryError(404, "audio_missing") from None
        except ClientResponseError as error:
            if error.status in (400, 404):
                raise DeliveryError(404, "audio_missing") from None
            raise DeliveryError(503, "telegram_unavailable") from None
        except (ClientError, ClientDecodeError):
            raise DeliveryError(503, "telegram_unavailable") from None
        except TelegramAPIError as error:
            raise transport_error(error) from None

    @staticmethod
    def notification_view(views: Views, context: dict) -> View:
        if context["state"] == "succeeded":
            task = context.get("task")
            if not task:
                raise DeliveryError(409, "notification_state_changed")
            view = views.confirmation(task)
            if context.get("provider_name") == "fake":
                view.text = "[Demo transcription]\n" + view.text
            return view
        if context["state"] == "failed":
            return View(failure_text(context.get("error_code")))
        raise DeliveryError(409, "notification_not_ready")

    async def notify(self, identifier: str, lease: str) -> dict:
        async with self.lock(identifier):
            context = await self.api.voice_context(identifier, lease, "notification")
            if context.get("suppressed") or context.get("deleted") or context["state"] == "deleted":
                self.pending_checkpoints.pop(identifier, None)
                return {"outcome": "suppressed", "message_id": None}
            actor = context["telegram_user_id"]
            views = Views(self.navigation, actor, actor)
            view = self.notification_view(views, context)
            message_id = context.get("sent_message_id") or context.get("acknowledgement_message_id")
            pending = self.pending_checkpoints.get(identifier)
            if pending is not None:
                await self.api.notification_checkpoint(identifier, lease, pending)
                self.pending_checkpoints.pop(identifier, None)
                message_id = pending
            options = {
                "chat_id": actor,
                "text": view.text,
                "reply_markup": view.markup,
                "parse_mode": None,
                "link_preview_options": LinkPreviewOptions(is_disabled=True),
            }
            try:
                if message_id:
                    try:
                        await self.bot.edit_message_text(message_id=message_id, **options)
                    except TelegramBadRequest as error:
                        description = error.message.lower()
                        if "message is not modified" in description:
                            pass
                        elif any(
                            value in description
                            for value in (
                                "message to edit not found",
                                "message can't be edited",
                                "message cannot be edited",
                            )
                        ):
                            message_id = None
                        else:
                            raise
                if message_id is None:
                    # Resolve again immediately before fallback: deletion may have committed
                    # during the failed edit. Telegram and DB delivery still cannot be atomic.
                    latest = await self.api.voice_context(identifier, lease, "notification")
                    if latest.get("suppressed") or latest.get("deleted"):
                        return {"outcome": "suppressed", "message_id": None}
                    view = self.notification_view(views, latest)
                    options.update(text=view.text, reply_markup=view.markup)
                    result = await self.bot.send_message(**options)
                    message_id = result.message_id
                    self.pending_checkpoints[identifier] = message_id
                    if len(self.pending_checkpoints) > 1024:
                        self.pending_checkpoints.popitem(last=False)
                    await self.api.notification_checkpoint(identifier, lease, message_id)
                    self.pending_checkpoints.pop(identifier, None)
            except TelegramAPIError as error:
                raise transport_error(error) from None
            except (ClientError, ClientDecodeError, TimeoutError):
                raise DeliveryError(503, "telegram_unavailable") from None
            keys = [
                button.callback_data
                for row in (view.markup.inline_keyboard if view.markup else [])
                for button in row
                if button.callback_data
            ]
            self.navigation.bind(keys, actor, actor, message_id)
            return {"outcome": "sent", "message_id": message_id}


class InternalAuthentication:
    """Check server credentials before reading/validating any internal request body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/internal/"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        settings = scope["app"].state.configuration
        expected = ("Bearer " + settings.bot_api_key.get_secret_value()).encode()
        if b"origin" in headers or b"cookie" in headers:
            response = JSONResponse({"error": {"code": "browser_forbidden"}}, status_code=403)
        elif not hmac.compare_digest(headers.get(b"authorization", b""), expected):
            response = JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
        else:
            return await self.app(scope, receive, send)
        log_event(
            logger,
            "bot_internal_rejected",
            level=logging.WARNING,
            error_code="service_authentication_failed",
            http_status=response.status_code,
            outcome="rejected",
        )
        await response(scope, receive, send)


async def lease_body(request: Request) -> str:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024:
            raise DeliveryError(413, "request_too_large")
    try:
        document = json.loads(body)
        if not isinstance(document, dict) or set(document) != {"lease_token"}:
            raise ValueError
        return str(UUID(document["lease_token"]))
    except (ValueError, TypeError, AttributeError):
        raise DeliveryError(422, "invalid_request") from None


def install_voice_routes(application: FastAPI) -> None:
    application.add_middleware(InternalAuthentication)

    async def handle(request: Request, request_id: str, operation: str):
        try:
            identifier = str(UUID(request_id))
        except ValueError:
            log_event(
                logger,
                "bot_voice_transport_failed",
                level=logging.WARNING,
                error_code="invalid_request",
                http_status=422,
                outcome="rejected",
            )
            return JSONResponse(
                {"error": {"code": "invalid_request"}},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        # Service authentication has already passed; only a validated request UUID
        # becomes correlation metadata. Never use the lease token or caller body.
        with correlation_context(identifier):
            return await perform(request, identifier, operation)

    async def perform(request: Request, identifier: str, operation: str):
        try:
            lease = await lease_body(request)
            adapter = request.app.state.voice_adapter
            if adapter is None:
                raise DeliveryError(503, "telegram_not_configured")
            if operation == "audio":
                data = await adapter.audio(identifier, lease)
                return Response(
                    data,
                    media_type="application/octet-stream",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            return JSONResponse(
                await adapter.notify(identifier, lease), headers={"Cache-Control": "no-store"}
            )
        except (DeliveryError, APIError) as error:
            log_event(
                logger,
                "bot_voice_transport_failed",
                level=logging.WARNING,
                error_code=error.code,
                http_status=error.status,
                request_id=identifier,
                outcome="failed",
            )
            headers = {"Cache-Control": "no-store"}
            retry_after = getattr(error, "retry_after", None)
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
            return JSONResponse(
                {"error": {"code": error.code}}, status_code=error.status, headers=headers
            )

    @application.post("/internal/voice/{request_id}/audio")
    async def audio(request: Request, request_id: str):
        return await handle(request, request_id, "audio")

    @application.post("/internal/voice/{request_id}/notify")
    async def notify(request: Request, request_id: str):
        return await handle(request, request_id, "notify")
