# -*- coding: utf-8 -*-
# ============================================================
# ЗАГЛУШКА ДЛЯ ПРОВЕРКИ ХОСТИНГА
# Задача файла: доказать, что контейнер поднимается, файлы читаются
# и домен отдаёт страницу. Никакой логики смен здесь нет.
#
# Особенность: веб-сервер запускается ПЕРВЫМ и остаётся жив, даже если
# Telegram недоступен или токен неверный. Это разделяет две проблемы:
#   - не открывается домен       -> виноват хостинг/контейнер
#   - домен открыт, бот молчит   -> виноват токен/Telegram
# ============================================================
import asyncio
import logging
import os
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("stub")

STUB_VERSION = "ЗАГЛУШКА v1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# Порт: bothost передаёт PORT. WEB_PORT оставлен на случай другой площадки.
PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT") or 3000)

TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("API_TOKEN")
)

print("=" * 60, flush=True)
print(f"== {STUB_VERSION}: процесс стартовал ==", flush=True)
print(f"== рабочая папка: {BASE_DIR}", flush=True)
print(f"== index.html рядом: {os.path.exists(INDEX_PATH)}", flush=True)
print(f"== порт: {PORT}", flush=True)
print(f"== токен найден: {'да' if TOKEN else 'НЕТ'}", flush=True)
print("=" * 60, flush=True)

STARTED_AT = datetime.now()


# ── Веб-сервер ────────────────────────────────────────────────
async def handle_index(request):
    from aiohttp import web
    if not os.path.exists(INDEX_PATH):
        return web.Response(
            text="index.html не найден рядом с main.py", status=404
        )
    return web.FileResponse(INDEX_PATH, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    })


async def handle_health(request):
    from aiohttp import web
    return web.json_response({
        "ok": True,
        "version": STUB_VERSION,
        "started_at": STARTED_AT.isoformat(),
        "uptime_sec": int((datetime.now() - STARTED_AT).total_seconds()),
        "index_found": os.path.exists(INDEX_PATH),
        "token_present": bool(TOKEN),
        "port": PORT,
        "files": sorted(os.listdir(BASE_DIR))[:20],
    })


async def start_web():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"ВЕБ-СЕРВЕР ПОДНЯТ на 0.0.0.0:{PORT} — домен должен открываться")
    return runner


# ── Telegram ──────────────────────────────────────────────────
async def start_bot():
    if not TOKEN:
        logger.warning("Токен не задан — Telegram не запускаю, держу только сайт.")
        return
    try:
        from aiogram import Bot, Dispatcher, F
        from aiogram.types import Message

        bot = Bot(token=TOKEN)
        dp = Dispatcher()

        @dp.message(F.text)
        async def any_message(message: Message):
            await message.answer(
                f"{STUB_VERSION} на связи.\n"
                f"Сайт: работает на порту {PORT}.\n"
                f"index.html найден: {'да' if os.path.exists(INDEX_PATH) else 'нет'}"
            )

        me = await bot.get_me()
        logger.info(f"TELEGRAM ПОДКЛЮЧЁН: @{me.username} (id={me.id})")
        await dp.start_polling(bot)
    except Exception as exc:
        # Telegram упал — сайт всё равно продолжает работать.
        logger.error(f"TELEGRAM НЕ ЗАПУСТИЛСЯ: {type(exc).__name__}: {exc}")


async def main():
    await start_web()          # сайт поднимаем первым и не роняем
    await start_bot()
    # Если бот завершился, держим процесс живым ради сайта.
    logger.info("Telegram завершился, сайт продолжает работать.")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено вручную.")
