# -*- coding: utf-8 -*-
# ============================================================
# BibiBike Bot — обновление поверх рабочей версии (старая БД совместима).
#
# СОХРАНЕНО ИЗ ОРИГИНАЛА:
#   - схема БД и bibibike_work.db (новые поля добавляются через ALTER)
#   - парсер parse_message / get_action_type, autodelete, /setname, /status, /help
#   - живое сообщение-отчёт в теме ОТЧЁТЫ, тема NPB (замены АКБ), роль Чарджер ⚡
#
# ЭТО ОБНОВЛЕНИЕ:
#   1. Отчёт: строка статуса 🟢/🔴 + подсветка 🟩 (перемещение/поправка/ремонт).
#   2. Парсер ручного открытия: «Смену начал Иванов И.И. скаут» (+ «смену закончил»).
#   3. Авто-закрытие смены: тумблер в мини-аппе (8/10/12 ч + 10 мин форы),
#      фоновая задача закрывает смену по дедлайну, если не закрыли сами.
#   4. Убрана команда /fix (ручное переопределение цифр).
#   5. Мини-апп: правка комментария в истории, сессия админки в localStorage,
#      админка без денег (длительность + разбивка по типам, закрытие смены).
#
# ФИЛОСОФИЯ: бот реагирует на управление сменой только по слешу; роль — это
#   подпись в отчёте, считается любое действие любому сотруднику.
# ============================================================

import asyncio
import logging
import re
import os
import sys
import json
import hmac
import hashlib
import base64
import html
import math
import aiosqlite
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import BaseFilter
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.exceptions import TelegramBadRequest

# Необязательно: подхватываем .env, если он есть (на BotHost переменные и так заданы).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
# Токен берём из переменных окружения. Разные хостинги называют её по-разному:
# BotHost отдаёт TOKEN / API_TOKEN / TELEGRAM_BOT_TOKEN, где-то это BOT_TOKEN.
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("API_TOKEN")
    or os.getenv("TOKEN")
)

GROUP_ID = -1003431950710   # Логистика Краснодар (t.me/c/3431950710)
CHAT1_THREAD_ID = 1        # Тех. Задания (рабочий)
CHAT2_THREAD_ID = 3        # ОТЧЕТЫ

# === Тема NPB (замены АКБ) ===
NPB_THREAD_ID = 866        # тема «NPB»

# Мультигород хранится в таблице cities. Эта запись нужна для бесшовной
# миграции существующей базы Краснодара. Дополнительные города задаются одной
# JSON-переменной CITIES_CONFIG_JSON, после запуска они также сохраняются в БД:
# [{"key":"stavropol","name":"Ставрополь","group_id":-100..., 
#   "topic_tasks":1,"topic_npb":2,"topic_reports":3,"timezone_offset":3}]
DEFAULT_CITY_KEY = "krasnodar"
DEFAULT_CITY_NAME = "Краснодар"
CITIES_CONFIG_JSON = os.getenv("CITIES_CONFIG_JSON", "").strip()

# ============================================================
# ГОРОДА-ЗАГЛУШКИ: Ставрополь, Красная Поляна, Химки
# ============================================================
# Такие же разделы, как у Краснодара выше. Пока стоят ЗАГЛУШКИ —
# когда получишь доступ к группе города, впиши реальные ID группы
# и тем (узнать: команда /topicid в нужной теме), и город заработает
# полностью: бот начнёт слушать и писать в его группе.
#
# Пока стоят заглушки: города видны в приложении (выбор города,
# регистрация, открытие смены), но отчёт в Telegram-группу города
# не постится — группы с таким ID не существует, ошибка уходит в лог,
# смена при этом сохраняется нормально (safe_flush_report_update).
#
# ВАЖНО: заглушки group_id обязаны быть РАЗНЫМИ у разных городов —
# в базе на group_id стоит UNIQUE. Не копируй одно значение в два города.

# --- Ставрополь (ЗАГЛУШКИ — заменить на реальные ID) ---
STAVROPOL_GROUP_ID      = -1000000000002   # ID группы «Логистика Ставрополь»
STAVROPOL_TOPIC_TASKS   = 1                # ID темы «Тех. Задания»
STAVROPOL_TOPIC_NPB     = 2                # ID темы «NPB»
STAVROPOL_TOPIC_REPORTS = 3                # ID темы «ОТЧЕТЫ»

# --- Красная Поляна (ЗАГЛУШКИ — заменить на реальные ID) ---
POLYANA_GROUP_ID        = -1000000000003   # ID группы «Логистика Красная Поляна»
POLYANA_TOPIC_TASKS     = 1                # ID темы «Тех. Задания»
POLYANA_TOPIC_NPB       = 2                # ID темы «NPB»
POLYANA_TOPIC_REPORTS   = 3                # ID темы «ОТЧЕТЫ»

# --- Химки: ОДИН город, но у каждой роли своя телеграм-группа ---
# В приложении сотрудник выбирает просто «Химки», а роль решает, в какую
# группу уйдёт его смена. Для базы это ОДИН город (один city_id), поэтому
# админка, история, КПД и месячные итоги видят весь город целиком —
# и скаутов, и водителей — без каких-либо изменений в запросах.
#
# NO_TOPIC — заглушка для темы, которой в группе нет (в Химках нет АКБ).
# Ни одно сообщение с таким thread_id не придёт, тема просто не сработает.
NO_TOPIC = -1

# Скауты Химки (t.me/c/3951407451)
KHIMKI_SCOUTS_GROUP_ID       = -1003951407451
KHIMKI_SCOUTS_TOPIC_MOVES    = 2            # «Перемещения»: 4-значные номера = перемещения
KHIMKI_SCOUTS_TOPIC_REPORTS  = 3            # «Отчёты»: начало и конец смены

# Водители Химки (t.me/c/4375614106)
KHIMKI_DRIVERS_GROUP_ID      = -1004375614106
KHIMKI_DRIVERS_TOPIC_MOVES   = 11           # «Подвозы»: 4-значные номера = перемещения
KHIMKI_DRIVERS_TOPIC_REPORTS = 2            # «Отчёты»: начало и конец смены

# Чарджеров в Химках нет. Появятся — добавь сюда третий блок и запись
# в "role_groups" ниже, больше ничего менять не потребуется.

# === НОВОЕ: живое сообщение обновляется не чаще, чем раз в N секунд ===
DEBOUNCE_SEC = 20

# ============================================================
# === НОВОЕ: МИНИ-ПРИЛОЖЕНИЕ (ЗАРПЛАТА) =====================
# ============================================================
# BotHost передаёт порт reverse proxy через стандартную переменную PORT.
# WEB_PORT оставлен запасным вариантом для совместимости со старой настройкой.
# Значение в панели BotHost и фактически прослушиваемый порт должны совпадать.
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT") or "3000")

# Имя бота (без @) и short-name Mini App из BotFather (/newapp) —
# нужны, чтобы под отчётом появилась кнопка «Моя зарплата».
# Юзернейм основного бота — для кнопок открытия приложения в группе.
BOT_USERNAME = os.getenv("BOT_USERNAME", "bbbotdelaetbot")
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "zp")

# === НОВОЕ: прямой https-адрес страницы приложения (бот сам её отдаёт на BotHost).
# Нужен для web_app-кнопки, которая открывает Mini App в один тап прямо из отчёта.
# Если пусто — кнопка откатится на старую url-ссылку t.me/бот/shortname.
# Задавать ТОЛЬКО через переменную окружения, дефолт — публичный адрес бота. ===
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bot-1784606726-6491-kponamarev.bothost.tech/")

# Домен, с которого открывается сама страница мини-приложения (GitHub Pages).
# Нужен для CORS, чтобы браузер разрешил запросы к API бота.
WEBAPP_ALLOW_ORIGIN = os.getenv("WEBAPP_ALLOW_ORIGIN", "https://voglogpro.github.io")

# === НОВОЕ: бот сам отдаёт страницу мини-приложения (index.html рядом с этим файлом) ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# Краснодар = московское время (UTC+3)
MSK = timezone(timedelta(hours=3))

# Модель оплаты по умолчанию для новых сотрудников
# Метка сборки: видна в логах при старте и в мини-приложении (Настройки).
# По ней сразу понятно, какая версия реально запущена на хостинге.
BUILD_VERSION = "2026-07-21 · стабильный ввод + Обед + BotHost runtime fix"

DEFAULT_PAY_TYPE = "hourly"       # hourly | salary | piece
DEFAULT_PAY_AMOUNT = 350.0        # ₽/час, ₽/смену или ₽/замену — зависит от типа

# Авто-закрытие смены: сотрудник выбирает длительность, бот добавляет фору
# (GRACE), чтобы человек успел закрыть сам/дописать комментарий до дедлайна.
AUTO_CLOSE_CHOICES = (8, 10, 12)  # часы на выбор в мини-приложении
DEFAULT_AUTO_CLOSE_HOURS = 10
AUTO_CLOSE_GRACE_MIN = 10

# Пароль админки не хранится в репозитории. Если ADMIN_PASSWORD пуст, админка
# отключена. После проверки сервер выдаёт подписанную сессию на несколько часов.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "123")
ADMIN_SESSION_TTL_SEC = int(os.getenv("ADMIN_SESSION_TTL_SEC", str(8 * 60 * 60)))
INIT_DATA_MAX_AGE_SEC = int(os.getenv("INIT_DATA_MAX_AGE_SEC", str(24 * 60 * 60)))
CITY_MEMBERSHIP_TTL_SEC = int(os.getenv("CITY_MEMBERSHIP_TTL_SEC", "300"))
# При первом успешном входе каждый администратор закрепляется за текущим
# городом в admin_city_access. Обычные настройки профиля эту связь не меняют.

def _webapp_button():
    """Кнопка под отчётом, открывающая мини-приложение прямо из группы.

    ВАЖНО: web_app-кнопки в inline-клавиатуре разрешены Telegram ТОЛЬКО в
    приватных чатах с ботом. В группе (а отчёты постятся в группу) такая кнопка
    вызывает Bad Request: BUTTON_TYPE_INVALID. Поэтому под отчётом в группе
    используем url-кнопку t.me/бот/shortname — она открывает то же Mini App
    в один тап и разрешена в группах.
    """
    if BOT_USERNAME:
        url = f"https://t.me/{BOT_USERNAME}/{WEBAPP_SHORTNAME}"
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⚡ Смена", url=url)]]
        )
    return None

# Список районов больше не ограничивает открытие смены (район — любой текст),
# оставлен для истории:
DISTRICTS = ["красная", "фмр", "юмр", "восточка", "ставрополька", "гмр"]

# ИНИЦИАЛИЗАЦИЯ РОУТЕРОВ
work_router = Router()
cmd_router = Router()

# ============================================================
# ЛОГИРОВАНИЕ  (пишем в stdout, чтобы BotHost точно показывал логи)
# ============================================================
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
print("== BibiBike Bot: процесс стартовал, читаю настройки ==", flush=True)

# Проверка наличия токена перед запуском
if not BOT_TOKEN:
    print(
        "КРИТИЧЕСКАЯ ОШИБКА: токен бота не найден ни в одной переменной "
        "(BOT_TOKEN / TOKEN / API_TOKEN / TELEGRAM_BOT_TOKEN). "
        "Проверь переменные окружения бота.",
        flush=True,
    )
    logger.error("Токен не найден — выхожу.")
    sys.exit(1)

# === НОВОЕ: бот создаётся на уровне модуля, чтобы редактировать живое сообщение из любых функций ===
bot = Bot(token=BOT_TOKEN)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
DB_PATH = os.path.join(os.getenv("DATA_DIR", BASE_DIR), "bibibike_work.db")
# База лежит в постоянной папке (на BotHost это /app/data), поэтому смены,
# зарплаты и история НЕ обнуляются при обновлении бота из GitHub.

CITIES_BY_ID = {}
CITIES_BY_GROUP = {}
# city_id -> {роль: вариант города с группой и темами этой роли}
CITY_ROLE_GROUPS = {}


class ActiveShiftExists(Exception):
    """У сотрудника уже есть активная смена в одном из городов."""


def _city_tz(city):
    """Часовой пояс города как фиксированный UTC offset (для городов РФ)."""
    try:
        offset = int((city or {}).get("timezone_offset", 3))
    except (TypeError, ValueError):
        offset = 3
    return timezone(timedelta(hours=max(-12, min(14, offset))))


def _configured_cities():
    configs = [{
        "key": DEFAULT_CITY_KEY,
        "name": DEFAULT_CITY_NAME,
        "group_id": GROUP_ID,
        "topic_tasks": CHAT1_THREAD_ID,
        "topic_npb": NPB_THREAD_ID,
        "topic_reports": CHAT2_THREAD_ID,
        "timezone_offset": 3,
    }, {
        # Ставрополь — работает на заглушках, впиши реальные ID выше
        "key": "stavropol",
        "name": "Ставрополь",
        "group_id": STAVROPOL_GROUP_ID,
        "topic_tasks": STAVROPOL_TOPIC_TASKS,
        "topic_npb": STAVROPOL_TOPIC_NPB,
        "topic_reports": STAVROPOL_TOPIC_REPORTS,
        "timezone_offset": 3,
    }, {
        # Красная Поляна — работает на заглушках, впиши реальные ID выше
        "key": "krasnaya_polyana",
        "name": "Красная Поляна",
        "group_id": POLYANA_GROUP_ID,
        "topic_tasks": POLYANA_TOPIC_TASKS,
        "topic_npb": POLYANA_TOPIC_NPB,
        "topic_reports": POLYANA_TOPIC_REPORTS,
        "timezone_offset": 3,
    }, {
        # Химки — ОДИН город, две группы по ролям.
        # Базовые topic_* = группа скаутов (она же group_id города).
        # Водители описаны в role_groups: своя группа и свои темы.
        "key": "khimki",
        "name": "Химки",
        "group_id": KHIMKI_SCOUTS_GROUP_ID,
        "topic_tasks": KHIMKI_SCOUTS_TOPIC_MOVES,
        "topic_moves": None,   # единый парсер, как в Краснодаре
        "topic_npb": NO_TOPIC,          # темы АКБ в Химках нет
        "topic_reports": KHIMKI_SCOUTS_TOPIC_REPORTS,
        "timezone_offset": 3,
        "role_groups": [
            {
                "role": "Скаут",
                "group_id": KHIMKI_SCOUTS_GROUP_ID,
                "topic_tasks": KHIMKI_SCOUTS_TOPIC_MOVES,
                "topic_moves": None,   # единый парсер, как в Краснодаре
                "topic_npb": NO_TOPIC,
                "topic_reports": KHIMKI_SCOUTS_TOPIC_REPORTS,
            },
            {
                "role": "Водитель",
                "group_id": KHIMKI_DRIVERS_GROUP_ID,
                "topic_tasks": KHIMKI_DRIVERS_TOPIC_MOVES,
                "topic_moves": None,   # единый парсер, как в Краснодаре
                "topic_npb": NO_TOPIC,
                "topic_reports": KHIMKI_DRIVERS_TOPIC_REPORTS,
            },
        ],
    }]
    if not CITIES_CONFIG_JSON:
        return configs
    try:
        raw = json.loads(CITIES_CONFIG_JSON)
        if isinstance(raw, dict):
            raw = [dict(value, key=key) for key, value in raw.items()]
        if not isinstance(raw, list):
            raise ValueError("ожидался список или объект")
        by_key = {item["key"]: item for item in configs}
        for item in raw:
            if not isinstance(item, dict):
                continue
            city = dict(item)
            key = str(city.get("key") or "").strip().lower()
            required = ("name", "group_id", "topic_tasks", "topic_npb", "topic_reports")
            if not key or any(city.get(field) is None for field in required):
                logger.warning("Пропущена неполная запись города в CITIES_CONFIG_JSON")
                continue
            city["key"] = key
            if city.get("topic_moves") is not None:
                try:
                    city["topic_moves"] = int(city["topic_moves"])
                except (TypeError, ValueError):
                    city["topic_moves"] = None
            for field in ("group_id", "topic_tasks", "topic_npb", "topic_reports"):
                city[field] = int(city[field])
            city["timezone_offset"] = int(city.get("timezone_offset", 3))
            by_key[key] = city
        return list(by_key.values())
    except Exception as exc:
        logger.error(f"CITIES_CONFIG_JSON не прочитан: {exc}. Использую Краснодар.")
        return configs


async def refresh_cities_cache(db=None):
    own_connection = db is None
    if own_connection:
        db = await aiosqlite.connect(DB_PATH)
    try:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM cities WHERE is_active = 1")).fetchall()
        try:
            role_rows = await (await db.execute("SELECT * FROM city_role_groups")).fetchall()
        except Exception:
            role_rows = []          # таблицы ещё нет (первый запуск) — работаем как раньше
        roles_by_city = {}
        for rrow in role_rows:
            rg = dict(rrow)
            roles_by_city.setdefault(rg["city_id"], []).append(rg)

        CITIES_BY_ID.clear()
        CITIES_BY_GROUP.clear()
        CITY_ROLE_GROUPS.clear()
        for row in rows:
            city = dict(row)
            city_id = city["id"]
            CITIES_BY_ID[city_id] = city
            CITIES_BY_GROUP[city["group_id"]] = city

            # Ролевые группы: на каждую делаем «вариант» города — та же запись
            # (тот же city["id"]!), но с group_id и темами этой группы.
            # Благодаря общему city_id вся статистика города остаётся единой.
            for rg in roles_by_city.get(city_id, []):
                variant = dict(city)
                variant["group_id"] = rg["group_id"]
                variant["topic_tasks"] = rg["topic_tasks"]
                variant["topic_npb"] = rg["topic_npb"]
                variant["topic_moves"] = rg["topic_moves"]
                variant["topic_reports"] = rg["topic_reports"]
                variant["role_group"] = rg["role"]
                CITIES_BY_GROUP[rg["group_id"]] = variant
                CITY_ROLE_GROUPS.setdefault(city_id, {})[_norm_role(rg["role"])] = variant
    finally:
        if own_connection:
            await db.close()


def _norm_role(role):
    return (role or "").strip().lower()


def get_city(city_id):
    return CITIES_BY_ID.get(city_id)


def city_role_groups(city_id):
    """Ролевые группы города: {'скаут': вариант, 'водитель': вариант}.

    Пусто для обычных городов (Краснодар) — там одна группа на всех.
    """
    return CITY_ROLE_GROUPS.get(city_id) or {}


def city_for_role(city_id, role):
    """Куда писать смену сотрудника: вариант города под его роль.

    Для городов без ролевых групп возвращает город как есть — поведение
    Краснодара не меняется вообще.
    """
    city = get_city(city_id)
    if not city:
        return None
    groups = city_role_groups(city_id)
    if not groups:
        return city
    return groups.get(_norm_role(role)) or city


def city_requires_role(city_id):
    """У города группы разделены по ролям — значит роль обязательна."""
    return bool(city_role_groups(city_id))


def city_supported_roles(city_id):
    """Роли, для которых в городе есть группа (для понятной ошибки)."""
    groups = city_role_groups(city_id)
    return [variant.get("role_group") for variant in groups.values()]


def get_city_by_group(group_id):
    return CITIES_BY_GROUP.get(group_id)


def get_default_city():
    for city in CITIES_BY_ID.values():
        if city.get("city_key") == DEFAULT_CITY_KEY:
            return city
    return next(iter(CITIES_BY_ID.values()), None)

