from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .db import Database
from .security import TelegramIdentity, qr_digest


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
                await db.execute(
                    """INSERT INTO points(campaign_id,seq,name,address,latitude,longitude,radius_m,reward_title,reward_text,qr_code_hash,qr_public_hint,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (campaign_id, seq, name, address, lat, lon, 100, reward, "Настройте реальную скидку и условия в админке.", qr_digest(self.settings.qr_secret, raw), raw[-6:].upper(), 1, now, now),
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
            raise QuestError("privacy_required", "Нужно согласиться с правилами геопозиции.")
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
                    (session_id, campaign["id"], identity.user_id, "awaiting_location", 1, iso(started), iso(expires)),
                )
                for point in points:
                    await db.execute(
                        """INSERT INTO session_points(session_id,point_id,seq,point_name,address,latitude,longitude,radius_m,reward_title,reward_text,partner_hours)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (session_id, point["id"], point["seq"], point["name"], point["address"], point["latitude"], point["longitude"], point["radius_m"], point["reward_title"], point["reward_text"], point["partner_hours"]),
                    )
                return await self._state_in_tx(db, identity.user_id)

    async def state(self, identity: TelegramIdentity) -> dict:
        await self.upsert_participant(identity)
        async with self.db.transaction() as db:
            await self._expire_in_tx(db, identity.user_id)
            return await self._state_in_tx(db, identity.user_id)

    async def _expire_in_tx(self, db, user_id: int) -> None:
        now = iso()
        await db.execute(
            """UPDATE sessions SET status='expired'
               WHERE user_id=? AND status IN ('awaiting_location','active') AND expires_at < ?""",
            (user_id, now),
        )

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
                "SELECT id point_id,seq,name point_name,address,latitude,longitude,radius_m,reward_title,reward_text,partner_hours,NULL location_seen_at,NULL qr_seen_at,NULL completed_at,NULL reward_code FROM points WHERE campaign_id=? AND active=1 ORDER BY seq",
                (campaign["id"],),
            )).fetchall()
        last_age = None
        if session and session["last_location_at"]:
            last_age = max(0, int((utcnow() - parse_dt(session["last_location_at"])).total_seconds()))
        point_data = []
        for point in points:
            item = row_dict(point)
            item.pop("id", None)
            if session and session["last_latitude"] is not None:
                item["distance_m"] = round(haversine_m(session["last_latitude"], session["last_longitude"], point["latitude"], point["longitude"]))
            else:
                item["distance_m"] = None
            item["map_url"] = f"https://yandex.ru/maps/?pt={point['longitude']},{point['latitude']}&z=17&l=map"
            item.pop("latitude", None)
            item.pop("longitude", None)
            point_data.append(item)
        entitlement = None
        if session:
            entitlement = await (await db.execute("SELECT public_code,status FROM premium_entitlements WHERE session_id=?", (session["id"],))).fetchone()
        return {
            "campaign": {
                "title": campaign["title"], "city": campaign["city"], "status": campaign["status"],
                "premium_title": campaign["premium_title"], "premium_instruction": campaign["premium_instruction"],
            },
            "session": row_dict(session),
            "points": point_data,
            "last_location_age_sec": last_age,
            "location_stale": bool(session and (last_age is None or last_age > self.settings.location_stale_sec)),
            "premium": row_dict(entitlement),
            "support_url": self.settings.support_url,
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

    async def scan(self, identity: TelegramIdentity, qr_code: str, request_id: str) -> dict:
        qr_code = qr_code.strip()
        if not qr_code or len(qr_code) > 256:
            raise QuestError("invalid_qr", "Код не распознан.")
        rejected = False
        completed_seq = None
        async with self.lock_for(identity.user_id):
            async with self.db.transaction() as db:
                await self._expire_in_tx(db, identity.user_id)
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
                if session["status"] not in ("awaiting_location", "active") and not duplicate:
                    raise QuestError("no_active_session", "Активный квест не найден.", 409)
                if rejected:
                    current = None
                else:
                    current = await (await db.execute(
                        """SELECT sp.*,p.qr_code_hash,p.qr_public_hint FROM session_points sp JOIN points p ON p.id=sp.point_id
                           WHERE sp.session_id=? AND sp.seq=?""", (session["id"], session["current_seq"])
                    )).fetchone()
                if rejected:
                    pass
                else:
                    digest = qr_digest(self.settings.qr_secret, qr_code)
                    manual_ok = bool(current and len(qr_code) == 6 and hmac.compare_digest(qr_code.upper(), current["qr_public_hint"].upper()))
                    accepted = bool(current and (hmac.compare_digest(digest, current["qr_code_hash"]) or manual_ok))
                    reason = "" if accepted else "wrong_point"
                    await db.execute(
                        "INSERT INTO qr_scans(session_id,point_id,token_fingerprint,request_id,scanned_at,accepted,reject_reason) VALUES(?,?,?,?,?,?,?)",
                        (session["id"], current["point_id"] if current else None, hashlib.sha256(qr_code.encode()).hexdigest()[:12], request_id, iso(), int(accepted), reason),
                    )
                    if not accepted:
                        rejected = True
                    else:
                        await db.execute("UPDATE session_points SET qr_seen_at=COALESCE(qr_seen_at,?) WHERE id=?", (iso(), current["id"]))
                        if await self._try_complete_in_tx(db, session["id"], current["seq"]):
                            completed_seq = current["seq"]
            if rejected:
                raise QuestError("wrong_qr", "Это код другой точки. Проверь табличку у стойки.", 409)
            async with self.db.transaction() as db:
                result = await self._state_in_tx(db, identity.user_id)
                result["event"] = {"point_completed": completed_seq}
                return result

    async def _try_complete_in_tx(self, db, session_id: str, seq: int) -> bool:
        point = await (await db.execute("SELECT * FROM session_points WHERE session_id=? AND seq=?", (session_id, seq))).fetchone()
        if not point or point["completed_at"] or not point["location_seen_at"] or not point["qr_seen_at"]:
            return False
        now = iso()
        reward_code = f"BB-{secrets.token_hex(3).upper()}"
        await db.execute("UPDATE session_points SET completed_at=?,reward_code=? WHERE id=? AND completed_at IS NULL", (now, reward_code, point["id"]))
        session = await (await db.execute("SELECT * FROM sessions WHERE id=?", (session_id,))).fetchone()
        if seq >= 3:
            premium_code = f"KP-{secrets.token_hex(4).upper()}"
            await db.execute("UPDATE sessions SET status='completed',current_seq=4,completed_at=? WHERE id=?", (now, session_id))
            await db.execute(
                "INSERT OR IGNORE INTO premium_entitlements(session_id,public_code,status,created_at) VALUES(?,?,'pending',?)",
                (session_id, premium_code, now),
            )
        else:
            await db.execute("UPDATE sessions SET current_seq=? WHERE id=?", (seq + 1, session_id))
        return True

    async def admin_overview(self) -> dict:
        campaign = row_dict(await self._campaign())
        points = [row_dict(r) for r in await self.db.fetchall("SELECT * FROM points ORDER BY seq")]
        metrics = row_dict(await self.db.fetchone(
            """SELECT COUNT(*) total, SUM(status IN ('awaiting_location','active')) active,
               SUM(status='completed') completed, SUM(status='expired') expired FROM sessions"""
        ))
        recent = [row_dict(r) for r in await self.db.fetchall(
            """SELECT s.id,s.status,s.current_seq,s.started_at,s.completed_at,s.last_location_at,s.integrity_status,
               p.user_id,p.display_name,p.username,e.status premium_status,e.public_code premium_code
               FROM sessions s JOIN participants p ON p.user_id=s.user_id
               LEFT JOIN premium_entitlements e ON e.session_id=s.id
               ORDER BY s.started_at DESC LIMIT 500"""
        )]
        return {"campaign": campaign, "points": points, "metrics": metrics, "recent": recent}

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
            await db.execute("UPDATE campaigns SET status=?,session_duration_min=?,updated_at=? WHERE id=?", (status, duration, iso(), campaign["id"]))
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
        except (KeyError, TypeError, ValueError):
            raise QuestError("invalid_point", "Заполни все обязательные поля.")
        if not all((name, address, reward_title, reward_text)) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or not 30 <= radius <= 500:
            raise QuestError("invalid_point", "Проверь адрес, координаты, радиус и награду.")
        async with self.db.transaction() as db:
            before = await (await db.execute("SELECT * FROM points WHERE id=?", (point_id,))).fetchone()
            if not before:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            await db.execute(
                """UPDATE points SET name=?,address=?,latitude=?,longitude=?,radius_m=?,reward_title=?,reward_text=?,partner_hours=?,updated_at=? WHERE id=?""",
                (name, address, latitude, longitude, radius, reward_title, reward_text, hours, iso(), point_id),
            )
            await db.execute("INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)", (admin_id, "point.update", "point", str(point_id), json.dumps(row_dict(before), ensure_ascii=False), json.dumps(payload, ensure_ascii=False), iso()))
        return await self.admin_overview()

    async def rotate_qr(self, admin_id: int, point_id: int) -> str:
        raw = "bbq-v1-" + secrets.token_urlsafe(28)
        async with self.db.transaction() as db:
            point = await (await db.execute("SELECT id FROM points WHERE id=?", (point_id,))).fetchone()
            if not point:
                raise QuestError("point_not_found", "Точка не найдена.", 404)
            await db.execute("UPDATE points SET qr_code_hash=?,qr_public_hint=?,updated_at=? WHERE id=?", (qr_digest(self.settings.qr_secret, raw), raw[-6:].upper(), iso(), point_id))
            await db.execute("INSERT INTO admin_audit(admin_id,action,entity_type,entity_id,after_json,created_at) VALUES(?,?,?,?,?,?)", (admin_id, "point.qr.rotate", "point", str(point_id), '{"rotated":true}', iso()))
        return raw

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
            async with self.db.transaction() as db:
                await db.execute(
                    """DELETE FROM location_observations WHERE received_at<? AND session_id IN
                       (SELECT id FROM sessions WHERE status IN ('completed','expired','cancelled'))""", (cutoff,)
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                continue
