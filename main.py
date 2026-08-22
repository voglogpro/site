# -*- coding: utf-8 -*-
from __future__ import annotations

"""Квест «Бибибайк» — Telegram-бот с мини-приложением.

Единый файл: настройки, база, безопасность, логика квеста, бот и веб-сервер.
Разделы идут в порядке зависимостей и отмечены заголовками — ищи по названию
раздела, если нужно что-то поправить.

Запуск:  python main.py
"""

import aiohttp
import aiosqlite
import asyncio
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import qrcode
import secrets
import signal
import re
import time
import uuid

from aiogram import Bot
from aiogram import Bot, Dispatcher
from aiogram import Bot, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
    TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultVideo, MenuButtonWebApp, Message, WebAppInfo,
)
from aiohttp import web
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse


# ========================================================================
# НАСТРОЙКИ · переменные окружения и конфигурация
# ========================================================================

load_dotenv()


def _ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return frozenset(result)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    webapp_url: str
    admin_ids: frozenset[int]
    qr_secret: str
    web_port: int
    data_dir: Path
    db_name: str
    timezone: str
    init_data_max_age_sec: int
    admin_ticket_ttl_sec: int
    admin_password: str
    admin_session_ttl_sec: int
    session_duration_min: int
    location_stale_sec: int
    location_retention_days: int
    support_url: str
    support_chat_id: str
    map_tile_url: str
    routing_upstream: str
    mapgl_key: str
    mapgl_style: str
    mapgl_style_light: str
    mapgl_style_dark: str
    map_attribution: str
    dev_mode: bool
    dev_user_id: int
    scan_require_geo: bool
    public_link_base: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name

    @property
    def admin_url(self) -> str:
        return f"{self.webapp_url.rstrip('/')}/admin.html"


# CARTO убран намеренно: их тайлы доступны только корпоративным клиентам
# и получателям грантов, для коммерческого проекта это нарушение условий.
DEFAULT_TILE_SOURCES = (
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
)

DEFAULT_MAPGL_LIGHT_STYLE = "c080bb6a-8134-4993-93a1-5b4d8c36a59b"
DEFAULT_MAPGL_DARK_STYLE = "9643e8da-173b-4359-9fee-8a1fe58e68aa"

QUEST_SHARE_VIDEO = "bbbike-quest-invite.mp4"
QUEST_SHARE_TEXT = """Бибибайк КВЕСТ 💚

Добро пожаловать в квест «Бибибайк» 💚

Гуляй по Красной Поляне, отмечайся на локациях, получай подарки от наших партнёров. А за завершённый квест — бесплатная подписка «Бибибайк» на 30 дней для старта на байке.

Здесь всё просто:
1. Выбери любую из трёх точек.
2. Построй маршрут в Яндекс Картах или 2ГИС.
3. На месте отсканируй QR через мини-приложение и забери подарок.

Как только отсканировано 3 уникальных QR-кода — квест считается пройденным. А в подарок — Подписка 30 дней «Бибибайк» 🛵

Во время поездки следи за дорогой, а телефон используй только после полной остановки."""


def quest_share_result(settings: "Settings", bot_username: str, build_version: str) -> InlineQueryResultVideo:
    """Build the native Telegram share payload with the invitation video."""
    base = settings.webapp_url.rstrip("/")
    version = re.sub(r"[^A-Za-z0-9_-]", "", build_version.rsplit(" ", 1)[-1])[:20] or "current"
    open_url = f"https://t.me/{bot_username}?startapp=quest" if bot_username else base
    return InlineQueryResultVideo(
        id=f"bbbike-quest-{version}"[:64],
        video_url=f"{base}/static/{QUEST_SHARE_VIDEO}?v={version}",
        mime_type="video/mp4",
        thumbnail_url=f"{base}/static/bb-bike-logo.jpg?v={version}",
        title="Квест «Бибибайк» · Красная Поляна",
        description="Три точки, подарки партнёров и Подписка 30 дней",
        caption=QUEST_SHARE_TEXT,
        parse_mode=None,
        video_width=1072,
        video_height=1920,
        video_duration=34,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть квест «Бибибайк»", url=open_url)
        ]]),
    )


def _tile_upstreams() -> tuple[str, ...]:
    raw = os.getenv("TILE_UPSTREAMS", "")
    chosen = [item.strip() for item in raw.split(",") if item.strip()]
    if chosen:
        return tuple(chosen)
    # На Android WebView векторный WebGL может быть недоступен даже при
    # исправном ключе. В таком случае тот же серверный proxy сначала пробует
    # официальный 2ГИС Raster Tiles API и лишь затем резервные OSM-тайлы.
    # Если тариф ключа не включает Raster Tiles, ответ будет пропущен.
    sources: list[str] = []
    map_key = os.getenv("MAPGL_KEY", "").strip()
    if map_key:
        sources.append(
            "https://tile0.maps.2gis.com/v2/tiles/online_sd/{z}/{x}/{y}.png"
            f"?key={map_key}"
        )
    sources.extend(DEFAULT_TILE_SOURCES)
    return tuple(sources)


def _tile_sources(_style_tag: str) -> tuple[str, ...]:
    """Список подложек: сначала из MAP_TILE_URLS/MAP_TILE_URL, затем запасные."""
    raw = os.getenv("MAP_TILE_URLS", "")
    chosen = [item.strip() for item in raw.split(",") if item.strip()]
    primary = os.getenv("MAP_TILE_URL", "").strip()
    if primary and primary not in chosen:
        chosen.insert(0, primary)
    # Собственная раздача идёт первой: телефон гарантированно достаёт до
    # сервера квеста, а внешние CDN у части гостей недоступны.
    proxy = f"/tiles/{_style_tag}/{{z}}/{{x}}/{{y}}.png"
    if proxy not in chosen:
        chosen.insert(0, proxy)
    for fallback in DEFAULT_TILE_SOURCES:
        if fallback not in chosen:
            chosen.append(fallback)
    return tuple(chosen)


def load_settings() -> Settings:
    dev_mode = _bool("DEV_MODE")
    token = os.getenv("BOT_TOKEN", "").strip()
    url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    qr_secret = os.getenv("QR_SECRET", "").strip()
    # ADMIN_IDS больше не управляет входом в CRM. Список оставлен только для
    # необязательных служебных команд бота (например, загрузки фото).
    admin_ids = _ids(os.getenv("ADMIN_IDS", ""))
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    dev_user_id = int(os.getenv("DEV_USER_ID", "999000111"))
    if not dev_mode:
        missing = []
        if not token:
            missing.append("BOT_TOKEN")
        if not url.startswith("https://"):
            missing.append("WEBAPP_URL (HTTPS)")
        if len(admin_password) < 10:
            missing.append("ADMIN_PASSWORD (минимум 10 символов)")
        if len(qr_secret) < 32:
            missing.append("QR_SECRET (минимум 32 символа)")
        if missing:
            raise RuntimeError("Не заданы обязательные настройки: " + ", ".join(missing))
    if dev_mode:
        token = token or "000000:development-token"
        url = url or "http://127.0.0.1:3000"
        qr_secret = qr_secret or "local-preview-secret-never-production"
        admin_password = admin_password or "local-admin-password"
        # Раньше здесь было `admin_ids or {dev_user_id}`, но DEFAULT_ADMIN_IDS
        # всегда непустой, поэтому локальный разработчик не мог открыть CRM.
        admin_ids = admin_ids | frozenset({dev_user_id})
    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        bot_token=token,
        webapp_url=url,
        admin_ids=admin_ids,
        qr_secret=qr_secret,
        web_port=int(os.getenv("WEB_PORT", os.getenv("PORT", "3000"))),
        data_dir=data_dir,
        db_name=os.getenv("DB_NAME", "bibibike_quest.db"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
        # Telegram keeps the same initData while an already opened Mini App is
        # running.  Twelve hours keeps a day trip alive without accepting old
        # authentication indefinitely.
        init_data_max_age_sec=int(os.getenv("INIT_DATA_MAX_AGE_SEC", "43200")),
        admin_ticket_ttl_sec=int(os.getenv("ADMIN_TICKET_TTL_SEC", "43200")),
        admin_password=admin_password,
        admin_session_ttl_sec=int(os.getenv("ADMIN_SESSION_TTL_SEC", "43200")),
        session_duration_min=int(os.getenv("SESSION_DURATION_MIN", "240")),
        location_stale_sec=int(os.getenv("LOCATION_STALE_SEC", "300")),
        location_retention_days=int(os.getenv("LOCATION_RETENTION_DAYS", "7")),
        support_url=os.getenv("SUPPORT_URL", "https://t.me/bbbike_support_bot"),
        # Optional duplicate notification. The durable operator request is
        # always created in CRM; this target is only a convenience copy.
        support_chat_id=os.getenv("SUPPORT_CHAT_ID", "").strip(),
        map_tile_url=os.getenv("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
        # Подпись на карте. Указание OpenStreetMap обязательно по лицензии
        # тайлов, но выводится компактно и рядом с брендом.
        map_attribution=os.getenv("MAP_ATTRIBUTION", "Бибибайк · © OpenStreetMap"),
        # Несколько подложек подряд. Один источник — единая точка отказа:
        # если он недоступен из сети гостя (роуминг, VPN, блокировка), карта
        # остаётся пустой. Приложение перебирает список сверху вниз.
        # Первой идёт тёмная подложка — она совпадает с оформлением квеста.
        # Откуда сервер берёт квадраты карты для собственной раздачи.
        # Меняется переменной TILE_UPSTREAMS, если появится свой поставщик
        # карт (например, Яндекс по договору с ключом).
        # Движок построения маршрутов по дорогам. Публичный сервер OSRM
        # рассчитан на пробные нагрузки: при росте трафика стоит поднять
        # свой или подключить платный и указать его здесь.
        routing_upstream=os.getenv("ROUTING_UPSTREAM", "https://routing.openstreetmap.de/routed-bike/route/v1").strip(),
        # TILE_THEME=light вернёт обычную светлую карту без перекраски.
        # Ключ доступа к картам 2ГИС. Пока он пуст, приложение работает на
        # прежней карте — так подключение можно готовить, ничего не ломая.
        mapgl_key=os.getenv("MAPGL_KEY", "").strip(),
        # Идентификатор стиля из редактора 2ГИС. Пустой — стиль по умолчанию.
        mapgl_style=os.getenv("MAPGL_STYLE", "").strip(),
        # Пара опубликованных стилей позволяет менять тему методом
        # setStyleById(), не уничтожая карту, маркеры и построенный маршрут.
        # MAPGL_STYLE оставлен как прежнее имя одного фиксированного стиля.
        mapgl_style_light=os.getenv("MAPGL_STYLE_LIGHT", "").strip() or DEFAULT_MAPGL_LIGHT_STYLE,
        mapgl_style_dark=os.getenv("MAPGL_STYLE_DARK", "").strip() or DEFAULT_MAPGL_DARK_STYLE,
        dev_mode=dev_mode,
        dev_user_id=dev_user_id,
        # Выключено по умолчанию: включай, только если все точки проверены
        # на реальном приёме GPS, иначе внутри каменных зданий люди
        # не смогут поставить штамп.
        scan_require_geo=_bool("SCAN_REQUIRE_GEO"),
        # Адрес, который печатается на табличке. По умолчанию — сам квест.
        # Если поставить сюда https://bb.bike/q, ссылка станет фирменной,
        # а куда она ведёт, можно будет менять уже без перепечатки.
        public_link_base=os.getenv("PUBLIC_LINK_BASE", "").strip().rstrip("/"),
    )

# ========================================================================
# БАЗА ДАННЫХ · схема и подключение
# ========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    city TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused','ended')),
    session_duration_min INTEGER NOT NULL DEFAULT 240 CHECK(session_duration_min BETWEEN 30 AND 1440),
    premium_title TEXT NOT NULL DEFAULT 'Подписка 30 дней',
    premium_instruction TEXT NOT NULL DEFAULT 'Нажми «Получить подписку». Команда Бибибайк проверит квест и подключит бесплатную подписку на 30 дней для старта на байке.',
    starts_at TEXT,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK(seq BETWEEN 1 AND 3),
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    radius_m INTEGER NOT NULL DEFAULT 100 CHECK(radius_m BETWEEN 30 AND 500),
    reward_title TEXT NOT NULL,
    reward_text TEXT NOT NULL,
    partner_hours TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    qr_code_hash TEXT NOT NULL,
    qr_public_hint TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, seq)
);

CREATE TABLE IF NOT EXISTS participants (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL,
    language_code TEXT NOT NULL DEFAULT '',
    privacy_accepted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    user_id INTEGER NOT NULL REFERENCES participants(user_id),
    status TEXT NOT NULL CHECK(status IN ('awaiting_location','active','completed','expired','cancelled')),
    current_seq INTEGER NOT NULL DEFAULT 1 CHECK(current_seq BETWEEN 1 AND 4),
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completed_at TEXT,
    live_chat_id INTEGER,
    live_message_id INTEGER,
    last_location_at TEXT,
    last_latitude REAL,
    last_longitude REAL,
    integrity_status TEXT NOT NULL DEFAULT 'ok' CHECK(integrity_status IN ('ok','warning','review')),
    integrity_note TEXT NOT NULL DEFAULT '',
    UNIQUE(campaign_id, user_id)
);

CREATE TABLE IF NOT EXISTS session_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    point_id INTEGER NOT NULL REFERENCES points(id),
    seq INTEGER NOT NULL,
    point_name TEXT NOT NULL,
    address TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    radius_m INTEGER NOT NULL,
    reward_title TEXT NOT NULL,
    reward_text TEXT NOT NULL,
    partner_hours TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    location_seen_at TEXT,
    min_distance_m REAL,
    qr_seen_at TEXT,
    completed_at TEXT,
    reward_code TEXT,
    reward_redeemed_at TEXT,
    reward_redeem_request_id TEXT,
    UNIQUE(session_id, seq),
    UNIQUE(session_id, point_id)
);

CREATE TABLE IF NOT EXISTS location_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    telegram_update_id INTEGER UNIQUE,
    request_id TEXT UNIQUE,
    chat_id INTEGER,
    message_id INTEGER,
    source TEXT NOT NULL CHECK(source IN ('live','miniapp')),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    accuracy_m REAL,
    distance_m REAL,
    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
    anomaly_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS location_session_time ON location_observations(session_id, observed_at);