async def init_db():
    _kpi_refreshed_hours.clear()
    repair_shift_ids = []
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                group_id INTEGER NOT NULL UNIQUE,
                topic_tasks INTEGER NOT NULL,
                topic_npb INTEGER NOT NULL,
                topic_moves INTEGER,
                topic_reports INTEGER NOT NULL,
                timezone_offset INTEGER NOT NULL DEFAULT 3,
                is_active INTEGER NOT NULL DEFAULT 1,
                managed_by_config INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Ролевые группы города: у одной роли — своя телеграм-группа со своими
        # темами (Химки). Город при этом остаётся ОДНИМ (один city_id), поэтому
        # админка, история и КПД видят весь город целиком без изменений.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS city_role_groups (
                city_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                group_id INTEGER NOT NULL UNIQUE,
                topic_tasks INTEGER NOT NULL,
                topic_npb INTEGER NOT NULL,
                topic_moves INTEGER,
                topic_reports INTEGER NOT NULL,
                PRIMARY KEY (city_id, role)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                role TEXT,
                city_id INTEGER,
                pay_type TEXT DEFAULT 'hourly',
                pay_amount REAL DEFAULT 350,
                edit_mode INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                full_name TEXT,
                role TEXT,
                start_time TEXT,
                end_time TEXT,
                district TEXT,
                comment TEXT,
                is_active INTEGER DEFAULT 1,
                report_msg_id INTEGER,
                created_at TEXT,
                earned REAL DEFAULT 0,
                pay_type_snap TEXT,
                pay_amount_snap REAL,
                city_id INTEGER,
                start_at TEXT,
                end_at TEXT,
                source TEXT DEFAULT 'bot',
                source_message_id INTEGER,
                on_lunch INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shift_id INTEGER,
                message_id INTEGER,
                action_type TEXT,
                bike_codes TEXT,
                quantity INTEGER DEFAULT 0,
                city_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kpi_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                snapshot_hour TEXT NOT NULL,
                actions_count INTEGER NOT NULL DEFAULT 0,
                worked_minutes INTEGER NOT NULL DEFAULT 0,
                efficiency REAL NOT NULL DEFAULT 0,
                UNIQUE(city_id, user_id, snapshot_hour)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monthly_aggregates (
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                full_name TEXT,
                role TEXT,
                shifts_count INTEGER NOT NULL DEFAULT 0,
                worked_minutes INTEGER NOT NULL DEFAULT 0,
                actions_count INTEGER NOT NULL DEFAULT 0,
                earned REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(city_id, user_id, month)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manual_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'needs_review',
                parse_error TEXT,
                shift_id INTEGER,
                sender_name TEXT,
                pay_type_snap TEXT,
                pay_amount_snap REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(city_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS work_message_links (
                city_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                shift_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_event_version REAL,
                PRIMARY KEY(city_id, chat_id, user_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_city_access (
                user_id INTEGER PRIMARY KEY,
                city_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # Декады — расчётные периоды админки. «Обнуление» счётчиков это старт
        # новой декады: данные смен остаются в БД, меняется только точка отсчёта.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payroll_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                created_by INTEGER,
                created_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_periods_city "
            "ON payroll_periods(city_id, ended_at)"
        )
        await db.commit()

        # === МИГРАЦИЯ: добавляем chat_id в ключ work_message_links ===
        # В городе с несколькими группами (Химки: скауты + водители) один
        # city_id обслуживает два чата. message_id в Telegram уникален только
        # ВНУТРИ чата, поэтому старый ключ (city_id, user_id, message_id)
        # давал коллизии: привязка из одной группы перекрывала другую, и
        # действия молча не записывались. Ключ теперь включает chat_id.
        try:
            cols = await (await db.execute("PRAGMA table_info(work_message_links)")).fetchall()
            has_chat = any((c[1] if not isinstance(c, dict) else c["name"]) == "chat_id"
                           for c in cols)
            if cols and not has_chat:
                logger.info("Миграция work_message_links: добавляю chat_id в ключ…")
                await db.execute("""
                    CREATE TABLE work_message_links_new (
                        city_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL DEFAULT 0,
                        user_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        shift_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        last_event_version REAL,
                        PRIMARY KEY(city_id, chat_id, user_id, message_id)
                    )
                """)
                # chat_id старым связям проставляем по основной группе города
                await db.execute("""
                    INSERT OR IGNORE INTO work_message_links_new
                        (city_id, chat_id, user_id, message_id, shift_id, created_at,
                         last_event_version)
                    SELECT l.city_id,
                           COALESCE((SELECT c.group_id FROM cities c WHERE c.id = l.city_id), 0),
                           l.user_id, l.message_id, l.shift_id, l.created_at, l.last_event_version
                    FROM work_message_links l
                """)
                await db.execute("DROP TABLE work_message_links")
                await db.execute("ALTER TABLE work_message_links_new RENAME TO work_message_links")
                await db.commit()
                logger.info("Миграция work_message_links: готово")
        except Exception as exc:
            # Миграция не должна ронять старт бота ни при каких условиях.
            logger.warning(f"Миграция work_message_links пропущена: {exc}")

        # Автоматическая миграция для старых баз данных
        try:
            await db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
            await db.commit()
            logger.info("Миграция: Колонка message_id успешно добавлена в таблицу actions.")
        except aiosqlite.OperationalError:
            pass

        # === НОВОЕ: миграция под живое сообщение — храним id сообщения-отчёта смены ===
        try:
            await db.execute("ALTER TABLE shifts ADD COLUMN report_msg_id INTEGER")
            await db.commit()
            logger.info("Миграция: Колонка report_msg_id успешно добавлена в таблицу shifts.")
        except aiosqlite.OperationalError:
            pass

        # === НОВОЕ: модель оплаты у сотрудника (для мини-приложения) ===
        for ddl in [
            "ALTER TABLE users ADD COLUMN pay_type TEXT DEFAULT 'hourly'",
            "ALTER TABLE users ADD COLUMN pay_amount REAL DEFAULT 350",
            # === НОВОЕ: тумблер «Режим редактирования» (личный, у каждого свой) ===
            "ALTER TABLE users ADD COLUMN edit_mode INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN city_id INTEGER",
            # Авто-закрытие смены: личный дефолт сотрудника (вкл/выкл + часы)
            "ALTER TABLE users ADD COLUMN auto_close INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN auto_close_hours INTEGER DEFAULT 10",
        ]:
            try:
                await db.execute(ddl); await db.commit()
            except aiosqlite.OperationalError:
                pass

        # === НОВОЕ: дата смены + замороженный заработок (для истории/зарплаты) ===
        for ddl in [
            "ALTER TABLE shifts ADD COLUMN created_at TEXT",
            "ALTER TABLE shifts ADD COLUMN earned REAL DEFAULT 0",
            "ALTER TABLE shifts ADD COLUMN pay_type_snap TEXT",
            "ALTER TABLE shifts ADD COLUMN pay_amount_snap REAL",
            "ALTER TABLE shifts ADD COLUMN city_id INTEGER",
            "ALTER TABLE shifts ADD COLUMN start_at TEXT",
            "ALTER TABLE shifts ADD COLUMN end_at TEXT",
            "ALTER TABLE shifts ADD COLUMN source TEXT DEFAULT 'bot'",
            "ALTER TABLE shifts ADD COLUMN source_message_id INTEGER",
            # Дедлайн авто-закрытия (уже с учётом +10 мин форы), NULL = не закрывать
            "ALTER TABLE shifts ADD COLUMN auto_close_at TEXT",
            # Явная привязка смены к декаде. NULL у старых смен — для них
            # период определяется по дате старта (обратная совместимость).
            "ALTER TABLE shifts ADD COLUMN period_id INTEGER",
            # Дневные/ночные декады: группа расчётного периода.
            "ALTER TABLE payroll_periods ADD COLUMN segment TEXT",
            # Информационный статус для живого отчёта. Не участвует во времени,
            # заработке, KPI или подсчёте действий.
            "ALTER TABLE shifts ADD COLUMN on_lunch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE actions ADD COLUMN city_id INTEGER",
            "ALTER TABLE cities ADD COLUMN managed_by_config INTEGER NOT NULL DEFAULT 0",
            # Тема, где голые 4-значные номера = перемещения (Химки). NULL —
            # у города такой темы нет, парсер работает как раньше, по глаголам.
            "ALTER TABLE cities ADD COLUMN topic_moves INTEGER",
            # chat_id в actions: message_id уникален внутри ЧАТА, а не глобально.
            # Без этого в городе с двумя группами (Химки) сообщения из разных
            # групп с одинаковым message_id затирали друг друга.
            "ALTER TABLE actions ADD COLUMN chat_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manual_reports ADD COLUMN sender_name TEXT",
            "ALTER TABLE manual_reports ADD COLUMN pay_type_snap TEXT",
            "ALTER TABLE manual_reports ADD COLUMN pay_amount_snap REAL",
            "ALTER TABLE work_message_links ADD COLUMN last_event_version REAL",
        ]:
            try:
                await db.execute(ddl); await db.commit()
            except aiosqlite.OperationalError:
                pass

        # Города, которыми управляет CITIES_CONFIG_JSON, деактивируются при
        # удалении из конфига. Записи, добавленные админом напрямую в БД,
        # managed_by_config=0 и не затрагиваются.
        await db.execute(
            "UPDATE cities SET is_active = 0 WHERE managed_by_config = 1 "
            "AND id NOT IN (SELECT DISTINCT city_id FROM shifts WHERE is_active = 1)"
        )
        for city in _configured_cities():
            moves_topic = city.get("topic_moves")
            moves_topic = int(moves_topic) if moves_topic is not None else None
            params = (city["key"], city["name"], int(city["group_id"]),
                      int(city["topic_tasks"]), int(city["topic_npb"]), moves_topic,
                      int(city["topic_reports"]), int(city.get("timezone_offset", 3)))
            try:
                await db.execute(
                    "INSERT INTO cities (city_key, name, group_id, topic_tasks, topic_npb, "
                    "topic_moves, topic_reports, timezone_offset, is_active, managed_by_config) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1) "
                    "ON CONFLICT(city_key) DO UPDATE SET name=excluded.name, "
                    "group_id=excluded.group_id, topic_tasks=excluded.topic_tasks, "
                    "topic_npb=excluded.topic_npb, topic_moves=excluded.topic_moves, "
                    "topic_reports=excluded.topic_reports, "
                    "timezone_offset=excluded.timezone_offset, is_active=1, managed_by_config=1",
                    params
                )
            except Exception:
                # Такой group_id уже занят записью с другим city_key: обновляем её,
                # не трогая ключ. Без этого запуск падает на боевой базе с
                # UNIQUE(cities.group_id), и веб-сервер вообще не поднимается —
                # именно так «умерла» Основа. Синхронизация городов ни при каком
                # раскладе не должна ронять старт бота.
                await db.execute(
                    "UPDATE cities SET name = ?, topic_tasks = ?, topic_npb = ?, "
                    "topic_moves = ?, topic_reports = ?, timezone_offset = ?, is_active = 1, "
                    "managed_by_config = 1 WHERE group_id = ?",
                    (city["name"], int(city["topic_tasks"]), int(city["topic_npb"]),
                     moves_topic, int(city["topic_reports"]),
                     int(city.get("timezone_offset", 3)), int(city["group_id"]))
                )
        # Ролевые группы: пересобираем под текущий конфиг. Города без
        # role_groups (Краснодар и др.) не затрагиваются вообще.
        for city in _configured_cities():
            role_groups = city.get("role_groups") or []
            row = await (await db.execute(
                "SELECT id FROM cities WHERE city_key = ?", (city["key"],)
            )).fetchone()
            if not row:
                continue
            cid = row[0]
            await db.execute("DELETE FROM city_role_groups WHERE city_id = ?", (cid,))
            for rg in role_groups:
                moves = rg.get("topic_moves")
                try:
                    await db.execute(
                        "INSERT INTO city_role_groups (city_id, role, group_id, topic_tasks, "
                        "topic_npb, topic_moves, topic_reports) VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(group_id) DO UPDATE SET city_id=excluded.city_id, "
                        "role=excluded.role, topic_tasks=excluded.topic_tasks, "
                        "topic_npb=excluded.topic_npb, topic_moves=excluded.topic_moves, "
                        "topic_reports=excluded.topic_reports",
                        (cid, rg["role"], int(rg["group_id"]), int(rg["topic_tasks"]),
                         int(rg["topic_npb"]), int(moves) if moves is not None else None,
                         int(rg["topic_reports"]))
                    )
                except Exception as exc:
                    # Ролевые группы не должны ронять старт бота ни при каких условиях.
                    logger.warning(f"Не удалось записать ролевую группу {rg.get('role')}: {exc}")
        await db.commit()
        await refresh_cities_cache(db)
        default_city = get_default_city()
        if not default_city:
            raise RuntimeError("В таблице cities нет активного города")

        default_city_id = default_city["id"]
        await db.execute("UPDATE users SET city_id = ? WHERE city_id IS NULL", (default_city_id,))
        await db.execute("UPDATE shifts SET city_id = ? WHERE city_id IS NULL", (default_city_id,))
        await db.execute(
            "UPDATE actions SET city_id = COALESCE((SELECT city_id FROM shifts "
            "WHERE shifts.id = actions.shift_id), ?) WHERE city_id IS NULL",
            (default_city_id,)
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_user_city_active "
            "ON shifts(user_id, city_id, is_active)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_shift_city ON actions(shift_id, city_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_city_active_start "
            "ON shifts(city_id, is_active, start_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_city_user_start "
            "ON shifts(city_id, user_id, start_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_message_links_shift "
            "ON work_message_links(shift_id)"
        )
        duplicate_active = await (await db.execute(
            "SELECT user_id, MAX(id) AS keep_id, COUNT(*) AS amount FROM shifts "
            "WHERE is_active = 1 GROUP BY user_id HAVING COUNT(*) > 1"
        )).fetchall()
        for uid, keep_id, amount in duplicate_active:
            await db.execute(
                "UPDATE shifts SET is_active = 0, end_time = COALESCE(end_time, start_time), "
                "end_at = COALESCE(end_at, start_at), earned = 0, on_lunch = 0 "
                "WHERE user_id = ? AND is_active = 1 AND id <> ?",
                (uid, keep_id)
            )
            logger.warning(
                f"Миграция: у uid={uid} было {amount} активных смен; "
                "оставлена самая новая."
            )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_shift_per_user "
            "ON shifts(user_id) WHERE is_active = 1"
        )
        # Версии до постоянной привязки сообщения хранили её только в actions.
        # Заполняем новую таблицу до начала обработки edit-событий.
        legacy_links = await (await db.execute(
            "SELECT a.city_id, a.user_id, a.message_id, a.shift_id, "
            "COALESCE(s.created_at, ?) FROM actions a JOIN shifts s ON s.id = a.shift_id "
            "WHERE a.city_id IS NOT NULL AND a.message_id IS NOT NULL AND a.id = "
            "(SELECT MAX(a2.id) FROM actions a2 WHERE a2.city_id = a.city_id "
            "AND a2.user_id = a.user_id AND a2.message_id = a.message_id)",
            (datetime.now(timezone.utc).isoformat(),)
        )).fetchall()
        await db.executemany(
            "INSERT OR IGNORE INTO work_message_links "
            "(city_id, user_id, message_id, shift_id, created_at) VALUES (?, ?, ?, ?, ?)",
            legacy_links
        )

        # Если предыдущий аварийный запуск успел создать две смены из одного
        # Telegram-отчёта, оставляем последнюю до создания UNIQUE-индекса.
        duplicate_sources = await (await db.execute(
            "SELECT city_id, source_message_id, MAX(id) AS keep_id FROM shifts "
            "WHERE source_message_id IS NOT NULL GROUP BY city_id, source_message_id "
            "HAVING COUNT(*) > 1"
        )).fetchall()
        for duplicate_city_id, source_message_id, keep_id in duplicate_sources:
            stale_ids = [row[0] for row in await (await db.execute(
                "SELECT id FROM shifts WHERE city_id = ? AND source_message_id = ? AND id <> ?",
                (duplicate_city_id, source_message_id, keep_id)
            )).fetchall()]
            for stale_id in stale_ids:
                await db.execute(
                    "UPDATE manual_reports SET shift_id = ? WHERE shift_id = ?", (keep_id, stale_id)
                )
                await db.execute("DELETE FROM actions WHERE shift_id = ?", (stale_id,))
                await db.execute("DELETE FROM shifts WHERE id = ?", (stale_id,))
            logger.warning(
                f"Миграция: дубли ручного отчёта {source_message_id} города "
                f"{duplicate_city_id} объединены в смену {keep_id}."
            )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_report_source "
            "ON shifts(city_id, source_message_id) WHERE source_message_id IS NOT NULL"
        )

        # Старые строки получают полноценные даты из created_at и HH:MM.
        db.row_factory = aiosqlite.Row
        old_rows = await (await db.execute(
            "SELECT id, city_id, start_time, end_time, created_at, start_at, end_at "
            "FROM shifts WHERE start_at IS NULL"
        )).fetchall()
        for row in old_rows:
            city = get_city(row["city_id"]) or default_city
            tz = _city_tz(city)
            try:
                base_date = datetime.fromisoformat(row["created_at"]).astimezone(tz).date() \
                    if row["created_at"] else datetime.now(tz).date()
            except Exception:
                base_date = datetime.now(tz).date()
            try:
                hour, minute = map(int, (row["start_time"] or "0:00").split(":"))
                start_at = datetime.combine(base_date, datetime.min.time(), tzinfo=tz).replace(
                    hour=hour, minute=minute)
                end_at = None
                if row["end_time"]:
                    eh, em = map(int, row["end_time"].split(":"))
                    end_at = datetime.combine(base_date, datetime.min.time(), tzinfo=tz).replace(
                        hour=eh, minute=em)
                    if end_at < start_at:
                        end_at += timedelta(days=1)
                await db.execute(
                    "UPDATE shifts SET start_at = ?, end_at = COALESCE(end_at, ?) WHERE id = ?",
                    (start_at.isoformat(), end_at.isoformat() if end_at else None, row["id"])
                )
            except Exception as exc:
                logger.warning(f"Не удалось восстановить дату смены {row['id']}: {exc}")
        await db.commit()

        repair_shift_ids = [row[0] for row in await (await db.execute(
            "SELECT id FROM shifts WHERE is_active = 0 AND pay_type_snap IS NULL "
            "AND COALESCE(earned, 0) = 0"
        )).fetchall()]

    logger.info("БД готова")
    for shift_id in repair_shift_ids:
        try:
            await freeze_earned(shift_id)
        except Exception as exc:
            logger.error(f"Не удалось восстановить расчёт закрытой смены {shift_id}: {exc}")

async def add_user(uid, name, role, city_id=None):
    # ВАЖНО: не используем INSERT OR REPLACE — иначе стёрлись бы pay_type/pay_amount.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, city_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, "
            "role=excluded.role, city_id=COALESCE(excluded.city_id, users.city_id)",
            (uid, name, role, city_id)
        )
        await db.commit()
async def set_user_city(uid, city_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, city_id) VALUES (?, '', '', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET city_id=excluded.city_id",
            (uid, city_id)
        )
        await db.commit()

# === НОВОЕ: сохранить модель оплаты (из настроек мини-приложения) ===
async def set_user_pay(uid, pay_type, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, pay_type, pay_amount) "
            "VALUES (?, '', '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET pay_type=excluded.pay_type, pay_amount=excluded.pay_amount",
            (uid, pay_type, amount)
        )
        await db.commit()

# === НОВОЕ: сохранить тумблер «Режим редактирования» (из настроек мини-приложения).
# Обновляет ТОЛЬКО edit_mode, не затрагивая имя/роль/оплату. ===
async def set_user_edit_mode(uid, on):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, edit_mode) "
            "VALUES (?, '', '', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET edit_mode=excluded.edit_mode",
            (uid, 1 if on else 0)
        )
        await db.commit()

# Сохранить дефолт авто-закрытия (вкл/выкл + часы) — на все следующие смены.
async def set_user_auto_close(uid, enabled, hours):
    hours = hours if hours in AUTO_CLOSE_CHOICES else DEFAULT_AUTO_CLOSE_HOURS
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, auto_close, auto_close_hours) "
            "VALUES (?, '', '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET auto_close=excluded.auto_close, "
            "auto_close_hours=excluded.auto_close_hours",
            (uid, 1 if enabled else 0, hours)
        )
        await db.commit()

# ── Декады (расчётные периоды админки) ────────────────────────
# Счётчики «обнуляются» стартом новой декады: смены остаются в БД,
# меняется только точка отсчёта. Ничего не удаляется.

# ── Дневные и ночные декады ───────────────────────────────────
# Сотрудники делятся на группы по времени ОТКРЫТИЯ смены:
#   день  — старт с 05:00 до 16:59;
#   ночь  — старт с 17:00 до 04:59.
# У каждой группы своя декада и своё обнуление, чтобы начальник
# считал зарплату дневным и ночным раздельно.

DAY_SEGMENT_START = 5    # 05:00 — начало «дневного» окна
DAY_SEGMENT_END = 17     # 17:00 — с этого часа старт считается ночным

SEGMENT_LABELS = {"day": "Дневные", "night": "Ночные"}


def _shift_segment(shift, city=None):
    """'day' или 'night' — по часу открытия смены в часовом поясе города."""
    raw = shift.get("start_at") or shift.get("created_at")
    hour = None
    dt = _parse_datetime(raw) if raw else None
    if dt:
        if city:
            dt = dt.astimezone(_city_tz(city))
        hour = dt.hour
    else:
        # Запасной путь: строка "ЧЧ:ММ" из start_time.
        m = re.match(r"^(\d{1,2}):\d{2}", str(shift.get("start_time") or ""))
        if m:
            hour = int(m.group(1)) % 24
    if hour is None:
        return "day"
    return "day" if DAY_SEGMENT_START <= hour < DAY_SEGMENT_END else "night"


async def ensure_city_period(city_id, segment="day"):
    """Возвращает открытую декаду города для группы (day/night), создавая её.

    Первая декада группы начинается с начала текущего месяца — чтобы после
    обновления цифры не обнулились сами по себе. Старые записи без segment
    считаются дневными.
    """
    segment = "night" if segment == "night" else "day"
    city = get_city(city_id)
    if not city:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM payroll_periods WHERE city_id = ? AND ended_at IS NULL "
            "AND (segment = ? OR (? = 'day' AND segment IS NULL)) "
            "ORDER BY id DESC LIMIT 1", (city_id, segment, segment)
        )).fetchone()
        if row:
            period = dict(row)
            if not period.get("segment"):
                await db.execute(
                    "UPDATE payroll_periods SET segment = 'day' WHERE id = ?",
                    (period["id"],))
                # Бывшая общая декада становится дневной. Ночные смены,
                # привязанные к ней, отвязываем: дальше они сверяются с
                # ночной декадой по дате старта — как и вели себя раньше.
                legacy = await (await db.execute(
                    "SELECT id, start_at, created_at, start_time FROM shifts "
                    "WHERE period_id = ?", (period["id"],)
                )).fetchall()
                night_ids = [r["id"] for r in legacy
                             if _shift_segment(dict(r), city) == "night"]
                if night_ids:
                    marks = ",".join("?" * len(night_ids))
                    await db.execute(
                        f"UPDATE shifts SET period_id = NULL WHERE id IN ({marks})",
                        night_ids)
                await db.commit()
                period["segment"] = "day"
            return period
        now = datetime.now(_city_tz(city))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cur = await db.execute(
            "INSERT INTO payroll_periods (city_id, started_at, created_at, segment) "
            "VALUES (?, ?, ?, ?)",
            (city_id, month_start.isoformat(), now.isoformat(), segment)
        )
        await db.commit()
        return {"id": cur.lastrowid, "city_id": city_id,
                "started_at": month_start.isoformat(), "ended_at": None,
                "created_by": None, "created_at": now.isoformat(),
                "segment": segment}


async def city_periods(city_id):
    """Обе открытые декады города: {'day': {...}, 'night': {...}}."""
    return {
        "day": await ensure_city_period(city_id, "day"),
        "night": await ensure_city_period(city_id, "night"),
    }


def _shift_in_period(shift, periods, city=None):
    """Входит ли смена в ТЕКУЩУЮ декаду своей группы (день/ночь)."""
    segment = _shift_segment(shift, city)
    period = (periods or {}).get(segment) or {}
    pid = period.get("id")
    shift_pid = shift.get("period_id")
    if shift_pid:
        return shift_pid == pid
    started = shift.get("start_at") or shift.get("created_at")
    period_start = period.get("started_at")
    return bool(period_start and started and started >= period_start)


async def start_new_period(city_id, uid, segment="day"):
    """Закрывает текущую декаду группы и открывает новую.

    Обнуляется только выбранная группа: декада другой группы не трогается.
    Активные смены ЭТОЙ группы переезжают в новую декаду целиком.
    """
    segment = "night" if segment == "night" else "day"
    city = get_city(city_id)
    if not city:
        raise ValueError("Неизвестный город")
    # Гарантируем существование записи (и миграцию segment=NULL -> day).
    await ensure_city_period(city_id, segment)
    now = datetime.now(_city_tz(city))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute(
            "SELECT * FROM payroll_periods WHERE city_id = ? AND ended_at IS NULL "
            "AND (segment = ? OR (? = 'day' AND segment IS NULL)) "
            "ORDER BY id DESC LIMIT 1", (city_id, segment, segment)
        )).fetchone()
        if current:
            started = _parse_datetime(current["started_at"])
            # Две декады за одну минуту — почти наверняка двойной клик.
            if started and (now - started).total_seconds() < 60:
                await db.rollback()
                return dict(current)
            await db.execute(
                "UPDATE payroll_periods SET ended_at = ? WHERE id = ?",
                (now.isoformat(), current["id"])
            )
        cur = await db.execute(
            "INSERT INTO payroll_periods (city_id, started_at, created_by, created_at, segment) "
            "VALUES (?, ?, ?, ?, ?)",
            (city_id, now.isoformat(), uid, now.isoformat(), segment)
        )
        # Активные смены выбранной группы переносим в новую декаду: они ещё
        # не закрыты и не оплачены, поэтому относятся к периоду, в котором
        # завершатся. Смены другой группы не трогаем.
        active_rows = await (await db.execute(
            "SELECT id, start_at, created_at, start_time FROM shifts "
            "WHERE city_id = ? AND is_active = 1", (city_id,)
        )).fetchall()
        move_ids = [r["id"] for r in active_rows
                    if _shift_segment(dict(r), city) == segment]
        if move_ids:
            marks = ",".join("?" * len(move_ids))
            await db.execute(
                f"UPDATE shifts SET period_id = ? WHERE id IN ({marks})",
                [cur.lastrowid, *move_ids]
            )
        await db.commit()
        logger.info(
            f"Новая декада ({segment}) в городе {city_id} открыта админом {uid}; "
            f"перенесено активных смен: {len(move_ids)}."
        )
        return {"id": cur.lastrowid, "city_id": city_id,
                "started_at": now.isoformat(), "ended_at": None,
                "created_by": uid, "created_at": now.isoformat(),
                "segment": segment}


