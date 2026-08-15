"""Release smoke: v2 migration, any-order QR, analytics and one premium."""
from __future__ import annotations
import asyncio, itertools, os, sys, tempfile
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
from quest.service import QuestError, QuestService

async def scenario(order: tuple[int, ...]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        settings = load_settings(); path = Path(tmp) / "smoke.db"
        db = Database(path); await db.initialize(); await db.close()
        db = Database(path); await db.initialize()  # повторная миграция должна быть идемпотентной
        try:
            service = QuestService(db, settings); await service.ensure_demo_campaign()
            identity = TelegramIdentity(settings.dev_user_id, "smoke", "Тест", "", "ru")
            try: await service.start(identity, True)
            except QuestError as exc: assert exc.code == "campaign_inactive"
            overview = await service.admin_overview(); codes = []
            assert len(overview["qr_codes"]) == 3
            for point in overview["points"]:
                await service.admin_update_point(settings.dev_user_id, point["id"], {
                    "name":f"Точка {point['seq']}","address":f"Адрес {point['seq']}",
                    "latitude":point["latitude"],"longitude":point["longitude"],"radius_m":100,
                    "reward_title":f"Подарок {point['seq']}","reward_text":"Smoke","partner_hours":"09:00–21:00"})
                codes.append(await service.rotate_qr(settings.dev_user_id, point["id"]))
            if order == (0, 1, 2):
                _, extra_id = await service.create_qr(settings.dev_user_id, overview["points"][0]["id"], "Вторая стойка")
                extra = await service.admin_overview()
                assert any(q["id"] == extra_id and q["active"] for q in extra["qr_codes"])
                await service.set_qr_active(settings.dev_user_id, extra_id, False)
            await service.admin_update_campaign(settings.dev_user_id,{"status":"active","session_duration_min":240})
            state = await service.start(identity, True); assert state["session"]["status"] == "active"
            for n,idx in enumerate(order):
                state = await service.scan(identity,codes[idx],f"scan-{n}")
                assert len([p for p in state["points"] if p["completed_at"]]) == n+1
            assert state["session"]["status"] == "completed" and state["premium"]["status"] == "pending"
            duplicate = await service.scan(identity,codes[order[-1]],f"scan-{len(order)-1}")
            assert duplicate["session"]["status"] == "completed"
            repeated = await service.scan(identity,codes[order[-1]],"fresh-repeat")
            assert repeated["session"]["status"] == "completed"
            row = await db.fetchone("SELECT COUNT(*) count FROM premium_entitlements WHERE session_id=?",(state["session"]["id"],))
            assert row["count"] == 1
            old = await db.fetchone("SELECT COUNT(*) count FROM sessions WHERE last_latitude IS NOT NULL")
            assert old["count"] == 0
        finally: await db.close()

async def main() -> None:
    for order in itertools.permutations(range(3)): await scenario(order)
    print("PASS: v2 database, all 6 point orders, QR idempotency and one premium")
if __name__ == "__main__": asyncio.run(main())
