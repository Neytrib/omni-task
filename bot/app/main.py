"""Bot HTTP readiness and optional sequential Telegram polling lifecycle."""

import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
from aiogram import Bot
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bot.app.api import APIClient, APIError
from bot.app.config import BotSettings
from bot.app.polling import PollingStatus, run_bot
from bot.app.voice import VoiceAdapter, install_voice_routes
from omni_logging import configure_logging


def create_app(settings: BotSettings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging("bot")
        configuration = settings or BotSettings()
        application.state.configuration = configuration
        application.state.voice_adapter = None
        async with httpx.AsyncClient(
            base_url=configuration.api_base_url,
            headers={"Authorization": f"Bearer {configuration.bot_api_key.get_secret_value()}"},
            timeout=httpx.Timeout(10, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            api = APIClient(client)
            application.state.api_client = api
            status = PollingStatus(state="disabled")
            application.state.polling = status
            stop = asyncio.Event()
            bot = None
            polling_task = None
            if configuration.telegram_bot_token is not None:
                from bot.app.handlers import create_dispatcher

                bot = Bot(configuration.telegram_bot_token.get_secret_value())
                dispatcher = create_dispatcher(api, configuration)
                application.state.voice_adapter = VoiceAdapter(
                    api,
                    bot,
                    configuration,
                    dispatcher["navigation"],
                )
                status.state = "starting"
                polling_task = asyncio.create_task(run_bot(bot, dispatcher, stop, status))
            try:
                yield
            finally:
                stop.set()
                if polling_task is not None:
                    polling_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await polling_task
                if bot is not None:
                    await bot.session.close()

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    install_voice_routes(application)

    @application.get("/health/ready")
    async def ready():
        try:
            await application.state.api_client.health()
        except APIError:
            return JSONResponse({"status": "unavailable", "dependency": "api"}, status_code=503)
        polling = application.state.polling
        healthy = polling.state in {"disabled", "polling"}
        return JSONResponse(
            {"status": "ready" if healthy else "unavailable", "telegram_polling": polling.state},
            status_code=200 if healthy else 503,
        )

    return application


app = create_app()
