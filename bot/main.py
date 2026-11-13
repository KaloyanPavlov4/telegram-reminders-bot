import logging
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.ext import Application

from .config import Settings
from .db import Database
from .handlers import State, register, tick
from .llm import GeminiParser


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    db = Database(settings.db_path)
    await db.connect()
    tz = ZoneInfo(await db.get_setting("tz") or settings.tz)
    app.bot_data["state"] = State(
        settings=settings,
        db=db,
        llm=GeminiParser(settings.gemini_api_key, settings.gemini_model),
        tz=tz,
    )
    await app.bot.set_my_commands(
        [
            BotCommand("list", "Everything upcoming"),
            BotCommand("today", "Today"),
            BotCommand("week", "Next 7 days"),
            BotCommand("done", "Mark as done: /done <id>"),
            BotCommand("delete", "Cancel a reminder: /delete <id>"),
            BotCommand("tz", "Show or set timezone"),
            BotCommand("help", "How to use the bot"),
        ]
    )
    app.job_queue.run_repeating(tick, interval=settings.tick_seconds, first=3)


async def _post_shutdown(app: Application) -> None:
    state = app.bot_data.get("state")
    if state:
        await state.db.close()


def build(settings: Settings) -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_token)
        # Don't let a slow Gemini call hold up commands and button presses.
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings
    register(app, settings)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build(Settings()).run_polling(allowed_updates=Update.ALL_TYPES)