def _period_info(period, city, now=None):
    """Данные о декаде для фронта: дата старта и какой идёт день."""
    if not period:
        return None
    started = _parse_datetime(period.get("started_at"))
    now = now or datetime.now(_city_tz(city))
    day_number = 1
    if started:
        day_number = (now.date() - started.astimezone(_city_tz(city)).date()).days + 1
    return {
        "id": period.get("id"),
        "started_at": period.get("started_at"),
        "started_label": _fmt_date(period.get("started_at")),
        "day_number": max(1, day_number),
        "overdue": max(1, day_number) > 10,
        "opened_by": period.get("created_by"),
    }


async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def get_active_shift(uid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if city_id is None:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
                (uid,)
            )
        else:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 1 "
                "ORDER BY id DESC LIMIT 1", (uid, city_id)
            )
        r = await c.fetchone()
        return dict(r) if r else None

async def get_last_shift(uid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if city_id is None:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT 1",
                (uid,)
            )
        else:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "ORDER BY id DESC LIMIT 1", (uid, city_id)
            )
        r = await c.fetchone()
        return dict(r) if r else None

# === НОВОЕ: смена по id (нужно живому сообщению) ===
async def get_shift_by_id(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM shifts WHERE id = ?", (sid,))
        r = await c.fetchone()
        return dict(r) if r else None

def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _resolve_start_at(time_str, city, now=None):
    """Привязывает старт к сегодня; время >12ч «назад» считаем завтрашним
    (безопасный переход через полночь), позже текущего — отложенный старт сегодня."""
    tz = _city_tz(city)
    now = now.astimezone(tz) if now else datetime.now(tz)
    hour, minute = map(int, time_str.split(":"))
    candidate = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=hour, minute=minute)
    if now - candidate > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def _resolve_end_at(shift, time_str, city, now=None):
    tz = _city_tz(city)
    now = now.astimezone(tz) if now else datetime.now(tz)
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at:
        start_at = _resolve_start_at(shift["start_time"], city, now)
    start_at = start_at.astimezone(tz)
    hour, minute = map(int, time_str.split(":"))
    # Будущую смену можно закрыть как отменённую, но она не
    # должна превращаться в оплаченную будущую смену.
    if now < start_at:
        return start_at

    candidate = datetime.combine(start_at.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=hour, minute=minute)
    if candidate < start_at:
        candidate += timedelta(days=1)
    # Время окончания нельзя задавать в будущем: иначе бот начислит
    # ещё не отработанные часы. Две минуты — допуск на разницу часов.
    if candidate > now + timedelta(minutes=2):
        raise ValueError("Время окончания не может быть в будущем.")
    return candidate


def _resolve_manual_interval(start_time, end_time, city, message_time=None):
    """Привязывает закрытый ручной отчёт к дате его отправки."""
    tz = _city_tz(city)
    now = message_time.astimezone(tz) if message_time else datetime.now(tz)
    sh, sm = map(int, start_time.split(":"))
    eh, em = map(int, end_time.split(":"))
    end_at = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=eh, minute=em)
    # Ручной отчёт — уже завершённая смена. Если её конец ещё не
    # наступил сегодня, значит отчёт относится к прошлому дню.
    if end_at > now + timedelta(minutes=15):
        end_at -= timedelta(days=1)
    start_at = datetime.combine(end_at.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=sh, minute=sm)
    if start_at > end_at:
        start_at -= timedelta(days=1)
    return start_at, end_at


def _shift_worked_min(shift, now=None):
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at:
        if shift.get("start_time") and shift.get("end_time"):
            return _worked_min(shift["start_time"], shift["end_time"])
        return 0
    end_at = _parse_datetime(shift.get("end_at"))
    if not end_at:
        city = get_city(shift.get("city_id")) or get_default_city()
        tz = _city_tz(city)
        end_at = now.astimezone(tz) if now else datetime.now(tz)
    return max(0, int((end_at - start_at).total_seconds() // 60))


def _shift_is_scheduled(shift, now=None):
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at or not shift.get("is_active"):
        return False
    city = get_city(shift.get("city_id")) or get_default_city()
    current = now.astimezone(_city_tz(city)) if now else datetime.now(_city_tz(city))
    return current < start_at


async def start_shift(uid, name, role, time, district, city_id, source="bot",
                      source_message_id=None, now=None, auto_close=None,
                      auto_close_hours=None):
    city = get_city(city_id)
    if not city:
        raise ValueError("Неизвестный город")
    start_at = _resolve_start_at(time, city, now)
    # Авто-закрытие: явные значения из мини-приложения или сохранённый дефолт.
    if auto_close is None:
        u = await get_user(uid) or {}
        auto_close = bool(u.get("auto_close"))
        auto_close_hours = u.get("auto_close_hours")
    hours = auto_close_hours if auto_close_hours in AUTO_CLOSE_CHOICES else DEFAULT_AUTO_CLOSE_HOURS
    auto_close_at = None
    if auto_close:
        auto_close_at = (start_at + timedelta(hours=hours,
                                              minutes=AUTO_CLOSE_GRACE_MIN)).isoformat()
    # Смена закрепляется за декадой СВОЕЙ группы: день или ночь — по часу старта.
    shift_segment = _shift_segment({"start_at": start_at.isoformat()}, city)
    period = await ensure_city_period(city_id, shift_segment)
    period_id = (period or {}).get("id")
    async with aiosqlite.connect(DB_PATH) as db:
        # BEGIN IMMEDIATE сериализует два почти одновременных старта.
        await db.execute("BEGIN IMMEDIATE")
        active = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? AND is_active = 1 LIMIT 1", (uid,)
        )).fetchone()
        if active:
            await db.rollback()
            raise ActiveShiftExists()
        now_iso = (now.astimezone(_city_tz(city)) if now else datetime.now(_city_tz(city))).isoformat()
        c = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active, "
            "created_at, city_id, start_at, source, source_message_id, auto_close_at, period_id) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (uid, name, role, time, district, now_iso, city_id, start_at.isoformat(),
             source, source_message_id, auto_close_at, period_id)
        )
        await db.commit()
        return c.lastrowid

# === НОВОЕ: расчёт заработка ===
def _worked_min(start_time, end_time):
    sp = start_time.split(':'); ep = end_time.split(':')
    sm = int(sp[0]) * 60 + int(sp[1])
    em = int(ep[0]) * 60 + int(ep[1])
    if em < sm:
        em += 24 * 60
    return em - sm

def compute_earned(pay_type, amount, worked_min, battery_count):
    amount = amount or 0
    if pay_type == "salary":              # оклад за смену — фикс
        return round(amount, 2)
    if pay_type == "piece":               # сделка — за каждую замену АКБ
        return round(amount * (battery_count or 0), 2)
    return round(amount * (worked_min or 0) / 60.0, 2)   # почасовая

async def freeze_earned(sid):
    """Фиксируем сумму на момент закрытия смены — потом ставку можно менять, история не перепишется."""
    shift = await get_shift_by_id(sid)
    if not shift:
        return
    user = await get_user(shift['user_id']) or {}
    # Повторный /fix или правка ручного отчёта меняют цифры, но не
    # историческую ставку. Текущую ставку берём только при первом закрытии.
    pay_type = shift.get('pay_type_snap') or user.get('pay_type') or DEFAULT_PAY_TYPE
    amount = shift.get('pay_amount_snap')
    if amount is None:
        amount = user.get('pay_amount')
    if amount is None:
        amount = DEFAULT_PAY_AMOUNT
    stats = await get_stats(sid)
    wm = _shift_worked_min(shift) if shift.get('end_time') else 0
    start_at = _parse_datetime(shift.get("start_at"))
    end_at = _parse_datetime(shift.get("end_at"))
    if start_at and end_at and end_at <= start_at:
        earned = 0
    else:
        earned = compute_earned(pay_type, amount, wm, stats.get('battery', 0))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET earned = ?, pay_type_snap = ?, pay_amount_snap = ? WHERE id = ?",
            (earned, pay_type, amount, sid)
        )
        await db.commit()
    if start_at and shift.get("city_id"):
        await refresh_monthly_aggregate(
            shift["city_id"], shift["user_id"], start_at.strftime("%Y-%m")
        )

async def end_shift(uid, time, comment="", city_id=None, now=None):
    shift = await get_active_shift(uid, city_id)
    if not shift:
        return None
    city = get_city(shift.get("city_id")) or get_default_city()
    scheduled = _shift_is_scheduled(shift, now)
    end_at = _resolve_end_at(shift, time, city, now)
    stored_end_time = shift.get("start_time") if scheduled else time
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, end_at = ?, comment = ?, "
            "on_lunch = 0 "
            "WHERE id = ? AND is_active = 1",
            (stored_end_time, end_at.isoformat(), comment, shift["id"])
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        sid = shift["id"]
    # === НОВОЕ: заморозить заработок закрытой смены ===
    if sid:
        await freeze_earned(sid)
    return sid

# === НОВОЕ: запомнить id живого сообщения смены ===
async def set_report_msg_id(sid, mid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET report_msg_id = ? WHERE id = ?", (mid, sid))
        await db.commit()

async def add_action(uid, sid, mid, atype, codes=None, qty=0, city_id=None):
    cstr = ",".join(codes) if codes else ""
    if city_id is None:
        shift = await get_shift_by_id(sid)
        city_id = shift.get("city_id") if shift else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
            "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, sid, mid, atype, cstr, qty, city_id)
        )
        await db.commit()

async def delete_actions_by_message(uid, mid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        where = "user_id = ? AND message_id = ?"
        params = [uid, mid]
        if city_id is not None:
            where += " AND city_id = ?"
            params.append(city_id)
        rows = await (await db.execute(
            f"SELECT DISTINCT shift_id FROM actions WHERE {where} ORDER BY shift_id", params
        )).fetchall()
        await db.execute(
            f"DELETE FROM actions WHERE {where}", params
        )
        await db.commit()
        return [row[0] for row in rows]


async def get_action_shift_ids(uid, mid, city_id, chat_id=0):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT shift_id FROM actions WHERE user_id = ? AND message_id = ? "
            "AND city_id = ? AND chat_id = ? ORDER BY shift_id",
            (uid, mid, city_id, chat_id)
        )).fetchall()
        return [row[0] for row in rows]


async def replace_message_actions(uid, mid, city_id, shift_id, actions, event_version,
                                  chat_id=0):
    """Атомарно заменяет результат разбора сообщения; последняя правка побеждает.

    chat_id входит в ключ: в городе с двумя группами (Химки) message_id
    из разных чатов совпадают, и без chat_id привязка из чужой группы
    приводила к тихому отказу записи.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        version_row = await (await db.execute(
            "SELECT shift_id, last_event_version FROM work_message_links "
            "WHERE city_id = ? AND chat_id = ? AND user_id = ? AND message_id = ?",
            (city_id, chat_id, uid, mid)
        )).fetchone()
        shift_exists = await (await db.execute(
            "SELECT 1 FROM shifts WHERE id = ? AND user_id = ? AND city_id = ?",
            (shift_id, uid, city_id)
        )).fetchone()
        if not version_row or version_row[0] != shift_id or not shift_exists:
            await db.rollback()
            return [], False
        if (version_row[1] is not None
                and event_version < float(version_row[1])):
            await db.rollback()
            return [], False
        rows = await (await db.execute(
            "SELECT DISTINCT shift_id FROM actions WHERE user_id = ? AND message_id = ? "
            "AND city_id = ? AND chat_id = ? ORDER BY shift_id",
            (uid, mid, city_id, chat_id)
        )).fetchall()
        await db.execute(
            "DELETE FROM actions WHERE user_id = ? AND message_id = ? AND city_id = ? "
            "AND chat_id = ?",
            (uid, mid, city_id, chat_id)
        )
        for action in actions:
            await db.execute(
                "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                "quantity, city_id, chat_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, shift_id, mid, action["action_type"],
                 ",".join(action.get("bike_codes") or []), action.get("quantity", 0),
                 city_id, chat_id)
            )
        await db.execute(
            "UPDATE work_message_links SET last_event_version = ? "
            "WHERE city_id = ? AND chat_id = ? AND user_id = ? AND message_id = ?",
            (event_version, city_id, chat_id, uid, mid)
        )
        await db.commit()
        return [row[0] for row in rows], True


async def get_work_message_shift(uid, mid, city_id, chat_id=0):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT shift_id FROM work_message_links "
            "WHERE city_id = ? AND chat_id = ? AND user_id = ? AND message_id = ?",
            (city_id, chat_id, uid, mid)
        )).fetchone()
        return row[0] if row else None


async def link_work_message(uid, mid, city_id, shift_id, created_at=None, chat_id=0):
    """Привязка сообщения к смене.

    chat_id обязателен: message_id в Telegram уникален только ВНУТРИ чата.
    В городе с двумя группами (Химки) без него сообщения из разных групп
    с одинаковым message_id перетирали друг друга, и действия терялись.
    """
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO work_message_links (city_id, chat_id, user_id, message_id, shift_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_id, chat_id, user_id, message_id) DO NOTHING",
            (city_id, chat_id, uid, mid, shift_id, created_at)
        )
        await db.commit()

async def get_stats(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT action_type, bike_codes, quantity FROM actions WHERE shift_id = ?",
            (sid,)
        )
        rows = await c.fetchall()
        # === НОВОЕ: добавлен счётчик 'battery' (замены АКБ из темы NPB) ===
        s = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0, 'battery': 0}
        for r in rows:
            atype = r['action_type']
            if atype in s:
                codes = r['bike_codes']
                if codes:
                    s[atype] += len(codes.split(','))
                if r['quantity']:
                    s[atype] += r['quantity']
        return s

# ============================================================
# ПАРСИНГ ТЕКСТА О ТЕКУЩЕЙ РАБОТЕ
# ============================================================
_WORK_TYPOS = {
    "перемистил": "переместил",
    "пиреместил": "переместил",
    "переместл": "переместил",
    "перемстил": "переместил",
    "перестил": "переместил",
    "попровил": "поправил",
    "паправил": "поправил",
    "попарвил": "поправил",
    "ремнот": "ремонт",
    "превез": "привез",
    "привз": "привез",
    "привезз": "привез",
    "вывезз": "вывез",
    "заменл": "заменил",
    "батерею": "батарею",
}

# Порядок важен: работа с СЦ и АКБ точнее общих глаголов.
_ACTION_PATTERNS = (
    ("to_sc", re.compile(
        r"(?:\b(?:прив[её]з|доставил|отв[её]з|зав[её]з)\w*\b[^;.!?\n]{0,24}"
        r"\b(?:на|в)\s*сц\b)|(?:\bна\s*сц\b)")),
    ("from_sc", re.compile(
        r"(?:\b(?:выв[её]з|забрал|ув[её]з)\w*\b[^;.!?\n]{0,24}"
        r"\b(?:из|с)\s*сц\b)|(?:\b(?:из|с)\s*сц\b)")),
    ("battery", re.compile(
        r"(?:\b(?:замен|помен|сменил|перестав)\w*\b[^;.!?\n]{0,20}\b(?:акб|батаре\w*)\b)"
        r"|(?:\b(?:акб|батаре\w*)\b[^;.!?\n]{0,20}\b(?:замен|помен|сменил|перестав)\w*\b)")),
    ("repair", re.compile(
        r"\b(?:ремонт\w*|отремонт\w*|почин\w*|чин[июяе]\w*)\b")),
    ("move", re.compile(
        r"\b(?:перемест\w*|перемещ\w*|перен[её]с\w*|перестав\w*|"
        r"перегнал\w*|передвин\w*|перекат\w*|перев[её]з\w*|"
        r"переброс\w*|расстав\w*|перетян\w*)\b")),
    ("fix", re.compile(
        r"\b(?:поправ(?:ил|ила|или|лено|лены|ить|лял|ляла)\w*|выровн\w*|"
        r"поднял\w*|почист\w*|очист\w*|прот[её]р\w*|помыл\w*)\b"
        r"|\bпоставил\s+ровно\b")),
)


def _normalise_work_text(text):
    text = str(text or "").lower().replace("cц", "сц").replace("сc", "сц")
    for typo, fixed in _WORK_TYPOS.items():
        text = re.sub(rf"\b{re.escape(typo)}\b", fixed, text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _distance_to_span(position, start, end):
    if start <= position <= end:
        return 0
    return min(abs(position - start), abs(position - end))


def _clause_action_matches(clause):
    if re.match(r"^(?:что|как|где|почему|зачем)\b", clause):
        return []
    found = []
    for priority, (atype, pattern) in enumerate(_ACTION_PATTERNS):
        for match in pattern.finditer(clause):
            found.append({
                "action_type": atype,
                "start": match.start(),
                "end": match.end(),
                "priority": priority,
            })
    # Указатель будущего может стоять после глагола. Привязываем его к
    # ближайшему действию, чтобы в «сделал 1234, завтра поправлю 5678»
    # не потерять уже выполненную первую часть.
    future_targets = set()
    for cue in re.finditer(r"\b(?:завтра|послезавтра|позже)\b", clause):
        if found:
            nearest = min(
                found,
                key=lambda item: _distance_to_span(cue.start(), item["start"], item["end"]),
            )
            between = clause[min(cue.start(), nearest["end"]):max(cue.start(), nearest["start"])]
            if "," not in between:
                future_targets.add(id(nearest))
    negative_targets = set()
    for cue in re.finditer(
        r"\bне\s+(?:делал\w*|выполнял\w*|ремонтировал\w*|чинил\w*|менял\w*)\b",
        clause,
    ):
        if found:
            nearest = min(
                found,
                key=lambda item: _distance_to_span(cue.start(), item["start"], item["end"]),
            )
            negative_targets.add(id(nearest))
    question_targets = set()
    for cue in re.finditer(r"\bли\b", clause):
        if found:
            nearest = min(
                found,
                key=lambda item: _distance_to_span(cue.start(), item["start"], item["end"]),
            )
            question_targets.add(id(nearest))

    # Убираем вложенные менее точные совпадения (например, «переставил АКБ»
    # не должно одновременно стать перемещением).
    selected = []
    for candidate in sorted(found, key=lambda x: (x["priority"], x["start"], -x["end"])):
        if id(candidate) in future_targets or id(candidate) in negative_targets \
                or id(candidate) in question_targets:
            continue
        prefix = clause[max(0, candidate["start"] - 32):candidate["start"]]
        if re.search(
            r"(?:^|[\s,:;—-])(?:не|буду|будет|будем|нужно|надо|план|"
            r"планирую|планируем|собираюсь|хочу|можно|кто|завтра|стоит|находится|остался|"
            r"был|была|были|сейчас|"
            r"отправь(?:те)?|отвези(?:те)?|забери(?:те)?)"
            r"[\s,:;—-]*(?:\w+[\s,:;—-]+){0,2}$", prefix
        ):
            continue
        matched_text = clause[candidate["start"]:candidate["end"]]
        if (candidate["action_type"] in {"to_sc", "from_sc"}
                and matched_text.strip() in {"на сц", "в сц", "с сц", "из сц"}
                and re.search(
                    r"\b(?:фото|вопрос|подскаж|статус|сломан|неисправ|был|была|были|"
                    r"сейчас|стоит|находится|остался)\w*\b",
                    clause,
                )):
            continue
        if re.search(
            r"\b(?:перемести(?:ть|те)?|перенеси(?:те)?|переставь(?:те)?|"
            r"поправь(?:те)?|поправить|почини(?:ть|те)?|"
            r"отремонтируй(?:те)?|отремонтировать|почисти(?:ть|те)?|"
            r"очисти(?:ть|те)?|помой(?:те)?|"
            r"замени(?:ть|те)?|поменяй(?:те)?|забери(?:ть|те)?|"
            r"отвези(?:ти|те)?|привези(?:ти|те)?|вывези(?:ти|те)?)\b",
            matched_text,
        ):
            continue
        overlaps = any(
            not (candidate["end"] <= current["start"] or candidate["start"] >= current["end"])
            for current in selected
        )
        if not overlaps:
            selected.append(candidate)
    return sorted(selected, key=lambda x: x["start"])


def _parse_message_extensions(text):
    """Разбирает живой текст, привязывая номера к ближайшему действию.

    Четырёхзначное число — номер байка. Одно-трёхзначное число считается
    количеством только рядом с распознанным действием и не считается, если в
    этой же части сообщения уже перечислены номера байков.
    """
    text = _normalise_work_text(text)
    if not text:
        return []

    # Даты не разрываем на псевдо-количества: «12.07.2026» не означает 12 байков.
    text = re.sub(r"(?<!\d)\d{1,4}[./-]\d{1,2}[./-]\d{1,4}(?!\d)", " ", text)
    # «в 9.30» — время, а не девять действий.
    text = re.sub(r"(?<!\d)(?:[01]?\d|2[0-3])[.]\d{2}(?!\d)", " ", text)

    totals = {}
    # Точка между цифрами остаётся частью десятичного числа; остальные точки
    # по-прежнему разделяют предложения.
    clauses = [
        part.strip() for part in re.split(r"[\n;!?]+|(?<!\d)\.|\.(?!\d)", text)
        if part.strip()
    ]
    for clause in clauses:
        clause = re.split(
            r"\b(?:и|а)\s+(?:завтра|послезавтра|позже)\b", clause, maxsplit=1
        )[0].strip()
        # План после запятой не должен ни отменять уже выполненную часть, ни
        # отдавать её парсеру свои номера байков.
        clause = ",".join(
            part for part in clause.split(",")
            if not re.search(r"\b(?:завтра|послезавтра|позже)\b", part)
        ).strip()
        if not clause:
            continue
        matches = _clause_action_matches(clause)
        if not matches:
            continue

        assigned_codes = {index: [] for index in range(len(matches))}
        for code_match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", clause):
            code = code_match.group(1)
            suffix = clause[code_match.end():code_match.end() + 12]
            if 1900 <= int(code) <= 2099 and re.match(r"\s*(?:год|г\.)", suffix):
                continue
            nearest = min(
                range(len(matches)),
                key=lambda i: _distance_to_span(code_match.start(), matches[i]["start"], matches[i]["end"])
            )
            if code not in assigned_codes[nearest]:
                assigned_codes[nearest].append(code)

        # Совместимость со старым поведением: если в одной фразе указан один
        # байк и несколько действий над ним, этот номер относится ко всем.
        all_clause_codes = list(dict.fromkeys(
            code for codes in assigned_codes.values() for code in codes
        ))
        if len(all_clause_codes) == 1 and len(matches) > 1:
            for index in range(len(matches)):
                if not assigned_codes[index]:
                    assigned_codes[index] = all_clause_codes.copy()

        assigned_qty = {index: 0 for index in range(len(matches))}
        for qty_match in re.finditer(r"(?<![\d:])(\d{1,3})(?![\d:])", clause):
            suffix = clause[qty_match.end():qty_match.end() + 12]
            prefix = clause[max(0, qty_match.start() - 3):qty_match.start()]
            if re.match(r"[.,]\d", suffix) or re.search(r"\d[.,]\s*$", prefix):
                continue
            if re.match(r"\s*(?:км|мин|час|руб|₽|%|год)", suffix) or "%" in prefix:
                continue
            nearest = min(
                range(len(matches)),
                key=lambda i: _distance_to_span(qty_match.start(), matches[i]["start"], matches[i]["end"])
            )
            # Если перечислены конкретные байки, количество не дублирует их.
            if not assigned_codes[nearest]:
                assigned_qty[nearest] += int(qty_match.group(1))

        for index, match in enumerate(matches):
            codes = assigned_codes[index]
            qty = assigned_qty[index]
            if not codes and qty <= 0:
                continue
            item = totals.setdefault(match["action_type"], {"bike_codes": [], "quantity": 0})
            for code in codes:
                if code not in item["bike_codes"]:
                    item["bike_codes"].append(code)
            item["quantity"] += qty

    order = ("move", "fix", "repair", "battery", "to_sc", "from_sc")
    return [
        {"action_type": atype, "bike_codes": totals[atype]["bike_codes"],
         "quantity": totals[atype]["quantity"]}
        for atype in order if atype in totals
    ]


def get_action_type(kw):
    """Эталонное сопоставление из текущей версии GitHub без изменений."""
    if kw in ['привез на сц', 'привёз на сц', 'на сц привез', 'на сц']:
        return 'to_sc'
    if kw in ['вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц']:
        return 'from_sc'
    if kw in ['ремонт', 'поломк', 'сломан']:
        return 'repair'
    if kw in ['переместил', 'перенес', 'перенёс', 'переставил', 'перемещ']:
        return 'move'
    if kw in ['поправил', 'выровнял', 'чист', 'поправ']:
        return 'fix'
    return None


def _parse_message_github(text):
    """Дословная логика parse_message из main ветки voglogpro/bibibike-bot."""
    text = text.lower().strip()
    all_codes = re.findall(r'\b(\d{4})\b', text)
    lines = text.split('\n')

    repair_codes = []
    for line in lines:
        if any(kw in line for kw in ['ремонт', 'поломк', 'сломан']):
            repair_codes.extend(re.findall(r'\b(\d{4})\b', line))

    keywords_found = []

    for kw in ['привез на сц', 'привёз на сц', 'на сц привез',
               'вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц',
               'ремонт', 'поломк', 'сломан',
               'переместил', 'перенес', 'перенёс', 'переставил', 'перемещ',
               'поправил', 'выровнял', 'чист', 'поправ',
               'на сц', 'из сц']:
        if kw in text:
            atype = get_action_type(kw)
            if atype and atype not in [a['action_type'] for a in keywords_found]:
                qty = 0
                for line in lines:
                    if kw in line:
                        qty_match = re.search(r'(?<!\d)(\d{1,3})(?!\d)(?![а-яa-z])', line)
                        if qty_match:
                            num = int(qty_match.group(1))
                            if not re.search(r'\b\d{4}\b', line):
                                qty = num
                        break
                keywords_found.append({'action_type': atype, 'quantity': qty})

    if not keywords_found:
        return []

    qty_actions = [kw for kw in keywords_found if kw['quantity'] > 0]
    code_actions = [kw for kw in keywords_found if kw['quantity'] == 0]
    results = []

    for kw in qty_actions:
        results.append({'action_type': kw['action_type'], 'bike_codes': [], 'quantity': kw['quantity']})

    for kw in code_actions:
        if kw['action_type'] == 'repair':
            codes = repair_codes.copy() if repair_codes else []
        else:
            codes = all_codes.copy() if all_codes else []
        results.append({'action_type': kw['action_type'], 'bike_codes': codes, 'quantity': 0})

    return results


# ═══════════════════════════════════════════════════════════════
# ТРЕТИЙ СЛОЙ ПАРСЕРА: опечатки и Т9
# Включается ТОЛЬКО когда эталонный и расширенный слои вернули пусто,
# поэтому регрессия невозможна. Слой лишь чинит слово в тексте, а разбор
# номеров и количества выполняет прежний расширенный парсер.
# ═══════════════════════════════════════════════════════════════

# (корень для сравнения, канонная замена, тип действия)
_FUZZY_STEMS = (
    ("перемещ", "переместил", "move"),
    ("перемест", "переместил", "move"),
    ("перенес", "перенес", "move"),
    ("передвин", "передвинул", "move"),
    ("перекат", "перекатил", "move"),
    ("поправ", "поправил", "fix"),
    ("выровн", "выровнял", "fix"),
    ("почист", "почистил", "fix"),
    ("ремонт", "ремонт", "repair"),
    ("поломк", "поломка", "repair"),
    ("отремонт", "отремонтировал", "repair"),
    ("почин", "починил", "repair"),
    ("привез", "привез", "to_sc"),
    ("доставил", "доставил", "to_sc"),
    ("вывез", "вывез", "from_sc"),
    ("забрал", "забрал", "from_sc"),
    ("заменил", "заменил", "battery"),
    ("поменял", "поменял", "battery"),
)

# Слова, которые нельзя чинить: близки по написанию, но действием не являются
# (в т.ч. повелительные формы — просьбы, а не выполненная работа).
_FUZZY_STOP = {
    "поставил", "поставила", "посмотрел", "проверил", "потерял", "получил",
    "поехал", "пошел", "пошёл", "помог", "посчитал", "поработал", "поговорил",
    "привет", "подскажи", "подскажите", "покажи", "покажите", "помогите",
    "перерыв", "переписал", "перезвоню", "перекур", "передал", "переделал",
    "перемести", "переместите", "перенеси", "перенесите", "переставь",
    "поправь", "поправьте", "почини", "почините", "привези", "привезите",
    "вывези", "вывезите", "забери", "заберите", "замени", "замените",
    "поменяй", "поменяйте", "отвези", "отвезите", "доставь", "доставьте",
}


def _damerau_levenshtein(a, b, limit=3):
    """Расстояние с учётом перестановки соседних букв (частая опечатка Т9)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2, prev = None, list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + cost)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]


