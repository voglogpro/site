"""Release smoke: v2 migration, any-order QR, analytics and one premium."""
from __future__ import annotations
import asyncio, hashlib, itertools, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DEV_MODE", "1")
os.environ.setdefault("BOT_TOKEN", "000000:development-token")
os.environ.setdefault("WEBAPP_URL", "http://127.0.0.1:3000")
os.environ.setdefault("ADMIN_IDS", "7785586524")
os.environ.setdefault("QR_SECRET", "development-only-qr-secret-32-chars")
os.environ.setdefault("ADMIN_PASSWORD", "development-admin-password")
from quest.config import load_settings
from quest.db import Database
from quest.security import TelegramIdentity, create_admin_ticket, validate_admin_ticket
from quest.service import QuestError, QuestService
import main as production


async def production_reward_scenario() -> None:
    """Проверяет именно запускаемый main.py, включая одноразовую выдачу."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        settings = production.load_settings()
        db = production.Database(Path(tmp) / "production.db")
        await db.initialize()
        try:
            service = production.QuestService(db, settings)
            await service.ensure_demo_campaign()
            identity = production.TelegramIdentity(settings.dev_user_id, "smoke", "Тест", "", "ru")
            overview = await service.admin_overview()
            codes = []
            for point in overview["points"]:
                await service.admin_update_point(0, point["id"], {
                    "name": f"Точка {point['seq']}", "address": f"Адрес {point['seq']}",
                    "latitude": point["latitude"], "longitude": point["longitude"], "radius_m": 100,
                    "reward_title": f"Подарок {point['seq']}", "reward_text": "Smoke",
                    "partner_hours": "09:00–21:00", "description": "Тестовая точка", "photo_url": "",
                })
                codes.append(await service.rotate_qr(0, point["id"]))
            await service.admin_update_campaign(0, {"status": "active", "session_duration_min": 240})
            await service.start(identity, True)
            state = await service.scan(identity, codes[0], "production-scan")
            reward = state["points"][0]
            assert "reward_code" not in reward and reward["reward_available"] and not reward["reward_used"]
            issued = await service.redeem_reward(identity, 1)
            assert issued["reward"]["code"].startswith("BB-")
            used = issued["data"]["points"][0]
            assert "reward_code" not in used and used["reward_used"] and not used["reward_available"]
            try:
                await service.redeem_reward(identity, 1)
            except production.QuestError as exc:
                assert exc.code == "reward_used"
            else:
                raise AssertionError("reward was issued twice")
            cookie = production.create_admin_session(settings, now=1_000)
            assert production.validate_admin_session(cookie, settings, now=1_001)
            assert not production.validate_admin_session(cookie + "x", settings, now=1_001)
        finally:
            await db.close()

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
            assert state["map"]["bounds"] == {"south": 43.6, "west": 40.1, "north": 43.77, "east": 40.37}
            if order == (0, 1, 2):
                session_id = state["session"]["id"]
                async with db.transaction() as tx:
                    await tx.execute(
                        "UPDATE sessions SET status='active',expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                        (session_id,),
                    )
                resumed = await service.state(identity)
                assert resumed["session"]["id"] == session_id
                assert resumed["session"]["status"] == "active"
                second = overview["points"][1]
                await service.admin_update_point(settings.dev_user_id, second["id"], {
                    "name":"Сохранённая точка","address":"Сохранённый адрес",
                    "latitude":second["latitude"],"longitude":second["longitude"],"radius_m":100,
                    "reward_title":"Сохранённый подарок","reward_text":"После перезапуска","partner_hours":"10:00–20:00"
                })
                snapshot = await service.state(identity)
                assert snapshot["points"][1]["point_name"] == "Сохранённая точка"
                await service.record_event(identity, "point_view", "persistent-event", second["id"])
                await db.close()
                db = Database(path); await db.initialize()
                service = QuestService(db, settings)
                persisted = await service.state(identity)
                assert persisted["session"]["id"] == session_id
                assert persisted["points"][1]["point_name"] == "Сохранённая точка"
                assert (await db.fetchone("SELECT COUNT(*) count FROM quest_events WHERE request_id='persistent-event'"))["count"] == 1
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
    leaflet = (Path(__file__).resolve().parent.parent / "static" / "vendor" / "leaflet-1.9.4.asset").read_bytes()
    assert hashlib.sha256(leaflet.rstrip(b"\n")).hexdigest() == "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a"
    settings = load_settings()
    admin_id = next(iter(settings.admin_ids))
    ticket = create_admin_ticket(admin_id, settings, now=1_000)
    assert validate_admin_ticket(ticket, settings, now=1_001) == admin_id
    assert validate_admin_ticket(ticket + "x", settings, now=1_001) is None
    assert validate_admin_ticket(ticket, settings, now=1_000 + settings.admin_ticket_ttl_sec + 1) is None
    for order in itertools.permutations(range(3)): await scenario(order)
    await production_reward_scenario()
    print("PASS: persistence, maps, 6 point orders, QR idempotency, password session and one-time rewards")
if __name__ == "__main__": asyncio.run(main())
