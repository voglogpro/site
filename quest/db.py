from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


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
    premium_title TEXT NOT NULL DEFAULT 'Premium BBBIKE на 30 дней',
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