def _fuzzy_action_for_word(word):
    """Подбирает действие для слова с опечаткой. None — если не уверены."""
    if len(word) < 5 or word in _FUZZY_STOP:
        return None
    matches = []
    for stem, canon, atype in _FUZZY_STEMS:
        if word[:2] != stem[:2]:          # защита: «поправил» ≠ «привёз»
            continue
        probe = word[:len(stem)]
        if len(probe) < len(stem) - 1:
            continue
        limit = 1 if len(stem) <= 6 else 2
        dist = _damerau_levenshtein(probe, stem, limit)
        if dist <= limit:
            matches.append((dist, atype, canon))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    best = matches[0]
    # Неоднозначность между разными типами действий — не угадываем.
    if len({item[1] for item in matches if item[0] == best[0]}) > 1:
        logger.info(f"fuzzy: '{word}' — неоднозначно, пропускаю")
        return None
    return best


def _fuzzy_correct_text(text):
    """Возвращает (исправленный текст, список правок)."""
    fixes = []

    def repl(match):
        word = match.group(0)
        found = _fuzzy_action_for_word(word)
        if not found:
            return word
        dist, atype, canon = found
        fixes.append((word, canon, atype, dist))
        return canon

    corrected = re.sub(r"[а-яa-z]{5,}", repl, text)
    return corrected, fixes


def _parse_message_fuzzy(text):
    """Разбор сообщения с опечатками. Работает только при наличии чисел."""
    if not isinstance(text, str):
        return []
    normalised = _normalise_work_text(text)
    if not normalised:
        return []
    # Без номеров байков или количества не реагируем — иначе бот начнёт
    # ловить обычную переписку в чате.
    if not re.search(r"(?<!\d)\d{1,4}(?!\d)", normalised):
        return []
    corrected, fixes = _fuzzy_correct_text(normalised)
    if not fixes:
        return []
    results = _parse_message_extensions(corrected)
    if not results and '\n' in corrected:
        results = _parse_message_extensions(re.sub(r"\s*\n+\s*", " ", corrected))
    if results:
        for word, canon, atype, dist in fixes:
            logger.info(f"fuzzy: '{word}' -> {atype} ('{canon}', dist={dist})")
    return results


def parse_message(text):
    """Три слоя: эталон → расширения → опечатки.

    Старые распознанные сообщения всегда проходят через исходную функцию.
    Каждый следующий слой включается, только если предыдущий не нашёл ничего,
    поэтому результат прежних сообщений не меняется.
    """
    if not isinstance(text, str):
        return []
    legacy = _parse_message_github(text)
    if legacy:
        return legacy
    additions = _parse_message_extensions(text)
    if not additions and '\n' in text:
        additions = _parse_message_extensions(re.sub(r"\s*\n+\s*", " ", text))
    if additions:
        return additions
    return _parse_message_fuzzy(text)

# === НОВОЕ: парсер темы NPB — голые 4-значные номера = замены АКБ ===
def parse_npb_message(text):
    """Эталонная логика NPB из текущей версии GitHub без изменений."""
    codes = re.findall(r'\b(\d{4})\b', text)
    if not codes:
        return []
    return [{'action_type': 'battery', 'bike_codes': codes, 'quantity': 0}]


# === НОВОЕ: парсер темы «Перемещения»/«Подвозы» (Химки) ===
def parse_moves_message(text):
    """Голые 4-значные номера = перемещения.

    Работает по образцу NPB, только результат — 'move', а не 'battery'.
    Применяется ТОЛЬКО в теме, указанной как topic_moves у города
    (Химки: «Перемещения» у скаутов, «Подвозы» у водителей).
    Слова-глаголы здесь не нужны: достаточно самих номеров.
    """
    codes = re.findall(r'\b(\d{4})\b', text)
    if not codes:
        return []
    return [{'action_type': 'move', 'bike_codes': codes, 'quantity': 0}]


def topic_parser_kind(city, thread_id):
    """Каким парсером разбирать сообщение в этой теме.

    'moves'  — голые номера считаются перемещениями (тема topic_moves);
    'npb'    — голые номера считаются заменами АКБ (тема topic_npb);
    'tasks'  — обычный парсер по глаголам, как во всех остальных темах.
    """
    # Единый парсер для ВСЕХ городов — эталонный краснодарский разбор по словам.
    # Отдельно живёт только тема NPB (голые номера = замены АКБ), она есть
    # в Краснодаре и отсутствует в Химках.
    if thread_id == city.get("topic_npb"):
        return "npb"
    return "tasks"


def parse_manual_report(text):
    """Строгий разбор ручного итогового отчёта из темы ОТЧЁТЫ.

    Уверенным считаем сообщение с явно подписанными началом и окончанием
    либо ровно с двумя корректными временами. Дополнительное время обеда или
    перерыва не мешает, если границы смены подписаны. Неоднозначное сообщение
    сохраняется для проверки админом и само не влияет на статистику.
    """
    normalised = _normalise_work_text(text)
    if not normalised:
        return None, "пустое сообщение"
    if re.search(
        r"\b(?:завтра|послезавтра|план\w*|буду|будет|отмен\w*|"
        r"не\s+(?:работал\w*|выходил\w*))\b",
        normalised,
    ):
        return None, "сообщение похоже на план или отмену, а не завершённую смену"
    times = []
    for match in re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", normalised):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            times.append(f"{hour}:{minute:02d}")
    time_pattern = r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)"
    start_matches = re.findall(
        rf"\b(?:начал(?:а|и)?|начало|стартовал(?:а|и)?)\b[^\d\n]{{0,24}}{time_pattern}",
        normalised,
    )
    end_matches = re.findall(
        rf"\b(?:закончил(?:а|и)?|окончил(?:а|и)?|конец|окончание|финиш)\b"
        rf"[^\d\n]{{0,24}}{time_pattern}",
        normalised,
    )
    if len(start_matches) == 1 and len(end_matches) == 1:
        start_time = f"{int(start_matches[0][0])}:{start_matches[0][1]}"
        end_time = f"{int(end_matches[0][0])}:{end_matches[0][1]}"
    elif len(times) == 2:
        start_time, end_time = times
    else:
        return None, (
            "неоднозначные времена: подпишите начало и окончание смены"
            if times else "не найдены время начала и время окончания"
        )
    actions = parse_message(normalised)
    report_cue = re.search(
        r"\b(?:отч[её]т|смена|начал\w*|закончил\w*|скаут|водитель|чарджер|"
        r"перемещено|поправлено|акб|ремонт)\b", normalised
    )
    if not actions and not report_cue:
        return None, "сообщение не похоже на отчёт смены"
    return {
        "start_time": start_time,
        "end_time": end_time,
        "actions": actions,
    }, None


# Оба порядка слов: «начал смену» и «смену начал» (то же для закрытия).
_START_VERB = r"(?:начал|начала|начали|открыл|открыла|открыли)"
_END_VERB = (r"(?:закончил|закончила|закончили|завершил|завершила|завершили|"
             r"закрыл|закрыла|закрыли)")
_MANUAL_SHIFT_START_RE = re.compile(
    rf"^(?:я\s+)?(?:{_START_VERB}\s+смену|смену\s+{_START_VERB})$"
)
_MANUAL_SHIFT_END_RE = re.compile(
    rf"^(?:я\s+)?(?:{_END_VERB}\s+смену|смену\s+{_END_VERB})$"
)
# Открытие с именем и ролью: «Смену начал Иванов И.И. скаут».
_NAMED_SHIFT_START_RE = re.compile(
    rf"^(?:я\s+)?(?:{_START_VERB}\s+смену|смену\s+{_START_VERB})\s+(.+)$"
)
_ROLE_WORDS = {
    "скаут": "Скаут", "scout": "Скаут",
    "водитель": "Водитель", "driver": "Водитель", "вод": "Водитель",
    "чарджер": "Чарджер", "charger": "Чарджер", "чардж": "Чарджер",
}


def _clean_signal_tail(text):
    raw = re.sub(r"[\s.!?,;:…✅☑✔️👍]+$", "", str(text or "").strip()).strip()
    return raw, raw.lower().replace("ё", "е")


def _manual_shift_signal(text):
    """Короткая фраза о смене без имени (оба порядка слов)."""
    raw, low = _clean_signal_tail(text)
    low = re.sub(r"\s+", " ", low).strip()
    if _MANUAL_SHIFT_START_RE.fullmatch(low):
        return "start"
    if _MANUAL_SHIFT_END_RE.fullmatch(low):
        return "end"
    return None


def _named_shift_start(text):
    """«Смену начал Иванов И.И. скаут» → {'name','role'}. Иначе None.

    Регистр имени берём из исходного текста (lower() не меняет длину строки,
    поэтому позиции совпадают), роль — по последнему слову-должности.
    """
    raw, low = _clean_signal_tail(text)
    low = re.sub(r"\s+", " ", low)
    raw = re.sub(r"\s+", " ", raw)
    match = _NAMED_SHIFT_START_RE.match(low)
    if not match:
        return None
    tail = raw[match.start(1):].strip()
    words = tail.split()
    role = ""
    if words and words[-1].lower().replace("ё", "е") in _ROLE_WORDS:
        role = _ROLE_WORDS[words[-1].lower().replace("ё", "е")]
        words = words[:-1]
    name = " ".join(words).strip()
    if not name:
        return None
    return {"name": name, "role": role}


def _as_aware_datetime(value, default=None):
    """Приводит дату сообщения к aware-datetime (UTC).

    На некоторых версиях aiogram/хостинга message.date / edit_date приходят
    целым Unix-timestamp, а не datetime — тогда .tzinfo падает. Здесь
    поддерживаем оба варианта: int/float, наивный и aware datetime.
    """
    if value is None:
        return default if default is not None else datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _message_time_in_city(message, city):
    value = _as_aware_datetime(getattr(message, "date", None))
    return value.astimezone(_city_tz(city))


async def _start_manual_signal_shift(message, city, event_time, name=None, role=None):
    """Идемпотентно создаёт смену из фразы «начал смену».

    Если имя передано (форма «Смену начал Иванов И.И. скаут») — обновляем
    профиль сотрудника этими имя+роль и открываем смену под ними.
    """
    uid = message.from_user.id
    message_id = message.message_id
    user = await get_user(uid) or {}
    full_name = name or user.get("full_name") or message.from_user.full_name or f"Сотрудник #{uid}"
    role = role if role else (user.get("role") or "")
    if name:
        await add_user(uid, full_name, role, city["id"])
    start_time = event_time.strftime("%H:%M")
    start_at = _resolve_start_at(start_time, city, event_time)
    seg = _shift_segment({"start_at": start_at.isoformat()}, city)
    period = await ensure_city_period(city["id"], seg)
    period_id = (period or {}).get("id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await (await db.execute(
            "SELECT id FROM shifts WHERE city_id = ? AND source_message_id = ? LIMIT 1",
            (city["id"], message_id),
        )).fetchone()
        if existing:
            await db.rollback()
            return existing[0]
        active = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? AND is_active = 1 LIMIT 1", (uid,)
        )).fetchone()
        if active:
            await db.rollback()
            return None
        cursor = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active, "
            "created_at, city_id, start_at, source, source_message_id, period_id) "
            "VALUES (?, ?, ?, ?, '', 1, ?, ?, ?, 'manual_signal', ?, ?)",
            (uid, full_name, role, start_time, event_time.isoformat(), city["id"],
             start_at.isoformat(), message_id, period_id),
        )
        await db.commit()
        return cursor.lastrowid


async def handle_manual_shift_signal(message, city):
    """Молча открывает/закрывает ручную смену; бот-смены не изменяет."""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return False
    text = (message.text or message.caption or "").strip()
    event_time = _message_time_in_city(message, city)
    # Форма с именем: «Смену начал Иванов И.И. скаут» — регистрируем и открываем.
    named = _named_shift_start(text)
    if named:
        await _start_manual_signal_shift(
            message, city, event_time, name=named["name"], role=named["role"]
        )
        return True
    signal = _manual_shift_signal(text)
    if not signal:
        return False
    if signal == "start":
        await _start_manual_signal_shift(message, city, event_time)
        return True

    active = await get_active_shift(message.from_user.id)
    if (active and active.get("city_id") == city["id"]
            and active.get("source") == "manual_signal"):
        sid = await end_shift(
            message.from_user.id,
            event_time.strftime("%H:%M"),
            city_id=city["id"],
            now=event_time,
        )
        # У ручной смены обычно нет отдельного живого отчёта. Но если сотрудник
        # нажимал «Обед», отчёт уже создан — финально обновим именно его.
        if sid and active.get("report_msg_id"):
            await safe_flush_report_update(sid)
    return True


