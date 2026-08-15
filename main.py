from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from quest.api import create_web_app
from quest.bot import build_router, setup_bot_commands
from quest.config import load_settings
from quest.db import Database
from quest.service import QuestService

BUILD_VERSION = "2026-08-15 · Krasnaya Polyana Quest 2.0 · свободный маршрут"


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("bibibike.quest")
    db = Database(settings.db_path)
    await db.initialize()
    service = QuestService(db, settings)
    await service.ensure_demo_campaign()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(service, settings))
    await setup_bot_commands(bot)

    app = create_web_app(service, settings, bot, BUILD_VERSION)
    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.web_port)
    await site.start()
    log.info("Mini App и API слушают 0.0.0.0:%s", settings.web_port)
    log.info("Версия: %s", BUILD_VERSION)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    polling = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        name="telegram-polling",
    )
    janitor = asyncio.create_task(service.janitor(stop), name="privacy-janitor")
    waiter = asyncio.create_task(stop.wait(), name="shutdown-waiter")
    done, _ = await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if polling in done and polling.exception():
        raise polling.exception()
    stop.set()
    await dp.stop_polling()
    for task in (polling, janitor, waiter):
        if not task.done():
            task.cancel()
    await asyncio.gather(polling, janitor, waiter, return_exceptions=True)
    await runner.cleanup()
    await bot.session.close()
    await db.close()
    log.info("BibiBike Quest остановлен штатно")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