CREATE TABLE IF NOT EXISTS qr_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    point_id INTEGER,
    token_fingerprint TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    scanned_at TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
    reject_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS premium_entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
    public_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','issued','cancelled')),
    requested_at TEXT,
    request_id TEXT UNIQUE,
    support_notified_at TEXT,
    support_notification_claim TEXT,
    support_notification_claimed_at TEXT,
    issued_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES participants(user_id) ON DELETE CASCADE,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    participant_code TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    mode_active INTEGER NOT NULL DEFAULT 0 CHECK(mode_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES support_conversations(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK(direction IN ('user','operator','system')),
    kind TEXT NOT NULL DEFAULT 'message' CHECK(kind IN ('message','premium_request')),
    text TEXT NOT NULL,
    source_key TEXT UNIQUE,
    telegram_message_id INTEGER,
    admin_id INTEGER,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS support_conversations_status_time
    ON support_conversations(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS support_messages_conversation_time
    ON support_messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS support_messages_unread
    ON support_messages(conversation_id, direction, read_at);

CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_grants (
    user_id INTEGER PRIMARY KEY,
    granted_by INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_qr_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT 'Основной QR',
    code_hash TEXT NOT NULL,
    manual_code TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    scan_count INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(point_id, manual_code)
);
CREATE INDEX IF NOT EXISTS point_qr_point_active ON point_qr_codes(point_id, active);

CREATE TABLE IF NOT EXISTS quest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('catalog_open','point_view','navigator_open','qr_open')),
    point_id INTEGER REFERENCES points(id) ON DELETE SET NULL,
    navigator TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quest_events_type_time ON quest_events(event_type, created_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=10000")
        await self._db.executescript(SCHEMA)
        # Миграция v2 совместима с уже работающей БД: старые QR становятся
        # первым активным QR новой панели, а ожидающие Live Location сессии
        # сразу продолжают работать в упрощённом сценарии.
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self._db.execute(
            """INSERT OR IGNORE INTO point_qr_codes(point_id,label,code_hash,manual_code,active,created_at,updated_at)
               SELECT id,'Основной QR',qr_code_hash,qr_public_hint,1,?,? FROM points""",
            (now, now),
        )
        await self._db.execute("UPDATE sessions SET status='active' WHERE status='awaiting_location'")
        # Фото локации: показывается участнику в карточке точки,
        # чтобы он понимал, как выглядит место, куда едет.
        # Колонка добавляется в обе таблицы: points — источник, session_points —
        # снимок точки на момент старта квеста. Если пропустить вторую,
        # у уже существующих баз падает старт квеста.
        # description — рассказ о месте в карточке локации: чем оно интересно
        # и зачем туда ехать. Добавляется тем же способом, что и photo_url.
        for table in ("points", "session_points"):
            for column in ("photo_url", "description"):
                try:
                    await self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
                except Exception:
                    pass      # колонка уже есть — это нормально
        try:
            await self._db.execute("ALTER TABLE session_points ADD COLUMN reward_redeemed_at TEXT")
        except Exception:
            pass
        try:
            await self._db.execute("ALTER TABLE session_points ADD COLUMN reward_redeem_request_id TEXT")
        except Exception:
            pass
        # Subscription v3: completion unlocks the reward, while an explicit user
        # action creates the support request. Every ALTER is idempotent for
        # already-running SQLite volumes.
        for column, definition in (
            ("requested_at", "TEXT"),
            ("request_id", "TEXT"),
            ("support_notified_at", "TEXT"),
            ("support_notification_claim", "TEXT"),
            ("support_notification_claimed_at", "TEXT"),
            # Телефон участника: по нему поддержка Бибибайк привязывает
            # подписку. Хранится только у завершивших квест.
            ("phone", "TEXT"),
        ):
            try:
                await self._db.execute(
                    f"ALTER TABLE premium_entitlements ADD COLUMN {column} {definition}"
                )
            except Exception:
                pass
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS premium_request_id_unique "
            "ON premium_entitlements(request_id) WHERE request_id IS NOT NULL"
        )
        await self._db.execute(
            "INSERT INTO schema_meta(key,value) VALUES('version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        await self._db.execute(
            """UPDATE campaigns SET premium_title='Подписка 30 дней'
               WHERE premium_title IN ('Премиум bb.bike на 30 дней','Premium bb.bike на 30 дней','Premium BBBIKE на 30 дней')"""
        )
        await self._db.execute(
            """UPDATE campaigns
               SET premium_instruction='Нажми «Получить подписку». Команда Бибибайк проверит квест и подключит бесплатную подписку на 30 дней для старта на байке.'
               WHERE premium_instruction IN ('Покажи этот экран администратору. Премиум будет оформлен вручную.','Нажми «Получить Premium». Команда BBBIKE проверит квест и подключит подписку на 30 дней.')"""
        )
        await self._db.commit()
        check = await (await self._db.execute("PRAGMA quick_check")).fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {check[0] if check else 'no result'}")

    @property
    def connection(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("Database is not initialized")
        return self._db

    @asynccontextmanager
    async def transaction(self):
        async with self._lock:
            db = self.connection
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                await db.rollback()
                raise
            else:
                await db.commit()

    async def fetchone(self, sql: str, params=()):
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params=()):
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchall()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

# ========================================================================
# БЕЗОПАСНОСТЬ · проверка Telegram, админ-доступ, подписи QR
# ========================================================================

@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    user_id: int
    username: str
    first_name: str
    last_name: str
    language_code: str

    @property
    def display_name(self) -> str:
        value = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return value or self.username or f"Участник {self.user_id}"


def identity_from_message(message: Message) -> TelegramIdentity:
    """Build the same participant identity for bot and Mini App traffic."""
    user = message.from_user
    if not user:
        raise QuestError("telegram_user_required", "Не удалось определить участника.", 400)
    return TelegramIdentity(
        user_id=user.id,
        username=(user.username or "")[:64],
        first_name=(user.first_name or "")[:128],
        last_name=(user.last_name or "")[:128],
        language_code=(user.language_code or "")[:16],
    )


def validate_init_data(raw: str, settings: Settings) -> TelegramIdentity | None:
    if settings.dev_mode and raw == "dev":
        return TelegramIdentity(settings.dev_user_id, "dev_admin", "Кирилл", "", "ru")
    if not raw:
        return None
    try:
        parsed = dict(parse_qsl(raw, keep_blank_values=True))
        received = parsed.pop("hash")
        auth_date = int(parsed.get("auth_date", "0"))
    except (KeyError, TypeError, ValueError):
        return None
    age = int(time.time()) - auth_date
    if auth_date <= 0 or age < -60 or age > settings.init_data_max_age_sec:
        return None
    check = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received):
        return None
    try:
        user = json.loads(parsed["user"])
        return TelegramIdentity(
            user_id=int(user["id"]),
            username=str(user.get("username") or "")[:64],
            first_name=str(user.get("first_name") or "")[:128],
            last_name=str(user.get("last_name") or "")[:128],
            language_code=str(user.get("language_code") or "")[:16],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def request_identity(request: web.Request, settings: Settings) -> TelegramIdentity:
    cached = request.get("telegram_identity")
    if cached:
        return cached
    auth = request.headers.get("Authorization", "")
    raw = auth[4:] if auth.startswith("tma ") else request.headers.get("X-Telegram-Init-Data", "")
    identity = validate_init_data(raw, settings)
    if not identity:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "telegram_auth_required"}), content_type="application/json")
    request["telegram_identity"] = identity
    return identity


# Админы, выданные на ходу через бота. Список живёт в таблице admin_grants,
# а здесь держится его копия в памяти: проверка прав вызывается из обычных
# синхронных функций (тикеты, middleware), где нельзя дождаться запроса к БД.
# Копия обновляется при выдаче и снятии прав и заполняется при старте.
GRANTED_ADMINS: set[int] = set()


def is_admin(user_id: int, settings: Settings) -> bool:
    """Права есть у владельцев из окружения и у выданных через бота."""
    return user_id in settings.admin_ids or user_id in GRANTED_ADMINS


def is_root_admin(user_id: int, settings: Settings) -> bool:
    """Владелец из ADMIN_IDS. Только он раздаёт и снимает доступ.

    Так исключена цепочка повышений: приглашённый администратор не может
    ни назначить себе помощников, ни разжаловать того, кто его пригласил.
    """
    return user_id in settings.admin_ids


ADMIN_COOKIE = "bb_admin"


