"""Private Telegram routers. The API owns task validation, identity and persistence."""

from aiogram import Dispatcher, Router

from bot.app.api import APIClient
from bot.app.config import BotSettings
from bot.app.interface import TelegramInterface
from bot.app.navigation import Navigation


def create_dispatcher(api: APIClient, settings: BotSettings) -> Dispatcher:
    navigation = Navigation()
    interface = TelegramInterface(api, settings, navigation)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher["navigation"] = navigation
    dispatcher["interface"] = interface
    router = Router(name="private_tasks")
    router.message.register(interface.message)
    router.callback_query.register(interface.callback)
    dispatcher.include_router(router)
    return dispatcher