async def capture_manual_report(message: Message, city):
    """Молча сохраняет ручной отчёт, не отвечая сотруднику в теме."""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return
    text = (message.text or message.caption or "").strip()
    parsed, error = parse_manual_report(text)
    uid = message.from_user.id
    sender_name = message.from_user.full_name or f"Сотрудник #{uid}"
    tz = _city_tz(city)
    message_time = _as_aware_datetime(getattr(message, "date", None)).astimezone(tz)
    raw_event = getattr(message, "edit_date", None) or getattr(message, "date", None)
    event_time = _as_aware_datetime(raw_event).astimezone(tz)
    user = await get_user(uid) or {}
    current_pay_type = user.get("pay_type") or DEFAULT_PAY_TYPE
    current_pay_amount = user.get("pay_amount")
    if current_pay_amount is None:
        current_pay_amount = DEFAULT_PAY_AMOUNT

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Проверка пересечений и создание смены должны быть одной операцией:
        # иначе два одновременных отчёта успеют оба пройти SELECT.
        await db.execute("BEGIN IMMEDIATE")
        old = await (await db.execute(
            "SELECT mr.id, mr.shift_id, mr.updated_at, mr.pay_type_snap AS report_pay_type, "
            "mr.pay_amount_snap AS report_pay_amount, s.user_id AS shift_user_id, "
            "s.start_at AS shift_start_at, s.pay_type_snap AS shift_pay_type, "
            "s.pay_amount_snap AS shift_pay_amount, s.source AS shift_source, "
            "s.is_active AS shift_is_active, s.full_name AS shift_full_name, "
            "s.role AS shift_role, s.report_msg_id AS shift_report_msg_id "
            "FROM manual_reports mr LEFT JOIN shifts s ON s.id = mr.shift_id "
            "WHERE mr.city_id = ? AND mr.message_id = ?",
            (city["id"], message.message_id)
        )).fetchone()
        if old and old["updated_at"] and event_time.isoformat() < old["updated_at"]:
            await db.rollback()
            return
        old_shift_id = old["shift_id"] if old else None
        old_shift_user_id = old["shift_user_id"] if old else None
        target_shift_id = old_shift_id
        target_source = old["shift_source"] if old else None
        target_full_name = old["shift_full_name"] if old else None
        target_role = old["shift_role"] if old else None
        target_report_msg_id = old["shift_report_msg_id"] if old else None
        old_start_at = _parse_datetime(old["shift_start_at"]) if old else None
        old_month = old_start_at.strftime("%Y-%m") if old_start_at else None

        active_row = await (await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (uid,),
        )).fetchone()
        if active_row and active_row["id"] != target_shift_id:
            if (target_shift_id is None and active_row["city_id"] == city["id"]
                    and active_row["source"] == "manual_signal"):
                target_shift_id = active_row["id"]
                target_source = active_row["source"]
                target_full_name = active_row["full_name"]
                target_role = active_row["role"]
                target_report_msg_id = active_row["report_msg_id"]
            else:
                parsed = None
                error = "у сотрудника уже есть активная смена бота"
        pay_type_snap = (
            (old["report_pay_type"] if old else None)
            or (old["shift_pay_type"] if old else None)
            or current_pay_type
        )
        pay_amount_snap = old["report_pay_amount"] if old else None
        if pay_amount_snap is None and old:
            pay_amount_snap = old["shift_pay_amount"]
        if pay_amount_snap is None:
            pay_amount_snap = current_pay_amount

        start_at = end_at = None
        if parsed:
            start_at, end_at = _resolve_manual_interval(
                parsed["start_time"], parsed["end_time"], city, message_time
            )
            duration = end_at - start_at
            if duration <= timedelta(0) or duration > timedelta(hours=18):
                parsed = None
                error = "неоднозначная или слишком длинная смена"
            else:
                if target_shift_id is None:
                    manual_candidates = await (await db.execute(
                        "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? "
                        "AND source = 'manual_signal' "
                        "AND start_at IS NOT NULL AND julianday(start_at) < julianday(?) "
                        "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) "
                        "> julianday(?) ORDER BY id DESC LIMIT 2",
                        (uid, city["id"], end_at.isoformat(), start_at.isoformat()),
                    )).fetchall()
                    if len(manual_candidates) == 1:
                        candidate = manual_candidates[0]
                        target_shift_id = candidate["id"]
                        target_source = candidate["source"]
                        target_full_name = candidate["full_name"]
                        target_role = candidate["role"]
                        target_report_msg_id = candidate["report_msg_id"]
                conflict = await (await db.execute(
                    "SELECT id FROM shifts WHERE user_id = ? "
                    "AND id <> COALESCE(?, -1) AND start_at IS NOT NULL "
                    "AND julianday(start_at) < julianday(?) "
                    "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) > julianday(?) "
                    "LIMIT 1",
                    (uid, target_shift_id, end_at.isoformat(), start_at.isoformat())
                )).fetchone()
                if conflict:
                    parsed = None
                    error = f"интервал пересекается с уже учтённой сменой #{conflict[0]}"

        if not parsed:
            review_shift_id = target_shift_id if target_source == "manual_signal" else None
            if target_shift_id and target_source == "manual_signal":
                # Ручная сигнальная смена является реальным журналом работы.
                # Невалидная правка итогового отчёта не должна её удалять.
                await db.execute(
                    "DELETE FROM actions WHERE shift_id = ? AND message_id = ?",
                    (target_shift_id, message.message_id),
                )
            elif old_shift_id:
                await db.execute("DELETE FROM actions WHERE shift_id = ?", (old_shift_id,))
                await db.execute(
                    "DELETE FROM shifts WHERE id = ? AND source = 'manual_chat'", (old_shift_id,)
                )
            await db.execute(
                "INSERT INTO manual_reports (city_id, user_id, message_id, raw_text, status, "
                "parse_error, shift_id, sender_name, pay_type_snap, pay_amount_snap, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'needs_review', ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(city_id, message_id) DO UPDATE SET raw_text=excluded.raw_text, "
                "status='needs_review', parse_error=excluded.parse_error, "
                "shift_id=excluded.shift_id, "
                "sender_name=excluded.sender_name, "
                "pay_type_snap=COALESCE(manual_reports.pay_type_snap, excluded.pay_type_snap), "
                "pay_amount_snap=COALESCE(manual_reports.pay_amount_snap, excluded.pay_amount_snap), "
                "updated_at=excluded.updated_at",
                (city["id"], uid, message.message_id, text, error, review_shift_id, sender_name,
                 pay_type_snap, pay_amount_snap,
                 message_time.isoformat(), event_time.isoformat())
            )
            await db.commit()
            if review_shift_id:
                preserved = await get_shift_by_id(review_shift_id)
                if preserved and not preserved.get("is_active"):
                    await freeze_earned(review_shift_id)
            if old_month and old_shift_user_id is not None:
                await refresh_monthly_aggregate(city["id"], old_shift_user_id, old_month)
            return

        if target_source == "manual_signal":
            full_name = target_full_name or sender_name
            role = target_role or user.get("role") or ""
        else:
            full_name = user.get("full_name") or sender_name
            role = user.get("role") or ""
        worked_minutes = int((end_at - start_at).total_seconds() // 60)
        battery_count = sum(
            len(action.get("bike_codes") or []) + int(action.get("quantity") or 0)
            for action in parsed["actions"] if action["action_type"] == "battery"
        )
        earned = compute_earned(
            pay_type_snap, pay_amount_snap, worked_minutes, battery_count
        )

        store_report_actions = True
        if target_shift_id:
            shift_id = target_shift_id
            source = "manual_signal" if target_source == "manual_signal" else "manual_chat"
            await db.execute(
                "UPDATE shifts SET user_id=?, full_name=?, role=?, start_time=?, end_time=?, "
                "start_at=?, end_at=?, created_at=?, is_active=0, on_lunch=0, city_id=?, source=?, "
                "earned=?, pay_type_snap=COALESCE(pay_type_snap, ?), "
                "pay_amount_snap=COALESCE(pay_amount_snap, ?) WHERE id=?",
                (uid, full_name, role, parsed["start_time"], parsed["end_time"],
                 start_at.isoformat(), end_at.isoformat(), start_at.isoformat(), city["id"],
                 source, earned, pay_type_snap, pay_amount_snap, shift_id)
            )
            if source == "manual_signal":
                # Сохраняем действия, уже собранные эталонным парсером в рабочих темах.
                # Итоговый отчёт добавляет сводные действия лишь если других записей нет.
                await db.execute(
                    "DELETE FROM actions WHERE shift_id = ? AND message_id = ?",
                    (shift_id, message.message_id),
                )
                has_live_actions = await (await db.execute(
                    "SELECT 1 FROM actions WHERE shift_id = ? LIMIT 1", (shift_id,)
                )).fetchone()
                store_report_actions = not bool(has_live_actions)
            else:
                await db.execute("DELETE FROM actions WHERE shift_id = ?", (shift_id,))
        else:
            cursor = await db.execute(
                "INSERT INTO shifts (user_id, full_name, role, start_time, end_time, is_active, "
                "created_at, city_id, start_at, end_at, source, source_message_id, "
                "earned, pay_type_snap, pay_amount_snap) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'manual_chat', ?, ?, ?, ?)",
                (uid, full_name, role, parsed["start_time"], parsed["end_time"],
                 start_at.isoformat(), city["id"], start_at.isoformat(), end_at.isoformat(),
                 message.message_id, earned, pay_type_snap, pay_amount_snap)
            )
            shift_id = cursor.lastrowid

        if store_report_actions:
            for action in parsed["actions"]:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                    "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, shift_id, message.message_id, action["action_type"],
                     ",".join(action.get("bike_codes") or []), action.get("quantity", 0), city["id"])
                )
        await db.execute(
            "INSERT INTO manual_reports (city_id, user_id, message_id, raw_text, status, "
            "parse_error, shift_id, sender_name, pay_type_snap, pay_amount_snap, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'accepted', NULL, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_id, message_id) DO UPDATE SET raw_text=excluded.raw_text, "
            "status='accepted', parse_error=NULL, shift_id=excluded.shift_id, "
            "sender_name=excluded.sender_name, "
            "pay_type_snap=COALESCE(manual_reports.pay_type_snap, excluded.pay_type_snap), "
            "pay_amount_snap=COALESCE(manual_reports.pay_amount_snap, excluded.pay_amount_snap), "
            "updated_at=excluded.updated_at",
            (city["id"], uid, message.message_id, text, shift_id, sender_name,
             pay_type_snap, pay_amount_snap,
             message_time.isoformat(), event_time.isoformat())
        )
        await db.commit()
    await freeze_earned(shift_id)
    if target_source == "manual_signal" and target_report_msg_id:
        await safe_flush_report_update(shift_id)
    new_month = start_at.strftime("%Y-%m")
    if (old_month and old_shift_user_id is not None
            and (old_month != new_month or old_shift_user_id != uid)):
        await refresh_monthly_aggregate(city["id"], old_shift_user_id, old_month)

# ============================================================
# ФУНКЦИЯ АВТОУДАЛЕНИЯ КОМАНД  (оригинальная)
# ============================================================
async def auto_delete(msg: Message, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

# ============================================================
# === НОВОЕ: ЖИВОЕ СООБЩЕНИЕ СМЕНЫ =========================
# ============================================================
_pending_updates = {}   # shift_id -> asyncio.Task (дебаунс)
_report_update_locks = {}  # shift_id -> {lock, users}; защита от дублей/гонок

def _role_text(role):
    role_emoji = ""
    if role == "Скаут":
        role_emoji = " 🚶"
    elif role == "Водитель":
        role_emoji = " 🚚"
    elif role == "Чарджер":       # === НОВОЕ: роль чарджера ===
        role_emoji = " ⚡"
    return f" | {role}{role_emoji}" if role else ""

def _duration(start_time, end_time):
    sp = start_time.split(':')
    ep = end_time.split(':')
    sm = int(sp[0]) * 60 + int(sp[1])
    em = int(ep[0]) * 60 + int(ep[1])
    if em < sm:
        em += 24 * 60
    diff = em - sm
    return f"{diff // 60} ч. {diff % 60} мин."


def _duration_shift(shift):
    diff = _shift_worked_min(shift)
    return f"{diff // 60} ч. {diff % 60} мин."

def build_report_text(shift, stats):
    """Формат сохранён из оригинального отчёта + строка АКБ."""
    full_name = html.escape(shift.get('full_name') or "Сотрудник")
    report = f"<b>{full_name}</b>{_role_text(shift.get('role'))}\n"
    waiting = _shift_is_scheduled(shift)
    closed = not shift.get('is_active') and shift.get('end_time')
    # Цветовой индикатор статуса смены
    if closed:
        report += "🔴 Смена закрыта\n"
    elif waiting:
        report += "🟢 Ожидает начала\n"
    else:
        report += "🟢 Смена активна\n"
        if shift.get("on_lunch"):
            report += "🍽 Сейчас на обеде\n"
    report += f"Начал: {html.escape(shift['start_time'])}"
    if waiting:
        report += " (ожидает начала)"
    report += "\n"

    if closed:
        report += f"Закончил: {html.escape(shift['end_time'])}\n"
        report += f"Отработано: {_duration_shift(shift)}\n"

    if shift.get('district'):
        report += f"Район: {html.escape(shift['district'].upper())}\n"

    report += "\nСтатистика за смену:\n"

    has_any = False
    if stats['move'] > 0:
        report += f"🛵 Перемещено: {stats['move']}\n"; has_any = True
    if stats['fix'] > 0:
        report += f"💚 Поправлено: {stats['fix']}\n"; has_any = True
    if stats['repair'] > 0:
        report += f"🔧 Ремонт: {stats['repair']}\n"; has_any = True
    if stats['battery'] > 0:
        report += f"🔋 Поменял АКБ: {stats['battery']}\n"; has_any = True
    if stats['to_sc'] > 0:
        report += f"Привез на СЦ: {stats['to_sc']}\n"; has_any = True
    if stats['from_sc'] > 0:
        report += f"Вывез из СЦ: {stats['from_sc']}\n"; has_any = True
    if not has_any:
        report += "— пока нет действий\n"

    if closed and shift.get('comment'):
        report += f"\nКомментарий: {html.escape(shift['comment'])}"

    return report

async def update_report_message(shift_id, force_new=False):
    """Сериализует полную отправку/правку отчёта одной смены.

    Без этого два одновременных запроса могли оба увидеть пустой
    report_msg_id и создать два сообщения. Свежая смена читается уже внутри
    блокировки, поэтому закрытие всегда оставляет финальное закрытое состояние.
    """
    entry = _report_update_locks.get(shift_id)
    if entry is None:
        entry = {"lock": asyncio.Lock(), "users": 0}
        _report_update_locks[shift_id] = entry
    entry["users"] += 1
    try:
        async with entry["lock"]:
            await _update_report_message_locked(shift_id, force_new=force_new)
    finally:
        entry["users"] -= 1
        if entry["users"] == 0 and _report_update_locks.get(shift_id) is entry:
            _report_update_locks.pop(shift_id, None)


async def _update_report_message_locked(shift_id, force_new=False):
    """Отредактировать живое сообщение смены (или пересоздать при /fix)."""
    shift = await get_shift_by_id(shift_id)
    if not shift:
        return
    # Группа выбирается по роли сотрудника: в городах с ролевыми группами
    # (Химки) смена скаута уходит в группу скаутов, водителя — в водительскую.
    city = city_for_role(shift.get("city_id"), shift.get("role"))
    if not city:
        logger.error(f"Не найден город смены {shift_id}")
        return
    stats = await get_stats(shift_id)
    text = build_report_text(shift, stats)
    msg_id = shift.get('report_msg_id')
    # === Кнопка «Открыть приложение» ТОЛЬКО пока смена активна.
    # На закрытии смены (is_active=0) markup=None → кнопка исчезает, без спама. ===
    markup = _webapp_button() if shift.get('is_active') else None

    if force_new and msg_id:
        try:
            await bot.delete_message(city["group_id"], msg_id)
        except TelegramBadRequest:
            pass
        msg_id = None

    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=city["group_id"], message_id=msg_id,
                parse_mode="HTML", reply_markup=markup
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
            # сообщение удалили вручную — пришлём новое ниже

    msg = await bot.send_message(
        city["group_id"], text, message_thread_id=city["topic_reports"],
        parse_mode="HTML", reply_markup=markup
    )
    await set_report_msg_id(shift_id, msg.message_id)

def schedule_report_update(shift_id):
    """Дебаунс: не редактируем чаще, чем раз в DEBOUNCE_SEC (защита от флуд-лимита)."""
    task = _pending_updates.get(shift_id)
    if task and not task.done():
        return

    async def _later():
        await asyncio.sleep(DEBOUNCE_SEC)
        _pending_updates.pop(shift_id, None)
        try:
            await update_report_message(shift_id)
        except Exception as e:
            logger.error(f"Не удалось обновить отчёт смены {shift_id}: {e}")

    _pending_updates[shift_id] = asyncio.create_task(_later())

async def flush_report_update(shift_id, force_new=False):
    """Немедленное обновление (открытие/закрытие смены, /fix) — отменяем дебаунс."""
    task = _pending_updates.pop(shift_id, None)
    if task and not task.done():
        task.cancel()
    await update_report_message(shift_id, force_new=force_new)


async def safe_flush_report_update(shift_id, force_new=False):
    """Отчёт в Telegram — вторичный шаг после записи смены в БД.

    Если Telegram временно недоступен, не возвращаем ложную ошибку
    клиенту, когда смена уже успешно открыта/закрыта.
    """
    try:
        await flush_report_update(shift_id, force_new=force_new)
        return True
    except Exception as exc:
        logger.error(f"Смена {shift_id} сохранена, но Telegram-отчёт не обновился: {exc}")
        return False

# === НОВОЕ: команда /app — постим и закрепляем в теме кнопку открытия приложения ===
async def post_app_button(message: Message):
    markup = _webapp_button()
    try:
        await message.delete()
    except Exception:
        pass
    if not markup:
        return
    m = await bot.send_message(
        message.chat.id,
        "💰 <b>BibiBike</b> — смена, заработок и рейтинг в приложении.",
        message_thread_id=message.message_thread_id,
        parse_mode="HTML",
        reply_markup=markup,
    )
    try:
        await bot.pin_chat_message(message.chat.id, m.message_id, disable_notification=True)
    except Exception:
        pass

# ============================================================
# ОБРАБОТКА РАБОЧЕГО СООБЩЕНИЯ  (оригинальная + триггер живого отчёта)
# ============================================================
class CityTopicFilter(BaseFilter):
    def __init__(self, topic_kind):
        self.topic_kind = topic_kind

    async def __call__(self, message: Message):
        city = get_city_by_group(message.chat.id)
        if not city:
            return False
        thread_id = message.message_thread_id
        if self.topic_kind == "reports":
            matches = thread_id == city["topic_reports"]
        else:
            # Сохраняем рабочий контрак бота: слушать все темы группы
            # города, кроме «ОТЧЁТОВ». NPB всё ещё определяется отдельно.
            matches = thread_id != city["topic_reports"]
        return {"city": city} if matches else False


async def process_work_message(message: Message, city, npb=False, edited=False, moves=False):
    text = message.text or message.caption or ""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return

    uid = message.from_user.id
    # chat_id обязателен: message_id уникален только внутри чата, а у города
    # может быть несколько групп (Химки: скауты и водители).
    chat_id = message.chat.id
    linked_shift_id = await get_work_message_shift(uid, message.message_id, city["id"], chat_id)
    existing_shift_ids = await get_action_shift_ids(uid, message.message_id, city["id"], chat_id)
    if edited:
        # Постоянная привязка сохраняется даже когда правка временно сделала
        # сообщение нераспознаваемым. Для старой БД используем shift_id удалённых
        # actions, а для впервые распознанной правки — дату исходного сообщения.
        fallback_shift_id = linked_shift_id or (existing_shift_ids[0] if existing_shift_ids else None)
        shift = await get_shift_by_id(fallback_shift_id) if fallback_shift_id else None
        if not shift:
            candidate = await get_active_shift(uid, city["id"])
            message_date = getattr(message, "date", None)
            if candidate and message_date:
                message_date = _as_aware_datetime(message_date).astimezone(_city_tz(city))
                bounds = [
                    value for value in (
                        _parse_datetime(candidate.get("start_at")),
                        _parse_datetime(candidate.get("created_at")),
                    ) if value
                ]
                lower_bound = min(value.astimezone(_city_tz(city)) for value in bounds) \
                    if bounds else None
                if lower_bound and message_date + timedelta(minutes=2) >= lower_bound:
                    shift = candidate
    else:
        shift = await get_active_shift(uid, city["id"])
    if not shift:
        logger.info(
            f"ПРОПУЩЕНО: нет активной смены. uid={uid} город={city['name']} "
            f"chat={chat_id} тема={message.message_thread_id} msg={message.message_id}"
        )
        return

    message_date = getattr(message, "date", None)
    raw_event = getattr(message, "edit_date", None) if edited else message_date
    event_date = _as_aware_datetime(raw_event)
    event_version = event_date.timestamp()
    if edited or existing_shift_ids:
        await link_work_message(
            uid, message.message_id, city["id"], shift["id"],
            message_date.isoformat() if message_date else None, chat_id,
        )

    if not text or text.startswith('/') or re.match(r'^\d{1,2}:\d{2}\s*', text):
        # Фото/стикер без подписи может получить корректную подпись уже после
        # закрытия смены, поэтому пустое рабочее сообщение тоже привязываем.
        if not text:
            await link_work_message(
                uid, message.message_id, city["id"], shift["id"],
                message_date.isoformat() if message_date else None, chat_id,
            )
        removed_shift_ids, applied = await replace_message_actions(
            uid, message.message_id, city["id"], shift["id"], [], event_version, chat_id
        )
        if not applied:
            return
        for sid in removed_shift_ids:
            changed = await get_shift_by_id(sid)
            if changed and not changed.get("is_active"):
                await freeze_earned(sid)
            schedule_report_update(sid)
        return

    await link_work_message(
        uid, message.message_id, city["id"], shift["id"],
        message_date.isoformat() if message_date else None, chat_id,
    )

    # === НОВОЕ: в теме NPB считаем голые номера как замены АКБ ===
    if moves:
        actions = parse_moves_message(text)   # тема перемещений: голые номера
    elif npb:
        actions = parse_npb_message(text)
    else:
        actions = parse_message(text)
    logger.info(
        f"РАЗБОР: город={city['name']} роль_группы={city.get('role_group') or '—'} "
        f"chat={chat_id} тема={message.message_thread_id} смена={shift['id']} "
        f"msg={message.message_id} правка={edited} -> {actions}"
    )

    removed_shift_ids, applied = await replace_message_actions(
        uid, message.message_id, city["id"], shift["id"], actions, event_version, chat_id
    )
    if not applied:
        logger.warning(
            f"ЗАПИСЬ ОТКЛОНЕНА: chat={chat_id} msg={message.message_id} "
            f"смена={shift['id']} город={city['name']} — привязка не совпала "
            f"или смена изменилась"
        )
        return
    for action in actions:
        logger.info(f"Записано: {shift['full_name']} — {action}")

    # === НОВОЕ: обновляем живое сообщение (с дебаунсом) ===
    if actions or removed_shift_ids:
        changed = await get_shift_by_id(shift['id'])
        if changed and not changed.get("is_active"):
            await freeze_earned(shift['id'])
        for sid in removed_shift_ids:
            if sid != shift['id']:
                old_shift = await get_shift_by_id(sid)
                if old_shift and not old_shift.get("is_active"):
                    await freeze_earned(sid)
                schedule_report_update(sid)
        schedule_report_update(shift['id'])

# ============================================================
# ЧАТ 1 (и остальные темы, кроме ОТЧЕТОВ) — НОВЫЕ СООБЩЕНИЯ
# ============================================================
@work_router.message(CityTopicFilter("work"))
async def work_chat(message: Message, city):
    # === НОВОЕ: /topicid — узнать ID темы (для настройки конфига) ===
    if (message.text or "") == "/topicid":
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /app — закрепить кнопку приложения в этой теме ===
    if (message.text or "").strip() == "/app":
        await post_app_button(message)
        return

    # === НОВОЕ: у каждой темы свой парсер (перемещения / NPB / обычный) ===
    kind = topic_parser_kind(city, message.message_thread_id)
    await process_work_message(message, city, npb=(kind == "npb"), moves=(kind == "moves"))

# ============================================================
# РЕДАКТИРОВАННЫЕ РАБОЧИЕ СООБЩЕНИЯ  (оригинал + NPB)
# ============================================================
@work_router.edited_message(CityTopicFilter("work"))
async def work_chat_edit(message: Message, city):
    logger.info(f"СООБЩЕНИЕ ОТРЕДАКТИРОВАНО: {message.message_id}")
    kind = topic_parser_kind(city, message.message_thread_id)
    await process_work_message(message, city, npb=(kind == "npb"),
                               moves=(kind == "moves"), edited=True)

# ============================================================
# ЧАТ 2 — УПРАВЛЕНИЕ СМЕНАМИ И ОТЧЕТАМИ
# ============================================================
@cmd_router.message(CityTopicFilter("reports"))
async def cmd_chat(message: Message, city):
    user_id = message.from_user.id
    active_any = await get_active_shift(user_id)
    if not active_any or active_any.get("city_id") == city["id"]:
        await set_user_city(user_id, city["id"])
    user = await get_user(user_id)
    full_name = (user or {}).get('full_name') or message.from_user.full_name
    role = (user or {}).get('role') or ""
    text = (message.text or message.caption or "").strip()

    # Ручной отчёт по-прежнему не вызывает ответа бота, но строгий парсер
    # сохраняет его для админской статистики. Неоднозначное попадает в проверку.
    if not text.startswith('/'):
        if await handle_manual_shift_signal(message, city):
            return
        await capture_manual_report(message, city)
        return

    # === НОВОЕ: /app — закрепить кнопку приложения и в теме ОТЧЁТЫ ===
    if text == "/app":
        await post_app_button(message)
        return

    # /help
    if text == "/help":
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            "BibiBike - команды:\n\n"
            "Начать смену (район — любое слово или без него):\n"
            "/09:00 фмр\n/09:00 весь город, загрузил 35\n/09:00\n\n"
            "Закончить смену:\n/18:00\n/18:00 Комментарий\n\n"
            "Установить имя и роль:\n/setname Фамилия И.О. скаут\n"
            "(роли: скаут, водитель, чарджер)\n\n"
            "Статус: /status\n"
            "ID темы: /topicid"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /topicid и в теме отчётов ===
    if text == "/topicid":
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # /status  (оригинальный)
    if text == "/status":
        try:
            await message.delete()
        except:
            pass
        shift = await get_active_shift(user_id, city["id"])
        if shift:
            shift_status = (
                f"Смена начнётся в {shift['start_time']}" if _shift_is_scheduled(shift)
                else f"Активная смена с {shift['start_time']}"
            )
            msg = await message.answer(
                f"{full_name}{_role_text(shift.get('role'))}\n"
                f"{shift_status}\n"
                + (f"Район: {shift['district'].upper()}" if shift.get('district') else "")
            )
        else:
            msg = await message.answer("Нет активной смены.")
        asyncio.create_task(auto_delete(msg))
        return

    # /setname ...  (оригинальный + роль Чарджер)
    if text.startswith("/setname"):
        try:
            await message.delete()
        except:
            pass
        parts = text.split(maxsplit=1)
        if len(parts) >= 2:
            args = parts[1].strip().split()
            if len(args) >= 2:
                new_role = args[-1].lower()
                if new_role in ["скаут", "scout"]:
                    new_role = "Скаут"
                elif new_role in ["водитель", "driver", "вод"]:
                    new_role = "Водитель"
                elif new_role in ["чарджер", "charger", "чардж"]:   # === НОВОЕ ===
                    new_role = "Чарджер"
                else:
                    msg = await message.answer("Укажите роль: скаут, водитель или чарджер\nПример: /setname Иванов И.И. чарджер")
                    asyncio.create_task(auto_delete(msg))
                    return
                new_name = " ".join(args[:-1])
                await add_user(user_id, new_name, new_role, city["id"])
                msg = await message.answer(f"Сохранено: {new_name} | {new_role}")
            else:
                msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        else:
            msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        asyncio.create_task(auto_delete(msg))
        return

    # Обработка команд времени (Начало / Конец смены)
    # Бот реагирует ТОЛЬКО на слеш — кто не хочет пользоваться,
    # пишет отчёты вручную как раньше, бот его не трогает.
    if not text.startswith('/'):
        return

    text = text[1:]
    active_shift = await get_active_shift(user_id)
    time_match = re.match(r'(\d{1,2}:\d{2})\s*(.*)', text)

    if time_match:
        try:
            await message.delete()
        except:
            pass

        time_str = _valid_time(time_match.group(1))
        if not time_str:
            msg = await message.answer("Ошибка: время должно быть от 00:00 до 23:59.")
            asyncio.create_task(auto_delete(msg))
            return
        extra = time_match.group(2).strip()

        if active_shift and active_shift.get("city_id") != city["id"]:
            active_city = get_city(active_shift.get("city_id")) or {}
            msg = await message.answer(
                f"У вас уже открыта смена в городе {active_city.get('name', 'другом городе')}. "
                "Закройте её в группе этого города."
            )
            asyncio.create_task(auto_delete(msg))
            return

        if not active_shift:
            # НАЧАЛО СМЕНЫ
            # === НОВОЕ: район/зона — любой текст целиком (или пусто).
            # Чарджер может указать зону и порог: /20:55 весь город, загрузил 35 ===
            district = extra.lower()
            role_for_shift = role if role else ""
            try:
                sid = await start_shift(
                    user_id, full_name, role_for_shift, time_str, district, city["id"]
                )
            except ActiveShiftExists:
                msg = await message.answer("У вас уже открыта смена.")
                asyncio.create_task(auto_delete(msg))
                return

            # === НОВОЕ: создаётся ЖИВОЕ сообщение, бот редактирует его всю смену ===
            await safe_flush_report_update(sid)
            logger.info(f"Смена начата: {full_name}, {time_str}, {district or '—'}")
            return

        else:
            # КОНЕЦ СМЕНЫ
            comment = extra if extra else ""
            try:
                sid = await end_shift(user_id, time_str, comment, city["id"])
            except ValueError as exc:
                msg = await message.answer(str(exc))
                asyncio.create_task(auto_delete(msg))
                return
            if not sid:
                msg = await message.answer("Ошибка завершения смены.")
                asyncio.create_task(auto_delete(msg))
                return

            # === НОВОЕ: финальная правка живого сообщения ===
            await safe_flush_report_update(sid)
            logger.info(f"Смена завершена: {full_name}")
            return

    return


@cmd_router.edited_message(CityTopicFilter("reports"))
async def cmd_chat_edit(message: Message, city):
    text = (message.text or message.caption or "").strip()
    if not text.startswith('/'):
        if await handle_manual_shift_signal(message, city):
            return
        await capture_manual_report(message, city)

# ============================================================
# === НОВОЕ: HTTP API ДЛЯ МИНИ-ПРИЛОЖЕНИЯ ===================
# ============================================================
MAX_LEVEL = 100

# XP за одно действие по сложности: перемещение/СЦ=10, ремонт/АКБ=5, поправка=3.
XP_WEIGHTS = {
    "move": 10,
    "to_sc": 10,
    "from_sc": 10,
    "repair": 5,
    "battery": 5,
    "fix": 3,
}

# Звание каждые 10 уровней + тир (цвет бейджа в приложении)
LEVEL_TITLES = [
    (1,  "Пеший",        "bronze"),
    (10, "Велик",        "bronze"),
    (20, "Скутер",       "silver"),
    (30, "Молния",       "silver"),
    (40, "Гонщик",       "gold"),
    (50, "Профи",        "gold"),
    (60, "Мастер",       "platinum"),
    (70, "Ас",           "platinum"),
    (80, "Легенда",      "diamond"),
    (90, "Бог Асфальта", "diamond"),
]

def _title_for_level(lvl):
    title, tier = LEVEL_TITLES[0][1], LEVEL_TITLES[0][2]
    for need, name, t in LEVEL_TITLES:
        if lvl >= need:
            title, tier = name, t
    return title, tier

def _level_from_xp(total):
    # Нелинейная прогрессия: на уровень L нужно 60 + 12·L + 0.35·L² XP.
    lvl, rem = 1, total
    while lvl < MAX_LEVEL:
        need = int(60 + 12 * lvl + 0.35 * lvl * lvl)
        if rem < need:
            return lvl, rem, need
        rem -= need
        lvl += 1
    return MAX_LEVEL, 0, 0   # потолок достигнут

async def get_lifetime(uid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # === НОВОЕ: XP считается с учётом веса типа действия (см. XP_WEIGHTS).
        # count — сколько "штук" в строке (номера байков + quantity),
        # затем умножаем на вес типа. quantity может быть -1 (отмена действия
        # из приложения) — тогда XP корректно уменьшается. ===
        if city_id is None:
            c = await db.execute(
                "SELECT action_type, bike_codes, quantity FROM actions WHERE user_id = ?", (uid,)
            )
        else:
            c = await db.execute(
                "SELECT action_type, bike_codes, quantity FROM actions WHERE user_id = ? AND city_id = ?",
                (uid, city_id)
            )
        total = 0
        for r in await c.fetchall():
            count = 0
            if r['bike_codes']:
                count += len(r['bike_codes'].split(','))
            if r['quantity']:
                count += r['quantity']
            total += count * XP_WEIGHTS.get(r['action_type'], 1)
        if total < 0:
            total = 0
        if city_id is None:
            c2 = await db.execute(
                "SELECT COALESCE(SUM(earned), 0), COUNT(*) FROM shifts "
                "WHERE user_id = ? AND is_active = 0", (uid,)
            )
        else:
            c2 = await db.execute(
                "SELECT COALESCE(SUM(earned), 0), COUNT(*) FROM shifts "
                "WHERE user_id = ? AND city_id = ? AND is_active = 0", (uid, city_id)
            )
        row2 = await c2.fetchone()
        total_earned, shifts_count = row2[0], row2[1]
        return total, total_earned, shifts_count

async def get_history(uid, city_id=None, limit=90):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if city_id is None:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT ?",
                (uid, limit)
            )
        else:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "ORDER BY id DESC LIMIT ?", (uid, city_id, limit)
            )
        return [dict(r) for r in await c.fetchall()]

