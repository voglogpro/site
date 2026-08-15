from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, WebAppInfo,
)

from .config import Settings
from .service import QuestService


def app_keyboard(settings: Settings, is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="Открыть квест", web_app=WebAppInfo(url=settings.webapp_url))]]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="Панель управления", web_app=WebAppInfo(url=settings.admin_url))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть квест"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="admin", description="Панель управления"),
        BotCommand(command="participant", description="Статус участника (админ)"),
    ])


def build_router(service: QuestService, settings: Settings) -> Router:
    router = Router(name="quest")

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        if message.chat.type != "private" or not message.from_user:
            return
        text = (
            "<b>Квест bb.bike по Красной Поляне</b>\n\n"
            "Три партнёрские точки, подарок на каждой и месяц премиума после финиша. "
            "Выбирай точки в любом порядке, открывай маршрут в Яндекс Картах или 2ГИС и подтверждай визит QR-кодом.\n\n"
            "Во время поездки смотри в телефон только после полной остановки."
        )
        await message.answer(text, reply_markup=app_keyboard(settings, message.from_user.id in settings.admin_ids))

    @router.message(Command("progress"))
    async def progress(message: Message):
        if not message.from_user:
            return
        await message.answer("Твой маршрут и все сохранённые подарки находятся в приложении.", reply_markup=app_keyboard(settings))

    @router.message(Command("help"))
    async def help_message(message: Message):
        await message.answer(
            "Открой приложение, выбери любую непройденную точку и построй маршрут. Для сортировки по расстоянию можно один раз разрешить геопозицию — она не отправляется боту.\n\n"
            f"Поддержка: {settings.support_url}"
        )

    @router.message(Command("admin"))
    async def admin(message: Message):
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            await message.answer("Панель доступна только администраторам квеста.")
            return
        await message.answer("Панель управления квестом:", reply_markup=app_keyboard(settings, True))

    @router.message(Command("participant"))
    async def participant(message: Message, command: CommandObject):
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            return
        try:
            user_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Использование: <code>/participant Telegram_ID</code>")
            return
        item = await service.participant_brief(user_id)
        if not item:
            await message.answer("Участник с таким ID ещё не начинал квест.")
            return
        progress = 3 if item["current_seq"] > 3 else max(0, item["current_seq"] - 1)
        await message.answer(
            f"<b>{item['display_name']}</b>\n"
            f"Прогресс: {progress}/3\n"
            f"Статус: {item['status']}\n"
            f"Проверка: {item['integrity_status']}\n"
            f"Premium: {item['premium_status'] or 'не назначен'}"
        )

    return router