def create_admin_session(settings: Settings, now: int | None = None) -> str:
    """Короткая подписанная сессия после входа по паролю."""
    expires_at = int(time.time() if now is None else now) + settings.admin_session_ttl_sec
    payload = str(expires_at)
    signature = hmac.new(
        settings.qr_secret.encode(), f"admin-session:{payload}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def validate_admin_session(value: str, settings: Settings, now: int | None = None) -> bool:
    try:
        raw_expires, received = value.split(".", 1)
        expires_at = int(raw_expires)
    except (AttributeError, TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if expires_at < current or expires_at > current + settings.admin_session_ttl_sec + 60:
        return False
    expected = hmac.new(
        settings.qr_secret.encode(), f"admin-session:{expires_at}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def require_admin(request: web.Request, settings: Settings) -> TelegramIdentity:
    if not validate_admin_session(request.cookies.get(ADMIN_COOKIE, ""), settings):
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": "admin_password_required", "message": "Войди по паролю."}),
            content_type="application/json",
        )
    # Служебный actor_id для журнала. Доступ больше не связан с Telegram ID.
    return TelegramIdentity(0, "password-admin", "Администратор", "", "ru")


def create_admin_ticket(user_id: int, settings: Settings, now: int | None = None) -> str:
    """Create a short-lived command launch ticket bound to one Telegram admin."""
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + settings.admin_ticket_ttl_sec
    payload = f"{int(user_id)}.{expires_at}"
    signature = hmac.new(
        settings.qr_secret.encode(), f"admin-launch:{payload}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def validate_admin_ticket(ticket: str, settings: Settings, now: int | None = None) -> int | None:
    if settings.dev_mode and ticket == "dev":
        return settings.dev_user_id
    try:
        raw_user_id, raw_expires, received = ticket.split(".", 2)
        user_id, expires_at = int(raw_user_id), int(raw_expires)
    except (AttributeError, TypeError, ValueError):
        return None
    current = int(time.time() if now is None else now)
    if not is_admin(user_id, settings) or expires_at < current:
        return None
    # Reject implausibly long tickets even if a future configuration is wrong.
    if expires_at > current + settings.admin_ticket_ttl_sec + 60:
        return None
    payload = f"{user_id}.{expires_at}"
    expected = hmac.new(
        settings.qr_secret.encode(), f"admin-launch:{payload}".encode(), hashlib.sha256
    ).hexdigest()
    return user_id if hmac.compare_digest(expected, received) else None


SERVICE_WORKER_JS = r"""/* Service worker квеста Бибибайк.
 *
 * Зачем: в Красной Поляне связь пропадает целыми участками. Без этого файла
 * закрытое и заново открытое мини-приложение не грузится вообще — не открывается
 * даже уже заработанный промокод, который человек показывает на кассе.
 *
 * Правила простые:
 *   • HTML — сначала сеть, при ошибке отдаём сохранённую копию. Так продолжает
 *     работать автообновление версии после деплоя.
 *   • Статика (Leaflet, стили, логотип) — сначала кэш: она не меняется.
 *   • Тайлы карты — отдаём из кэша и тихо обновляем, с ограничением объёма.
 *   • /api/** — только сеть. Прогресс и промокоды не кэшируются здесь,
 *     этим занимается localStorage в самом приложении.
 */
const VERSION = 'bbq-sw-v1';
const SHELL = `${VERSION}-shell`;
const TILES = `${VERSION}-tiles`;
const TILE_LIMIT = 1200;

const PRECACHE = [
  '/',
  '/assets/leaflet-1.9.4.js',
  '/static/vendor/leaflet.css',
  '/static/bb-bike-logo.jpg',
  '/static/bb-bike-scooter-cutout.png',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL)
      .then(cache => cache.addAll(PRECACHE))
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => !key.startsWith(VERSION)).map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

async function trimCache(name, limit) {
  const cache = await caches.open(name);
  const keys = await cache.keys();
  if (keys.length <= limit) return;
  await Promise.all(keys.slice(0, keys.length - limit).map(key => cache.delete(key)));
}

async function networkFirst(request) {
  const cache = await caches.open(SHELL);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request) || await cache.match('/');
    if (cached) return cached;
    throw error;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(SHELL);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok) cache.put(request, response.clone());
  return response;
}

async function tile(request) {
  const cache = await caches.open(TILES);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then(response => {
      if (response && response.ok) {
        cache.put(request, response.clone()).then(() => trimCache(TILES, TILE_LIMIT));
      }
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Прогресс, штампы и админка всегда идут в сеть.
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/admin.html')) return;

  if (request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    event.respondWith(networkFirst(request));
    return;
  }

  if (url.origin === self.location.origin &&
      (url.pathname.startsWith('/static/') || url.pathname.startsWith('/assets/'))) {
    event.respondWith(cacheFirst(request));
    return;
  }

  if (/tile|\.png$/i.test(url.pathname) && url.origin !== self.location.origin) {
    event.respondWith(tile(request));
  }
});
"""


QR_CODE_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


# ── Тёмная перекраска тайлов ──────────────────────────────────────────
# Готовые тёмные подложки (CARTO, Stadia, Mapbox) либо платные, либо
# требуют ключ и запрещены коммерческим проектам без лицензии. Поэтому
# карта собирается из обычных тайлов OpenStreetMap, а тёмный вид сервер
# делает сам.
#
# Почему нельзя обойтись фильтром в браузере: в исходных тайлах фон
# бежевый (241 из 255), а дороги белые (255) — разница всего 5%. Любое
# затемнение сохраняет пропорцию, и дороги сливаются с фоном. Здесь
# отношение переворачивается: считается отклонение от цвета фона, и всё,
# что от него отличается — дороги, дома, подписи — становится СВЕТЛЕЕ
# тёмной подложки. Именно так устроены фирменные тёмные карты.
TILE_PAPER = 241          # бежевый фон OpenStreetMap

# Готовые палитры. Выбирается переменной TILE_PALETTE, менять код не нужно.
#   night     — глубокий графит, спокойные дороги (по умолчанию)
#   graphite  — светлее и контрастнее, город читается лучше
#   forest    — тёплый тёмно-зелёный под фирменный цвет Бибибайк
TILE_PALETTES = {
    "night":    {"bg": (18, 20, 23),  "ink": (186, 194, 205), "ink_max": 150,
                 "gain": 6.0, "water": (18, 34, 52),  "park": (18, 32, 22)},
    "graphite": {"bg": (30, 33, 38),  "ink": (208, 215, 224), "ink_max": 178,
                 "gain": 5.0, "water": (26, 44, 66),  "park": (26, 42, 30)},
    "forest":   {"bg": (14, 22, 17),  "ink": (176, 196, 178), "ink_max": 150,
                 "gain": 6.0, "water": (16, 34, 46),  "park": (20, 40, 26)},
}


def tile_palette(name: str) -> dict:
    return TILE_PALETTES.get(name, TILE_PALETTES["night"])


def tile_style_tag(theme: str, palette_name: str) -> str:
    """Короткий отпечаток оформления карты.

    Он входит в адрес тайла, поэтому при смене палитры телефоны
    подтягивают картинки заново. Без него у тех, кто уже открывал квест,
    неделю оставались бы старые тайлы: часть карты светлая, часть тёмная.
    """
    palette = tile_palette(palette_name)
    raw = f"{theme}:{palette_name}:{palette}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


def mapgl_key_for(settings: Settings) -> str:
    """Вернуть клиентский ключ, не считая обычный state как показ карты.

    Раньше каждое фоновое обновление состояния и открытие CRM уменьшало
    самодельный дневной лимит. Реальный показ учитывает сам 2ГИС при
    создании MapGL, поэтому сериализация ключа не должна его расходовать.
    """
    return settings.mapgl_key


def is_quest_code(value: str) -> bool:
    """Пускаем на редирект только собственные коды.

    Без этой проверки /q/<что угодно> превратился бы в открытый редирект,
    которым удобно прикрывать фишинговые ссылки чужим доменом.
    """
    return bool(value) and len(value) <= 128 and value.startswith("bbq-") and set(value) <= QR_CODE_ALLOWED


def normalize_phone(raw: str) -> str:
    """Привести телефон к виду +7XXXXXXXXXX.

    Люди пишут номер как придётся: со скобками, дефисами, через 8.
    Поддержке нужен один предсказуемый формат, поэтому лишнее убираем,
    а привычную «восьмёрку» переводим в +7.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if not (11 <= len(digits) <= 15):
        return ""
    return "+" + digits


def extract_quest_code(value: str) -> str:
    """Достать код точки из того, что реально приходит со сканера.

    Печатный QR теперь содержит ссылку https://t.me/<бот>?startapp=<код>,
    чтобы её открывала обычная камера телефона. Но в приложение может прийти
    и голый код (старые таблички, ручной ввод), и полная ссылка — сканер
    Telegram отдаёт именно её. Разбираем все варианты одинаково.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "t.me/" in value or value.startswith(("http://", "https://", "tg://")):
        try:
            parsed = urlparse(value)
            query = dict(parse_qsl(parsed.query))
            for key in ("startapp", "start", "tgWebAppStartParam"):
                if query.get(key):
                    return query[key].strip()
            tail = parsed.path.rstrip("/").split("/")[-1]
            return tail.strip()
        except ValueError:
            return value
    return value


def qr_digest(secret: str, raw_code: str) -> str:
    return hmac.new(secret.encode(), raw_code.encode(), hashlib.sha256).hexdigest()

# ========================================================================
# ЛОГИКА КВЕСТА · сессии, точки, штампы, награды
# ========================================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def row_dict(row) -> dict[str, Any] | None:
    return dict(row) if row else None


class QuestError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class QuestService:
    # Рабочий прямоугольник курортного кластера: Красная Поляна,
    # Эсто-Садок и горные курорты. Сервер повторяет ограничение CRM-карты.
    POLYANA_BOUNDS = ((43.60, 40.10), (43.77, 40.37))
    # Запас к радиусу точки: городской GPS ошибается на десятки метров,
    # и без запаса человек стоит у стойки, а штамп не ставится.
    GEO_TOLERANCE_M = 75
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._user_locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, user_id: int) -> asyncio.Lock:
        # Long campaigns can see many unique Telegram ids. Keep active locks,
        # but periodically drop unlocked entries so the dictionary cannot grow
        # forever after one-off visitors.
        if len(self._user_locks) > 2048:
            for stale_id in [key for key, lock in self._user_locks.items() if not lock.locked()][:512]:
                self._user_locks.pop(stale_id, None)
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def ensure_demo_campaign(self) -> None:
        """Create a disabled demo only; never pretend placeholders are real partners."""
        now = iso()
        async with self.db.transaction() as db:
            existing = await (await db.execute("SELECT id FROM campaigns LIMIT 1")).fetchone()
            if existing:
                return
            cursor = await db.execute(
                """INSERT INTO campaigns(slug,title,city,status,session_duration_min,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("polyana-3", "Три места. Три подарка.", "Красная Поляна", "draft", self.settings.session_duration_min, now, now),
            )
            campaign_id = cursor.lastrowid
            demo = [
                (1, "Партнёрская точка 1", "Укажите реальный адрес", 43.6800, 40.2050, "Подарок точки 1"),
                (2, "Партнёрская точка 2", "Укажите реальный адрес", 43.6750, 40.2100, "Подарок точки 2"),
                (3, "Партнёрская точка 3", "Укажите реальный адрес", 43.6700, 40.2150, "Подарок точки 3"),
            ]
            for seq, name, address, lat, lon, reward in demo:
                raw = secrets.token_urlsafe(24)
                digest = qr_digest(self.settings.qr_secret, raw)
                manual = raw[-6:].upper()
                point_cursor = await db.execute(
                    """INSERT INTO points(campaign_id,seq,name,address,latitude,longitude,radius_m,reward_title,reward_text,qr_code_hash,qr_public_hint,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (campaign_id, seq, name, address, lat, lon, 100, reward, "Настройте реальную скидку и условия в админке.", digest, manual, 1, now, now),
                )
                await db.execute(
                    """INSERT INTO point_qr_codes(point_id,label,code_hash,manual_code,active,created_at,updated_at)
                       VALUES(?,'Основной QR',?,?,1,?,?)""",
                    (point_cursor.lastrowid, digest, manual, now, now),
                )

    async def upsert_participant(self, identity: TelegramIdentity, privacy: bool = False) -> None:
        now = iso()
        async with self.db.transaction() as db:
            await db.execute(
                """INSERT INTO participants(user_id,username,display_name,language_code,privacy_accepted_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,
                   language_code=excluded.language_code,privacy_accepted_at=COALESCE(participants.privacy_accepted_at,excluded.privacy_accepted_at),updated_at=excluded.updated_at""",
                (identity.user_id, identity.username, identity.display_name, identity.language_code, now if privacy else None, now, now),
            )

    async def _campaign(self, db=None):
        conn = db or self.db.connection
        return await (await conn.execute("SELECT * FROM campaigns ORDER BY id LIMIT 1")).fetchone()

    async def start(self, identity: TelegramIdentity, privacy_accepted: bool) -> dict:
        if not privacy_accepted:
            raise QuestError("rules_required", "Нужно согласиться с правилами квеста.")
        await self.upsert_participant(identity, True)
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                campaign = await self._campaign(db)
                if not campaign or campaign["status"] != "active":
                    raise QuestError("campaign_inactive", "Квест пока не запущен.", 409)
                existing = await (await db.execute(
                    "SELECT * FROM sessions WHERE campaign_id=? AND user_id=?", (campaign["id"], identity.user_id)
                )).fetchone()
                if existing:
                    await self._resume_expired_in_tx(db, identity.user_id, campaign)
                    return await self._state_in_tx(db, identity.user_id)
                points = await (await db.execute(
                    "SELECT * FROM points WHERE campaign_id=? AND active=1 ORDER BY seq", (campaign["id"],)
                )).fetchall()
                if len(points) != 3:
                    raise QuestError("route_not_ready", "Маршрут ещё готовят.", 409)
                session_id = str(uuid.uuid4())
                started = utcnow()
                expires = started + timedelta(minutes=campaign["session_duration_min"])
                await db.execute(
                    """INSERT INTO sessions(id,campaign_id,user_id,status,current_seq,started_at,expires_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (session_id, campaign["id"], identity.user_id, "active", 1, iso(started), iso(expires)),
                )
                for point in points:
                    await db.execute(
                        """INSERT INTO session_points(session_id,point_id,seq,point_name,address,latitude,longitude,radius_m,reward_title,reward_text,partner_hours,photo_url,description)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (session_id, point["id"], point["seq"], point["name"], point["address"], point["latitude"], point["longitude"], point["radius_m"], point["reward_title"], point["reward_text"], point["partner_hours"],
                         point["photo_url"] if "photo_url" in point.keys() else "",
                         point["description"] if "description" in point.keys() else ""),
                    )
                return await self._state_in_tx(db, identity.user_id)

    async def state(self, identity: TelegramIdentity) -> dict:
        await self.upsert_participant(identity)
        async with self.db.transaction() as db:
            await self._expire_in_tx(db, identity.user_id)
            await self._resume_expired_in_tx(db, identity.user_id)
            return await self._state_in_tx(db, identity.user_id)

    async def _expire_in_tx(self, db, user_id: int) -> None:
        """Дедлайн для участников отключён.

        Раньше сессия сама переходила в 'expired' по истечении времени —
        человек мог не успеть доехать и увидеть «маршрут приостановлен».
        Теперь квест проходится без ограничения по времени: прогресс
        сохраняется столько, сколько идёт сама кампания.
        """
        return

    async def _resume_expired_in_tx(self, db, user_id: int, campaign=None) -> bool:
        """Turn an expired incomplete trip into a resumable pause.

        A session is unique per campaign, so leaving it expired permanently
        would lock the participant out forever.  We keep the same session,
        stamps and rewards and only renew its activity window.
        """
        campaign = campaign or await self._campaign(db)
        if not campaign or campaign["status"] != "active":
            return False
        expires = utcnow() + timedelta(minutes=max(30, int(campaign["session_duration_min"])))
        cursor = await db.execute(
            """UPDATE sessions SET status='active',expires_at=?
               WHERE campaign_id=? AND user_id=? AND status='expired'
                 AND EXISTS (
                    SELECT 1 FROM session_points sp
                    WHERE sp.session_id=sessions.id AND sp.completed_at IS NULL
                 )""",
            (iso(expires), campaign["id"], user_id),
        )
        return cursor.rowcount > 0

    async def _state_in_tx(self, db, user_id: int) -> dict:
        campaign = await self._campaign(db)
        if not campaign:
            return {"campaign": None, "session": None, "points": []}
        session = await (await db.execute(
            "SELECT * FROM sessions WHERE campaign_id=? AND user_id=?", (campaign["id"], user_id)
        )).fetchone()
        if session:
            points = await (await db.execute(
                "SELECT * FROM session_points WHERE session_id=? ORDER BY seq", (session["id"],)
            )).fetchall()
        else:
            points = await (await db.execute(
                "SELECT id point_id,seq,name point_name,address,latitude,longitude,radius_m,reward_title,reward_text,partner_hours,photo_url,description,NULL location_seen_at,NULL qr_seen_at,NULL completed_at,NULL reward_code,NULL reward_redeemed_at FROM points WHERE campaign_id=? AND active=1 ORDER BY seq",
                (campaign["id"],),
            )).fetchall()
        last_age = None
        if session and session["last_location_at"]:
            last_age = max(0, int((utcnow() - parse_dt(session["last_location_at"])).total_seconds()))
        point_data = []
        route_distance_m = 0.0
        previous_point = None
        for point in points:
            item = row_dict(point)
            item.pop("id", None)
            # Сам промокод не входит в обычное состояние приложения. Он
            # возвращается только атомарным запросом выдачи и показывается
            # ровно один раз на устройстве сотруднику заведения.
            item["reward_available"] = bool(
                point["completed_at"] and point["reward_code"] and not point["reward_redeemed_at"]
            )
            item["reward_used"] = bool(point["reward_redeemed_at"])
            item.pop("reward_code", None)
            item.pop("reward_redeem_request_id", None)
            # Расстояние теперь вычисляется только на устройстве пользователя.
            # Координата участника не отправляется и не хранится на сервере.
            item["distance_m"] = None
            item["map_url"] = f"https://yandex.ru/maps/?pt={point['longitude']},{point['latitude']}&z=17&l=map"
            item["yandex_route_url"] = f"https://yandex.ru/maps/?rtext=~{point['latitude']},{point['longitude']}&rtt=bc"
            item["dgis_route_url"] = f"https://2gis.ru/directions/tab/car/points/|{point['longitude']},{point['latitude']}"
            if previous_point:
                route_distance_m += haversine_m(
                    previous_point["latitude"], previous_point["longitude"],
                    point["latitude"], point["longitude"],
                )
            previous_point = point
            point_data.append(item)
        entitlement = None
        if session:
            entitlement = await (await db.execute(
                """SELECT public_code,status,requested_at,support_notified_at,issued_at,phone,
                          (support_notification_claim IS NOT NULL) support_notification_pending
                   FROM premium_entitlements WHERE session_id=?""",
                (session["id"],),
            )).fetchone()
        session_data = row_dict(session)
        if session_data:
            for key in ("live_chat_id", "live_message_id", "last_location_at", "last_latitude", "last_longitude"):
                session_data.pop(key, None)
        return {
            "campaign": {
                "title": campaign["title"], "city": campaign["city"], "status": campaign["status"],
                "premium_title": campaign["premium_title"], "premium_instruction": campaign["premium_instruction"],
                "route_distance_m": round(route_distance_m),
            },
            "session": session_data,
            "points": point_data,
            "last_location_age_sec": last_age,
            "location_stale": False,
            "location_mode": "optional_local_once",
            "scan_requires_geo": self.settings.scan_require_geo,
            "premium": row_dict(entitlement),
            "support_url": self.settings.support_url,
            "map": {
                "tile_url": self.settings.map_tile_url,
                "mapgl_key": mapgl_key_for(self.settings),
                "mapgl_style": self.settings.mapgl_style,
                "mapgl_styles": {
                    "light": self.settings.mapgl_style_light,
                    "dark": self.settings.mapgl_style_dark,
                },
                "attribution": self.settings.map_attribution,
                "bounds": {
                    "south": self.POLYANA_BOUNDS[0][0],
                    "west": self.POLYANA_BOUNDS[0][1],
                    "north": self.POLYANA_BOUNDS[1][0],
                    "east": self.POLYANA_BOUNDS[1][1],
                },
            },
        }

    async def record_location(
        self, user_id: int, latitude: float, longitude: float, accuracy_m: float | None,
        *, source: str, observed_at: datetime | None = None, telegram_update_id: int | None = None,
        request_id: str | None = None, chat_id: int | None = None, message_id: int | None = None,
    ) -> dict:
        if not math.isfinite(latitude) or not -90 <= latitude <= 90 or not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise QuestError("invalid_location", "Некорректные координаты.")
        if accuracy_m is not None and (not math.isfinite(accuracy_m) or accuracy_m < 0):
            accuracy_m = None
        observed_at = observed_at or utcnow()
        request_id = request_id or f"loc-{uuid.uuid4()}"
        async with self.lock_for(user_id):
            async with self.db.transaction() as db:
                await self._expire_in_tx(db, user_id)
                await self._resume_expired_in_tx(db, user_id)
                session = await (await db.execute(
                    "SELECT * FROM sessions WHERE user_id=? ORDER BY started_at DESC LIMIT 1", (user_id,)
                )).fetchone()
                if not session or session["status"] not in ("awaiting_location", "active"):
                    raise QuestError("no_active_session", "Сначала начни квест в приложении.", 409)
                duplicate = await (await db.execute(
                    "SELECT id FROM location_observations WHERE request_id=? OR (? IS NOT NULL AND telegram_update_id=?)",
                    (request_id, telegram_update_id, telegram_update_id),
                )).fetchone()
                if duplicate:
                    return await self._state_in_tx(db, user_id)
                current = await (await db.execute(
                    "SELECT * FROM session_points WHERE session_id=? AND seq=?", (session["id"], session["current_seq"])
                )).fetchone()
                distance = haversine_m(latitude, longitude, current["latitude"], current["longitude"]) if current else None
                anomaly = ""
                accepted = 1
                if accuracy_m is not None and accuracy_m > 150:
                    anomaly = "low_accuracy"
                    accepted = 0
                previous = await (await db.execute(
                    "SELECT observed_at,latitude,longitude,accuracy_m FROM location_observations WHERE session_id=? AND accepted=1 ORDER BY observed_at DESC LIMIT 1", (session["id"],)
                )).fetchone()
                if previous:
                    seconds = max(1.0, (observed_at - parse_dt(previous["observed_at"])).total_seconds())
                    speed = haversine_m(previous["latitude"], previous["longitude"], latitude, longitude) / seconds * 3.6
                    if seconds >= 5 and speed > 90 and (accuracy_m or 999) <= 100 and (previous["accuracy_m"] or 999) <= 100:
                        anomaly = "impossible_speed"
                        accepted = 0
                await db.execute(
                    """INSERT INTO location_observations(session_id,telegram_update_id,request_id,chat_id,message_id,source,observed_at,received_at,latitude,longitude,accuracy_m,distance_m,accepted,anomaly_reason)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (session["id"], telegram_update_id, request_id, chat_id, message_id, source, iso(observed_at), iso(), latitude, longitude, accuracy_m, distance, accepted, anomaly),
                )
                if accepted:
                    await db.execute(
                        """UPDATE sessions SET status='active',last_location_at=?,last_latitude=?,last_longitude=?,
                           live_chat_id=COALESCE(?,live_chat_id),live_message_id=COALESCE(?,live_message_id) WHERE id=?""",
                        (iso(observed_at), latitude, longitude, chat_id, message_id, session["id"]),
                    )
                    if current:
                        await db.execute(
                            "UPDATE session_points SET min_distance_m=CASE WHEN min_distance_m IS NULL OR min_distance_m>? THEN ? ELSE min_distance_m END WHERE id=?",
                            (distance, distance, current["id"]),
                        )
                        if distance <= current["radius_m"]:
                            await db.execute("UPDATE session_points SET location_seen_at=COALESCE(location_seen_at,?) WHERE id=?", (iso(observed_at), current["id"]))
                            completed_now = await self._try_complete_in_tx(db, session["id"], current["seq"])
                        else:
                            completed_now = False
                    else:
                        completed_now = False
                elif anomaly:
                    await db.execute("UPDATE sessions SET integrity_status='warning',integrity_note=? WHERE id=?", (anomaly, session["id"]))
                result = await self._state_in_tx(db, user_id)
                result["event"] = {"point_completed": current["seq"] if accepted and completed_now else None}
                return result

    async def scan(self, identity: TelegramIdentity, qr_code: str, request_id: str, position: tuple[float, float] | None = None) -> dict:
        qr_code = extract_quest_code(qr_code)
        if not qr_code or len(qr_code) > 256 or not request_id or len(request_id) > 80:
            raise QuestError("invalid_qr", "Код не распознан.")
        rejected = False
        completed_seq = None
        already_seq = None
        unknown_code = False
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                await self._expire_in_tx(db, identity.user_id)
                await self._resume_expired_in_tx(db, identity.user_id)
                session = await (await db.execute(
                    "SELECT * FROM sessions WHERE user_id=? ORDER BY started_at DESC LIMIT 1", (identity.user_id,)
                )).fetchone()
                if not session:
                    raise QuestError("no_active_session", "Активный квест не найден.", 409)
                duplicate = await (await db.execute("SELECT id,accepted,reject_reason FROM qr_scans WHERE request_id=?", (request_id,))).fetchone()
                if duplicate:
                    if not duplicate["accepted"]:
                        rejected = True
                    else:
                        return await self._state_in_tx(db, identity.user_id)
                if session["status"] not in ("awaiting_location", "active", "completed") and not duplicate:
                    raise QuestError("no_active_session", "Активный квест не найден.", 409)
                current = None
                if rejected:
                    pass
                else:
                    digest = qr_digest(self.settings.qr_secret, qr_code)
                    candidates = await (await db.execute(
                        """SELECT sp.*,q.id qr_id,q.code_hash,q.manual_code
                           FROM session_points sp
                           JOIN point_qr_codes q ON q.point_id=sp.point_id AND q.active=1
                           WHERE sp.session_id=?
                           ORDER BY sp.seq,q.id""",
                        (session["id"],),
                    )).fetchall()
                    for candidate in candidates:
                        manual_ok = len(qr_code) == 6 and hmac.compare_digest(qr_code.upper(), candidate["manual_code"].upper())
                        if hmac.compare_digest(digest, candidate["code_hash"]) or manual_ok:
                            current = candidate
                            break
                    # Проверка близости выключена по умолчанию: статичный QR у
                    # стойки сам по себе подтверждает визит. Если её включить
                    # (SCAN_REQUIRE_GEO=1), тексты в приложении тоже меняются.
                    if current is not None and self.settings.scan_require_geo:
                        allowed = (current["radius_m"] or 100) + self.GEO_TOLERANCE_M
                        if position is None:
                            raise QuestError("geo_required", "Нажми «Показать, где я» — штамп ставится рядом с точкой.", 409)
                        away = haversine_m(position[0], position[1], current["latitude"], current["longitude"])
                        if away > allowed:
                            raise QuestError(
                                "too_far",
                                f"До «{current['point_name']}» ещё {int(away)} м. Подойди ближе и отсканируй снова.",
                                409,
                            )
                    accepted = current is not None
                    if not accepted:
                        # Код вообще не наш или отключён — это другая ошибка,
                        # чем «код соседней точки», и подсказка должна отличаться.
                        known = await (await db.execute(
                            "SELECT id FROM point_qr_codes WHERE code_hash=? OR upper(manual_code)=?",
                            (digest, qr_code.upper()),
                        )).fetchone()
                        unknown_code = known is None
                    reason = "" if accepted else ("unknown_code" if unknown_code else "wrong_point")
                    await db.execute(
                        "INSERT INTO qr_scans(session_id,point_id,token_fingerprint,request_id,scanned_at,accepted,reject_reason) VALUES(?,?,?,?,?,?,?)",
                        (session["id"], current["point_id"] if current else None, hashlib.sha256(qr_code.encode()).hexdigest()[:12], request_id, iso(), int(accepted), reason),
                    )
                    if not accepted:
                        rejected = True
                    else:
                        await db.execute(
                            "UPDATE point_qr_codes SET scan_count=scan_count+1,last_scanned_at=?,updated_at=? WHERE id=?",
                            (iso(), iso(), current["qr_id"]),
                        )
                        if current["completed_at"]:
                            # Код верный, но точка уже отмечена. Раньше это
                            # возвращалось молча и человек не понимал, засчиталось
                            # ли повторное сканирование.
                            already_seq = current["seq"]
                        else:
                            await db.execute("UPDATE session_points SET qr_seen_at=COALESCE(qr_seen_at,?) WHERE id=?", (iso(), current["id"]))
                        if not current["completed_at"] and await self._try_complete_in_tx(db, session["id"], current["seq"]):
                            completed_seq = current["seq"]
            if rejected:
                if unknown_code:
                    raise QuestError("unknown_qr", "Такого кода нет. Проверь зелёную табличку Бибибайк у стойки.", 409)
                raise QuestError("wrong_qr", "Это код другой точки. Проверь табличку у стойки.", 409)
            async with self.db.transaction() as db:
                result = await self._state_in_tx(db, identity.user_id)
                result["event"] = {"point_completed": completed_seq, "already_completed": already_seq}
                return result

    async def _try_complete_in_tx(self, db, session_id: str, seq: int) -> bool:
        point = await (await db.execute("SELECT * FROM session_points WHERE session_id=? AND seq=?", (session_id, seq))).fetchone()
        if not point or point["completed_at"] or not point["qr_seen_at"]:
            return False
        now = iso()
        reward_code = f"BB-{secrets.token_hex(3).upper()}"
        await db.execute("UPDATE session_points SET completed_at=?,reward_code=? WHERE id=? AND completed_at IS NULL", (now, reward_code, point["id"]))
        progress = await (await db.execute(
            "SELECT COALESCE(SUM(completed_at IS NOT NULL),0) completed,COUNT(*) total FROM session_points WHERE session_id=?",
            (session_id,),
        )).fetchone()
        if progress["completed"] >= progress["total"]:
            premium_code = f"KP-{secrets.token_hex(4).upper()}"
            await db.execute("UPDATE sessions SET status='completed',current_seq=4,completed_at=? WHERE id=?", (now, session_id))
            await db.execute(
                "INSERT OR IGNORE INTO premium_entitlements(session_id,public_code,status,created_at) VALUES(?,?,'pending',?)",
                (session_id, premium_code, now),
            )
        else:
            await db.execute("UPDATE sessions SET status='active',current_seq=? WHERE id=?", (min(3, progress["completed"] + 1), session_id))
        return True

    async def redeem_reward(self, identity: TelegramIdentity, seq: int, request_id: str) -> dict:
        """Одноразово показать промокод уже полученного подарка.

        Транзакционная блокировка не даёт двум быстрым нажатиям или двум
        телефонам получить один код дважды. После ответа обычный state больше
        никогда не содержит секрет, только статус «использован».
        """
        if seq not in (1, 2, 3):
            raise QuestError("bad_reward", "Награда не найдена.", 404)
        request_id = (request_id or "").strip()
        if not (16 <= len(request_id) <= 100) or not re.fullmatch(r"[A-Za-z0-9._:-]+", request_id):
            raise QuestError("bad_request_id", "Не удалось безопасно открыть промокод. Попробуй ещё раз.", 400)
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                point = await (await db.execute(
                    """SELECT sp.* FROM session_points sp
                       JOIN sessions s ON s.id=sp.session_id
                       WHERE s.user_id=? AND sp.seq=?""",
                    (identity.user_id, seq),
                )).fetchone()
                if not point or not point["completed_at"] or not point["reward_code"]:
                    raise QuestError("reward_locked", "Сначала получи штамп этой точки.", 409)
                if point["reward_redeemed_at"]:
                    # Повтор с тем же id означает, что сервер успел отметить
                    # код, но первый HTTP-ответ оборвался. Возвращаем ровно
                    # тот же результат, чтобы пользователь не потерял подарок.
                    if point["reward_redeem_request_id"] != request_id:
                        raise QuestError("reward_used", "Этот промокод уже был показан и отмечен как использованный.", 409)
                    redeemed_at = point["reward_redeemed_at"]
                else:
                    redeemed_at = iso()
                    await db.execute(
                        """UPDATE session_points
                           SET reward_redeemed_at=?,reward_redeem_request_id=?
                           WHERE id=? AND reward_redeemed_at IS NULL""",
                        (redeemed_at, request_id, point["id"]),
                    )
                data = await self._state_in_tx(db, identity.user_id)
                return {
                    "data": data,
                    "reward": {
                        "code": point["reward_code"],
                        "point_name": point["point_name"],
                        "reward_title": point["reward_title"],
                        "redeemed_at": redeemed_at,
                    },
                }

    async def _support_conversation_in_tx(
        self, db, user_id: int, *, session_id: str | None = None,
        participant_code: str = "", phone: str = "", activate: bool = False,
    ) -> str:
        """Create or refresh the participant's single support thread."""
        now = iso()
        existing = await (await db.execute(
            "SELECT id FROM support_conversations WHERE user_id=?", (user_id,)
        )).fetchone()
        if existing:
            await db.execute(
                """UPDATE support_conversations
                   SET session_id=COALESCE(?,session_id),
                       participant_code=CASE WHEN ?<>'' THEN ? ELSE participant_code END,
                       phone=CASE WHEN ?<>'' THEN ? ELSE phone END,
                       status='open',mode_active=CASE WHEN ? THEN 1 ELSE mode_active END,
                       updated_at=?,closed_at=NULL
                   WHERE id=?""",
                (
                    session_id, participant_code, participant_code, phone, phone,
                    1 if activate else 0, now, existing["id"],
                ),
            )
            return str(existing["id"])
        conversation_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO support_conversations(
                   id,user_id,session_id,participant_code,phone,status,mode_active,created_at,updated_at
               ) VALUES(?,?,?,?,?,'open',?,?,?)""",
            (
                conversation_id, user_id, session_id, participant_code, phone,
                1 if activate else 0, now, now,
            ),
        )
        return conversation_id

    async def request_premium(self, identity: TelegramIdentity, request_id: str, phone: str = "") -> dict:
        """Create one explicit and retry-safe subscription request after completion."""
        request_id = (request_id or "").strip()
        phone = normalize_phone(phone)
        if not phone:
            raise QuestError("bad_phone", "Проверь номер телефона — он нужен, чтобы подключить подписку.", 400)
        if not (16 <= len(request_id) <= 100) or not re.fullmatch(r"[A-Za-z0-9._:-]+", request_id):
            raise QuestError("bad_request_id", "Не удалось безопасно отправить заявку. Попробуй ещё раз.", 400)
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                row = await (await db.execute(
                    """SELECT e.*,s.status session_status,p.user_id,p.display_name,p.username
                       FROM premium_entitlements e
                       JOIN sessions s ON s.id=e.session_id
                       JOIN participants p ON p.user_id=s.user_id
                       WHERE s.user_id=? ORDER BY s.started_at DESC LIMIT 1""",
                    (identity.user_id,),
                )).fetchone()
                if not row or row["session_status"] != "completed":
                    raise QuestError("premium_locked", "Сначала заверши все три точки квеста.", 409)
                if row["status"] == "cancelled":
                    raise QuestError("premium_cancelled", "Эта заявка отменена. Напиши в поддержку Бибибайк.", 409)
                # Телефон обновляем и при повторной отправке: человек мог
                # ошибиться в цифре и прислать заявку заново.
                if phone:
                    await db.execute("UPDATE premium_entitlements SET phone=? WHERE id=?", (phone, row["id"]))
                requested_at = row["requested_at"] or iso()
                if not row["requested_at"]:
                    try:
                        await db.execute(
                            "UPDATE premium_entitlements SET requested_at=?,request_id=? WHERE id=? AND requested_at IS NULL",
                            (requested_at, request_id, row["id"]),
                        )
                    except aiosqlite.IntegrityError:
                        raise QuestError("duplicate_request", "Эта заявка уже обработана. Обнови экран.", 409)

                # CRM is the source of truth for support. The deterministic
                # source_key makes browser retries safe and never duplicates
                # the completed-quest request.
                conversation_id = await self._support_conversation_in_tx(
                    db,
                    row["user_id"],
                    session_id=row["session_id"],
                    participant_code=row["public_code"],
                    phone=phone or (row["phone"] if "phone" in row.keys() else "") or "",
                )
                await db.execute(
                    """INSERT OR IGNORE INTO support_messages(
                           conversation_id,direction,kind,text,source_key,created_at
                       ) VALUES(?,'system','premium_request',?,?,?)""",
                    (
                        conversation_id,
                        "Заявка на подписку 30 дней. "
                        f"ID участника: {row['public_code']}. Телефон: "
                        f"{phone or (row['phone'] if 'phone' in row.keys() else '') or 'не указан'}.",
                        f"premium:{row['session_id']}",
                        requested_at,
                    ),
                )

                claim = ""
                if self.settings.support_chat_id and row["status"] == "pending" and not row["support_notified_at"]:
                    stale_before = iso(utcnow() - timedelta(minutes=5))
                    claim = uuid.uuid4().hex
                    cursor = await db.execute(
                        """UPDATE premium_entitlements
                           SET support_notification_claim=?,support_notification_claimed_at=?
                           WHERE id=? AND support_notified_at IS NULL
                             AND (support_notification_claim IS NULL OR support_notification_claimed_at<?)""",
                        (claim, iso(), row["id"], stale_before),
                    )
                    if cursor.rowcount != 1:
                        claim = ""
                data = await self._state_in_tx(db, identity.user_id)
                return {
                    "data": data,
                    "notification_claim": claim,
                    "session_id": row["session_id"],
                    "participant": {
                        "user_id": row["user_id"],
                        "display_name": row["display_name"],
                        "username": row["username"],
                        "public_code": row["public_code"],
                        "requested_at": requested_at,
                        "phone": phone or (row["phone"] if "phone" in row.keys() else "") or "",
                    },
                }

    async def activate_support(self, identity: TelegramIdentity) -> dict:
        """Open the CRM thread and remember that free text belongs to support."""
        await self.upsert_participant(identity)
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                latest = await (await db.execute(
                    """SELECT s.id,e.public_code,e.phone
                       FROM sessions s
                       LEFT JOIN premium_entitlements e ON e.session_id=s.id
                       WHERE s.user_id=? ORDER BY s.started_at DESC LIMIT 1""",
                    (identity.user_id,),
                )).fetchone()
                conversation_id = await self._support_conversation_in_tx(
                    db,
                    identity.user_id,
                    session_id=latest["id"] if latest else None,
                    participant_code=(latest["public_code"] if latest else "") or "",
                    phone=(latest["phone"] if latest else "") or "",
                    activate=True,
                )
                return {"conversation_id": conversation_id}

    async def deactivate_support(self, user_id: int) -> bool:
        async with self.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE support_conversations SET mode_active=0,updated_at=? WHERE user_id=?",
                (iso(), user_id),
            )
            return cursor.rowcount > 0

    async def receive_support_message(
        self, identity: TelegramIdentity, text: str, telegram_message_id: int | None = None,
    ) -> bool:
        """Store a participant message only while support mode is active."""
        clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (text or "").strip())[:3000]
        if not clean:
            return False
        await self.upsert_participant(identity)
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                conversation = await (await db.execute(
                    "SELECT id,mode_active FROM support_conversations WHERE user_id=?",
                    (identity.user_id,),
                )).fetchone()
                if not conversation or not conversation["mode_active"]:
                    return False
                source_key = (
                    f"tg:{identity.user_id}:{telegram_message_id}"
                    if telegram_message_id is not None else f"tg:{identity.user_id}:{uuid.uuid4().hex}"
                )
                await db.execute(
                    """INSERT OR IGNORE INTO support_messages(
                           conversation_id,direction,kind,text,source_key,telegram_message_id,created_at
                       ) VALUES(?,'user','message',?,?,?,?)""",
                    (conversation["id"], clean, source_key, telegram_message_id, iso()),
                )
                await db.execute(
                    """UPDATE support_conversations
                       SET status='open',updated_at=?,closed_at=NULL WHERE id=?""",
                    (iso(), conversation["id"]),
                )
                return True

    async def admin_support_conversations(self) -> list[dict]:
        rows = await self.db.fetchall(
            """SELECT c.id,c.user_id,c.session_id,c.participant_code,c.phone,c.status,
                      c.created_at,c.updated_at,c.closed_at,p.display_name,p.username,
                      (SELECT COUNT(*) FROM support_messages m
                       WHERE m.conversation_id=c.id AND m.direction='user' AND m.read_at IS NULL) unread_count,
                      (SELECT text FROM support_messages m
                       WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) last_message,
                      (SELECT direction FROM support_messages m
                       WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) last_direction
               FROM support_conversations c
               JOIN participants p ON p.user_id=c.user_id
               ORDER BY unread_count DESC,c.updated_at DESC LIMIT 500"""
        )
        return [row_dict(row) for row in rows]

    async def admin_support_detail(self, conversation_id: str) -> dict:
        async with self.db.transaction() as db:
            conversation = await (await db.execute(
                """SELECT c.*,p.display_name,p.username
                   FROM support_conversations c JOIN participants p ON p.user_id=c.user_id
                   WHERE c.id=?""",
                (conversation_id,),
            )).fetchone()
            if not conversation:
                raise QuestError("support_not_found", "Обращение не найдено.", 404)
            await db.execute(
                """UPDATE support_messages SET read_at=COALESCE(read_at,?)
                   WHERE conversation_id=? AND direction='user'""",
                (iso(), conversation_id),
            )
            messages = await (await db.execute(
                """SELECT id,direction,kind,text,created_at,read_at
                   FROM support_messages WHERE conversation_id=? ORDER BY id""",
                (conversation_id,),
            )).fetchall()
            return {
                "conversation": row_dict(conversation),
                "messages": [row_dict(message) for message in messages],
            }

    async def support_reply_target(self, conversation_id: str) -> dict:
        row = await self.db.fetchone(
            """SELECT c.id,c.user_id,c.participant_code,c.phone,p.display_name
               FROM support_conversations c JOIN participants p ON p.user_id=c.user_id
               WHERE c.id=?""",
            (conversation_id,),
        )
        if not row:
            raise QuestError("support_not_found", "Обращение не найдено.", 404)
        return row_dict(row)

    async def record_support_reply(
        self, conversation_id: str, admin_id: int, text: str, telegram_message_id: int | None = None,
    ) -> dict:
        clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (text or "").strip())[:3000]
        if not clean:
            raise QuestError("empty_support_reply", "Напиши ответ участнику.", 400)
        async with self.db.transaction() as db:
            exists = await (await db.execute(
                "SELECT id FROM support_conversations WHERE id=?", (conversation_id,)
            )).fetchone()
            if not exists:
                raise QuestError("support_not_found", "Обращение не найдено.", 404)
            await db.execute(
                """INSERT INTO support_messages(
                       conversation_id,direction,kind,text,telegram_message_id,admin_id,created_at,read_at
                   ) VALUES(?,'operator','message',?,?,?,?,?)""",
                (conversation_id, clean, telegram_message_id, admin_id, iso(), iso()),
            )
            await db.execute(
                """UPDATE support_conversations
                   SET status='open',updated_at=?,closed_at=NULL WHERE id=?""",
                (iso(), conversation_id),
            )
        return await self.admin_support_detail(conversation_id)

    async def set_support_status(self, conversation_id: str, status: str) -> dict:
        if status not in {"open", "closed"}:
            raise QuestError("bad_support_status", "Некорректный статус обращения.", 400)
        async with self.db.transaction() as db:
            cursor = await db.execute(
                """UPDATE support_conversations
                   SET status=?,mode_active=CASE WHEN ?='closed' THEN 0 ELSE mode_active END,
                       closed_at=CASE WHEN ?='closed' THEN ? ELSE NULL END,updated_at=?
                   WHERE id=?""",
                (status, status, status, iso(), iso(), conversation_id),
            )
            if cursor.rowcount != 1:
                raise QuestError("support_not_found", "Обращение не найдено.", 404)
        return await self.admin_support_detail(conversation_id)

    async def finish_premium_notification(self, session_id: str, claim: str, success: bool) -> None:
        """Finish a claimed support delivery without duplicating messages."""
        if not claim:
            return
        async with self.db.transaction() as db:
            if success:
                await db.execute(
                    """UPDATE premium_entitlements
                       SET support_notified_at=?,support_notification_claim=NULL,
                           support_notification_claimed_at=NULL
                       WHERE session_id=? AND support_notification_claim=?""",
                    (iso(), session_id, claim),
                )
            else:
                await db.execute(
                    """UPDATE premium_entitlements
                       SET support_notification_claim=NULL,support_notification_claimed_at=NULL
                       WHERE session_id=? AND support_notification_claim=?""",
                    (session_id, claim),
                )

    async def admin_overview(self) -> dict:
        campaign = row_dict(await self._campaign())
        points = [row_dict(r) for r in await self.db.fetchall("SELECT * FROM points ORDER BY seq")]
        metrics = row_dict(await self.db.fetchone(
            """SELECT COUNT(*) total, SUM(status IN ('awaiting_location','active')) active,
               SUM(status='completed') completed, SUM(status='expired') expired FROM sessions"""
        ))
        funnel = row_dict(await self.db.fetchone(
            """SELECT COUNT(*) started,
               COALESCE((SELECT COUNT(DISTINCT session_id) FROM quest_events WHERE event_type='point_view'),0) point_viewed,
               COALESCE((SELECT COUNT(DISTINCT session_id) FROM quest_events WHERE event_type='navigator_open'),0) navigator_opened,
               COALESCE(SUM((SELECT COUNT(*) FROM session_points sp WHERE sp.session_id=sessions.id AND sp.completed_at IS NOT NULL)>=1),0) reached_point_1,
               COALESCE(SUM((SELECT COUNT(*) FROM session_points sp WHERE sp.session_id=sessions.id AND sp.completed_at IS NOT NULL)>=2),0) reached_point_2,
               COALESCE(SUM(status='completed'),0) reached_point_3
               FROM sessions"""
        ))
        reward_metrics = row_dict(await self.db.fetchone(
            """SELECT COUNT(*) rewards_unlocked,
               COUNT(DISTINCT session_id) rewarded_users
               FROM session_points WHERE completed_at IS NOT NULL"""
        ))
        premium_metrics = row_dict(await self.db.fetchone(
            """SELECT COALESCE(SUM(status='pending' AND requested_at IS NOT NULL),0) premium_pending,
               COALESCE(SUM(status='pending' AND requested_at IS NULL),0) premium_unrequested,
               COALESCE(SUM(status='issued'),0) premium_issued
               FROM premium_entitlements"""
        ))
        funnel.update(reward_metrics or {})
        funnel.update(premium_metrics or {})
        support_metrics = row_dict(await self.db.fetchone(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(status='open'),0) open,
                      COALESCE((SELECT COUNT(*) FROM support_messages
                                WHERE direction='user' AND read_at IS NULL),0) unread
               FROM support_conversations"""
        ))
        recent = [row_dict(r) for r in await self.db.fetchall(
            """SELECT s.id,s.status,s.current_seq,s.started_at,s.completed_at,s.integrity_status,
               (SELECT COUNT(*) FROM session_points sp WHERE sp.session_id=s.id AND sp.completed_at IS NOT NULL) completed_points,
               p.user_id,p.display_name,p.username,e.status premium_status,e.public_code premium_code,
               e.requested_at premium_requested_at,e.support_notified_at premium_support_notified_at,
               e.issued_at premium_issued_at,e.phone premium_phone
               FROM sessions s JOIN participants p ON p.user_id=s.user_id
               LEFT JOIN premium_entitlements e ON e.session_id=s.id
               ORDER BY s.started_at DESC LIMIT 500"""
        )]
        # The operator queue must not lose an old request merely because
        # newer sessions pushed it out of the participant preview.
        premium_requests = [row_dict(r) for r in await self.db.fetchall(
            """SELECT s.id,s.status,s.completed_at,
               p.user_id,p.display_name,p.username,
               e.status premium_status,e.public_code premium_code,
               e.requested_at premium_requested_at,
               e.support_notified_at premium_support_notified_at,
               e.issued_at premium_issued_at,e.phone premium_phone
               FROM premium_entitlements e
               JOIN sessions s ON s.id=e.session_id
               JOIN participants p ON p.user_id=s.user_id
               WHERE e.status='pending' AND e.requested_at IS NOT NULL
               ORDER BY e.requested_at ASC"""
        )]
        qr_codes = [row_dict(r) for r in await self.db.fetchall(
            """SELECT q.id,q.point_id,q.label,q.manual_code,q.active,q.scan_count,q.last_scanned_at,q.created_at,
                      p.seq,p.name point_name
               FROM point_qr_codes q JOIN points p ON p.id=q.point_id ORDER BY p.seq,q.id"""
        )]
        support_conversations = await self.admin_support_conversations()
        return {
            "campaign": campaign, "points": points, "metrics": metrics, "funnel": funnel,
            "recent": recent, "premium_requests": premium_requests, "qr_codes": qr_codes,
            "support_metrics": support_metrics, "support_conversations": support_conversations,
            "map": {"attribution": self.settings.map_attribution, "mapgl_key": mapgl_key_for(self.settings), "mapgl_style": self.settings.mapgl_style, "mapgl_styles": {"light": self.settings.mapgl_style_light, "dark": self.settings.mapgl_style_dark}},
            "map_bounds": {"south": self.POLYANA_BOUNDS[0][0], "west": self.POLYANA_BOUNDS[0][1], "north": self.POLYANA_BOUNDS[1][0], "east": self.POLYANA_BOUNDS[1][1]},
        }

    async def participant_brief(self, user_id: int) -> dict | None:
        row = await self.db.fetchone(
            """SELECT p.user_id,p.display_name,p.username,s.status,s.current_seq,s.started_at,s.completed_at,
               s.last_location_at,s.integrity_status,e.status premium_status,e.public_code premium_code,
               e.requested_at premium_requested_at,e.support_notified_at premium_support_notified_at,
               e.issued_at premium_issued_at
               FROM sessions s JOIN participants p ON p.user_id=s.user_id
               LEFT JOIN premium_entitlements e ON e.session_id=s.id WHERE s.user_id=?
               ORDER BY s.started_at DESC LIMIT 1""", (user_id,)
        )
        return row_dict(row)

    async def admin_update_campaign(self, admin_id: int, payload: dict) -> dict:
        allowed = {"draft", "active", "paused", "ended"}
        status = str(payload.get("status", ""))
        duration = int(payload.get("session_duration_min", self.settings.session_duration_min))
        premium_title = str(payload.get("premium_title", "")).strip()[:160]
        premium_instruction = str(payload.get("premium_instruction", "")).strip()[:500]
        if status not in allowed or not 30 <= duration <= 1440:
            raise QuestError("invalid_campaign", "Проверь статус и длительность.")
        async with self.db.transaction() as db:
            campaign = await self._campaign(db)
            if status == "active":
                points = await (await db.execute("SELECT COUNT(*) count FROM points WHERE campaign_id=? AND active=1", (campaign["id"],))).fetchone()
                if points["count"] != 3:
                    raise QuestError("route_not_ready", "Для запуска нужны ровно три активные точки.", 409)
                demo = await (await db.execute("SELECT COUNT(*) count FROM points WHERE campaign_id=? AND address LIKE 'Укажите реальный%'", (campaign["id"],))).fetchone()
                if demo["count"]:
                    raise QuestError("demo_points", "Сначала замени демо-точки реальными данными.", 409)
            before = json.dumps(row_dict(campaign), ensure_ascii=False)
            await db.execute(
                """UPDATE campaigns SET status=?,session_duration_min=?,
                   premium_title=COALESCE(NULLIF(?,''),premium_title),
                   premium_instruction=COALESCE(NULLIF(?,''),premium_instruction),updated_at=? WHERE id=?""",
                (status, duration, premium_title, premium_instruction, iso(), campaign["id"]),
            )
            await db.execute("INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)", (admin_id, "campaign.update", "campaign", str(campaign["id"]), before, json.dumps(payload, ensure_ascii=False), iso()))
        return await self.admin_overview()

    async def admin_update_point(self, admin_id: int, point_id: int, payload: dict) -> dict:
        try:
            name = str(payload["name"]).strip()[:120]
            address = str(payload["address"]).strip()[:200]
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
            radius = int(payload.get("radius_m", 100))
            reward_title = str(payload["reward_title"]).strip()[:120]
            reward_text = str(payload["reward_text"]).strip()[:300]
            hours = str(payload.get("partner_hours", "")).strip()[:120]
            photo = str(payload.get("photo_url", "")).strip()[:500]
            description = str(payload.get("description", "")).strip()[:600]
        except (KeyError, TypeError, ValueError):
            raise QuestError("invalid_point", "Заполни все обязательные поля.")
        south_west, north_east = self.POLYANA_BOUNDS
        in_polyana = south_west[0] <= latitude <= north_east[0] and south_west[1] <= longitude <= north_east[1]
        if not all((name, address, reward_title, reward_text)) or not in_polyana or not 30 <= radius <= 500:
            raise QuestError("invalid_point", "Проверь поля: координаты должны быть внутри зоны Красной Поляны, радиус — от 30 до 500 м.")
        async with self.db.transaction() as db:
            before = await (await db.execute("SELECT * FROM points WHERE id=?", (point_id,))).fetchone()
            if not before:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            await db.execute(
                """UPDATE points SET name=?,address=?,latitude=?,longitude=?,radius_m=?,reward_title=?,reward_text=?,partner_hours=?,photo_url=?,description=?,updated_at=? WHERE id=?""",
                (name, address, latitude, longitude, radius, reward_title, reward_text, hours,
                 photo, description, iso(), point_id),
            )
            # Keep already opened routes useful: unfinished places receive the
            # corrected address/reward/coordinates, while completed rewards
            # remain an immutable historical snapshot.
            await db.execute(
                """UPDATE session_points
                   SET point_name=?,address=?,latitude=?,longitude=?,radius_m=?,
                       reward_title=?,reward_text=?,partner_hours=?,photo_url=?,description=?
                   WHERE point_id=? AND completed_at IS NULL""",
                (name, address, latitude, longitude, radius, reward_title, reward_text, hours,
                 photo, description, point_id),
            )
            await db.execute("INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)", (admin_id, "point.update", "point", str(point_id), json.dumps(row_dict(before), ensure_ascii=False), json.dumps(payload, ensure_ascii=False), iso()))
        return await self.admin_overview()

    async def delete_qr(self, admin_id: int, qr_id: int) -> dict:
        """Удалить табличку насовсем.

        Отключение оставляет код в базе, и он продолжает занимать место
        в списке. Удаление нужно, когда табличка потеряна или напечатана
        с ошибкой. Сканы в статистике сохраняются — они привязаны к точке.
        """
        async with self.db.transaction() as db:
            row = await (await db.execute("SELECT id,point_id,label FROM point_qr_codes WHERE id=?", (qr_id,))).fetchone()
            if not row:
                raise QuestError("qr_not_found", "Такой таблички уже нет.", 404)
            left = await (await db.execute(
                "SELECT COUNT(*) c FROM point_qr_codes WHERE point_id=? AND id<>?", (row["point_id"], qr_id)
            )).fetchone()
            if not left["c"]:
                raise QuestError("last_qr", "Это последняя табличка точки. Сначала создай новую, иначе точку нельзя будет отметить.")
            await db.execute("DELETE FROM point_qr_codes WHERE id=?", (qr_id,))
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (admin_id, "qr.delete", "qr", str(qr_id), json.dumps(row_dict(row), ensure_ascii=False), "{}", iso()),
            )
        return {"qr_id": qr_id}

    async def load_granted_admins(self) -> None:
        """Поднять список выданных прав из базы в память при старте."""
        rows = await self.db.fetchall("SELECT user_id FROM admin_grants")
        GRANTED_ADMINS.clear()
        GRANTED_ADMINS.update(int(r["user_id"]) for r in rows)

    async def list_admins(self) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT user_id,granted_by,note,created_at FROM admin_grants ORDER BY created_at"
        )
        return [row_dict(r) for r in rows]

    async def grant_admin(self, actor_id: int, user_id: int, note: str = "") -> dict:
        """Выдать участнику доступ в CRM."""
        if user_id in self.settings.admin_ids:
            raise QuestError("already_root", "У этого человека уже есть постоянный доступ владельца.")
        async with self.db.transaction() as db:
            existing = await (await db.execute("SELECT user_id FROM admin_grants WHERE user_id=?", (user_id,))).fetchone()
            if existing:
                raise QuestError("already_admin", "Этот участник уже администратор.")
            await db.execute(
                "INSERT INTO admin_grants(user_id,granted_by,note,created_at) VALUES(?,?,?,?)",
                (user_id, actor_id, note[:200], iso()),
            )
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (actor_id, "admin.grant", "admin", str(user_id), "{}", json.dumps({"note": note}, ensure_ascii=False), iso()),
            )
        GRANTED_ADMINS.add(user_id)
        return {"user_id": user_id}

    async def revoke_admin(self, actor_id: int, user_id: int) -> dict:
        """Снять доступ. Владельцев из ADMIN_IDS снять нельзя — иначе можно
        остаться без единого администратора и потерять вход в CRM."""
        if user_id in self.settings.admin_ids:
            raise QuestError("root_admin", "Это владелец квеста из настроек сервера — доступ снимается только через ADMIN_IDS.")
        async with self.db.transaction() as db:
            existing = await (await db.execute("SELECT user_id FROM admin_grants WHERE user_id=?", (user_id,))).fetchone()
            if not existing:
                raise QuestError("not_admin", "У этого участника и так нет доступа.")
            await db.execute("DELETE FROM admin_grants WHERE user_id=?", (user_id,))
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (actor_id, "admin.revoke", "admin", str(user_id), "{}", "{}", iso()),
            )
        GRANTED_ADMINS.discard(user_id)
        return {"user_id": user_id}

    async def admin_active_points(self) -> list[dict]:
        """Список точек для кнопок выбора при загрузке фото в боте."""
        rows = await self.db.fetchall("SELECT id,seq,name,photo_url FROM points WHERE active=1 ORDER BY seq")
        return [row_dict(r) for r in rows]

    async def admin_set_point_photo(self, admin_id: int, point_id: int, file_id: str) -> dict:
        """Привязать к точке фото, присланное боту.

        Обновляем оба места, где хранится photo_url: у самой точки (для
        будущих участников) и у уже открытых session_points (для тех, кто
        квест уже начал) — так фото видят все, а не только новые сессии.
        """
        photo_url = f"/media/tg/{file_id}"
        async with self.db.transaction() as db:
            row = await (await db.execute("SELECT id,name FROM points WHERE id=?", (point_id,))).fetchone()
            if not row:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            await db.execute("UPDATE points SET photo_url=?,updated_at=? WHERE id=?", (photo_url, iso(), point_id))
            await db.execute(
                "UPDATE session_points SET photo_url=? WHERE point_id=? AND completed_at IS NULL",
                (photo_url, point_id),
            )
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (admin_id, "point.photo", "point", str(point_id), "{}", json.dumps({"photo_url": photo_url}, ensure_ascii=False), iso()),
            )
        return {"point_id": point_id, "name": row["name"], "photo_url": photo_url}

    async def rotate_qr(self, admin_id: int, point_id: int) -> str:
        raw = "bbq-v1-" + secrets.token_urlsafe(28)
        now = iso()
        async with self.db.transaction() as db:
            point = await (await db.execute("SELECT id FROM points WHERE id=?", (point_id,))).fetchone()
            if not point:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            digest = qr_digest(self.settings.qr_secret, raw)
            manual = raw[-6:].upper()
            await db.execute("UPDATE points SET qr_code_hash=?,qr_public_hint=?,updated_at=? WHERE id=?", (digest, manual, now, point_id))
            await db.execute("UPDATE point_qr_codes SET active=0,updated_at=? WHERE point_id=?", (now, point_id))
            await db.execute(
                "INSERT INTO point_qr_codes(point_id,label,code_hash,manual_code,active,created_at,updated_at) VALUES(?,? ,?,?,1,?,?)",
                (point_id, "Основной QR", digest, manual, now, now),
            )
            await db.execute("INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at) VALUES(?,?,?,?,?,?)", (admin_id, "point.qr.rotate", "point", str(point_id), '{"rotated":true}', iso()))
        return raw

    async def create_qr(self, admin_id: int, point_id: int, label: str) -> tuple[str, int]:
        label = label.strip()[:80] or "Дополнительный QR"
        raw = "bbq-v2-" + secrets.token_urlsafe(28)
        now = iso()
        async with self.db.transaction() as db:
            point = await (await db.execute("SELECT id FROM points WHERE id=?", (point_id,))).fetchone()
            if not point:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            cursor = await db.execute(
                """INSERT INTO point_qr_codes(point_id,label,code_hash,manual_code,active,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?)""",
                (point_id, label, qr_digest(self.settings.qr_secret, raw), raw[-6:].upper(), now, now),
            )
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (admin_id, "point.qr.create", "point_qr", str(cursor.lastrowid), json.dumps({"point_id": point_id, "label": label}, ensure_ascii=False), now),
            )
        return raw, int(cursor.lastrowid)

    async def link_external_qr(self, admin_id: int, point_id: int, raw_code: str,
                               label: str) -> int:
        """Привязывает уже существующий чужой QR к точке.

        Сам код нигде не хранится — только его хеш, как и у своих QR.
        Поэтому напечатанную табличку партнёра можно использовать как есть,
        ничего не переклеивая.
        """
        raw_code = (raw_code or "").strip()
        if not raw_code or len(raw_code) > 256:
            raise QuestError("invalid_qr", "Код пустой или слишком длинный.", 400)
        label = label.strip()[:80] or "Партнёрский QR"
        digest = qr_digest(self.settings.qr_secret, raw_code)
        now = iso()
        async with self.db.transaction() as db:
            point = await (await db.execute("SELECT id FROM points WHERE id=?", (point_id,))).fetchone()
            if not point:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            # Один и тот же код не должен вести на две разные точки.
            clash = await (await db.execute(
                "SELECT id, point_id FROM point_qr_codes WHERE code_hash=?", (digest,)
            )).fetchone()
            if clash:
                raise QuestError(
                    "qr_exists",
                    "Этот код уже привязан к точке №{}.".format(clash["point_id"]), 409)
            cursor = await db.execute(
                """INSERT INTO point_qr_codes(point_id,label,code_hash,manual_code,active,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?)""",
                (point_id, label, digest, secrets.token_hex(3).upper(), now, now),
            )
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (admin_id, "point.qr.link_external", "point_qr", str(cursor.lastrowid),
                 json.dumps({"point_id": point_id, "label": label}, ensure_ascii=False), now),
            )
        return int(cursor.lastrowid)

    async def set_qr_active(self, admin_id: int, qr_id: int, active: bool) -> dict:
        async with self.db.transaction() as db:
            qr = await (await db.execute("SELECT * FROM point_qr_codes WHERE id=?", (qr_id,))).fetchone()
            if not qr:
                raise QuestError("qr_not_found", "QR не найден.", 404)
            if not active:
                other = await (await db.execute(
                    "SELECT COUNT(*) count FROM point_qr_codes WHERE point_id=? AND active=1 AND id<>?",
                    (qr["point_id"], qr_id),
                )).fetchone()
                if not other["count"]:
                    raise QuestError("last_qr", "У точки должен остаться хотя бы один активный QR.", 409)
            await db.execute("UPDATE point_qr_codes SET active=?,updated_at=? WHERE id=?", (int(active), iso(), qr_id))
            await db.execute(
                "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (admin_id, "point.qr.status", "point_qr", str(qr_id), json.dumps({"active": bool(active)}), iso()),
            )
        return await self.admin_overview()

    async def record_event(self, identity: TelegramIdentity, event_type: str, request_id: str, point_id: int | None = None, navigator: str = "") -> None:
        if event_type not in {"catalog_open", "point_view", "navigator_open", "qr_open"}:
            raise QuestError("invalid_event", "Неизвестное событие.")
        if len(request_id) > 80 or navigator not in {"", "yandex", "2gis"}:
            raise QuestError("invalid_event", "Некорректное событие.")
        async with self.db.transaction() as db:
            await self._expire_in_tx(db, identity.user_id)
            await self._resume_expired_in_tx(db, identity.user_id)
            session = await (await db.execute(
                "SELECT id FROM sessions WHERE user_id=? AND status='active' ORDER BY started_at DESC LIMIT 1",
                (identity.user_id,),
            )).fetchone()
            if not session:
                return
            if point_id is not None:
                owned = await (await db.execute(
                    "SELECT 1 FROM session_points WHERE session_id=? AND point_id=?",
                    (session["id"], point_id),
                )).fetchone()
                if not owned:
                    raise QuestError("point_not_found", "Точка не найдена.", 404)
            await db.execute(
                "INSERT OR IGNORE INTO quest_events(session_id,user_id,event_type,point_id,navigator,request_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (session["id"], identity.user_id, event_type, point_id, navigator, request_id, iso()),
            )

    async def delete_participant(self, actor_id: int, session_id: str) -> None:
        """Полностью удаляет участника: сессию, штампы и события.

        Нужно для тестирования — после удаления человек снова видит
        заставку и инструкцию, как будто открыл квест впервые.
        """
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT id, user_id FROM sessions WHERE id = ?", (session_id,)
            )).fetchone()
            if not row:
                raise QuestError("session_not_found", "Участник не найден.", 404)
            # Порядок важен: сначала зависимые записи, потом сама сессия.
            await conn.execute("DELETE FROM session_points WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM quest_events WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            logging.getLogger("bibibike.quest").info(
                "Админ %s удалил участника: сессия=%s, telegram_id=%s",
                actor_id, session_id, row["user_id"],
            )

    async def mark_premium_issued(self, admin_id: int, session_id: str) -> dict:
        async with self.db.transaction() as db:
            row = await (await db.execute(
                """SELECT e.id,e.status,e.requested_at,e.public_code,p.user_id,p.display_name
                   FROM premium_entitlements e
                   JOIN sessions s ON s.id=e.session_id
                   JOIN participants p ON p.user_id=s.user_id
                   WHERE e.session_id=?""",
                (session_id,),
            )).fetchone()
            if not row:
                raise QuestError("premium_not_found", "Заявка на подписку не найдена.", 404)
            if not row["requested_at"]:
                raise QuestError("premium_not_requested", "Участник ещё не отправил заявку на подписку.", 409)
            if row["status"] not in {"pending", "issued"}:
                raise QuestError("premium_cancelled", "Заявка отменена и не может быть выдана.", 409)
            newly_issued = row["status"] == "pending"
            if newly_issued:
                await db.execute(
                    "UPDATE premium_entitlements SET status='issued',issued_at=? WHERE session_id=? AND status='pending'",
                    (iso(), session_id),
                )
                await db.execute(
                    "INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at) VALUES(?,?,?,?,?,?)",
                    (admin_id, "premium.issue", "session", session_id, '{"status":"issued"}', iso()),
                )
            return {
                "user_id": row["user_id"], "display_name": row["display_name"],
                "public_code": row["public_code"], "newly_issued": newly_issued,
            }

    async def export_rows(self):
        return await self.db.fetchall(
            """SELECT p.user_id,p.username,p.display_name,s.started_at,s.completed_at,
                      e.requested_at,e.public_code,e.phone,e.status,e.issued_at
               FROM premium_entitlements e JOIN sessions s ON s.id=e.session_id JOIN participants p ON p.user_id=s.user_id
               ORDER BY s.completed_at DESC"""
        )

    async def janitor(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            cutoff = iso(utcnow() - timedelta(days=self.settings.location_retention_days))
            async with self.db.transaction() as db:
                await db.execute(
                    """DELETE FROM location_observations WHERE received_at<? AND session_id IN
                       (SELECT id FROM sessions WHERE status IN ('completed','expired','cancelled'))""", (cutoff,)
                )
                await db.execute(
                    """UPDATE sessions SET last_location_at=NULL,last_latitude=NULL,last_longitude=NULL,
                       live_chat_id=NULL,live_message_id=NULL
                       WHERE status IN ('completed','expired','cancelled') OR last_location_at<?""",
                    (cutoff,),
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                continue

# ========================================================================
# БОТ · команды и обработка сообщений Telegram
# ========================================================================

def quest_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть квест", web_app=WebAppInfo(url=settings.webapp_url))
    ]])


