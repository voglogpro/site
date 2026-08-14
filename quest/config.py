from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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
    session_duration_min: int
    location_stale_sec: int
    location_retention_days: int
    support_url: str
    dev_mode: bool
    dev_user_id: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name

    @property
    def admin_url(self) -> str:
        return f"{self.webapp_url.rstrip('/')}/admin.html"


def load_settings() -> Settings:
    dev_mode = _bool("DEV_MODE")
    token = os.getenv("BOT_TOKEN", "").strip()
    url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    qr_secret = os.getenv("QR_SECRET", "").strip()
    admin_ids = _ids(os.getenv("ADMIN_IDS", ""))
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
        admin_ids = admin_ids or frozenset({dev_user_id})
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
        init_data_max_age_sec=int(os.getenv("INIT_DATA_MAX_AGE_SEC", "900")),
        session_duration_min=int(os.getenv("SESSION_DURATION_MIN", "240")),
        location_stale_sec=int(os.getenv("LOCATION_STALE_SEC", "300")),
        location_retention_days=int(os.getenv("LOCATION_RETENTION_DAYS", "7")),
        support_url=os.getenv("SUPPORT_URL", "https://t.me/bbbike_support"),
        dev_mode=dev_mode,
        dev_user_id=dev_user_id,
    )