def _fmt_date(created_at):
    if not created_at:
        return "—"
    try:
        return datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
    except Exception:
        return "—"

def _check_webapp_auth(init_data: str):
    """Проверяем подпись Telegram initData — так мы точно знаем, кто открыл приложение."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    data_check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", "0"))
        age = int(datetime.now(timezone.utc).timestamp()) - auth_date
        if auth_date <= 0 or age < -60 or age > INIT_DATA_MAX_AGE_SEC:
            return None
    except (TypeError, ValueError):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

def _get_init_data(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("tma "):
        return auth[4:]
    return request.headers.get("X-Init-Data", "")

async def _auth_user(request):
    tg_user = _check_webapp_auth(_get_init_data(request))
    if not tg_user or "id" not in tg_user:
        return None
    return tg_user


_city_membership_cache = {}


async def _is_city_member(uid, city_id):
    """Не даёт открыть смену в чужой закрытой группе через Mini App.

    В городах с ролевыми группами (Химки) достаточно состоять хотя бы
    в одной из групп города — скаутской или водительской.
    """
    city = get_city(city_id)
    if not city:
        return False
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _city_membership_cache.get((uid, city_id))
    if cached and cached[1] > now_ts:
        return cached[0]
    group_ids = [city["group_id"]]
    for variant in city_role_groups(city_id).values():
        if variant["group_id"] not in group_ids:
            group_ids.append(variant["group_id"])
    allowed = False
    for group_id in group_ids:
        try:
            member = await bot.get_chat_member(group_id, uid)
            status = getattr(member.status, "value", str(member.status)).lower().split(".")[-1]
            if status == "restricted":
                allowed = bool(getattr(member, "is_member", False))
            else:
                allowed = status in {"creator", "administrator", "member"}
        except Exception as exc:
            logger.warning(
                f"Не удалось проверить участие uid={uid} в группе {group_id} города {city_id}: {exc}"
            )
            continue
        if allowed:
            break
    ttl = max(30, CITY_MEMBERSHIP_TTL_SEC if allowed else min(60, CITY_MEMBERSHIP_TTL_SEC))
    _city_membership_cache[(uid, city_id)] = (allowed, now_ts + ttl)
    return allowed


async def _request_json_object(request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None

@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEBAPP_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, X-Init-Data, X-Admin-Token"
    )
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

async def api_state(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid)
    default_city = get_default_city()
    user_city = get_city((user or {}).get("city_id")) or default_city
    user_city_id = (user_city or {}).get("id")
    city = user_city
    if not city:
        return web.json_response({"error": "cities", "message": "Города не настроены."}, status=500)
    active_any = await get_active_shift(uid)
    if active_any and get_city(active_any.get("city_id")):
        city = get_city(active_any["city_id"])
    selected_city_id = city["id"]
    pay_type = (user or {}).get("pay_type") or DEFAULT_PAY_TYPE
    pay_amount = (user or {}).get("pay_amount")
    if pay_amount is None:
        pay_amount = DEFAULT_PAY_AMOUNT
    name = (user or {}).get("full_name") or ""
    role = (user or {}).get("role") or ""

    total, total_earned, shifts_count = await get_lifetime(uid, selected_city_id)
    lvl, xp, need = _level_from_xp(total)

    # Доход за текущую декаду СВОЕЙ группы (день/ночь) — по каждой смене
    # отдельно: дневные смены сверяются с дневной декадой, ночные с ночной.
    periods = await city_periods(selected_city_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        closed_rows = await (await db.execute(
            "SELECT earned, period_id, start_at, created_at, start_time "
            "FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
            "ORDER BY COALESCE(start_at, created_at) DESC",
            (uid, selected_city_id)
        )).fetchall()
    month_earned = sum(
        (r["earned"] or 0) for r in closed_rows
        if _shift_in_period(dict(r), periods, city)
    )
    # Подпись «с даты»: декада группы последней смены сотрудника.
    _last_seg = "day"
    if closed_rows:
        _last_seg = _shift_segment(dict(closed_rows[0]), city)
    period_start = ((periods.get(_last_seg) or {}).get("started_at")
                    or (periods.get("day") or {}).get("started_at")
                    or datetime.now(_city_tz(city)).replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    ).isoformat())

    shift = active_any if active_any and active_any.get("city_id") == selected_city_id else None
    shift_data = None
    if shift:
        stats = await get_stats(shift["id"])
        shift_data = {
            "start_time": shift["start_time"],
            "start_at": shift.get("start_at"),
            "district": (shift.get("district") or "").upper(),
            "stats": stats,
            "server_now": datetime.now(_city_tz(city)).isoformat(),
            "worked_min": _shift_worked_min(shift),
            "scheduled": _shift_is_scheduled(shift),
            "on_lunch": bool(shift.get("on_lunch")),
            "city": {"id": city["id"], "name": city["name"]},
        }

    last = await get_last_shift(uid, selected_city_id)
    last_data = None
    if last:
        last_data = {
            "date": _fmt_date(last.get("created_at")),
            "earned": last.get("earned") or 0,
            "worked": _duration_shift(last) if last.get("end_time") else "—",
        }

    title, tier = _title_for_level(lvl)
    return web.json_response({
        "user": {
            "id": uid,
            "name": name or tg_user.get("first_name", ""),
            "role": role,
            "pay_type": pay_type,
            "pay_amount": pay_amount,
            # === НОВОЕ: тумблер «Режим редактирования» ===
            "edit_mode": bool((user or {}).get("edit_mode")),
            # Дефолт авто-закрытия смены (для тумблера в форме старта)
            "auto_close": bool((user or {}).get("auto_close")),
            "auto_close_hours": (user or {}).get("auto_close_hours") or DEFAULT_AUTO_CLOSE_HOURS,
            # Город по умолчанию не подменяется городом активной смены.
            "city_id": user_city_id,
        },
        "registered": bool(user and name and role and user_city_id),
        "cities": [
            {"id": item["id"], "key": item["city_key"], "name": item["name"],
             "timezone_offset": item["timezone_offset"]}
            for item in sorted(CITIES_BY_ID.values(), key=lambda value: value["name"])
        ],
        "city": {"id": city["id"], "key": city["city_key"], "name": city["name"],
                 "timezone_offset": city["timezone_offset"]},
        "active": bool(shift),
        "shift": shift_data,
        "last": last_data,
        "level": {"level": lvl, "xp": xp, "need": need, "title": title, "tier": tier},
        "total_earned": total_earned,
        "lifetime": {"actions": total, "earned": total_earned, "shifts": shifts_count},
        "month_earned": month_earned,
        "build_version": BUILD_VERSION,
        "period_started_at": period_start,
        "period_started_label": _fmt_date(period_start),
    })

async def api_settings(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)

    city_id = body.get("city_id")
    if city_id is not None:
        if not isinstance(city_id, int) or not get_city(city_id):
            return web.json_response({"error": "city_id", "message": "Неизвестный город."}, status=400)
        if not await _is_city_member(uid, city_id):
            return web.json_response(
                {"error": "city_membership", "message": "Вы не состоите в рабочей группе этого города."},
                status=403)
        await set_user_city(uid, city_id)
    else:
        current_user = await get_user(uid)
        city_id = (current_user or {}).get("city_id") or (get_default_city() or {}).get("id")

    # Оплату сохраняем только если реально передана (регистрация шлёт лишь имя+роль,
    # иначе у нового сотрудника остались бы DEFAULT'ы, а не обнуление).
    if "pay_type" in body or "pay_amount" in body:
        pay_type = body.get("pay_type", DEFAULT_PAY_TYPE)
        if pay_type not in ("hourly", "salary", "piece"):
            return web.json_response({"error": "pay_type"}, status=400)
        try:
            pay_amount = float(body.get("pay_amount", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "pay_amount"}, status=400)
        if not math.isfinite(pay_amount) or pay_amount < 0 or pay_amount > 10_000_000:
            return web.json_response(
                {"error": "pay_amount", "message": "Укажи корректную ставку."}, status=400)
        await set_user_pay(uid, pay_type, pay_amount)

    # === НОВОЕ: тумблер «Режим редактирования» — сохраняем, если передан ===
    if "edit_mode" in body:
        await set_user_edit_mode(uid, bool(body.get("edit_mode")))

    # Имя и роль — необязательно (можно зарегистрироваться прямо в приложении)
    name = (body.get("name") or "").strip()
    role = (body.get("role") or "").strip().lower()
    role_map = {"скаут": "Скаут", "водитель": "Водитель", "чарджер": "Чарджер"}

    # В городах, где у каждой роли своя группа (Химки), роль обязательна:
    # без неё бот не знает, в какую группу писать смену.
    if city_requires_role(city_id):
        current_user = await get_user(uid)
        effective_role = role_map.get(role) or (current_user or {}).get("role") or ""
        if not effective_role:
            return web.json_response(
                {"error": "role_required",
                 "message": "В этом городе укажи роль — у каждой роли своя рабочая группа."},
                status=400)
        supported = city_supported_roles(city_id)
        if _norm_role(effective_role) not in {_norm_role(item) for item in supported}:
            return web.json_response(
                {"error": "role_unsupported",
                 "message": "В этом городе нет группы для роли "
                            f"«{effective_role}». Доступны: {', '.join(supported)}."},
                status=400)

    if name and role in role_map:
        await add_user(uid, name, role_map[role], city_id)

    return web.json_response({"ok": True})

# === НОВОЕ: старт/стоп смены из мини-приложения (результат = текстовой команде) ===
_TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')

def _valid_time(t):
    if not isinstance(t, str) or not t or not _TIME_RE.match(t):
        return None
    h, m = t.split(':')
    if int(h) > 23 or int(m) > 59:
        return None
    return f"{int(h)}:{m}"   # нормализуем как в чате: 9:20, 18:05

async def api_shift_start(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid)
    if not user or not user.get("full_name"):
        return web.json_response(
            {"error": "no_name", "message": "Сначала укажи имя и роль в Настройках."},
            status=400)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    city_id = body.get("city_id", user.get("city_id"))
    if not isinstance(city_id, int) or not get_city(city_id):
        return web.json_response({"error": "city_id", "message": "Выбери город."}, status=400)
    if not await _is_city_member(uid, city_id):
        return web.json_response(
            {"error": "city_membership", "message": "Вы не состоите в рабочей группе этого города."},
            status=403)
    # В городах с ролевыми группами (Химки) без подходящей роли смену
    # открыть нельзя — иначе непонятно, в какую группу писать отчёт.
    if city_requires_role(city_id):
        user_role = (user or {}).get("role") or ""
        supported = city_supported_roles(city_id)
        if _norm_role(user_role) not in {_norm_role(item) for item in supported}:
            return web.json_response(
                {"error": "role_required",
                 "message": "Укажи роль в Настройках — в этом городе у каждой роли "
                            f"своя группа. Доступны: {', '.join(supported)}."},
                status=400)
    if await get_active_shift(uid):
        return web.json_response(
            {"error": "already_active", "message": "Смена уже открыта."}, status=400)
    time_str = _valid_time((body.get("time") or "").strip())
    if not time_str:
        return web.json_response(
            {"error": "time", "message": "Время в формате ЧЧ:ММ."}, status=400)
    district = (body.get("district") or "").strip().lower()

    # Авто-закрытие: тумблер + выбор часов (8/10/12) из формы старта.
    auto_close = bool(body.get("auto_close"))
    try:
        auto_close_hours = int(body.get("auto_close_hours", DEFAULT_AUTO_CLOSE_HOURS))
    except (TypeError, ValueError):
        auto_close_hours = DEFAULT_AUTO_CLOSE_HOURS
    if auto_close_hours not in AUTO_CLOSE_CHOICES:
        auto_close_hours = DEFAULT_AUTO_CLOSE_HOURS
    await set_user_auto_close(uid, auto_close, auto_close_hours)

    try:
        sid = await start_shift(
            uid, user["full_name"], user.get("role") or "", time_str, district, city_id,
            auto_close=auto_close, auto_close_hours=auto_close_hours
        )
    except ActiveShiftExists:
        return web.json_response(
            {"error": "already_active", "message": "Смена уже открыта."}, status=400)
    await set_user_city(uid, city_id)
    report_ok = await safe_flush_report_update(sid)
    logger.info(f"Смена начата (из приложения): {user['full_name']}, {time_str}, {district or '—'}")
    shift = await get_shift_by_id(sid)
    return web.json_response({
        "ok": True, "scheduled": _shift_is_scheduled(shift), "report_updated": report_ok
    })

async def api_shift_stop(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    active_shift = await get_active_shift(uid)
    city_id = active_shift.get("city_id") if active_shift else None
    if not active_shift:
        return web.json_response(
            {"error": "not_active", "message": "Нет открытой смены."}, status=400)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    time_str = _valid_time((body.get("time") or "").strip())
    if not time_str:
        return web.json_response(
            {"error": "time", "message": "Время в формате ЧЧ:ММ."}, status=400)
    comment = (body.get("comment") or "").strip()

    try:
        sid = await end_shift(uid, time_str, comment, city_id)
    except ValueError as exc:
        return web.json_response({"error": "end_time", "message": str(exc)}, status=400)
    if not sid:
        return web.json_response({"error": "fail", "message": "Ошибка завершения."}, status=500)
    report_ok = await safe_flush_report_update(sid)
    logger.info(f"Смена завершена (из приложения): uid={uid}")
    return web.json_response({"ok": True, "report_updated": report_ok})


async def api_shift_lunch(request):
    """Включает или снимает информационный статус «на обеде».

    Статус хранится отдельно от действий и расчётов смены: он только меняет
    строку в живом Telegram-отчёте и отметку в админке.
    """
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response(
            {"error": "json", "message": "Ожидается JSON-объект."}, status=400
        )
    active = body.get("active")
    if not isinstance(active, bool):
        return web.json_response(
            {"error": "active", "message": "Статус обеда должен быть true или false."},
            status=400,
        )

    uid = tg_user["id"]
    shift = await get_active_shift(uid)
    if not shift:
        return web.json_response(
            {"error": "not_active", "message": "Нет открытой смены."}, status=400
        )
    if _shift_is_scheduled(shift):
        return web.json_response(
            {"error": "scheduled", "message": "Обед можно отметить после начала смены."},
            status=409,
        )

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shifts SET on_lunch = ? WHERE id = ? AND is_active = 1",
            (1 if active else 0, shift["id"]),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return web.json_response(
                {"error": "not_active", "message": "Смена уже закрыта."}, status=409
            )
        await db.commit()

    report_ok = await safe_flush_report_update(shift["id"])
    logger.info(
        f"Статус обеда {'включён' if active else 'снят'}: uid={uid}, смена={shift['id']}"
    )
    return web.json_response({
        "ok": True, "on_lunch": active, "report_updated": report_ok
    })

# === НОВОЕ: изменить любой из 6 счётчиков на ±1 из приложения (режим редактирования) ===
# Разрешённые типы действий, которые можно править из приложения.
EDITABLE_ACTIONS = ("move", "fix", "repair", "battery", "to_sc", "from_sc")

async def api_action_add(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid) or {}
    shift = await get_active_shift(uid)
    if not shift:
        return web.json_response(
            {"error": "not_active", "message": "Сначала открой смену."}, status=400)
    if not user.get("edit_mode"):
        return web.json_response(
            {"error": "edit_mode", "message": "Сначала включи режим редактирования."}, status=403)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    atype = body.get("action_type")
    if atype not in EDITABLE_ACTIONS:
        return web.json_response({"error": "action_type"}, status=400)

    # delta: +1 (добавить) или -1 (убрать). По умолчанию +1.
    # Приводим к int строго: bool/float/строку "1" не принимаем как валидные.
    delta = body.get("delta", 1)
    if not isinstance(delta, int) or isinstance(delta, bool) or delta not in (1, -1):
        return web.json_response({"error": "delta"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        current_shift = await (await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1", (uid,)
        )).fetchone()
        if not current_shift:
            await db.rollback()
            return web.json_response(
                {"error": "not_active", "message": "Смена уже закрыта."}, status=400)
        shift = dict(current_shift)
        # Проверка и -1 записываются в одной write-транзакции: два клика
        # не смогут одновременно увести счётчик в минус.
        if delta == -1:
            rows = await (await db.execute(
                "SELECT bike_codes, quantity FROM actions WHERE shift_id = ? AND action_type = ?",
                (shift["id"], atype)
            )).fetchall()
            current_amount = max(0, sum(_action_units(row) for row in rows))
            if current_amount <= 0:
                await db.rollback()
                return web.json_response(
                    {"error": "empty", "message": "Счётчик уже пустой."}, status=400)
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) "
            "VALUES (?, ?, 0, ?, '', ?, ?)",
            (uid, shift["id"], atype, delta, shift["city_id"])
        )
        await db.commit()

    schedule_report_update(shift["id"])
    sign = "+" if delta > 0 else ""
    logger.info(f"Действие из приложения: {atype} {sign}{delta} (uid={uid}, смена {shift['id']})")
    return web.json_response({"ok": True})

async def api_shift_delete(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    sid = body.get("shift_id")
    if not isinstance(sid, int):
        return web.json_response({"error": "shift_id"}, status=400)

    shift = await get_shift_by_id(sid)
    # Удалять можно ТОЛЬКО свою закрытую смену
    if not shift or shift.get("user_id") != uid:
        return web.json_response(
            {"error": "not_found", "message": "Смена не найдена."}, status=404)
    if shift.get("is_active"):
        return web.json_response(
            {"error": "active", "message": "Активную смену нельзя удалить — сначала закрой её."},
            status=400)

    # 1) Удаляем сообщение-отчёт из темы ОТЧЁТЫ (если оно ещё там)
    msg_id = shift.get("report_msg_id")
    # Группа по роли: в Химках отчёт водителя лежит в водительской группе.
    city = city_for_role(shift.get("city_id"), shift.get("role"))
    if msg_id:
        try:
            if city:
                await bot.delete_message(city["group_id"], msg_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить отчёт смены {sid} из группы: {e}")

    # 2) Удаляем смену и её действия из базы
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM actions WHERE shift_id = ?", (sid,))
        await db.execute("DELETE FROM manual_reports WHERE shift_id = ?", (sid,))
        await db.execute("DELETE FROM work_message_links WHERE shift_id = ?", (sid,))
        await db.execute("DELETE FROM shifts WHERE id = ?", (sid,))
        await db.commit()

    start_at = _parse_datetime(shift.get("start_at"))
    if start_at and shift.get("city_id"):
        await refresh_monthly_aggregate(
            shift["city_id"], uid, start_at.strftime("%Y-%m")
        )

    logger.info(f"Смена {sid} удалена пользователем {uid} (вместе с отчётом)")
    return web.json_response({"ok": True})

async def api_history(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    user = await get_user(uid) or {}
    active = await get_active_shift(uid)
    city_id = (active or {}).get("city_id") or user.get("city_id") or (get_default_city() or {}).get("id")
    rows = await get_history(uid, city_id)
    periods = await city_periods(city_id)
    home_city = get_city(city_id) or {}
    period_start = ((periods.get("day") or {}).get("started_at"))
    items = []
    for s in rows:
        worked = _duration_shift(s) if s.get("end_time") else "—"
        city = get_city(s.get("city_id")) or {}
        # Смена сверяется с декадой СВОЕЙ группы: день или ночь.
        in_period = _shift_in_period(s, periods, city or home_city)
        items.append({
            "shift_id": s["id"],
            "date": _fmt_date(s.get("created_at")),
            "start": s.get("start_time"),
            "end": s.get("end_time"),
            "worked": worked,
            "earned": s.get("earned") or 0,
            "pay_type": s.get("pay_type_snap") or "hourly",
            "district": (s.get("district") or "").upper(),
            "city_name": city.get("name", ""),
            "source": s.get("source") or "bot",
            "comment": s.get("comment") or "",
            # Смена входит в текущую декаду — по ней считается «Всего заработано».
            "in_period": in_period,
        })
    return web.json_response({"items": items, "period_started_at": period_start})


def _action_units(row):
    count = len((row["bike_codes"] or "").split(",")) if row["bike_codes"] else 0
    count += row["quantity"] or 0
    return count


async def get_shift_action_count(shift_id, db=None):
    own_connection = db is None
    if own_connection:
        db = await aiosqlite.connect(DB_PATH)
    try:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT bike_codes, quantity FROM actions WHERE shift_id = ?", (shift_id,)
        )).fetchall()
        return max(0, sum(_action_units(row) for row in rows))
    finally:
        if own_connection:
            await db.close()


async def refresh_monthly_aggregate(city_id, uid, month):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        shifts = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND user_id = ? AND is_active = 0 "
            "AND start_at LIKE ? ORDER BY id", (city_id, uid, month + "%")
        )).fetchall()
        if not shifts:
            await db.execute(
                "DELETE FROM monthly_aggregates WHERE city_id = ? AND user_id = ? AND month = ?",
                (city_id, uid, month)
            )
            await db.commit()
            return
        worked = 0
        actions = 0
        earned = 0.0
        for raw in shifts:
            shift = dict(raw)
            worked += _shift_worked_min(shift)
            actions += await get_shift_action_count(shift["id"], db)
            earned += shift.get("earned") or 0
        last = dict(shifts[-1])
        await db.execute(
            "INSERT INTO monthly_aggregates (city_id, user_id, month, full_name, role, "
            "shifts_count, worked_minutes, actions_count, earned, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_id, user_id, month) DO UPDATE SET "
            "full_name=excluded.full_name, role=excluded.role, shifts_count=excluded.shifts_count, "
            "worked_minutes=excluded.worked_minutes, actions_count=excluded.actions_count, "
            "earned=excluded.earned, updated_at=excluded.updated_at",
            (city_id, uid, month, last.get("full_name") or "Сотрудник", last.get("role") or "",
             len(shifts), worked, actions, round(earned, 2), datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def rebuild_monthly_aggregates():
    """Один раз при старте заполняет/восстанавливает месячные агрегаты.

    В обычной работе они обновляются точечно при закрытии, /fix,
    правке ручного отчёта и удалении смены, а не пересчитываются
    при каждом открытии админки.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT city_id, user_id, substr(start_at, 1, 7) AS month "
            "FROM shifts WHERE is_active = 0 AND city_id IS NOT NULL "
            "AND start_at IS NOT NULL AND length(start_at) >= 7"
        )).fetchall()
    for city_id, uid, month in rows:
        await refresh_monthly_aggregate(city_id, uid, month)