def admin_keyboard(settings: Settings, user_id: int = 0) -> InlineKeyboardMarkup:
    url = settings.admin_url
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть CRM", web_app=WebAppInfo(url=url))
    ]])


async def setup_bot_commands(bot: Bot, settings: Settings) -> None:
    """Best-effort синхронизация оформления, не блокирующая запуск приложения.

    Telegram учитывает даже повторную запись того же имени в flood control. Поэтому
    сначала читаем текущее значение и вызываем setter только при реальном изменении.
    Каждое поле независимо: лимит имени не мешает обновить меню или команды.
    """
    logger = logging.getLogger("bibibike.quest.bot-profile")
    desired_name = "Бибибайк КВЕСТ"
    desired_commands = [
        BotCommand(command="start", description="Открыть квест"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="support", description="Написать в поддержку"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="admin", description="Панель управления"),
        BotCommand(command="participant", description="Статус участника (админ)"),
        BotCommand(command="admins", description="Кто имеет доступ к CRM (админ)"),
    ]
    desired_short = "Квест Бибибайк по трём точкам Красной Поляны"
    desired_description = (
        "Выбирай партнёрские точки в любом порядке, строй маршрут, ставь QR-штампы "
        "и забирай подарки. После трёх точек — Подписка 30 дней."
    )
    desired_menu = MenuButtonWebApp(
        text="Открыть квест", web_app=WebAppInfo(url=settings.webapp_url)
    )

    async def reconcile(label: str, getter, matches, setter) -> None:
        try:
            current = await getter()
            if matches(current):
                logger.debug("Поле %s уже актуально", label)
                return
            await setter()
            logger.info("Обновлено поле Telegram: %s", label)
        except TelegramRetryAfter as exc:
            logger.warning(
                "Telegram ограничил обновление %s; повтор возможен через %s с. "
                "Запуск бота продолжается.", label, exc.retry_after,
            )
        except TelegramUnauthorizedError:
            logger.exception("Telegram отклонил BOT_TOKEN при обновлении %s", label)
            raise
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error("Telegram не принял поле %s: %s", label, exc)
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.warning("Временно не удалось синхронизировать %s: %s", label, exc)
        except TelegramAPIError as exc:
            logger.warning("Telegram API не обновил %s: %s", label, exc)

    await reconcile(
        "name", bot.get_my_name, lambda value: value.name == desired_name,
        lambda: bot.set_my_name(desired_name),
    )
    await reconcile(
        "commands", bot.get_my_commands,
        lambda value: [(item.command, item.description) for item in value]
        == [(item.command, item.description) for item in desired_commands],
        lambda: bot.set_my_commands(desired_commands),
    )
    await reconcile(
        "short_description", bot.get_my_short_description,
        lambda value: value.short_description == desired_short,
        lambda: bot.set_my_short_description(desired_short),
    )
    await reconcile(
        "description", bot.get_my_description,
        lambda value: value.description == desired_description,
        lambda: bot.set_my_description(desired_description),
    )
    await reconcile(
        "menu_button", bot.get_chat_menu_button,
        lambda value: getattr(value, "text", None) == desired_menu.text
        and getattr(getattr(value, "web_app", None), "url", None) == settings.webapp_url,
        lambda: bot.set_chat_menu_button(menu_button=desired_menu),
    )


