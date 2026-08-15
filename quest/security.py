from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from aiohttp import web

from .config import Settings


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


def require_admin(request: web.Request, settings: Settings) -> TelegramIdentity:
    identity = request_identity(request, settings)
    if identity.user_id not in settings.admin_ids:
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
    if user_id not in settings.admin_ids or expires_at < current:
        return None
    # Reject implausibly long tickets even if a future configuration is wrong.
    if expires_at > current + settings.admin_ticket_ttl_sec + 60:
        return None
    payload = f"{user_id}.{expires_at}"
    expected = hmac.new(
        settings.qr_secret.encode(), f"admin-launch:{payload}".encode(), hashlib.sha256
    ).hexdigest()
    return user_id if hmac.compare_digest(expected, received) else None


def qr_digest(secret: str, raw_code: str) -> str:
    return hmac.new(secret.encode(), raw_code.encode(), hashlib.sha256).hexdigest()
