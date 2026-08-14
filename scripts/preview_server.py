"""Local UI preview server. Never use this process in production."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DEV_MODE", "1")
os.environ.setdefault("WEB_PORT", "3217")
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data-preview"))

from aiohttp import web

from quest.api import create_web_app
from quest.config import load_settings
from quest.db import Database
from quest.service import QuestService


class PreviewBot:
    async def get_me(self):
        return type("PreviewUser", (), {"username": "bibibike_quest_preview_bot"})()


async def main() -> None:
    settings = load_settings()
    db = Database(settings.db_path)
    await db.initialize()
    service = QuestService(db, settings)
    await service.ensure_demo_campaign()
    app = create_web_app(service, settings, PreviewBot(), "local-preview")
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", settings.web_port).start()
    print(f"Preview: http://127.0.0.1:{settings.web_port}/?dev=1", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