def build_router(service: QuestService, settings: Settings) -> Router:
    router = Router(name="quest")

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        if message.chat.type != "private" or not message.from_user:
            return
        # Человек пришёл по ссылке с таблички партнёра. Он мог вообще не знать
        # про квест, поэтому сначала коротко объясняем, что это и что он получит.
        payload = (command.args or "").strip()
        if payload == "support":
            await service.activate_support(identity_from_message(message))
            await message.answer(
                "<b>Поддержка Бибибайк</b>\n\n"
                "Напиши одним или несколькими сообщениями, что случилось. "
                "Оператор увидит обращение в CRM и ответит прямо сюда.\n\n"
                "Чтобы закончить диалог, отправь /cancel."
            )
            return
        if payload.startswith("bbq-"):
            await message.answer(
                "<b>Ты нашёл точку квеста Бибибайк</b> 💚\n\n"
                "Это одна из трёх партнёрских точек в Красной Поляне. "
                "Открой приложение — отметка засчитается сама, а подарок партнёра "
                "сохранится в квесте.\n\n"
                "Собери все три штампа и получи бесплатную подписку на 30 дней для старта на байке.",
                reply_markup=quest_keyboard(settings),
            )
            return
        text = (
            "<b>Добро пожаловать в квест Бибибайк</b> 💚\n\n"
            "Гуляй по Красной Поляне, отмечайся на локациях, получай подарки "
            "от наших партнёров. А за завершённый квест — Подписка 30 дней "
            "для бесплатного старта на байке.\n\n"
            "Здесь всё просто:\n"
            "1. Выбери любую из трёх точек.\n"
            "2. Построй маршрут в Яндекс Картах или 2ГИС.\n"
            "3. На месте отсканируй QR через мини-приложение и забери подарок.\n\n"
            "Как только отсканировано 3 уникальных QR-кода — квест считается "
            "пройденным. А в подарок — Подписка 30 дней для бесплатного старта на байке 🛵\n\n"
            "Во время поездки следи за дорогой, а телефон используй только "
            "после полной остановки."
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
            "Поддержка: отправь команду /support — оператор ответит в этом чате."
        )

    @router.message(Command("support"))
    async def support(message: Message):
        if not message.from_user or message.chat.type != "private":
            return
        await service.activate_support(identity_from_message(message))
        await message.answer(
            "<b>Поддержка Бибибайк</b>\n\n"
            "Напиши сообщение — оно сразу появится у оператора в CRM. "
            "Ответ придёт в этот чат.\n\n"
            "Чтобы закончить диалог, отправь /cancel."
        )

    @router.message(Command("cancel"))
    async def cancel_support(message: Message):
        if not message.from_user or message.chat.type != "private":
            return
        stopped = await service.deactivate_support(message.from_user.id)
        await message.answer(
            "Диалог с поддержкой завершён. Открыть его снова можно командой /support."
            if stopped else "Сейчас нет активного диалога с поддержкой."
        )

    @router.message(Command("admin"))
    async def admin(message: Message):
        if not message.from_user:
            return
        await message.answer(
            "<b>CRM квеста</b>\n\nВход защищён паролем из ADMIN_PASSWORD.",
            reply_markup=admin_keyboard(settings, message.from_user.id),
        )

    # Фото между «пришло от админа» и «выбрана точка» живёт здесь: словарь на
    # процесс, ключ — Telegram ID админа. Переживать перезапуск не обязано —
    # это секунды между двумя сообщениями в чате.
    pending_photos: dict[int, str] = {}

    @router.message(F.photo, F.chat.type == "private")
    async def receive_point_photo(message: Message):
        if not message.from_user or not is_admin(message.from_user.id, settings):
            return
        # Самое крупное превью из присланных Telegram размеров — обычно
        # оригинал или максимально близкая к нему копия.
        pending_photos[message.from_user.id] = message.photo[-1].file_id
        points = await service.admin_active_points()
        if not points:
            await message.answer("В квесте пока нет точек — сначала добавь их в CRM.")
            return
        rows = [
            [InlineKeyboardButton(
                text=f"{'📷 ' if pt['photo_url'] else ''}{pt['seq']}. {pt['name']}",
                callback_data=f"photo:{pt['id']}",
            )]
            for pt in points
        ]
        rows.append([InlineKeyboardButton(text="Отмена", callback_data="photo:cancel")])
        await message.answer(
            "Для какой точки это фото?\n<i>📷 — уже есть фото, будет заменено.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("photo:"))
    async def apply_point_photo(callback: CallbackQuery):
        if not callback.from_user or not is_admin(callback.from_user.id, settings):
            return await callback.answer()
        choice = (callback.data or "").split(":", 1)[1]
        if choice == "cancel":
            pending_photos.pop(callback.from_user.id, None)
            await callback.answer("Отменено")
            if callback.message:
                await callback.message.edit_text("Загрузка фото отменена.")
            return
        file_id = pending_photos.pop(callback.from_user.id, None)
        if not file_id:
            await callback.answer("Это фото уже обработано или устарело — пришли его ещё раз", show_alert=True)
            return
        try:
            point_id = int(choice)
            # Файл остаётся в хранилище Telegram, у точки сохраняется только
            # его идентификатор. Раньше фото скачивалось на диск сервера — на
            # хостинге запись не удавалась, и загрузка падала с общей ошибкой.
            # Теперь ни диск, ни права на запись не нужны вовсе.
            result = await service.admin_set_point_photo(callback.from_user.id, point_id, file_id)
        except QuestError as exc:
            await callback.answer(exc.message, show_alert=True)
            return
        except Exception:
            log.exception("Не удалось привязать фото к точке %s", choice)
            await callback.answer("Не получилось сохранить фото. Попробуй ещё раз", show_alert=True)
            return
        await callback.answer("Фото сохранено")
        if callback.message:
            await callback.message.edit_text(f"Готово — фото добавлено к точке «{result['name']}» ✅")

    @router.message(Command("admins"))
    async def admins_list(message: Message):
        if not message.from_user or not is_admin(message.from_user.id, settings):
            return
        granted = await service.list_admins()
        owners = "\n".join(f"• <code>{uid}</code> — владелец" for uid in sorted(settings.admin_ids))
        invited = "\n".join(
            f"• <code>{item['user_id']}</code>{(' — ' + item['note']) if item['note'] else ''}"
            for item in granted
        ) or "• пока никого"
        hint = (
            "\n\nВыдать доступ: <code>/grant ID</code>\nСнять: <code>/revoke ID</code>"
            "\nID участника видно в CRM в списке участников."
            if is_root_admin(message.from_user.id, settings) else ""
        )
        await message.answer(f"<b>Доступ в CRM</b>\n\n{owners}\n\n<b>Приглашённые</b>\n{invited}{hint}")

    @router.message(Command("grant"))
    async def grant(message: Message, command: CommandObject):
        if not message.from_user or not is_admin(message.from_user.id, settings):
            return
        if not is_root_admin(message.from_user.id, settings):
            await message.answer("Приглашать администраторов может только владелец квеста.")
            return
        parts = (command.args or "").strip().split(maxsplit=1)
        try:
            user_id = int(parts[0])
        except (IndexError, ValueError):
            await message.answer("Использование: <code>/grant Telegram_ID Имя</code>\n\nИмя необязательно — оно нужно, чтобы потом помнить, кто это.")
            return
        note = parts[1] if len(parts) > 1 else ""
        try:
            await service.grant_admin(message.from_user.id, user_id, note)
        except QuestError as exc:
            await message.answer(exc.message)
            return
        await message.answer(
            f"Готово — участник <code>{user_id}</code> теперь администратор.\n\n"
            "Пусть отправит боту команду /admin, чтобы открыть CRM."
        )
        # Сообщаем самому человеку: иначе он не узнает, что доступ появился.
        try:
            await bot.send_message(
                user_id,
                "<b>Тебе открыли доступ к CRM квеста Бибибайк</b>\n\n"
                "Панель открывается командой /admin в этом чате. "
                "Ссылка персональная и живёт ограниченное время — "
                "если истечёт, просто вызови команду снова.",
            )
        except Exception:
            await message.answer(
                "Не смог написать ему сам — участник ещё не начинал диалог с ботом. "
                "Передай, что нужно отправить боту /admin."
            )

    @router.message(Command("revoke"))
    async def revoke(message: Message, command: CommandObject):
        if not message.from_user or not is_admin(message.from_user.id, settings):
            return
        if not is_root_admin(message.from_user.id, settings):
            await message.answer("Снимать доступ может только владелец квеста.")
            return
        try:
            user_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Использование: <code>/revoke Telegram_ID</code>")
            return
        try:
            await service.revoke_admin(message.from_user.id, user_id)
        except QuestError as exc:
            await message.answer(exc.message)
            return
        await message.answer(f"Доступ участника <code>{user_id}</code> снят.")

    @router.message(Command("participant"))
    async def participant(message: Message, command: CommandObject):
        if not message.from_user or not is_admin(message.from_user.id, settings):
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
            f"Подписка 30 дней: {item['premium_status'] or 'не назначена'}"
        )

    @router.message(F.text, F.chat.type == "private")
    async def support_text(message: Message):
        if not message.from_user or not message.text or message.text.startswith("/"):
            return
        stored = await service.receive_support_message(
            identity_from_message(message), message.text, message.message_id
        )
        if stored:
            await message.answer(
                "Сообщение отправлено оператору Бибибайк ✅\n"
                "Можно дописать детали следующим сообщением или завершить диалог командой /cancel."
            )

    return router

