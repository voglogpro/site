# -*- coding: utf-8 -*-
from __future__ import annotations

"""Квест «Паспорт долины» — Telegram-бот с мини-приложением.

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
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo,)
from aiohttp import web
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse


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
    session_duration_min: int
    location_stale_sec: int
    location_retention_days: int
    support_url: str
    map_tile_url: str
    map_tile_urls: tuple[str, ...]
    tile_upstreams: tuple[str, ...]
    routing_upstream: str
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


def _tile_upstreams() -> tuple[str, ...]:
    raw = os.getenv("TILE_UPSTREAMS", "")
    chosen = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(chosen) or (
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
    )


def _tile_sources() -> tuple[str, ...]:
    """Список подложек: сначала из MAP_TILE_URLS/MAP_TILE_URL, затем запасные."""
    raw = os.getenv("MAP_TILE_URLS", "")
    chosen = [item.strip() for item in raw.split(",") if item.strip()]
    primary = os.getenv("MAP_TILE_URL", "").strip()
    if primary and primary not in chosen:
        chosen.insert(0, primary)
    # Собственная раздача идёт первой: телефон гарантированно достаёт до
    # сервера квеста, а внешние CDN у части гостей недоступны.
    if "/tiles/{z}/{x}/{y}.png" not in chosen:
        chosen.insert(0, "/tiles/{z}/{x}/{y}.png")
    for fallback in DEFAULT_TILE_SOURCES:
        if fallback not in chosen:
            chosen.append(fallback)
    return tuple(chosen)


def load_settings() -> Settings:
    dev_mode = _bool("DEV_MODE")
    token = os.getenv("BOT_TOKEN", "").strip()
    url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    qr_secret = os.getenv("QR_SECRET", "").strip()
    # Администраторы по умолчанию. Дополняются переменной ADMIN_IDS.
    DEFAULT_ADMIN_IDS = frozenset({1473144466})
    admin_ids = _ids(os.getenv("ADMIN_IDS", "")) | DEFAULT_ADMIN_IDS
    dev_user_id = int(os.getenv("DEV_USER_ID", "999000111"))
    if not dev_mode:
        missing = []
        if not token:
            missing.append("BOT_TOKEN")
        if not url.startswith("https://"):
            missing.append("WEBAPP_URL (HTTPS)")
        if not admin_ids:
            missing.append("ADMIN_IDS")
        if len(qr_secret) < 32:
            missing.append("QR_SECRET (минимум 32 символа)")
        if missing:
            raise RuntimeError("Не заданы обязательные настройки: " + ", ".join(missing))
    if dev_mode:
        token = token or "000000:development-token"
        url = url or "http://127.0.0.1:3000"
        qr_secret = qr_secret or "local-preview-secret-never-production"
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
        session_duration_min=int(os.getenv("SESSION_DURATION_MIN", "240")),
        location_stale_sec=int(os.getenv("LOCATION_STALE_SEC", "300")),
        location_retention_days=int(os.getenv("LOCATION_RETENTION_DAYS", "7")),
        support_url=os.getenv("SUPPORT_URL", "https://t.me/bbbike_support"),
        map_tile_url=os.getenv("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
        # Подпись на карте. Указание OpenStreetMap обязательно по лицензии
        # тайлов, но выводится компактно и рядом с брендом.
        map_attribution=os.getenv("MAP_ATTRIBUTION", "bb.bike · © OpenStreetMap"),
        # Несколько подложек подряд. Один источник — единая точка отказа:
        # если он недоступен из сети гостя (роуминг, VPN, блокировка), карта
        # остаётся пустой. Приложение перебирает список сверху вниз.
        # Первой идёт тёмная подложка — она совпадает с оформлением квеста.
        map_tile_urls=_tile_sources(),
        # Откуда сервер берёт квадраты карты для собственной раздачи.
        # Меняется переменной TILE_UPSTREAMS, если появится свой поставщик
        # карт (например, Яндекс по договору с ключом).
        tile_upstreams=_tile_upstreams(),
        # Движок построения маршрутов по дорогам. Публичный сервер OSRM
        # рассчитан на пробные нагрузки: при росте трафика стоит поднять
        # свой или подключить платный и указать его здесь.
        routing_upstream=os.getenv("ROUTING_UPSTREAM", "https://router.project-osrm.org/route/v1").strip(),
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
    premium_title TEXT NOT NULL DEFAULT 'Премиум bb.bike на 30 дней',
    premium_instruction TEXT NOT NULL DEFAULT 'Покажи этот экран администратору. Премиум будет оформлен вручную.',
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
    issued_at TEXT,
    created_at TEXT NOT NULL
);

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
        await self._db.execute(
            "INSERT INTO schema_meta(key,value) VALUES('version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
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


def require_admin(request: web.Request, settings: Settings) -> TelegramIdentity:
    identity = request_identity(request, settings)
    if not is_admin(identity.user_id, settings):
        raise web.HTTPForbidden(text=json.dumps({"ok": False, "error": "admin_required"}), content_type="application/json")
    ticket = request.headers.get("X-Admin-Ticket", "")
    if validate_admin_ticket(ticket, settings) != identity.user_id:
        raise web.HTTPForbidden(
            text=json.dumps({"ok": False, "error": "admin_command_required"}),
            content_type="application/json",
        )
    return identity


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


SERVICE_WORKER_JS = r"""/* Service worker квеста «Паспорт долины».
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
const TILE_LIMIT = 220;

const PRECACHE = [
  '/',
  '/assets/leaflet-1.9.4.js',
  '/static/vendor/leaflet.css',
  '/static/bb-bike-logo.jpg',
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

  // Подложка карты: уже увиденные квадраты остаются доступными без сети.
  if (/tile|\.png$/i.test(url.pathname) && url.origin !== self.location.origin) {
    event.respondWith(tile(request));
  }
});
"""


QR_CODE_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def is_quest_code(value: str) -> bool:
    """Пускаем на редирект только собственные коды.

    Без этой проверки /q/<что угодно> превратился бы в открытый редирект,
    которым удобно прикрывать фишинговые ссылки чужим доменом.
    """
    return bool(value) and len(value) <= 128 and value.startswith("bbq-") and set(value) <= QR_CODE_ALLOWED


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
                "SELECT id point_id,seq,name point_name,address,latitude,longitude,radius_m,reward_title,reward_text,partner_hours,photo_url,description,NULL location_seen_at,NULL qr_seen_at,NULL completed_at,NULL reward_code FROM points WHERE campaign_id=? AND active=1 ORDER BY seq",
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
            # Расстояние теперь вычисляется только на устройстве пользователя.
            # Координата участника не отправляется и не хранится на сервере.
            item["distance_m"] = None
            item["map_url"] = f"https://yandex.ru/maps/?pt={point['longitude']},{point['latitude']}&z=17&l=map"
            item["yandex_route_url"] = f"https://yandex.ru/maps/?rtext=~{point['latitude']},{point['longitude']}&rtt=bc"
            item["dgis_route_url"] = f"https://2gis.ru/routeSearch/to/{point['longitude']},{point['latitude']}/go"
            if previous_point:
                route_distance_m += haversine_m(
                    previous_point["latitude"], previous_point["longitude"],
                    point["latitude"], point["longitude"],
                )
            previous_point = point
            point_data.append(item)
        entitlement = None
        if session:
            entitlement = await (await db.execute("SELECT public_code,status FROM premium_entitlements WHERE session_id=?", (session["id"],))).fetchone()
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
                "tile_urls": list(self.settings.map_tile_urls),
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
                    raise QuestError("unknown_qr", "Такого кода нет. Проверь зелёную табличку bb.bike у стойки.", 409)
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
            """SELECT COALESCE(SUM(status='pending'),0) premium_pending,
               COALESCE(SUM(status='issued'),0) premium_issued
               FROM premium_entitlements"""
        ))
        funnel.update(reward_metrics or {})
        funnel.update(premium_metrics or {})
        recent = [row_dict(r) for r in await self.db.fetchall(
            """SELECT s.id,s.status,s.current_seq,s.started_at,s.completed_at,s.integrity_status,
               (SELECT COUNT(*) FROM session_points sp WHERE sp.session_id=s.id AND sp.completed_at IS NOT NULL) completed_points,
               p.user_id,p.display_name,p.username,e.status premium_status,e.public_code premium_code
               FROM sessions s JOIN participants p ON p.user_id=s.user_id
               LEFT JOIN premium_entitlements e ON e.session_id=s.id
               ORDER BY s.started_at DESC LIMIT 500"""
        )]
        qr_codes = [row_dict(r) for r in await self.db.fetchall(
            """SELECT q.id,q.point_id,q.label,q.manual_code,q.active,q.scan_count,q.last_scanned_at,q.created_at,
                      p.seq,p.name point_name
               FROM point_qr_codes q JOIN points p ON p.id=q.point_id ORDER BY p.seq,q.id"""
        )]
        return {
            "campaign": campaign, "points": points, "metrics": metrics, "funnel": funnel,
            "recent": recent, "qr_codes": qr_codes,
            "map": {"tile_url": self.settings.map_tile_url, "tile_urls": list(self.settings.map_tile_urls), "attribution": self.settings.map_attribution},
            "map_bounds": {"south": self.POLYANA_BOUNDS[0][0], "west": self.POLYANA_BOUNDS[0][1], "north": self.POLYANA_BOUNDS[1][0], "east": self.POLYANA_BOUNDS[1][1]},
        }

    async def participant_brief(self, user_id: int) -> dict | None:
        row = await self.db.fetchone(
            """SELECT p.user_id,p.display_name,p.username,s.status,s.current_seq,s.started_at,s.completed_at,
               s.last_location_at,s.integrity_status,e.status premium_status,e.public_code premium_code
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

    async def mark_premium_issued(self, admin_id: int, session_id: str) -> None:
        async with self.db.transaction() as db:
            row = await (await db.execute("SELECT id FROM premium_entitlements WHERE session_id=?", (session_id,))).fetchone()
            if not row:
                raise QuestError("premium_not_found", "Заявка на премиум не найдена.", 404)
            await db.execute("UPDATE premium_entitlements SET status='issued',issued_at=? WHERE session_id=?", (iso(), session_id))
            await db.execute("INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at) VALUES(?,?,?,?,?,?)", (admin_id, "premium.issue", "session", session_id, '{"status":"issued"}', iso()))

    async def export_rows(self):
        return await self.db.fetchall(
            """SELECT p.user_id,p.username,p.display_name,s.started_at,s.completed_at,e.public_code,e.status
               FROM premium_entitlements e JOIN sessions s ON s.id=e.session_id JOIN participants p ON p.user_id=s.user_id
               ORDER BY s.completed_at DESC"""
        )

    async def janitor(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            cutoff = iso(utcnow() - timedelta(days=self.settings.location_retention_days))
            now = iso()
            async with self.db.transaction() as db:
                await db.execute(
                    "UPDATE sessions SET status='expired' WHERE status IN ('awaiting_location','active') AND expires_at<?",
                    (now,),
                )
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
        BotCommand(command="admins", description="Кто имеет доступ к CRM (админ)"),
    ])
    await bot.set_my_short_description("Квест bb.bike по трём точкам Красной Поляны")
    await bot.set_my_description(
        "Выбирай партнёрские точки в любом порядке, строй маршрут, ставь QR-штампы "
        "и забирай подарки. После трёх точек — Premium bb.bike."
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
        # Человек пришёл по ссылке с таблички партнёра. Он мог вообще не знать
        # про квест, поэтому сначала коротко объясняем, что это и что он получит.
        payload = (command.args or "").strip()
        if payload.startswith("bbq-"):
            await message.answer(
                "<b>Ты нашёл точку квеста bb.bike</b> 💚\n\n"
                "Это одна из трёх партнёрских точек в Красной Поляне. "
                "Открой приложение — отметка засчитается сама, а подарок партнёра "
                "сохранится в паспорте.\n\n"
                "Собери все три штампа и получи Premium bb.bike на 30 дней.",
                reply_markup=quest_keyboard(settings),
            )
            return
        text = (
            "<b>Добро пожаловать в квест bb.bike</b> 💚\n\n"
            "Гуляй по Красной Поляне, отмечайся на локациях, получай подарки "
            "от наших партнёров. А за завершённый квест — МЕСЯЦ бесплатной "
            "активации Bibibike.\n\n"
            "Здесь всё просто:\n"
            "1. Выбери любую из трёх точек.\n"
            "2. Построй маршрут в Яндекс Картах или 2ГИС.\n"
            "3. На месте отсканируй QR через мини-приложение и забери подарок.\n\n"
            "Как только отсканировано 3 уникальных QR-кода — квест считается "
            "пройденным. А в подарок — Premium bb.bike на 30 дней 🛵\n\n"
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
            f"Поддержка: {settings.support_url}"
        )

    @router.message(Command("admin"))
    async def admin(message: Message):
        if not message.from_user or not is_admin(message.from_user.id, settings):
            await message.answer("Панель доступна только администраторам квеста.")
            return
        await message.answer(
            "<b>CRM квеста</b>\n\nСсылка персональная и открывается только из этой команды.",
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
                "<b>Тебе открыли доступ к CRM квеста bb.bike</b>\n\n"
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
            f"Premium: {item['premium_status'] or 'не назначен'}"
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

    def allow(self, key: str, limit: int, window: int = 60) -> bool:
        now = time.monotonic()
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
            "default-src 'self' https://telegram.org; script-src 'self' 'unsafe-inline' https://telegram.org; "
            "worker-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
            "connect-src 'self'; frame-ancestors 'self' https://web.telegram.org"
        )
        return response

    app = web.Application(middlewares=[error_middleware, security_middleware], client_max_size=256 * 1024)

    async def index(_):
        return web.FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    async def admin_page(request):
        ticket = request.query.get("ticket", "")
        if validate_admin_ticket(ticket, settings) is None:
            raise web.HTTPFound(location="/")
        # admin.html deliberately lives outside the public static directory.
        # This signed handler is the only route capable of serving the CRM.
        return web.FileResponse(root / "admin.html", headers={"Cache-Control": "no-cache"})

    async def public_info(_):
        nonlocal bot_username_cache
        if not bot_username_cache:
            bot_username_cache = (await bot.get_me()).username or ""
        username = bot_username_cache
        return json_response({
            "bot_username": username,
            "chat_url": f"https://t.me/{username}?start=quest" if username else "",
            "app_url": f"https://t.me/{username}?startapp=quest&mode=fullscreen" if username else "",
        })

    async def privacy(_):
        return web.FileResponse(static / "privacy.html", headers={"Cache-Control": "public, max-age=300"})

    async def health(_):
        campaign = await service.db.fetchone("SELECT status FROM campaigns LIMIT 1")
        return json_response({"service": "bibibike-quest", "build_version": build_version, "database": True, "campaign_status": campaign["status"] if campaign else "missing"})

    async def ready(_):
        await service.db.fetchone("SELECT 1")
        return json_response({"ready": True})

    async def favicon(_):
        return web.FileResponse(static / "bb-bike-logo.jpg", headers={"Cache-Control": "public, max-age=86400"})

    # Небольшой кэш в памяти: и фото точек, и квадраты карты берутся
    # десятки раз подряд, а класть их на диск нельзя — на хостинге запись
    # недоступна. Старые записи вытесняются, чтобы память не росла.
    photo_cache: dict[str, bytes] = {}
    tile_cache: dict[str, bytes] = {}
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
        profile = "cycling" if request.query.get("mode", "bike") == "bike" else "foot"
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

    async def serve_tile(request):
        """Подложка карты через собственный сервер.

        У части гостей телефон не достаёт до картографических CDN — из-за
        VPN, оператора или блокировок, и карта оставалась пустой. До сервера
        квеста телефон достаёт всегда, иначе не открылось бы и само
        приложение, поэтому квадраты карты идут через него.
        """
        try:
            z = int(request.match_info["z"])
            x = int(request.match_info["x"])
            y = int(request.match_info["y"])
        except (KeyError, ValueError):
            raise web.HTTPNotFound()
        if not (1 <= z <= 19) or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
            raise web.HTTPNotFound()
        key = f"{z}/{x}/{y}"
        headers = {"Cache-Control": "public, max-age=604800"}
        cached = tile_cache.get(key)
        if cached is not None:
            return web.Response(body=cached, content_type="image/png", headers=headers)
        session = await client_session()
        for template in settings.tile_upstreams:
            url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{s}", "a")
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        continue
                    data = await response.read()
            except Exception:
                continue
            if not data:
                continue
            cache_put(tile_cache, key, data, 700)
            return web.Response(body=data, content_type="image/png", headers=headers)
        raise web.HTTPNotFound()

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
            "<title>Квест bb.bike · Красная Поляна</title>"
            "<style>body{margin:0;min-height:100vh;display:grid;place-content:center;justify-items:center;"
            "gap:18px;padding:28px;background:#07110b;color:#f2f7f1;text-align:center;"
            "font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}"
            "h1{margin:0;font-size:26px;line-height:1.15;letter-spacing:-.02em}"
            "p{margin:0;max-width:340px;color:#a9bbad}"
            "a{margin-top:6px;padding:15px 26px;border-radius:14px;background:#8fe300;color:#07110b;"
            "font-weight:800;text-decoration:none}</style></head><body>"
            "<h1>Ты нашёл точку квеста bb.bike</h1>"
            "<p>Это одна из трёх партнёрских точек в Красной Поляне. Открой квест в Telegram — "
            "отметка засчитается сама, а подарок партнёра сохранится в паспорте.</p>"
            f'<a href="{safe_target}">Открыть в Telegram</a>'
            "</body></html>"
        )
        return web.Response(text=page, content_type="text/html")

    async def service_worker(_):
        # Исходник лежит прямо в этом файле, чтобы обновление проекта сводилось
        # к замене index.html и main.py. Отдаётся из корня: иначе область
        # действия ограничится /static/ и не покроет саму страницу квеста.
        return web.Response(
            text=SERVICE_WORKER_JS,
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

    async def admin_overview(request):
        require_admin(request, settings)
        return json_response({"data": await service.admin_overview()})

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

    async def admin_premium(request):
        admin = require_admin(request, settings)
        await service.mark_premium_issued(admin.user_id, request.match_info["session_id"])
        return json_response({"data": await service.admin_overview()})

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
        writer.writerow(["Telegram ID", "Username", "Имя", "Старт", "Завершение", "Код", "Статус"])
        for row in rows:
            writer.writerow([_csv_safe(row[key]) for key in row.keys()])
        body = "\ufeff" + output.getvalue()
        return web.Response(body=body.encode("utf-8"), content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="bibibike-premium.csv"', "Cache-Control": "no-store"})

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
    app.router.add_get("/tiles/{z}/{x}/{y}.png", serve_tile)
    app.router.add_get("/api/quest/route", build_route)
    app.router.add_get("/media/{path:.+}", serve_media)
    app.router.add_get("/api/public/info", public_info)
    app.router.add_get("/api/quest/state", state)
    app.router.add_post("/api/quest/start", start)
    app.router.add_post("/api/quest/event", event)
    app.router.add_post("/api/quest/scan", scan)
    app.router.add_get("/api/admin/overview", admin_overview)
    app.router.add_post("/api/admin/campaign", admin_campaign)
    app.router.add_post("/api/admin/points/{point_id}", admin_point)
    app.router.add_post("/api/admin/points/{point_id}/rotate-qr", admin_rotate_qr)
    app.router.add_post("/api/admin/points/{point_id}/qr", admin_create_qr)
    app.router.add_post("/api/admin/qr/{qr_id}/status", admin_qr_status)
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
    for name in ("main.py", "index.html", "admin.html"):
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
    await setup_bot_commands(bot, settings)

    app = create_web_app(service, settings, bot, BUILD_VERSION)
    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.web_port)
    await site.start()
    log.info("Mini App и API слушают 0.0.0.0:%s", settings.web_port)
    log.info("Версия: %s", BUILD_VERSION)
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
