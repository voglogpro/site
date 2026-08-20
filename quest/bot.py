from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonWebApp, Message, WebAppInfo,
)

from .config import Settings
from .service import QuestService
from .security import create_admin_ticket


def quest_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть квест", web_app=WebAppInfo(url=settings.webapp_url))
    ]])


def admin_keyboard(settings: Settings, user_id: int) -> InlineKeyboardMarkup:
    ticket = create_admin_ticket(user_id, settings)
    url = f"{settings.admin_url}?ticket={ticket}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть CRM", web_app=WebAppInfo(url=url))
    ]])


async def setup_bot_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть квест"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="admin", description="Панель управления"),
        BotCommand(command="participant", description="Статус участника (админ)"),
    ])
    await bot.set_my_short_description("Квест BBBIKE по трём точкам Красной Поляны")
    await bot.set_my_description(
        "Выбирай партнёрские точки в любом порядке, строй маршрут, ставь QR-штампы "
        "и забирай подарки. После трёх точек — Premium BBBIKE."
    )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Открыть квест", web_app=WebAppInfo(url=settings.webapp_url))
    )


def build_router(service: QuestService, settings: Settings) -> Router:
    router = Router(name="quest")

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        if message.chat.type != "private" or not message.from_user:
            return
        text = (
            "<b>Добро пожаловать в квест BBBIKE по Красной Поляне</b>\n\n"
            "Здесь всё просто:\n"
            "1. Выбери любую из трёх точек.\n"
            "2. Построй маршрут в Яндекс Картах или 2ГИС.\n"
            "3. На месте отсканируй QR и забери подарок.\n\n"
            "Три штампа откроют Premium BBBIKE на 30 дней. "
            "Во время поездки смотри в телефон только после полной остановки."
        )
        await message.answer(text, reply_markup=quest_keyboard(settings))

    @router.message(Command("progress"))
    async def progress(message: Message):
        if not message.from_user:
            return
        await message.answer("Твой маршрут и все сохранённые подарки находятся в приложении.", reply_markup=quest_keyboard(settings))

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
        await message.answer(
            "<b>CRM квеста</b>\n\nСсылка персональная и открывается только из этой команды.",
            reply_markup=admin_keyboard(settings, message.from_user.id),
        )

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