# ========================================================================
# ВЕБ · HTTP-эндпоинты и отдача мини-приложения
# ========================================================================

log = logging.getLogger("bibibike.quest.api")


def json_response(data, status=200):
    response = web.json_response({"ok": True, **data}, status=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def support_draft_url(base_url: str, text: str) -> str:
    """Return a Telegram-compatible support link with a pre-filled draft."""
    base = (base_url or "").strip()
    if not base:
        return ""
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'text': text})}"


async def json_body(request: web.Request, max_keys=30) -> dict:
    if request.content_type != "application/json":
        raise QuestError("json_required", "Ожидается JSON.", 415)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise QuestError("invalid_json", "Не удалось прочитать запрос.")
    if not isinstance(body, dict) or len(body) > max_keys:
        raise QuestError("invalid_payload", "Некорректный запрос.")
    return body


@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except QuestError as exc:
        return web.json_response({"ok": False, "error": exc.code, "message": exc.message}, status=exc.status)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Ошибка API %s %s", request.method, request.path)
        return web.json_response({"ok": False, "error": "internal_error", "message": "Сервис временно недоступен."}, status=500)


class RateLimiter:
    def __init__(self):
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self._calls = 0

    def allow(self, key: str, limit: int, window: int = 60) -> bool:
        now = time.monotonic()
        self._calls += 1
        if self._calls % 256 == 0 and len(self.events) > 512:
            cutoff = now - 600
            for stale_key, stale_bucket in list(self.events.items()):
                if not stale_bucket or stale_bucket[-1] <= cutoff:
                    self.events.pop(stale_key, None)
        bucket = self.events[key]
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def _csv_safe(value) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def create_web_app(service: QuestService, settings: Settings, bot: Bot, build_version: str) -> web.Application:
    # main.py лежит в корне проекта, поэтому static и index.html — рядом с ним.
    # (В разбитой на модули версии файл был в quest/, отсюда лишний .parent.)
    root = Path(__file__).resolve().parent
    static = root / "static"
    limiter = RateLimiter()
    bot_username_cache = ""

    @web.middleware
    async def security_middleware(request, handler):
        response = await handler(request)
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(self)"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' https://telegram.org; "
            "script-src 'self' 'unsafe-inline' https://telegram.org https://mapgl.2gis.com https://*.2gis.com; "
            # Карты 2ГИС: библиотека, векторные тайлы и шрифты лежат на их домене,
            # рендеринг идёт в отдельном потоке, поэтому нужен blob-worker.
            "worker-src 'self' blob:; "
            "connect-src 'self' https://*.2gis.com https://*.2gis.ru https://*.2gis.cloud; "
            # Подписи на карте берут шрифты с домена 2ГИС. Без этой строки
            # библиотека загружалась, маркеры рисовались, а сама карта
            # оставалась чёрной — именно на этом всё и споткнулось.
            "font-src 'self' data: https://*.2gis.com https://*.2gis.ru; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
            "frame-ancestors 'self' https://web.telegram.org"
        )
        return response

    app = web.Application(middlewares=[error_middleware, security_middleware], client_max_size=256 * 1024)

    async def index(_):
        return web.FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    async def admin_page(_):
        # Страница публична, но данные и действия закрыты HttpOnly-сессией,
        # которую сервер выдаёт только после правильного ADMIN_PASSWORD.
        return web.FileResponse(root / "admin.html", headers={"Cache-Control": "no-cache"})

    async def admin_login(request):
        peer = request.headers.get("X-Forwarded-For", request.remote or "unknown").split(",", 1)[0].strip()
        if not limiter.allow(f"admin-login:{peer}", 8, 300):
            raise QuestError("rate_limited", "Слишком много попыток. Попробуй через пять минут.", 429)
        body = await json_body(request, max_keys=3)
        supplied = str(body.get("password") or "")
        if not hmac.compare_digest(supplied.encode(), settings.admin_password.encode()):
            # Одинаковый ответ не раскрывает, существует ли панель и как
            # именно устроен пароль.
            raise QuestError("bad_password", "Неверный пароль.", 401)
        response = json_response({"authenticated": True})
        response.set_cookie(
            ADMIN_COOKIE,
            create_admin_session(settings),
            max_age=settings.admin_session_ttl_sec,
            httponly=True,
            secure=not settings.dev_mode,
            samesite="Strict",
            path="/",
        )
        return response

    async def admin_logout(_):
        response = json_response({"authenticated": False})
        response.del_cookie(ADMIN_COOKIE, path="/")
        return response

    async def public_info(_):
        nonlocal bot_username_cache
        if not bot_username_cache:
            bot_username_cache = (await bot.get_me()).username or ""
        username = bot_username_cache
        return json_response({
            "bot_username": username,
            "chat_url": f"https://t.me/{username}?start=quest" if username else "",
            "app_url": f"https://t.me/{username}?startapp=quest&mode=fullscreen" if username else "",
            "support_url": f"https://t.me/{username}?start=support" if username else settings.support_url,
        })

    async def privacy(_):
        return web.FileResponse(static / "privacy.html", headers={"Cache-Control": "public, max-age=300"})

    async def health(_):
        campaign = await service.db.fetchone("SELECT status FROM campaigns LIMIT 1")
        # Диагностика карты: сразу видно, включена ли перекраска, доступна ли
        # библиотека и сколько квадратов уже лежит в кэше.
        try:
            import PIL  # noqa: F401
            imaging = True
        except Exception:
            imaging = False
        return json_response({"service": "bibibike-quest", "build_version": build_version, "database": True,
                              "campaign_status": campaign["status"] if campaign else "missing",
                              "map": "2gis"})

    async def ready(_):
        await service.db.fetchone("SELECT 1")
        return json_response({"ready": True})

    async def favicon(_):
        return web.FileResponse(static / "bb-bike-logo.jpg", headers={"Cache-Control": "public, max-age=86400"})

    # Небольшой кэш в памяти: и фото точек, и квадраты карты берутся
    # десятки раз подряд, а класть их на диск нельзя — на хостинге запись
    # недоступна. Старые записи вытесняются, чтобы память не росла.
    photo_cache: dict[str, bytes] = {}
    http_session: list = []

    async def client_session() -> aiohttp.ClientSession:
        if not http_session:
            http_session.append(aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "bb.bike quest (Krasnaya Polyana); +https://bb.bike"},
            ))
        return http_session[0]

    def cache_put(store: dict, key: str, value: bytes, limit: int) -> None:
        store[key] = value
        while len(store) > limit:
            store.pop(next(iter(store)))

    route_cache: dict[str, dict] = {}

    async def build_route(request):
        """Маршрут до точки прямо в приложении.

        Линия строится по дорогам внешним движком, но запрос идёт через наш
        сервер: у гостей под VPN прямой доступ к сторонним сервисам часто
        закрыт, а до квеста телефон достучится всегда. Если движок молчит,
        отдаём прямую линию — это хуже, но лучше пустого экрана.
        """
        identity = request_identity(request, settings)
        if not limiter.allow(f"route:{identity.user_id}", 30):
            raise QuestError("rate_limited", "Слишком часто. Подожди минуту.", 429)
        try:
            from_lat = float(request.query["from_lat"]); from_lon = float(request.query["from_lon"])
            to_lat = float(request.query["to_lat"]); to_lon = float(request.query["to_lon"])
        except (KeyError, ValueError):
            raise QuestError("bad_request", "Не хватает координат.", 400)
        for value in (from_lat, to_lat):
            if not -90 <= value <= 90:
                raise QuestError("bad_request", "Координаты вне диапазона.", 400)
        for value in (from_lon, to_lon):
            if not -180 <= value <= 180:
                raise QuestError("bad_request", "Координаты вне диапазона.", 400)
        # routed-bike использует стандартное имя OSRM-профиля `driving`,
        # хотя граф внутри сервиса велосипедный.
        profile = "driving"
        key = f"{profile}:{from_lat:.4f},{from_lon:.4f}:{to_lat:.4f},{to_lon:.4f}"
        if key in route_cache:
            return json_response(route_cache[key])
        straight = round(haversine_m(from_lat, from_lon, to_lat, to_lon))
        payload = {"ok": True, "fallback": True, "distance_m": straight,
                   "duration_s": int(straight / (4.2 if profile == "cycling" else 1.4)),
                   "points": [[from_lat, from_lon], [to_lat, to_lon]]}
        upstream = settings.routing_upstream
        if upstream:
            url = (upstream.rstrip("/") + f"/{profile}/{from_lon},{from_lat};{to_lon},{to_lat}"
                   "?overview=full&geometries=geojson")
            try:
                session = await client_session()
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json(content_type=None)
                        route = (data.get("routes") or [{}])[0]
                        coords = (route.get("geometry") or {}).get("coordinates") or []
                        if coords:
                            payload = {
                                "ok": True, "fallback": False,
                                "distance_m": int(route.get("distance") or straight),
                                "duration_s": int(route.get("duration") or 0),
                                # GeoJSON отдаёт долготу первой, карта ждёт наоборот.
                                "points": [[point[1], point[0]] for point in coords],
                            }
            except Exception:
                log.info("Движок маршрутов не ответил, отдаём прямую линию")
        if len(route_cache) > 120:
            route_cache.pop(next(iter(route_cache)))
        route_cache[key] = payload
        return json_response(payload)

    async def serve_tg_photo(request):
        """Фото точки, загруженное админом через бота.

        Само изображение хранится в Telegram, у нас лежит только его
        идентификатор. Ссылка на файл живёт около часа, поэтому адрес
        запрашивается заново, а содержимое держится в кэше.
        """
        file_id = request.match_info.get("file_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{8,256}", file_id):
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "public, max-age=86400"}
        cached = photo_cache.get(file_id)
        if cached is not None:
            return web.Response(body=cached, content_type="image/jpeg", headers=headers)
        try:
            tg_file = await bot.get_file(file_id)
            buffer = io.BytesIO()
            await bot.download_file(tg_file.file_path, destination=buffer)
            data = buffer.getvalue()
        except Exception:
            log.warning("Не удалось получить фото %s из Telegram", file_id[:12])
            raise web.HTTPNotFound()
        cache_put(photo_cache, file_id, data, 40)
        return web.Response(body=data, content_type="image/jpeg", headers=headers)

    async def serve_media(request):
        """Фото локаций, загруженные через бота.

        Файлы лежат в data_dir/media — том же volume, что и база, поэтому
        переживают перезапуск и передеплой контейнера. Путь из URL сверяется
        с реальным расположением на диске: без этого запрос вида
        /media/../../../etc/passwd мог бы читать файлы вне папки.
        """
        rel = request.match_info.get("path", "")
        media_root = (settings.data_dir / "media").resolve()
        target = (media_root / rel).resolve()
        if media_root not in target.parents and target != media_root:
            raise web.HTTPNotFound()
        if not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def qr_redirect(request):
        """То, что открывается при скане таблички обычной камерой.

        Телефон с Telegram уходит прямо в мини-приложение и получает штамп.
        Всем остальным (десктоп, браузер без Telegram) показываем короткую
        страницу с объяснением — человек мог отсканировать табличку случайно,
        и упереться в пустую ошибку он не должен.
        """
        raw = request.match_info.get("code", "")
        if not is_quest_code(raw):
            raise web.HTTPFound(location="/")
        target = await telegram_link(raw)
        if not target:
            raise web.HTTPFound(location="/")
        agent = request.headers.get("User-Agent", "").lower()
        wants_page = request.query.get("web") == "1" or not any(
            marker in agent for marker in ("android", "iphone", "ipad", "mobile")
        )
        if not wants_page:
            raise web.HTTPFound(location=target)
        safe_target = target.replace("&", "&amp;").replace('"', "&quot;")
        page = (
            "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Квест Бибибайк · Красная Поляна</title>"
            "<style>body{margin:0;min-height:100vh;display:grid;place-content:center;justify-items:center;"
            "gap:18px;padding:28px;background:#07110b;color:#f2f7f1;text-align:center;"
            "font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}"
            "h1{margin:0;font-size:26px;line-height:1.15;letter-spacing:-.02em}"
            "p{margin:0;max-width:340px;color:#a9bbad}"
            "a{margin-top:6px;padding:15px 26px;border-radius:14px;background:#8fe300;color:#07110b;"
            "font-weight:800;text-decoration:none}</style></head><body>"
            "<h1>Ты нашёл точку квеста Бибибайк</h1>"
            "<p>Это одна из трёх партнёрских точек в Красной Поляне. Открой квест в Telegram — "
            "отметка засчитается сама, а подарок партнёра сохранится в квесте.</p>"
            f'<a href="{safe_target}">Открыть в Telegram</a>'
            "</body></html>"
        )
        return web.Response(text=page, content_type="text/html")

    async def service_worker(_):
        # Исходник лежит прямо в этом файле, чтобы обновление проекта сводилось
        # к замене index.html и main.py. Отдаётся из корня: иначе область
        # действия ограничится /static/ и не покроет саму страницу квеста.
        safe_build = re.sub(r"[^A-Za-z0-9_.-]", "-", build_version or "dev")[:40]
        return web.Response(
            text=SERVICE_WORKER_JS.replace("bbq-sw-v1", f"bbq-sw-{safe_build}"),
            headers={
                "Content-Type": "application/javascript; charset=utf-8",
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )

    async def leaflet_asset(_):
        # The browser bundle deliberately uses a non-.js file in the repository:
        # BotHost has previously tried to execute vendor .js files during deploy.
        # This explicit same-origin route serves it with the correct browser MIME type.
        return web.FileResponse(
            static / "vendor" / "leaflet-1.9.4.asset",
            headers={
                "Content-Type": "application/javascript; charset=utf-8",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    async def state(request):
        nonlocal bot_username_cache
        identity = request_identity(request, settings)
        if not limiter.allow(f"state:{identity.user_id}", 60):
            raise QuestError("rate_limited", "Слишком много запросов.", 429)
        data = await service.state(identity)
        if not bot_username_cache:
            bot_username_cache = (await bot.get_me()).username or ""
        return json_response({
            "data": data,
            "is_admin": is_admin(identity.user_id, settings),
            "bot_username": bot_username_cache,
            "build_version": build_version,
        })

    async def start(request):
        identity = request_identity(request, settings)
        if not limiter.allow(f"start:{identity.user_id}", 5):
            raise QuestError("rate_limited", "Попробуй через минуту.", 429)
        body = await json_body(request)
        data = await service.start(identity, bool(body.get("privacy_accepted")))
        return json_response({"data": data}, 201)

    async def event(request):
        identity = request_identity(request, settings)
        if not limiter.allow(f"event:{identity.user_id}", 90):
            raise QuestError("rate_limited", "Слишком много запросов.", 429)
        body = await json_body(request)
        try:
            point_id = int(body["point_id"]) if body.get("point_id") is not None else None
        except (TypeError, ValueError):
            raise QuestError("invalid_event", "Некорректное событие.")
        await service.record_event(
            identity,
            str(body.get("event_type") or ""),
            str(body.get("request_id") or uuid.uuid4()),
            point_id,
            str(body.get("navigator") or ""),
        )
        return json_response({})

    async def scan(request):
        identity = request_identity(request, settings)
        if not limiter.allow(f"scan:{identity.user_id}", 10):
            raise QuestError("rate_limited", "Слишком много попыток. Подожди минуту.", 429)
        body = await json_body(request)
        # Разовая координата приходит только когда включена проверка близости
        # и человек уже нажал «Показать, где я». Она не сохраняется в базе.
        position = None
        if body.get("latitude") is not None and body.get("longitude") is not None:
            try:
                position = (float(body["latitude"]), float(body["longitude"]))
            except (TypeError, ValueError):
                position = None
        data = await service.scan(identity, str(body.get("qr_code") or ""), str(body.get("request_id") or uuid.uuid4()), position=position)
        completed = data.get("event", {}).get("point_completed")
        if completed:
            completed_count = len([point for point in data.get("points", []) if point.get("completed_at")])
            if completed_count >= 3:
                text = "<b>Маршрут пройден</b>\n\nВсе три точки подтверждены. Финальная награда уже в приложении."
            else:
                text = f"<b>Точка {completed} подтверждена</b>\n\nПодарок сохранён. Пройдено {completed_count} из 3 — следующую точку можно выбрать самому."
            try:
                await bot.send_message(identity.user_id, text)
            except Exception as exc:
                log.warning("Не удалось отправить уведомление user=%s: %s", identity.user_id, type(exc).__name__)
        return json_response({"data": data})

    async def redeem_reward(request):
        identity = request_identity(request, settings)
        if not limiter.allow(f"redeem:{identity.user_id}", 8):
            raise QuestError("rate_limited", "Подожди минуту и попробуй снова.", 429)
        body = await json_body(request, max_keys=2)
        result = await service.redeem_reward(
            identity, int(request.match_info["seq"]), str(body.get("request_id", ""))
        )
        return json_response(result)

    async def request_premium(request):
        identity = request_identity(request, settings)
        if not limiter.allow(f"premium-request:{identity.user_id}", 8):
            raise QuestError("rate_limited", "Подожди минуту и попробуй снова.", 429)
        body = await json_body(request, max_keys=3)
        phone = normalize_phone(str(body.get("phone", "")))
        if not phone:
            raise QuestError("bad_phone", "Проверь номер телефона — он нужен, чтобы подключить подписку.", 400)
        result = await service.request_premium(identity, str(body.get("request_id", "")), phone)
        participant = result.pop("participant")
        claim = result.pop("notification_claim")
        session_id = result.pop("session_id")
        draft = (
            "Здравствуйте! Я завершил квест Бибибайк в Красной Поляне и хочу получить "
            f"подписку на 30 дней. ID участника: {participant['public_code']}. "
            f"Телефон для подписки: {participant.get('phone') or 'не указан'}."
        )
        notified = bool(result["data"].get("premium", {}).get("support_notified_at"))
        if claim:
            target: str | int = settings.support_chat_id
            if re.fullmatch(r"-?\d+", settings.support_chat_id):
                target = int(settings.support_chat_id)
            username = f"@{participant['username']}" if participant["username"] else "без username"
            message = (
                "<b>Новая заявка Бибибайк · Подписка 30 дней</b>\n\n"
                f"Участник: {html.escape(participant['display_name'])}\n"
                f"Telegram: {html.escape(username)} · <code>{participant['user_id']}</code>\n"
                f"ID участника: <code>{html.escape(participant['public_code'])}</code>\n"
                f"Телефон: <code>{html.escape(participant.get('phone') or 'не указан')}</code>\n"
                f"Завершение подтверждено · заявка {html.escape(participant['requested_at'])}"
            )
            try:
                await bot.send_message(target, message)
                notified = True
            except Exception as exc:
                log.warning("Не удалось отправить заявку на подписку session=%s: %s", session_id, type(exc).__name__)
                notified = False
            await service.finish_premium_notification(session_id, claim, notified)
            result["data"] = await service.state(identity)
        support_pending = bool(result["data"].get("premium", {}).get("support_notification_pending"))
        return json_response({
            **result,
            "support_notified": notified,
            "support_pending": support_pending,
            "support_url": "" if notified or support_pending else support_draft_url(settings.support_url, draft),
        })

    async def share_invite(request):
        nonlocal bot_username_cache
        identity = request_identity(request, settings)
        if not limiter.allow(f"share-invite:{identity.user_id}", 10):
            raise QuestError("rate_limited", "Слишком много попыток. Подожди минуту.", 429)
        if not bot_username_cache:
            bot_username_cache = (await bot.get_me()).username or ""
        try:
            prepared = await bot.save_prepared_inline_message(
                identity.user_id,
                quest_share_result(settings, bot_username_cache, build_version),
                allow_user_chats=True,
                allow_bot_chats=False,
                allow_group_chats=True,
                allow_channel_chats=False,
            )
        except Exception as exc:
            log.warning(
                "Не удалось подготовить приглашение user=%s: %s",
                identity.user_id, type(exc).__name__,
            )
            raise QuestError(
                "share_unavailable",
                "Не удалось прикрепить видео. Проверь интернет и попробуй ещё раз.",
                502,
            )
        return json_response({"prepared_message_id": prepared.id})

    async def admin_overview(request):
        require_admin(request, settings)
        return json_response({"data": await service.admin_overview()})

    async def admin_support_detail(request):
        require_admin(request, settings)
        return json_response({
            "data": await service.admin_support_detail(request.match_info["conversation_id"])
        })

    async def admin_support_reply(request):
        admin = require_admin(request, settings)
        body = await json_body(request, max_keys=2)
        reply = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(body.get("text") or "").strip()
        )[:3000]
        if not reply:
            raise QuestError("empty_support_reply", "Напиши ответ участнику.", 400)
        conversation_id = request.match_info["conversation_id"]
        target = await service.support_reply_target(conversation_id)
        try:
            sent = await bot.send_message(
                target["user_id"],
                "<b>Ответ поддержки Бибибайк</b>\n\n" + html.escape(reply),
            )
        except Exception as exc:
            log.warning(
                "Не удалось ответить в поддержку conversation=%s: %s",
                conversation_id, type(exc).__name__,
            )
            raise QuestError(
                "support_delivery_failed",
                "Telegram не принял сообщение. Участник мог заблокировать бота — попробуй позже.",
                502,
            )
        detail = await service.record_support_reply(
            conversation_id, admin.user_id, reply, getattr(sent, "message_id", None)
        )
        return json_response({"data": detail})

    async def admin_support_status(request):
        require_admin(request, settings)
        body = await json_body(request, max_keys=2)
        return json_response({
            "data": await service.set_support_status(
                request.match_info["conversation_id"], str(body.get("status") or "")
            )
        })

    async def admin_campaign(request):
        admin = require_admin(request, settings)
        return json_response({"data": await service.admin_update_campaign(admin.user_id, await json_body(request))})

    async def admin_point(request):
        admin = require_admin(request, settings)
        return json_response({"data": await service.admin_update_point(admin.user_id, int(request.match_info["point_id"]), await json_body(request))})

    async def telegram_link(raw: str) -> str:
        """Прямая ссылка в мини-приложение."""
        nonlocal bot_username_cache
        if not bot_username_cache:
            bot_username_cache = (await bot.get_me()).username or ""
        if not bot_username_cache:
            return ""
        return f"https://t.me/{bot_username_cache}?startapp={raw}"

    async def quest_deep_link(raw: str) -> str:
        """Ссылка, которая печатается на табличке.

        Печатаем не прямую ссылку на Telegram, а собственный адрес вида
        <домен>/q/<код>. Он навсегда закреплён за конкретной точкой, а куда
        именно вести человека, решает сервер в момент скана. Поэтому смена
        имени бота, перенос на другой домен или временная страница акции
        больше никогда не потребуют перепечатывать таблички.
        """
        base = settings.public_link_base or settings.webapp_url.rstrip("/")
        if base:
            return f"{base}/q/{raw}"
        return await telegram_link(raw) or raw

    def qr_png(payload: str) -> str:
        # M даёт запас на потёртости и блики: табличка живёт на улице.
        code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=3)
        code.add_data(payload)
        code.make(fit=True)
        stream = io.BytesIO()
        code.make_image(fill_color="black", back_color="white").save(stream, format="PNG")
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")

    async def admin_rotate_qr(request):
        admin = require_admin(request, settings)
        point_id = int(request.match_info["point_id"])
        raw = await service.rotate_qr(admin.user_id, point_id)
        link = await quest_deep_link(raw)
        return json_response({"qr_code": raw, "qr_link": link, "manual_code": raw[-6:].upper(), "qr_png": qr_png(link), "point_id": point_id})

    async def admin_create_qr(request):
        admin = require_admin(request, settings)
        point_id = int(request.match_info["point_id"])
        body = await json_body(request)
        raw, qr_id = await service.create_qr(admin.user_id, point_id, str(body.get("label") or ""))
        link = await quest_deep_link(raw)
        return json_response({"qr_id": qr_id, "qr_code": raw, "qr_link": link, "manual_code": raw[-6:].upper(), "qr_png": qr_png(link), "point_id": point_id})

    async def admin_qr_status(request):
        admin = require_admin(request, settings)
        body = await json_body(request)
        data = await service.set_qr_active(admin.user_id, int(request.match_info["qr_id"]), bool(body.get("active")))
        return json_response({"data": data})

    async def admin_delete_qr(request):
        admin = require_admin(request, settings)
        await service.delete_qr(admin.user_id, int(request.match_info["qr_id"]))
        return json_response({"data": await service.admin_overview()})

    async def admin_list_admins(request):
        require_admin(request, settings)
        return json_response({
            "owners": sorted(settings.admin_ids),
            "granted": await service.list_admins(),
        })

    async def admin_grant(request):
        admin = require_admin(request, settings)
        if not is_root_admin(admin.user_id, settings):
            raise QuestError("root_only", "Приглашать администраторов может только владелец квеста.", 403)
        body = await json_body(request)
        try:
            user_id = int(str(body.get("user_id") or "").strip())
        except ValueError:
            raise QuestError("bad_id", "Telegram ID — это число. Его видно в списке участников.", 400)
        await service.grant_admin(admin.user_id, user_id, str(body.get("note") or ""))
        # Человек может не знать, что доступ открыли, поэтому пишем ему сами.
        try:
            await bot.send_message(
                user_id,
                "<b>Тебе открыли доступ к CRM квеста Бибибайк</b>\n\n"
                "Панель открывается командой /admin в этом чате.",
            )
            notified = True
        except Exception:
            notified = False
        return json_response({"user_id": user_id, "notified": notified})

    async def admin_revoke(request):
        admin = require_admin(request, settings)
        if not is_root_admin(admin.user_id, settings):
            raise QuestError("root_only", "Снимать доступ может только владелец квеста.", 403)
        await service.revoke_admin(admin.user_id, int(request.match_info["user_id"]))
        return json_response({"ok": True})

    async def admin_premium(request):
        admin = require_admin(request, settings)
        item = await service.mark_premium_issued(admin.user_id, request.match_info["session_id"])
        notified = False
        if item["newly_issued"]:
            try:
                await bot.send_message(
                    item["user_id"],
                    "<b>Подписка 30 дней подтверждена</b>\n\n"
                    f"Заявка <code>{html.escape(item['public_code'])}</code> обработана. "
                    "Если подписка не появилась, напиши в поддержку Бибибайк.",
                )
                notified = True
            except Exception as exc:
                log.warning("Не удалось уведомить о подписке user=%s: %s", item["user_id"], type(exc).__name__)
        return json_response({"data": await service.admin_overview(), "notified": notified})

    async def admin_link_qr(request):
        admin = require_admin(request, settings)
        payload = await request.json()
        await service.link_external_qr(
            admin.user_id,
            int(request.match_info["point_id"]),
            str(payload.get("code") or ""),
            str(payload.get("label") or ""),
        )
        return json_response({"data": await service.admin_overview()})

    async def admin_delete_participant(request):
        admin = require_admin(request, settings)
        await service.delete_participant(admin.user_id, request.match_info["session_id"])
        return json_response({"data": await service.admin_overview()})

    async def admin_export(request):
        require_admin(request, settings)
        rows = await service.export_rows()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Telegram ID", "Username", "Имя", "Старт", "Завершение",
            "Заявка на подписку 30 дней", "ID участника", "Телефон", "Статус", "Выдано",
        ])
        for row in rows:
            writer.writerow([_csv_safe(row[key]) for key in row.keys()])
        body = "\ufeff" + output.getvalue()
        return web.Response(body=body.encode("utf-8"), content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="bibibike-subscription-30-days.csv"', "Cache-Control": "no-store"})

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/admin.html", admin_page)
    app.router.add_get("/privacy.html", privacy)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/assets/leaflet-1.9.4.js", leaflet_asset)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/q/{code}", qr_redirect)
    app.router.add_get("/media/tg/{file_id}", serve_tg_photo)
    app.router.add_get("/api/quest/route", build_route)
    app.router.add_get("/media/{path:.+}", serve_media)
    app.router.add_get("/api/public/info", public_info)
    app.router.add_get("/api/quest/state", state)
    app.router.add_post("/api/quest/start", start)
    app.router.add_post("/api/quest/event", event)
    app.router.add_post("/api/quest/scan", scan)
    app.router.add_post("/api/quest/rewards/{seq}/redeem", redeem_reward)
    app.router.add_post("/api/quest/premium/request", request_premium)
    app.router.add_post("/api/quest/share/invite", share_invite)
    app.router.add_post("/api/admin/login", admin_login)
    app.router.add_post("/api/admin/logout", admin_logout)
    app.router.add_get("/api/admin/overview", admin_overview)
    app.router.add_get("/api/admin/support/{conversation_id}", admin_support_detail)
    app.router.add_post("/api/admin/support/{conversation_id}/reply", admin_support_reply)
    app.router.add_post("/api/admin/support/{conversation_id}/status", admin_support_status)
    app.router.add_post("/api/admin/campaign", admin_campaign)
    app.router.add_post("/api/admin/points/{point_id}", admin_point)
    app.router.add_post("/api/admin/points/{point_id}/rotate-qr", admin_rotate_qr)
    app.router.add_post("/api/admin/points/{point_id}/qr", admin_create_qr)
    app.router.add_post("/api/admin/qr/{qr_id}/status", admin_qr_status)
    app.router.add_post("/api/admin/qr/{qr_id}/delete", admin_delete_qr)
    app.router.add_post("/api/admin/premium/{session_id}/issued", admin_premium)
    app.router.add_post("/api/admin/points/{point_id}/qr/link", admin_link_qr)
    app.router.add_post("/api/admin/participants/{session_id}/delete", admin_delete_participant)
    app.router.add_get("/api/admin/export.csv", admin_export)
    app.router.add_static("/static/", static, show_index=False, append_version=True)

    # Выданные через бота права поднимаются из базы при запуске: без этого
    # после перезапуска приглашённые администраторы теряли доступ до первой
    # операции с правами.
    async def load_admins(_):
        await service.load_granted_admins()

    app.on_startup.append(load_admins)

    async def close_session(_):
        if http_session:
            await http_session[0].close()

    app.on_cleanup.append(close_session)
    return app

