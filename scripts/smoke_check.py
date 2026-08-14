"""Small release smoke check, intentionally not a full test suite."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DEV_MODE", "1")
os.environ.setdefault("BOT_TOKEN", "000000:development-token")
os.environ.setdefault("WEBAPP_URL", "http://127.0.0.1:3000")
os.environ.setdefault("ADMIN_IDS", "7785586524")
os.environ.setdefault("QR_SECRET", "development-only-qr-secret-32-chars")

from quest.config import load_settings
from quest.db import Database
from quest.security import TelegramIdentity
from quest.service import QuestError, QuestService, haversine_m


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        settings = load_settings()
        db = Database(Path(tmp) / "smoke.db")
        await db.initialize()
        try:
            service = QuestService(db, settings)
            await service.ensure_demo_campaign()
            identity = TelegramIdentity(settings.dev_user_id, "smoke", "Тест", "", "ru")
            state = await service.state(identity)
            assert state["campaign"]["status"] == "draft"
            assert len(state["points"]) == 3
            assert all("latitude" in point and "longitude" in point for point in state["points"])
            assert state["map"]["tile_url"].startswith("https://")
            assert state["campaign"]["route_distance_m"] > 0
            try:
                await service.start(identity, True)
            except QuestError as exc:
                assert exc.code == "campaign_inactive"
            else:
                raise AssertionError("Draft campaign must not start")
            assert haversine_m(43.68, 40.205, 43.68, 40.205) == 0

            overview = await service.admin_overview()
            assert overview["funnel"]["started"] == 0
            assert "map" in overview
            qr_codes = []
            for point in overview["points"]:
                await service.admin_update_point(settings.dev_user_id, point["id"], {
                    "name": f"Тестовая точка {point['seq']}",
                    "address": f"Тестовый адрес {point['seq']}",
                    "latitude": point["latitude"],
                    "longitude": point["longitude"],
                    "radius_m": 100,
                    "reward_title": f"Тестовый подарок {point['seq']}",
                    "reward_text": "Только для smoke-проверки",
                    "partner_hours": "09:00–21:00",
                })
                qr_codes.append(await service.rotate_qr(settings.dev_user_id, point["id"]))
            await service.admin_update_campaign(settings.dev_user_id, {"status": "active", "session_duration_min": 240})
            state = await service.start(identity, True)
            assert state["session"]["status"] == "awaiting_location"
            overview = await service.admin_overview()
            assert overview["funnel"]["started"] == 1
            for index, point in enumerate(overview["points"]):
                state = await service.record_location(
                    identity.user_id, point["latitude"], point["longitude"], 10,
                    source="miniapp", request_id=f"smoke-loc-{index}",
                )
                state = await service.scan(identity, qr_codes[index], f"smoke-qr-{index}")
            assert state["session"]["status"] == "completed"
            assert len([p for p in state["points"] if p["completed_at"]]) == 3
            assert state["premium"]["status"] == "pending"
            overview = await service.admin_overview()
            assert overview["funnel"]["reached_point_1"] == 1
            assert overview["funnel"]["reached_point_2"] == 1
            assert overview["funnel"]["reached_point_3"] == 1
            assert overview["funnel"]["rewarded_users"] == 1
            duplicate = await service.scan(identity, qr_codes[-1], "smoke-qr-2")
            assert duplicate["session"]["status"] == "completed"
            entitlements = await db.fetchone("SELECT COUNT(*) count FROM premium_entitlements WHERE session_id=?", (state["session"]["id"],))
            assert entitlements["count"] == 1
        finally:
            await db.close()
    print("PASS: database, safety gate, 3 geofences, 3 QR proofs and one premium entitlement")


if __name__ == "__main__":
    asyncio.run(main())