_kpi_refreshed_hours = {}


async def refresh_city_metrics(city_id):
    city = get_city(city_id)
    if not city:
        return
    now = datetime.now(_city_tz(city))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    hour = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        shifts = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND start_at < ? "
            "AND COALESCE(end_at, ?) > ?",
            (city_id, day_end.isoformat(), day_end.isoformat(), day_start.isoformat())
        )).fetchall()
        by_user = {}
        for raw in shifts:
            shift = dict(raw)
            item = by_user.setdefault(shift["user_id"], {"worked": 0, "actions": 0})
            item["worked"] += _shift_worked_min(shift, now)
            item["actions"] += await get_shift_action_count(shift["id"], db)
        # Снимок — полное состояние дня. Если последнюю смену сотрудника
        # удалили, его старый KPI не должен висеть в админке до полуночи.
        if by_user:
            user_ids = list(by_user)
            placeholders = ",".join("?" for _ in user_ids)
            await db.execute(
                f"DELETE FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour >= ? "
                f"AND snapshot_hour < ? AND user_id NOT IN ({placeholders})",
                (city_id, day_start.isoformat(), day_end.isoformat(), *user_ids)
            )
        else:
            await db.execute(
                "DELETE FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour >= ? "
                "AND snapshot_hour < ?",
                (city_id, day_start.isoformat(), day_end.isoformat())
            )
        for uid, item in by_user.items():
            efficiency = round(item["actions"] * 60 / item["worked"], 2) if item["worked"] else 0
            await db.execute(
                "INSERT INTO kpi_snapshots (city_id, user_id, snapshot_hour, actions_count, "
                "worked_minutes, efficiency) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(city_id, user_id, snapshot_hour) DO UPDATE SET "
                "actions_count=excluded.actions_count, worked_minutes=excluded.worked_minutes, "
                "efficiency=excluded.efficiency",
                (city_id, uid, hour, item["actions"], item["worked"], efficiency)
            )
        await db.commit()
    _kpi_refreshed_hours[city_id] = hour