# ========================================================================
# ЗАПУСК
# ========================================================================

def _build_fingerprint() -> str:
    """Короткий отпечаток фактически лежащих на сервере файлов.

    Раньше версия была зашита строкой и не менялась при обновлении кода,
    поэтому по /health нельзя было понять, доехал деплой или нет. Теперь
    отпечаток считается от содержимого файлов: совпал с локальным — на
    сервере ровно те же файлы, отличается — процесс работает на старом коде.
    """
    base = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in (
        "main.py", "index.html", "admin.html", "static/bb-bike-logo.jpg", "static/bb-bike-scooter-cutout.png",
        f"static/{QUEST_SHARE_VIDEO}",
        "static/admin.css", "static/vendor/leaflet.css",
        "static/vendor/leaflet-1.9.4.asset",
    ):
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:10]


BUILD_VERSION = f"Krasnaya Polyana Quest 2.4 · build {_build_fingerprint()}"


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

    app = create_web_app(service, settings, bot, BUILD_VERSION)
    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.web_port)
    await site.start()
    log.info("Mini App и API слушают 0.0.0.0:%s", settings.web_port)
    log.info("Версия: %s", BUILD_VERSION)
    # Оформление Telegram не является зависимостью HTTP/API. Даже многочасовой
    # RetryAfter на SetMyName больше не может остановить деплой и карту.
    profile_sync = asyncio.create_task(
        setup_bot_commands(bot, settings), name="telegram-profile-sync"
    )
    def report_profile_sync(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            # Ошибка оформления не должна останавливать HTTP/API, но и теряться
            # как "Task exception was never retrieved" тоже не должна.
            log.exception("Фоновая синхронизация профиля Telegram завершилась с ошибкой")
    profile_sync.add_done_callback(report_profile_sync)
    counts = {
        "campaigns": (await db.fetchone("SELECT COUNT(*) count FROM campaigns"))["count"],
        "points": (await db.fetchone("SELECT COUNT(*) count FROM points"))["count"],
        "participants": (await db.fetchone("SELECT COUNT(*) count FROM participants"))["count"],
        "sessions": (await db.fetchone("SELECT COUNT(*) count FROM sessions"))["count"],
        "events": (await db.fetchone("SELECT COUNT(*) count FROM quest_events"))["count"],
    }
    log.info(
        "Постоянная БД: %s · campaigns=%s points=%s participants=%s sessions=%s events=%s",
        settings.db_path, counts["campaigns"], counts["points"], counts["participants"],
        counts["sessions"], counts["events"],
    )

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
    polling_error: BaseException | None = None
    try:
        done, _ = await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done and not polling.cancelled():
            polling_error = polling.exception()
    finally:
        stop.set()
        if not polling.done():
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass
        for task in (polling, janitor, waiter, profile_sync):
            if not task.done():
                task.cancel()
        await asyncio.gather(polling, janitor, waiter, profile_sync, return_exceptions=True)
        await runner.cleanup()
        await bot.session.close()
        await db.close()
        log.info("Квест Бибибайк остановлен штатно")
    if polling_error is not None:
        raise polling_error


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
