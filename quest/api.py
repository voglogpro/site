from __future__ import annotations

import csv
import io
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

import base64
import qrcode
from aiohttp import web
from aiogram import Bot

from .config import Settings
from .security import request_identity, require_admin, validate_admin_ticket
from .service import QuestError, QuestService

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
    root = Path(__file__).resolve().parent.parent
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
            "is_admin": identity.user_id in settings.admin_ids,
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
        data = await service.scan(identity, str(body.get("qr_code") or ""), str(body.get("request_id") or uuid.uuid4()))
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

    async def admin_rotate_qr(request):
        admin = require_admin(request, settings)
        point_id = int(request.match_info["point_id"])
        raw = await service.rotate_qr(admin.user_id, point_id)
        image = qrcode.make(raw)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        encoded = base64.b64encode(stream.getvalue()).decode("ascii")
        return json_response({"qr_code": raw, "manual_code": raw[-6:].upper(), "qr_png": "data:image/png;base64," + encoded, "point_id": point_id})

    async def admin_create_qr(request):
        admin = require_admin(request, settings)
        point_id = int(request.match_info["point_id"])
        body = await json_body(request)
        raw, qr_id = await service.create_qr(admin.user_id, point_id, str(body.get("label") or ""))
        image = qrcode.make(raw)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        encoded = base64.b64encode(stream.getvalue()).decode("ascii")
        return json_response({"qr_id": qr_id, "qr_code": raw, "manual_code": raw[-6:].upper(), "qr_png": "data:image/png;base64," + encoded, "point_id": point_id})

    async def admin_qr_status(request):
        admin = require_admin(request, settings)
        body = await json_body(request)
        data = await service.set_qr_active(admin.user_id, int(request.match_info["qr_id"]), bool(body.get("active")))
        return json_response({"data": data})

    async def admin_premium(request):
        admin = require_admin(request, settings)
        await service.mark_premium_issued(admin.user_id, request.match_info["session_id"])
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
        return web.Response(body=body.encode("utf-8"), content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="bbbike-premium.csv"', "Cache-Control": "no-store"})

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/admin.html", admin_page)
    app.router.add_get("/privacy.html", privacy)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/assets/leaflet-1.9.4.js", leaflet_asset)
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
    app.router.add_get("/api/admin/export.csv", admin_export)
    app.router.add_static("/static/", static, show_index=False, append_version=True)
    return app
