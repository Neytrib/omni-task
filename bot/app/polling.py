"""Sequential polling that confirms an update only after its handler completes.

Restart may redeliver the last unconfirmed update; the API's source receipt prevents a
second task. Telegram retains pending updates for at most 24 hours, not indefinitely.
"""

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot.app.api import APIUnavailable
from omni_logging import correlation_context, log_event

logger = logging.getLogger("omni_task.bot")


@dataclass
class PollingStatus:
    state: str = "starting"
    error_code: str | None = None


async def pause(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


def retry_pause(error: Exception, retry_delay: float, failures: int) -> float:
    if isinstance(error, TelegramRetryAfter):
        return max(float(error.retry_after), retry_delay)
    return min(retry_delay * 2 ** min(failures - 1, 5), 30.0)


async def run_polling(
    bot: Bot,
    dispatcher: Dispatcher,
    stop: asyncio.Event,
    status: PollingStatus,
    *,
    poll_timeout: int = 25,
    retry_delay: float = 1.0,
) -> None:
    offset = None
    failures = 0
    try:
        while not stop.is_set():
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    limit=100,
                    timeout=poll_timeout,
                    allowed_updates=["message", "callback_query"],
                    request_timeout=poll_timeout + 10,
                )
            except (TelegramUnauthorizedError, TelegramConflictError):
                status.state, status.error_code = "error", "telegram_configuration"
                log_event(
                    logger,
                    "bot_polling_stopped",
                    level=logging.ERROR,
                    error_code="telegram_configuration",
                    outcome="failed",
                )
                return
            except Exception as error:
                failures += 1
                status.state, status.error_code = "retrying", "telegram_transport"
                log_event(
                    logger,
                    "bot_polling_retry",
                    level=logging.WARNING,
                    error_code="telegram_transport",
                    outcome="retrying",
                )
                await pause(stop, retry_pause(error, retry_delay, failures))
                continue
            failures = 0
            status.state, status.error_code = "polling", None
            for update in updates:
                with correlation_context():
                    # Retry this exact update before requesting a higher offset. feed_update
                    # propagates errors; aiogram's convenience _process_update swallows them.
                    while not stop.is_set():
                        try:
                            await dispatcher.feed_update(bot, update)
                        except (TelegramUnauthorizedError, TelegramConflictError):
                            status.state, status.error_code = "error", "telegram_configuration"
                            log_event(
                                logger,
                                "bot_polling_stopped",
                                level=logging.ERROR,
                                error_code="telegram_configuration",
                                outcome="failed",
                            )
                            return
                        except (TelegramForbiddenError, TelegramBadRequest):
                            # Handlers perform task API work before sending their result.
                            # Permanent output rejection must not block every user's queue.
                            log_event(
                                logger,
                                "bot_reply_rejected",
                                level=logging.WARNING,
                                error_code="delivery_rejected",
                                outcome="failed",
                            )
                            failures = 0
                            offset = update.update_id + 1
                            status.state, status.error_code = "polling", None
                            break
                        except Exception as error:
                            failures += 1
                            code = (
                                "api_unavailable"
                                if isinstance(error, APIUnavailable)
                                else "handler_failed"
                            )
                            status.state, status.error_code = "retrying", code
                            log_event(
                                logger,
                                "bot_update_retry",
                                level=logging.WARNING,
                                error_code=code,
                                outcome="retrying",
                            )
                            await pause(stop, retry_pause(error, retry_delay, failures))
                        else:
                            log_event(logger, "bot_update_handled", outcome="succeeded")
                            failures = 0
                            offset = update.update_id + 1
                            status.state, status.error_code = "polling", None
                            break
                    if stop.is_set():
                        return
    finally:
        if status.state != "error":
            status.state = "stopped"


async def configure_commands(
    bot: Bot, stop: asyncio.Event, status: PollingStatus, *, retry_delay: float = 1.0
) -> bool:
    """Idempotently advertise only supported commands in private chats."""
    commands = [
        BotCommand(command="start", description="Introduction and getting started"),
        BotCommand(command="help", description="Supported messages and commands"),
        BotCommand(command="list", description="Browse your tasks"),
        BotCommand(command="profile", description="Open your private dashboard"),
    ]
    failures = 0
    while not stop.is_set():
        try:
            await bot.set_my_commands(
                commands, scope=BotCommandScopeAllPrivateChats(), request_timeout=10
            )
            return True
        except (TelegramUnauthorizedError, TelegramConflictError, TelegramBadRequest):
            status.state, status.error_code = "error", "telegram_configuration"
            log_event(
                logger,
                "bot_commands_failed",
                level=logging.ERROR,
                error_code="telegram_configuration",
                outcome="failed",
            )
            return False
        except Exception as error:
            failures += 1
            status.state, status.error_code = "retrying", "telegram_setup"
            log_event(
                logger,
                "bot_commands_retry",
                level=logging.WARNING,
                error_code="telegram_setup",
                outcome="retrying",
            )
            await pause(stop, retry_pause(error, retry_delay, failures))
    status.state = "stopped"
    return False


async def run_bot(
    bot: Bot, dispatcher: Dispatcher, stop: asyncio.Event, status: PollingStatus
) -> None:
    if await configure_commands(bot, stop, status):
        await run_polling(bot, dispatcher, stop, status)
