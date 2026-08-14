from __future__ import annotations

import logging
from datetime import timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup, Update, WebAppInfo,
)

from .config import Settings
from .service import QuestError, QuestService

log = logging.getLogger("bibibike.quest.bot")


def app_keyboard(settings: Settings, is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="Открыть квест", web_app=WebAppInfo(url=settings.webapp_url))]]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="Панель управления", web_app=WebAppInfo(url=settings.admin_url))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def location_help_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить текущую геопозицию", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или открой скрепку → Геопозиция → Транслировать",
    )


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть квест"),
        BotCommand(command="location", description="Как включить геопозицию"),
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
            "Открой приложение — оно сохранит прогресс, даже если связь прервётся.\n\n"
            "Во время поездки смотри в телефон только после полной остановки."
        )
        await message.answer(text, reply_markup=app_keyboard(settings, message.from_user.id in settings.admin_ids))

    @router.message(Command("location"))
    async def location_help(message: Message):
        await message.answer(
            "<b>Как включить геопозицию</b>\n\n"
            "1. Нажми скрепку в этом чате.\n"
            "2. Выбери «Геопозиция».\n"
            "3. Нажми «Транслировать геопозицию».\n\n"
            "Обычная точка ниже тоже поможет при проверке, но для маршрута лучше трансляция.",
            reply_markup=location_help_keyboard(),
        )

    @router.message(Command("progress"))
    async def progress(message: Message):
        if not message.from_user:
            return
        await message.answer("Твой маршрут и все сохранённые подарки находятся в приложении.", reply_markup=app_keyboard(settings))

    @router.message(Command("help"))
    async def help_message(message: Message):
        await message.answer(
            "Если геопозиция пропала, уже пройденные точки не исчезнут. Включи трансляцию снова и продолжай.\n\n"
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
            f"Последняя геопозиция: {item['last_location_at'] or 'не получена'}\n"
            f"Проверка: {item['integrity_status']}\n"
            f"Premium: {item['premium_status'] or 'не назначен'}"
        )

    async def handle_location(message: Message, event_update: Update, edited: bool):
        if message.chat.type != "private" or not message.from_user or not message.location:
            return
        try:
            state = await service.record_location(
                message.from_user.id,
                message.location.latitude,
                message.location.longitude,
                message.location.horizontal_accuracy,
                source="live",
                observed_at=message.edit_date or message.date,
                telegram_update_id=event_update.update_id,
                request_id=f"tg-{event_update.update_id}",
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
            if not edited:
                current = state.get("session") or {}
                await message.answer(
                    "<b>Геопозиция подключена</b>\n\nМаршрут обновляется. Можно вернуться в приложение.",
                    reply_markup=app_keyboard(settings),
                )
                log.info("Геопозиция подключена: user=%s session=%s", message.from_user.id, current.get("id"))
            elif state.get("event", {}).get("point_completed"):
                number = state["event"]["point_completed"]
                await message.answer(
                    f"<b>Точка {number} подтверждена</b>\n\nГеопозиция и QR совпали. Подарок уже сохранён в приложении.",
                    reply_markup=app_keyboard(settings),
                )
        except QuestError as exc:
            if not edited:
                await message.answer(exc.message, reply_markup=app_keyboard(settings))

    @router.message(F.location)
    async def initial_location(message: Message, event_update: Update):
        await handle_location(message, event_update, False)

    @router.edited_message(F.location)
    async def edited_location(message: Message, event_update: Update):
        await handle_location(message, event_update, True)

    return router