async def ensure_city_metrics_current(city_id):
    """Не пересчитывает почасовой KPI при каждом 20-секундном обновлении UI."""
    city = get_city(city_id)
    if not city:
        return
    hour = datetime.now(_city_tz(city)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()
    if _kpi_refreshed_hours.get(city_id) == hour:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        exists = await (await db.execute(
            "SELECT 1 FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour = ? LIMIT 1",
            (city_id, hour),
        )).fetchone()
    if exists:
        _kpi_refreshed_hours[city_id] = hour
        return
    await refresh_city_metrics(city_id)


async def kpi_background_worker():
    while True:
        for city_id in list(CITIES_BY_ID):
            try:
                await refresh_city_metrics(city_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Не удалось обновить KPI города {city_id}: {exc}")
        now = datetime.now(timezone.utc)
        wait_seconds = 3600 - (now.minute * 60 + now.second)
        await asyncio.sleep(max(60, wait_seconds))


_started_report_updates = set()


async def _auto_close_shift(shift):
    """Закрывает смену по дедлайну auto_close_at. Идемпотентно: если смену
    уже закрыли сами — UPDATE не затронет строк и мы просто выходим."""
    city = get_city(shift.get("city_id")) or get_default_city()
    deadline = _parse_datetime(shift.get("auto_close_at"))
    if not deadline:
        return
    end_time = deadline.astimezone(_city_tz(city)).strftime("%H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, end_at = ?, on_lunch = 0 "
            "WHERE id = ? AND is_active = 1",
            (end_time, deadline.isoformat(), shift["id"])
        )
        await db.commit()
        if cur.rowcount != 1:
            return
    await freeze_earned(shift["id"])
    await safe_flush_report_update(shift["id"])
    logger.info(f"Смена {shift['id']} закрыта автоматически (дедлайн {end_time}).")


async def auto_close_worker():
    """Раз в 30 сек закрывает активные смены, у которых наступил дедлайн."""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(
                    "SELECT * FROM shifts WHERE is_active = 1 AND auto_close_at IS NOT NULL"
                )).fetchall()
            for row in rows:
                deadline = _parse_datetime(row["auto_close_at"])
                if deadline and now_utc >= deadline.astimezone(timezone.utc):
                    await _auto_close_shift(dict(row))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Авто-закрытие смен не сработало: {exc}")
        await asyncio.sleep(30)


async def scheduled_report_status_worker():
    """Снимает пометку «ожидает начала» без долгих ненадёжных таймеров."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(
                    "SELECT id, city_id, start_at FROM shifts WHERE is_active = 1 "
                    "AND start_at IS NOT NULL"
                )).fetchall()
            active_ids = {row["id"] for row in rows}
            _started_report_updates.intersection_update(active_ids)
            for row in rows:
                city = get_city(row["city_id"])
                start_at = _parse_datetime(row["start_at"])
                if (city and start_at and row["id"] not in _started_report_updates
                        and datetime.now(_city_tz(city)) >= start_at):
                    if await safe_flush_report_update(row["id"]):
                        _started_report_updates.add(row["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Не удалось обновить статус отложенной смены: {exc}")
        await asyncio.sleep(15)


def _admin_key():
    return hashlib.sha256((BOT_TOKEN + "\0" + ADMIN_PASSWORD).encode()).digest()


def _issue_admin_token(uid):
    expires = int(datetime.now(timezone.utc).timestamp()) + ADMIN_SESSION_TTL_SEC
    payload = json.dumps({"uid": uid, "exp": expires}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_admin_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}", expires


def _verify_admin_token(token, uid):
    if not token or "." not in token or not ADMIN_PASSWORD:
        return False
    encoded, received = token.rsplit(".", 1)
    expected = hmac.new(_admin_key(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return False
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        return payload.get("uid") == uid and int(payload.get("exp", 0)) >= int(
            datetime.now(timezone.utc).timestamp()
        )
    except Exception:
        return False


_admin_login_failures = {}


async def _admin_user(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return None
    uid = tg_user["id"]
    if not _verify_admin_token(request.headers.get("X-Admin-Token", ""), uid):
        return None
    return tg_user


async def _admin_city(uid, bind_if_missing=False):
    """Город администратора хранится отдельно и не меняется через настройки."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT city_id FROM admin_city_access WHERE user_id = ?", (uid,)
        )).fetchone()
        if row:
            return get_city(row[0])
        if not bind_if_missing:
            return None
        user = await (await db.execute(
            "SELECT city_id FROM users WHERE user_id = ?", (uid,)
        )).fetchone()
        city = get_city(user[0] if user else None)
        if not city:
            return None
        await db.execute(
            "INSERT OR IGNORE INTO admin_city_access (user_id, city_id, created_at) "
            "VALUES (?, ?, ?)",
            (uid, city["id"], datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        bound = await (await db.execute(
            "SELECT city_id FROM admin_city_access WHERE user_id = ?", (uid,)
        )).fetchone()
        return get_city(bound[0] if bound else None)


async def _admin_context(request):
    """Возвращает администратора и его серверно закреплённый город.

    Город никогда не берётся из параметров запроса: так подмена city_id в
    браузере не открывает данные другого филиала.
    """
    tg_user = await _admin_user(request)
    if not tg_user:
        return None
    user = await get_user(tg_user["id"])
    city = await _admin_city(tg_user["id"])
    return {"telegram_user": tg_user, "user": user or {}, "city": city}


async def api_admin_login(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    if not ADMIN_PASSWORD:
        return web.json_response(
            {"error": "admin_disabled", "message": "ADMIN_PASSWORD не настроен."}, status=503
        )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    failures = [stamp for stamp in _admin_login_failures.get(uid, []) if now_ts - stamp < 600]
    if len(failures) >= 5:
        return web.json_response(
            {"error": "rate_limit", "message": "Слишком много попыток. Повтори через 10 минут."},
            status=429
        )
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    password = body.get("password")
    if not isinstance(password, str) or not hmac.compare_digest(password, ADMIN_PASSWORD):
        failures.append(now_ts)
        _admin_login_failures[uid] = failures
        return web.json_response({"error": "password", "message": "Неверный пароль."}, status=403)
    city = await _admin_city(uid, bind_if_missing=True)
    if not city:
        return web.json_response(
            {
                "error": "admin_city",
                "message": "Сначала зарегистрируйтесь и выберите свой город в настройках.",
            },
            status=409,
        )
    _admin_login_failures.pop(uid, None)
    token, expires = _issue_admin_token(uid)
    return web.json_response({
        "ok": True,
        "token": token,
        "expires_at": expires,
        "city": {"id": city["id"], "name": city["name"]},
    })


async def approve_manual_report(report_id, start_time, end_time, expected_updated_at=None,
                                allowed_city_id=None):
    """Принимает ручной отчёт, переиспользуя связанную сигнальную смену."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if allowed_city_id is None:
            report = await (await db.execute(
                "SELECT * FROM manual_reports WHERE id = ? AND status = 'needs_review'",
                (report_id,)
            )).fetchone()
        else:
            report = await (await db.execute(
                "SELECT * FROM manual_reports WHERE id = ? AND city_id = ? "
                "AND status = 'needs_review'",
                (report_id, allowed_city_id),
            )).fetchone()
        if not report:
            raise LookupError("Ручной отчёт не найден или уже обработан.")
        if expected_updated_at is not None and report["updated_at"] != expected_updated_at:
            raise ValueError("Отчёт изменился в Telegram. Обновите админку и проверьте его снова.")
        city = get_city(report["city_id"])
        if not city:
            raise ValueError("Город отчёта больше не активен.")
        message_time = _parse_datetime(report["created_at"]) or datetime.now(_city_tz(city))
        start_at, end_at = _resolve_manual_interval(
            start_time, end_time, city, message_time
        )
        duration = end_at - start_at
        if duration <= timedelta(0) or duration > timedelta(hours=18):
            raise ValueError("Смена должна длиться больше 0 и не больше 18 часов.")

        target_shift = None
        if report["shift_id"]:
            linked = await (await db.execute(
                "SELECT * FROM shifts WHERE id = ? AND user_id = ? AND city_id = ?",
                (report["shift_id"], report["user_id"], report["city_id"]),
            )).fetchone()
            if linked and linked["source"] == "manual_signal":
                target_shift = linked
        if target_shift is None:
            candidates = await (await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? "
                "AND source = 'manual_signal' AND start_at IS NOT NULL "
                "AND julianday(start_at) < julianday(?) "
                "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) "
                "> julianday(?) ORDER BY id DESC LIMIT 2",
                (report["user_id"], report["city_id"], end_at.isoformat(),
                 start_at.isoformat()),
            )).fetchall()
            if len(candidates) > 1:
                raise ValueError("Интервал совпал с несколькими ручными сменами.")
            if len(candidates) == 1:
                target_shift = candidates[0]

        target_shift_id = target_shift["id"] if target_shift else None
        conflict = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? "
            "AND id <> COALESCE(?, -1) AND start_at IS NOT NULL "
            "AND julianday(start_at) < julianday(?) "
            "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) > julianday(?) "
            "LIMIT 1",
            (report["user_id"], target_shift_id, end_at.isoformat(), start_at.isoformat())
        )).fetchone()
        if conflict:
            raise ValueError(f"Интервал пересекается со сменой #{conflict[0]}.")

        user = await (await db.execute(
            "SELECT full_name, role, pay_type, pay_amount FROM users WHERE user_id = ?",
            (report["user_id"],)
        )).fetchone()
        if target_shift:
            full_name = target_shift["full_name"] or report["sender_name"] \
                or f"Сотрудник #{report['user_id']}"
            role = target_shift["role"] \
                or (user["role"] if user and user["role"] else "")
        else:
            full_name = (user["full_name"] if user and user["full_name"] else None) \
                or report["sender_name"] \
                or f"Сотрудник #{report['user_id']}"
            role = user["role"] if user and user["role"] else ""
        pay_type = report["pay_type_snap"] \
            or (target_shift["pay_type_snap"] if target_shift else None) \
            or (user["pay_type"] if user and user["pay_type"] else None) \
            or DEFAULT_PAY_TYPE
        pay_amount = report["pay_amount_snap"]
        if pay_amount is None and target_shift:
            pay_amount = target_shift["pay_amount_snap"]
        if pay_amount is None:
            pay_amount = user["pay_amount"] if user and user["pay_amount"] is not None \
                else DEFAULT_PAY_AMOUNT
        actions = parse_message(report["raw_text"])
        battery_count = sum(
            len(action.get("bike_codes") or []) + int(action.get("quantity") or 0)
            for action in actions if action["action_type"] == "battery"
        )
        worked_minutes = int(duration.total_seconds() // 60)
        earned = compute_earned(pay_type, pay_amount, worked_minutes, battery_count)
        store_report_actions = True
        if target_shift:
            shift_id = target_shift["id"]
            await db.execute(
                "UPDATE shifts SET full_name=?, role=?, start_time=?, end_time=?, "
                "is_active=0, on_lunch=0, created_at=?, start_at=?, end_at=?, earned=?, "
                "pay_type_snap=COALESCE(pay_type_snap, ?), "
                "pay_amount_snap=COALESCE(pay_amount_snap, ?) "
                "WHERE id=? AND source='manual_signal'",
                (full_name, role, start_time, end_time, start_at.isoformat(),
                 start_at.isoformat(), end_at.isoformat(), earned, pay_type, pay_amount,
                 shift_id),
            )
            await db.execute(
                "DELETE FROM actions WHERE shift_id = ? AND message_id = ?",
                (shift_id, report["message_id"]),
            )
            has_live_actions = await (await db.execute(
                "SELECT 1 FROM actions WHERE shift_id = ? LIMIT 1", (shift_id,)
            )).fetchone()
            store_report_actions = not bool(has_live_actions)
        else:
            cursor = await db.execute(
                "INSERT INTO shifts (user_id, full_name, role, start_time, end_time, is_active, "
                "created_at, city_id, start_at, end_at, source, source_message_id, earned, "
                "pay_type_snap, pay_amount_snap) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'manual_chat', ?, ?, ?, ?)",
                (report["user_id"], full_name, role, start_time, end_time,
                 start_at.isoformat(), report["city_id"], start_at.isoformat(),
                 end_at.isoformat(), report["message_id"], earned, pay_type, pay_amount),
            )
            shift_id = cursor.lastrowid
        if store_report_actions:
            for action in actions:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                    "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (report["user_id"], shift_id, report["message_id"], action["action_type"],
                     ",".join(action.get("bike_codes") or []), action.get("quantity", 0),
                     report["city_id"])
                )
        await db.execute(
            "UPDATE manual_reports SET status = 'accepted', parse_error = NULL, shift_id = ?, "
            "pay_type_snap = ?, pay_amount_snap = ?, updated_at = ? WHERE id = ?",
            (shift_id, pay_type, pay_amount, datetime.now(_city_tz(city)).isoformat(), report_id)
        )
        await db.commit()
    try:
        await freeze_earned(shift_id)
    except Exception as exc:
        logger.error(f"Смена {shift_id} учтена, но месячная сводка не обновилась: {exc}")
    if target_shift and target_shift["report_msg_id"]:
        await safe_flush_report_update(shift_id)
    return shift_id


async def api_admin_manual_approve(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403
        )
    body = await _request_json_object(request)
    if body is None:
        return web.json_response(
            {"error": "json", "message": "Ожидается JSON-объект."}, status=400
        )
    report_id = body.get("report_id")
    start_time = _valid_time(body.get("start_time"))
    end_time = _valid_time(body.get("end_time"))
    expected_updated_at = body.get("updated_at")
    if (not isinstance(report_id, int) or not start_time or not end_time
            or not isinstance(expected_updated_at, str) or not expected_updated_at):
        return web.json_response(
            {"error": "fields", "message": "Укажите корректные начало и окончание."}, status=400
        )
    try:
        shift_id = await approve_manual_report(
            report_id, start_time, end_time, expected_updated_at, city["id"]
        )
    except LookupError as exc:
        return web.json_response({"error": "not_found", "message": str(exc)}, status=404)
    except (ValueError, aiosqlite.IntegrityError) as exc:
        return web.json_response({"error": "manual_report", "message": str(exc)}, status=409)
    return web.json_response({"ok": True, "shift_id": shift_id})


async def _admin_shift_payload(shift, city, now, db):
    action_rows = await (await db.execute(
        "SELECT action_type, bike_codes, quantity FROM actions WHERE shift_id = ?",
        (shift["id"],),
    )).fetchall()
    # Разбивка действий по типам (без денег — админ видит только работу и время).
    stats = {"move": 0, "fix": 0, "repair": 0, "battery": 0, "to_sc": 0, "from_sc": 0}
    for row in action_rows:
        t = row["action_type"]
        if t in stats:
            stats[t] += _action_units(row)
    stats = {k: max(0, v) for k, v in stats.items()}
    actions = sum(stats.values())
    worked = _shift_worked_min(shift, now)
    if shift.get("is_active"):
        status = "scheduled" if _shift_is_scheduled(shift, now) else "active"
    else:
        status = "closed"
    return {
        "shift_id": shift["id"],
        "user_id": shift["user_id"],
        "name": shift.get("full_name") or "Сотрудник",
        "role": shift.get("role") or "",
        "source": shift.get("source") or "bot",
        "status": status,
        "date": _fmt_date(shift.get("start_at") or shift.get("created_at")),
        "start": shift.get("start_time"),
        "end": shift.get("end_time"),
        "start_at": shift.get("start_at"),
        "end_at": shift.get("end_at"),
        "district": shift.get("district") or "",
        "on_lunch": bool(shift.get("on_lunch")) if status == "active" else False,
        "worked_minutes": worked,
        "actions": actions,
        "stats": stats,
        "efficiency": round(actions * 60 / worked, 2) if worked else None,
    }


async def api_admin_dashboard(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403
        )
    city_id = city["id"]
    requested_city = request.query.get("city_id")
    if requested_city not in (None, ""):
        try:
            requested_city_id = int(requested_city)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "city_id", "message": "Некорректный город."}, status=400
            )
        if requested_city_id != city_id:
            return web.json_response(
                {"error": "admin_city", "message": "Доступ разрешён только к своему городу."},
                status=403,
            )

    await ensure_city_metrics_current(city_id)
    now = datetime.now(_city_tz(city))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    month = now.strftime("%Y-%m")
    periods = await city_periods(city_id)
    period = periods.get("day")
    period_start = min(
        [p.get("started_at") for p in periods.values() if p and p.get("started_at")]
        or [day_start.isoformat()]
    )
    period_ids = {p.get("id") for p in periods.values() if p}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        open_rows = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND is_active = 1 "
            "ORDER BY start_at, id",
            (city_id,),
        )).fetchall()
        closed_today_rows = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND is_active = 0 AND start_at < ? "
            "AND COALESCE(end_at, start_at) > ? ORDER BY start_at, id",
            (city_id, day_end.isoformat(), day_start.isoformat()),
        )).fetchall()
        open_items = [
            await _admin_shift_payload(dict(row), city, now, db) for row in open_rows
        ]
        closed_today_items = [
            await _admin_shift_payload(dict(row), city, now, db) for row in closed_today_rows
        ]

        monthly = await (await db.execute(
            "SELECT * FROM monthly_aggregates WHERE city_id = ? AND month = ? "
            "ORDER BY full_name", (city_id, month)
        )).fetchall()
        kpi_rows = await (await db.execute(
            "SELECT k.user_id, k.snapshot_hour, k.actions_count, k.worked_minutes, "
            "k.efficiency, COALESCE(NULLIF(u.full_name, ''), "
            "(SELECT s.full_name FROM shifts s WHERE s.city_id = k.city_id "
            "AND s.user_id = k.user_id ORDER BY s.id DESC LIMIT 1), 'Сотрудник') AS full_name, "
            "COALESCE(NULLIF(u.role, ''), (SELECT s.role FROM shifts s WHERE s.city_id = k.city_id "
            "AND s.user_id = k.user_id ORDER BY s.id DESC LIMIT 1), '') AS role "
            "FROM kpi_snapshots k JOIN (SELECT user_id, MAX(snapshot_hour) AS snapshot_hour "
            "FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour >= ? AND snapshot_hour < ? "
            "GROUP BY user_id) latest "
            "ON latest.user_id = k.user_id AND latest.snapshot_hour = k.snapshot_hour "
            "LEFT JOIN users u ON u.user_id = k.user_id WHERE k.city_id = ? "
            "ORDER BY full_name",
            (city_id, day_start.isoformat(), day_end.isoformat(), city_id)
        )).fetchall()
        latest = await (await db.execute(
            "SELECT MAX(snapshot_hour) FROM kpi_snapshots WHERE city_id = ? "
            "AND snapshot_hour >= ? AND snapshot_hour < ?",
            (city_id, day_start.isoformat(), day_end.isoformat())
        )).fetchone()

        # База возвращает по одной агрегированной строке на сотрудника, а не
        # всю многолетнюю историю при каждом автообновлении админки.
        employee_shift_rows = await (await db.execute(
            "SELECT s.user_id, "
            "COALESCE(NULLIF(u.full_name, ''), NULLIF((SELECT latest.full_name FROM shifts latest "
            "WHERE latest.city_id = ? AND latest.user_id = s.user_id "
            "ORDER BY COALESCE(latest.start_at, latest.created_at) DESC, latest.id DESC LIMIT 1), ''), '') "
            "AS full_name, "
            "COALESCE(NULLIF(u.role, ''), NULLIF((SELECT latest.role FROM shifts latest "
            "WHERE latest.city_id = ? AND latest.user_id = s.user_id "
            "ORDER BY COALESCE(latest.start_at, latest.created_at) DESC, latest.id DESC LIMIT 1), ''), '') "
            "AS role, COUNT(*) AS shifts_count, "
            "SUM(CASE WHEN s.is_active = 0 THEN 1 ELSE 0 END) AS closed_shifts, "
            "MAX(CASE WHEN s.is_active = 1 THEN 1 ELSE 0 END) AS has_open_shift, "
            "MAX(COALESCE(s.start_at, s.created_at)) AS last_shift_at "
            "FROM shifts s LEFT JOIN users u ON u.user_id = s.user_id AND u.city_id = ? "
            "WHERE s.city_id = ? GROUP BY s.user_id, u.full_name, u.role",
            (city_id, city_id, city_id, city_id),
        )).fetchall()
        registered_rows = await (await db.execute(
            "SELECT user_id, full_name, role FROM users WHERE city_id = ? ORDER BY full_name",
            (city_id,),
        )).fetchall()

        # Смены декад — берём кандидатов широким запросом (по любой из двух
        # декад или по дате), а точную принадлежность каждой смены к декаде
        # СВОЕЙ группы (день/ночь) проверяем в Python.
        _pid_a, _pid_b = (list(period_ids) + [None, None])[:2]
        period_shift_rows = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND (period_id IN (?, ?) OR "
            "(period_id IS NULL AND COALESCE(start_at, created_at) >= ?)) "
            "ORDER BY start_at, id",
            (city_id, _pid_a, _pid_b, period_start),
        )).fetchall()
        period_action_rows = await (await db.execute(
            "SELECT a.shift_id, a.action_type, a.bike_codes, a.quantity "
            "FROM actions a JOIN shifts s ON s.id = a.shift_id "
            "WHERE s.city_id = ? AND (s.period_id IN (?, ?) OR "
            "(s.period_id IS NULL AND COALESCE(s.start_at, s.created_at) >= ?))",
            (city_id, _pid_a, _pid_b, period_start),
        )).fetchall()

    employees = {}
    for row in employee_shift_rows:
        employees[row["user_id"]] = {
            "user_id": row["user_id"],
            "name": row["full_name"] or f"Сотрудник #{row['user_id']}",
            "role": row["role"] or "",
            "shifts": row["shifts_count"],
            "closed_shifts": row["closed_shifts"],
            "has_open_shift": bool(row["has_open_shift"]),
            "last_shift_at": row["last_shift_at"],
        }
    for row in registered_rows:
        employee = employees.setdefault(row["user_id"], {
            "user_id": row["user_id"],
            "name": row["full_name"] or f"Сотрудник #{row['user_id']}",
            "role": row["role"] or "",
            "shifts": 0,
            "closed_shifts": 0,
            "has_open_shift": False,
            "last_shift_at": None,
        })
        if row["full_name"]:
            employee["name"] = row["full_name"]
        if row["role"]:
            employee["role"] = row["role"]

    role_order = {"скаут": 0, "водитель": 1, "чарджер": 2}
    employee_items = sorted(
        employees.values(),
        key=lambda item: (
            role_order.get((item.get("role") or "").strip().lower(), 3),
            (item.get("name") or "").casefold(),
        ),
    )
    kpi_by_user = {row["user_id"]: row["efficiency"] for row in kpi_rows}
    for item in open_items + closed_today_items:
        if item["user_id"] in kpi_by_user:
            item["efficiency"] = kpi_by_user[item["user_id"]]

    open_today = []
    for raw, item in zip(open_rows, open_items):
        start_at = _parse_datetime(raw["start_at"])
        if not start_at or start_at < day_end:
            open_today.append(item)
    today_items = open_today + closed_today_items

    # Агрегат за декаду по группам: дневные и ночные считаются раздельно,
    # каждая смена сверяется с текущей декадой СВОЕЙ группы.
    actions_by_shift = {}
    for row in period_action_rows:
        actions_by_shift[row["shift_id"]] = (
            actions_by_shift.get(row["shift_id"], 0) + _action_units(row)
        )
    segment_totals = {"day": {}, "night": {}}
    for row in period_shift_rows:
        shift = dict(row)
        if not _shift_in_period(shift, periods, city):
            continue
        seg = _shift_segment(shift, city)
        item = segment_totals[seg].setdefault(shift["user_id"], {
            "user_id": shift["user_id"],
            "name": shift.get("full_name") or f"Сотрудник #{shift['user_id']}",
            "role": shift.get("role") or "",
            "shifts": 0, "worked_minutes": 0, "actions": 0, "open_now": False,
        })
        if shift.get("full_name"):
            item["name"] = shift["full_name"]
        if shift.get("role"):
            item["role"] = shift["role"]
        item["shifts"] += 1
        item["worked_minutes"] += _shift_worked_min(shift, now)
        item["actions"] += max(0, actions_by_shift.get(shift["id"], 0))
        if shift.get("is_active"):
            item["open_now"] = True

    def _sorted_segment(seg):
        return sorted(
            segment_totals[seg].values(),
            key=lambda item: (
                role_order.get((item.get("role") or "").strip().lower(), 3),
                (item.get("name") or "").casefold(),
            ),
        )

    period_day_items = _sorted_segment("day")
    period_night_items = _sorted_segment("night")
    period_items = period_day_items + [
        item for item in period_night_items
        if item["user_id"] not in segment_totals["day"]
    ]
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "generated_at": now.isoformat(),
        "kpi_updated_at": latest[0] if latest else None,
        "open": open_items,
        "closed_today": closed_today_items,
        # Оставлено для совместимости со старой версией Mini App.
        "today": today_items,
        "employees": employee_items,
        # Декады: дневная и ночная группы раздельно + сведения о каждой.
        "period": period_items,
        "period_day": period_day_items,
        "period_night": period_night_items,
        "period_info": _period_info(period, city, now),
        "period_info_day": _period_info(periods.get("day"), city, now),
        "period_info_night": _period_info(periods.get("night"), city, now),
        "month": [{
            "user_id": row["user_id"], "name": row["full_name"], "role": row["role"],
            "shifts": row["shifts_count"], "worked_minutes": row["worked_minutes"],
            "actions": row["actions_count"]
        } for row in monthly],
        "kpi": [{
            "user_id": row["user_id"], "name": row["full_name"], "role": row["role"],
            "snapshot_hour": row["snapshot_hour"], "actions": row["actions_count"],
            "worked_minutes": row["worked_minutes"], "efficiency": row["efficiency"]
        } for row in kpi_rows],
    })


async def api_admin_history(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403
        )
    try:
        user_id = int(request.query.get("user_id", ""))
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "fields", "message": "Некорректный сотрудник или лимит."}, status=400
        )
    if user_id <= 0 or limit < 1 or limit > 100 or offset < 0:
        return web.json_response(
            {"error": "fields", "message": "Лимит истории должен быть от 1 до 100."}, status=400
        )

    city_id = city["id"]
    scope = (request.query.get("scope") or "period").strip().lower()
    periods = await city_periods(city_id)
    period_start = min(
        [p.get("started_at") for p in periods.values() if p and p.get("started_at")]
        or ["0000"]
    )
    _pid_a, _pid_b = (list({p.get("id") for p in periods.values() if p})
                      + [None, None])[:2]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        profile = await (await db.execute(
            "SELECT full_name, role FROM users WHERE user_id = ? AND city_id = ?",
            (user_id, city_id),
        )).fetchone()
        latest_shift = await (await db.execute(
            "SELECT full_name, role FROM shifts WHERE user_id = ? AND city_id = ? "
            "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT 1",
            (user_id, city_id),
        )).fetchone()
        if not profile and not latest_shift:
            return web.json_response(
                {"error": "not_found", "message": "Сотрудник в вашем городе не найден."},
                status=404,
            )
        # По умолчанию — только текущая декада; ?scope=all вернёт всю историю.
        if scope == "all":
            rows = await (await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, city_id, limit + 1, offset),
            )).fetchall()
        else:
            # Кандидаты широким запросом, точная сверка с декадой СВОЕЙ
            # группы (день/ночь) — в Python по каждой смене.
            raw_rows = await (await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "AND (period_id IN (?, ?) OR (period_id IS NULL "
                "AND COALESCE(start_at, created_at) >= ?)) "
                "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, city_id, _pid_a, _pid_b, period_start,
                 limit + 1, offset),
            )).fetchall()
            rows = [r for r in raw_rows if _shift_in_period(dict(r), periods, city)]
        has_more = len(rows) > limit
        rows = rows[:limit]
        shift_ids = [row["id"] for row in rows]
        action_rows = []
        if shift_ids:
            placeholders = ",".join("?" for _ in shift_ids)
            action_rows = await (await db.execute(
                f"SELECT shift_id, action_type, bike_codes, quantity FROM actions "
                f"WHERE shift_id IN ({placeholders})",
                shift_ids,
            )).fetchall()

    stats_by_shift = {
        shift_id: {"move": 0, "fix": 0, "repair": 0, "battery": 0,
                   "to_sc": 0, "from_sc": 0}
        for shift_id in shift_ids
    }
    for row in action_rows:
        stats = stats_by_shift.get(row["shift_id"])
        if stats is not None and row["action_type"] in stats:
            stats[row["action_type"]] += _action_units(row)

    items = []
    for raw in rows:
        shift = dict(raw)
        stats = {
            action_type: max(0, count)
            for action_type, count in stats_by_shift.get(shift["id"], {}).items()
        }
        items.append({
            "shift_id": shift["id"],
            "date": _fmt_date(shift.get("start_at") or shift.get("created_at")),
            "start": shift.get("start_time"),
            "end": shift.get("end_time"),
            "start_at": shift.get("start_at"),
            "end_at": shift.get("end_at"),
            "worked_minutes": _shift_worked_min(shift),
            "district": shift.get("district") or "",
            "comment": shift.get("comment") or "",
            "source": shift.get("source") or "bot",
            "role": shift.get("role") or "",
            "actions": stats,
            "actions_total": sum(stats.values()),
        })
    name = ((profile["full_name"] if profile else None)
            or (latest_shift["full_name"] if latest_shift else None)
            or f"Сотрудник #{user_id}")
    role = ((profile["role"] if profile else None)
            or (latest_shift["role"] if latest_shift else None) or "")
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "employee": {"user_id": user_id, "name": name, "role": role},
        "items": items,
        "page": {
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": offset + len(items) if has_more else None,
        },
    })

async def api_admin_force_close(request):
    """Админ принудительно закрывает активную смену сотрудника своего города."""
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401)
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403)
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    sid = body.get("shift_id")
    if not isinstance(sid, int):
        return web.json_response({"error": "shift_id"}, status=400)
    shift = await get_shift_by_id(sid)
    if not shift or shift.get("city_id") != city["id"]:
        return web.json_response({"error": "not_found", "message": "Смена не найдена."}, status=404)
    if not shift.get("is_active"):
        return web.json_response({"ok": True, "already_closed": True})
    now = datetime.now(_city_tz(city))
    try:
        closed_id = await end_shift(shift["user_id"], now.strftime("%H:%M"), "", city["id"], now=now)
    except ValueError as exc:
        return web.json_response({"error": "end_time", "message": str(exc)}, status=400)
    if closed_id:
        await safe_flush_report_update(closed_id)
    logger.info(f"Смена {sid} закрыта админом {context['telegram_user']['id']}.")
    return web.json_response({"ok": True})


async def api_admin_period_new(request):
    """Открывает новую декаду: счётчики админки и заработка стартуют с нуля.

    Смены и суммы из базы не удаляются — меняется только точка отсчёта.
    """
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401)
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403)
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    if body.get("confirm") is not True:
        return web.json_response(
            {"error": "confirm", "message": "Нужно подтверждение."}, status=400)
    segment = "night" if body.get("segment") == "night" else "day"
    try:
        period = await start_new_period(
            city["id"], context["telegram_user"]["id"], segment)
    except ValueError as exc:
        return web.json_response({"error": "city", "message": str(exc)}, status=400)
    return web.json_response({
        "ok": True, "segment": segment, "period": _period_info(period, city)})


async def api_shift_comment(request):
    """Сотрудник редактирует комментарий к своей смене (в т.ч. закрытой)."""
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    sid = body.get("shift_id")
    if not isinstance(sid, int):
        return web.json_response({"error": "shift_id"}, status=400)
    comment = (body.get("comment") or "").strip()
    if len(comment) > 500:
        return web.json_response(
            {"error": "comment", "message": "Комментарий до 500 символов."}, status=400)
    shift = await get_shift_by_id(sid)
    if not shift or shift.get("user_id") != uid:
        return web.json_response({"error": "not_found", "message": "Смена не найдена."}, status=404)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET comment = ? WHERE id = ?", (comment, sid))
        await db.commit()
    # Обновляем сообщение-отчёт в теме (комментарий видно в закрытом отчёте).
    await safe_flush_report_update(sid)
    return web.json_response({"ok": True, "comment": comment})


async def serve_index(request):
    # Отдаём саму страницу мини-приложения с того же адреса, что и API —
    # тогда не нужен ни GitHub Pages, ни CORS.
    if os.path.exists(INDEX_PATH):
        # Без этих заголовков Telegram и браузер держат старую копию страницы,
        # и обновления мини-приложения не доезжают до сотрудников.
        return web.FileResponse(INDEX_PATH, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return web.Response(text="BibiBike API ok")

async def api_health(request):
    return web.json_response({
        "ok": True,
        "service": "bibibike-bot",
        "build_version": BUILD_VERSION,
        "index_html": os.path.exists(INDEX_PATH),
    })

async def start_api_server():
    try:
        app = web.Application(middlewares=[cors_mw])
        app.router.add_get("/api/state", api_state)
        app.router.add_post("/api/settings", api_settings)
        app.router.add_post("/api/shift/start", api_shift_start)
        app.router.add_post("/api/shift/stop", api_shift_stop)
        app.router.add_post("/api/shift/lunch", api_shift_lunch)
        app.router.add_post("/api/shift/delete", api_shift_delete)
        app.router.add_post("/api/shift/comment", api_shift_comment)
        app.router.add_post("/api/action/add", api_action_add)
        app.router.add_get("/api/history", api_history)
        app.router.add_post("/api/admin/login", api_admin_login)
        app.router.add_get("/api/admin/dashboard", api_admin_dashboard)
        app.router.add_get("/api/admin/history", api_admin_history)
        app.router.add_post("/api/admin/force-close", api_admin_force_close)
        app.router.add_post("/api/admin/period/new", api_admin_period_new)
        app.router.add_post("/api/admin/manual/approve", api_admin_manual_approve)
        app.router.add_get("/health", api_health)
        app.router.add_get("/index.html", serve_index)
        app.router.add_get("/", serve_index)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
        await site.start()
        logger.info(f"API мини-приложения слушает 0.0.0.0:{WEBAPP_PORT}")
    except Exception as e:
        # Веб-сервер не критичен для работы бота: если порт занят/закрыт —
        # просто пишем предупреждение, а бот продолжает работать как раньше.
        logger.warning(f"API мини-приложения не запустился ({e}). Бот работает без него.")

# ============================================================
# ЗАПУСК БОТА
# ============================================================
async def main():
    await init_db()
    await rebuild_monthly_aggregates()
    await start_api_server()   # === НОВОЕ: поднимаем API рядом с ботом ===
    kpi_task = asyncio.create_task(kpi_background_worker())
    scheduled_report_task = asyncio.create_task(scheduled_report_status_worker())
    auto_close_task = asyncio.create_task(auto_close_worker())
    dp = Dispatcher()
    dp.include_router(cmd_router)
    dp.include_router(work_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен! (живое сообщение + NPB + роль Чарджер)")
    logger.info(f"Версия сборки: {BUILD_VERSION}")
    for city in CITIES_BY_ID.values():
        logger.info(
            f"Город {city['name']}: группа {city['group_id']}, "
            f"задачи {city['topic_tasks']}, NPB {city['topic_npb']}, "
            f"перемещения {city.get('topic_moves') or '—'}, отчёты {city['topic_reports']}"
        )
    logger.info("=" * 50)

    try:
        await dp.start_polling(bot)
    finally:
        kpi_task.cancel()
        scheduled_report_task.cancel()
        auto_close_task.cancel()
        try:
            await asyncio.gather(kpi_task, scheduled_report_task, auto_close_task)
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"ФАТАЛЬНАЯ ОШИБКА при запуске бота: {e}", flush=True)
        traceback.print_exc()
        raise
