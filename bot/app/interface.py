"""Telegram interaction orchestration; all task rules and writes go through HTTP."""

import logging
from collections import OrderedDict

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from bot.app.api import APIClient, APIError, APIUnavailable
from bot.app.config import BotSettings
from bot.app.navigation import Action, Navigation, Page
from bot.app.views import View, Views
from omni_logging import log_event

logger = logging.getLogger("omni_task.bot.interface")


class TelegramInterface:
    def __init__(self, api: APIClient, settings: BotSettings, navigation: Navigation):
        self.api = api
        self.settings = settings
        self.navigation = navigation
        self.retry_notices: OrderedDict[tuple[int, int], None] = OrderedDict()
        self.voice_receipts: OrderedDict[tuple[int, int], Message] = OrderedDict()

    def help_text(self) -> str:
        return (
            "Creating tasks\n"
            "Text: each message becomes one Pending task, with its complete content saved.\n"
            "Voice: send a voice recording. I'll acknowledge it immediately and transcribe it "
            "in the background, then update the receipt with your task or a clear failure. "
            "One recording becomes one task; its complete transcript is saved.\n\n"
            "Commands\n"
            "/start — Introduction\n"
            "/help — Inputs and commands\n"
            "/list — Browse and open your tasks\n"
            "/profile — Open your private dashboard\n\n"
            "Managing tasks\n"
            "Tap a task in /list to see its details. Use Previous, Next and Back to tasks "
            "to navigate; Read full text shows every page of long content. "
            "Status buttons switch between Pending, In Progress and Completed. "
            "Delete task asks for confirmation: Confirm delete removes it; Cancel keeps it.\n\n"
            "Your tasks are private to your Telegram account. Use /profile for a fresh, "
            "short-lived dashboard link; no registration or login code is needed. "
            "The bot works in private chats only. Photos, documents, videos, audio files "
            "and captions are not supported."
        )

    @staticmethod
    def is_private(message: Message) -> bool:
        return bool(
            message.chat.type == "private"
            and message.from_user
            and not message.from_user.is_bot
            and message.from_user.id == message.chat.id
        )

    async def show(
        self, message: Message, actor: int, view: View, *, edit: bool = False
    ) -> Message:
        options = {
            "text": view.text,
            "reply_markup": view.markup,
            "parse_mode": None,
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
        try:
            result = await message.edit_text(**options) if edit else await message.answer(**options)
        except TelegramBadRequest as error:
            # Inspect known Telegram categories without ever logging request/response contents.
            description = error.message.lower()
            if edit and "message is not modified" in description:
                result = message
            elif edit and any(
                value in description
                for value in (
                    "message to edit not found",
                    "message can't be edited",
                    "message cannot be edited",
                )
            ):
                result = await message.answer(**options)
            elif (
                view.markup
                and any(button.url for row in view.markup.inline_keyboard for button in row)
                and "url" in description
                and any(word in description for word in ("invalid", "wrong", "unsupported"))
            ):
                log_event(
                    logger,
                    "dashboard_url_rejected",
                    level=logging.WARNING,
                    error_code="dashboard_url_rejected",
                    outcome="failed",
                )
                view = View(
                    "The dashboard link is temporarily unavailable. "
                    "Please try /profile again shortly."
                )
                result = await message.answer(
                    view.text,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            else:
                raise
        keys = [
            button.callback_data
            for row in (view.markup.inline_keyboard if view.markup else [])
            for button in row
            if button.callback_data
        ]
        message_id = result.message_id if isinstance(result, Message) else message.message_id
        if edit and message_id != message.message_id:
            self.navigation.bind([], actor, message.chat.id, message.message_id)
        self.navigation.bind(keys, actor, message.chat.id, message_id)
        return result if isinstance(result, Message) else message

    @staticmethod
    def error_text(error: APIError) -> str:
        if error.status in (401, 403):
            return "The bot cannot authenticate to the task service. Please try again later."
        if error.status == 410:
            return (
                "This message was already processed and its task was deleted. No new task was made."
            )
        if error.status == 409:
            return (
                "This message was already handled with different content. Use /list to check tasks."
            )
        if error.status == 422:
            return (
                "That input could not be accepted. Send non-empty text up to 50,000 characters "
                "without invalid control characters. No content was shortened."
            )
        return "The task service could not complete this action. Try /list or try again later."

    async def message(self, message: Message) -> None:
        if not self.is_private(message):
            return
        actor = message.from_user.id
        views = Views(self.navigation, actor, message.chat.id)
        text = message.text
        words = text.split(maxsplit=1) if text and text.startswith("/") else []
        command = words[0].split("@", 1)[0].lower() if words else ""
        if (
            text
            and text.startswith("/")
            and command not in ("/start", "/help", "/list", "/profile")
        ):
            await self.show(message, actor, View("Unknown command.\n\n" + self.help_text()))
            return
        if command == "/help":
            await self.show(message, actor, View(self.help_text()))
            return
        if message.voice:
            await self.voice_message(message, actor, views)
            return
        if text is None:
            await self.show(
                message,
                actor,
                View(
                    "That message type is not supported. Send text to create a task, or use /help."
                ),
            )
            return
        try:
            await self.api.ensure_user(actor, message.chat.id)
            if command == "/start":
                view = View(
                    "Welcome to Omni Task — your private task manager.\n\n"
                    "Send one text message to create one Pending task. "
                    "Your full message is preserved; buttons change its status.\n\n"
                    "/help — Inputs and commands"
                )
            elif command == "/profile":
                link = await self.api.login_link(actor)
                view = views.dashboard(link["url"])
            elif command == "/list":
                view = await self.list_view(actor, views, Page())
            else:
                task = await self.api.create_task(actor, message.message_id, text)
                view = views.confirmation(task)
            await self.show(message, actor, view)
            self.retry_notices.pop((actor, message.message_id), None)
        except APIUnavailable:
            # The polling loop will retry this exact Telegram source before confirming its offset.
            key = (actor, message.message_id)
            if key not in self.retry_notices:
                self.retry_notices[key] = None
                if len(self.retry_notices) > 2048:
                    self.retry_notices.popitem(last=False)
                try:
                    await self.show(
                        message,
                        actor,
                        View(
                            "The task service is temporarily unavailable. I'll retry this same "
                            "message automatically; you don't need to resend it."
                        ),
                    )
                except TelegramAPIError:
                    # Failed feedback cannot turn an unaccepted input into a terminal
                    # Telegram delivery failure and accidentally advance the poll offset.
                    pass
            raise
        except APIError as error:
            await self.show(message, actor, View(self.error_text(error)))

    async def voice_message(self, message: Message, actor: int, views: Views) -> None:
        from bot.app.voice import failure_text

        voice = message.voice
        if voice.duration <= 0 or voice.duration > self.settings.voice_max_duration_seconds:
            await self.show(
                message,
                actor,
                View(
                    "Voice recordings must be between 1 and "
                    f"{self.settings.voice_max_duration_seconds} "
                    "seconds. No task was created."
                ),
            )
            return
        if voice.file_size is not None and voice.file_size > self.settings.voice_max_bytes:
            await self.show(
                message,
                actor,
                View(
                    f"This voice recording exceeds the limit of {self.settings.voice_max_bytes:,} "
                    "bytes. No task was created."
                ),
            )
            return
        source = (actor, message.message_id)
        acknowledgement = self.voice_receipts.get(source)
        if acknowledgement is None:
            try:
                acknowledgement = await self.show(
                    message,
                    actor,
                    View("Voice received. Queuing transcription…"),
                )
                self.voice_receipts[source] = acknowledgement
                if len(self.voice_receipts) > 2048:
                    self.voice_receipts.popitem(last=False)
            except TelegramAPIError:
                # A failed receipt does not prevent durable acceptance; notification can send later.
                pass
        try:
            await self.api.ensure_user(actor, message.chat.id)
            outcome = await self.api.create_processing_request(
                actor,
                message.message_id,
                voice.file_id,
                voice.duration,
                voice.file_size,
                acknowledgement.message_id if acknowledgement else None,
            )
        except APIUnavailable:
            if acknowledgement:
                try:
                    await self.show(
                        acknowledgement,
                        actor,
                        View(
                            "The task service could not confirm acceptance yet. I'll retry this "
                            "same recording automatically; you don't need to resend it."
                        ),
                        edit=True,
                    )
                except TelegramAPIError:
                    pass
            raise
        except APIError as error:
            self.voice_receipts.pop(source, None)
            if acknowledgement:
                await self.show(acknowledgement, actor, View(self.error_text(error)), edit=True)
            return
        self.voice_receipts.pop(source, None)
        if acknowledgement is None:
            return
        canonical = outcome.get("acknowledgement_message_id")
        # The canonical receipt belongs to the queued notifier. Editing it after acceptance
        # could overwrite a fast worker's success. Only resolve additional receipt messages.
        if canonical == acknowledgement.message_id:
            return
        if outcome.get("deleted"):
            view = View(
                "This recording already created a task that was deleted. No new task was made."
            )
        elif outcome["state"] == "succeeded" and outcome.get("task_id"):
            try:
                task = await self.api.get_task(actor, outcome["task_id"])
                view = views.confirmation(task)
                if outcome.get("provider_name") == "fake":
                    view.text = "[Demo transcription]\n" + view.text
            except APIError as error:
                if error.status in (404, 410):
                    view = View(
                        "The task created from this recording was deleted. No new task was made."
                    )
                else:
                    view = View(
                        "This recording was already processed. Use /list to check its task."
                    )
        elif outcome["state"] == "failed":
            view = View(failure_text(outcome.get("error_code")))
        else:
            view = View(
                "This recording is already queued or processing. Its original confirmation "
                "will update when processing finishes; no new job was created."
            )
        await self.show(acknowledgement, actor, view, edit=True)

    async def list_view(self, actor: int, views: Views, page: Page, notice: str = "") -> View:
        try:
            result = await self.api.list_tasks(actor, cursor=page.cursor, limit=5)
        except APIError as error:
            if error.code not in ("stale_cursor", "invalid_cursor"):
                raise
            result = await self.api.list_tasks(actor, limit=5)
            page = Page()
            notice = (
                notice + " " if notice else ""
            ) + "Your task list changed. Showing the newest tasks."
        if not result["items"] and page.cursor:
            result = await self.api.list_tasks(actor, limit=5)
            page = Page()
        return views.tasks(result, page, notice)

    async def callback(self, callback: CallbackQuery) -> None:
        message = callback.message
        if (
            not isinstance(message, Message)
            or message.chat.type != "private"
            or callback.from_user.is_bot
            or message.chat.id != callback.from_user.id
        ):
            await self.answer_callback(
                callback, "Use these controls in your private chat with the bot."
            )
            return
        actor = callback.from_user.id
        action = self.navigation.resolve(callback.data, actor, message.chat.id, message.message_id)
        if action is None:
            await self.answer_callback(
                callback, "This control expired or is invalid. Send /list for fresh controls."
            )
            return
        # Clear Telegram's spinner before reading or changing anything through the API.
        await self.answer_callback(callback)
        views = Views(self.navigation, actor, message.chat.id)
        try:
            view = await self.action_view(actor, views, action)
        except APIUnavailable:
            view = views.unavailable(
                "The task service is temporarily unavailable. Please try /list again shortly."
            )
        except APIError as error:
            if error.status in (404, 410):
                view = views.unavailable("This task was deleted or is unavailable to you.")
            elif error.status == 409:
                try:
                    task = await self.api.get_task(actor, action.task_id)
                    view = views.task(
                        task,
                        action.page,
                        "This task changed. Review its current state and try again.",
                    )
                except APIError as refresh_error:
                    if refresh_error.status in (404, 410):
                        view = views.unavailable("This task was deleted or is unavailable to you.")
                    else:
                        view = views.unavailable(
                            "The task service is unavailable. Try /list again shortly."
                        )
            else:
                view = views.unavailable(self.error_text(error))
        await self.show(message, actor, view, edit=True)

    @staticmethod
    async def answer_callback(callback: CallbackQuery, text: str | None = None) -> None:
        try:
            await callback.answer(text=text, show_alert=bool(text))
        except TelegramBadRequest:
            # Telegram can expire a callback while an outage/retry is in progress.
            # Never include the callback data or Telegram method in logs.
            log_event(
                logger, "bot_callback_expired", error_code="callback_expired", outcome="ignored"
            )

    async def action_view(self, actor: int, views: Views, action: Action) -> View:
        if action.kind == "list":
            return await self.list_view(actor, views, action.page)
        if action.kind == "dashboard":
            link = await self.api.login_link(actor)
            return views.dashboard(link["url"], action)
        if action.kind == "confirm":
            await self.api.delete_task(actor, action.task_id, action.version)
            return await self.list_view(actor, views, action.page, "Task deleted.")
        task = await self.api.get_task(actor, action.task_id)
        if action.kind == "full":
            return views.full_text(task, action.page, action.text_page)
        if action.kind == "delete":
            return views.delete(task, action.page)
        if action.kind == "status":
            if task["status"] == action.status:
                return views.task(task, action.page)
            if task["version"] != action.version:
                return views.task(
                    task, action.page, "This task changed. Review its current state and try again."
                )
            task = await self.api.change_status(
                actor, action.task_id, action.status, action.version
            )
        return views.task(task, action.page)
