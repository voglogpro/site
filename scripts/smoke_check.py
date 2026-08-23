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
from aiogram.methods import SetMyName


async def production_reward_scenario() -> None:
    """Проверяет именно запускаемый main.py, включая одноразовую выдачу."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["SUPPORT_CHAT_ID"] = ""
        settings = production.load_settings()
        db = production.Database(Path(tmp) / "production.db")
        await db.initialize()
        try:
            service = production.QuestService(db, settings)
            await service.ensure_demo_campaign()
            identity = production.TelegramIdentity(settings.dev_user_id, "smoke", "Тест", "", "ru")
            overview = await service.admin_overview()
            assert overview["map"]["mapgl_styles"] == {
                "light": settings.mapgl_style_light, "dark": settings.mapgl_style_dark,
            }
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
            redeem_id = "reward-smoke-request-0001"
            issued = await service.redeem_reward(identity, 1, redeem_id)
            assert issued["reward"]["code"].startswith("BB-")
            used = issued["data"]["points"][0]
            assert "reward_code" not in used and used["reward_used"] and not used["reward_available"]
            recovered = await service.redeem_reward(identity, 1, redeem_id)
            assert recovered["reward"]["code"] == issued["reward"]["code"]
            try:
                await service.redeem_reward(identity, 1, "reward-smoke-request-0002")
            except production.QuestError as exc:
                assert exc.code == "reward_used"
            else:
                raise AssertionError("reward was issued twice")
            await service.scan(identity, codes[1], "production-scan-2")
            completed_state = await service.scan(identity, codes[2], "production-scan-3")
            assert completed_state["session"]["status"] == "completed"
            assert completed_state["premium"]["requested_at"] is None
            premium_id = "premium-smoke-request-0001"
            requested = await service.request_premium(identity, premium_id, "+7 (999) 123-45-67")
            assert requested["data"]["premium"]["requested_at"]
            assert requested["data"]["premium"]["phone"] == "+79991234567"
            assert requested["notification_claim"] == ""
            repeated_request = await service.request_premium(identity, premium_id, "+7 (999) 123-45-67")
            assert repeated_request["data"]["premium"]["requested_at"] == requested["data"]["premium"]["requested_at"]
            conversations = await service.admin_support_conversations()
            assert len(conversations) == 1
            conversation = conversations[0]
            assert conversation["participant_code"].startswith("KP-")
            assert conversation["phone"] == "+79991234567"
            assert (await db.fetchone(
                "SELECT COUNT(*) count FROM support_messages WHERE kind='premium_request'"
            ))["count"] == 1
            await service.activate_support(identity)
            assert await service.receive_support_message(identity, "Не вижу подписку", 501)
            assert await service.receive_support_message(identity, "Не вижу подписку", 501)
            assert (await db.fetchone(
                "SELECT COUNT(*) count FROM support_messages WHERE source_key=?",
                (f"tg:{identity.user_id}:501",),
            ))["count"] == 1
            conversations = await service.admin_support_conversations()
            assert conversations[0]["unread_count"] == 1
            detail = await service.admin_support_detail(conversation["id"])
            assert detail["conversation"]["participant_code"].startswith("KP-")
            target = await service.support_reply_target(conversation["id"])
            assert target["user_id"] == identity.user_id
            replied = await service.record_support_reply(conversation["id"], 0, "Заявка принята", 777)
            assert replied["messages"][-1]["direction"] == "operator"
            closed = await service.set_support_status(conversation["id"], "closed")
            assert closed["conversation"]["status"] == "closed"
            overview = await service.admin_overview()
            assert overview["funnel"]["premium_pending"] == 1
            row = next(p for p in overview["recent"] if p["id"] == completed_state["session"]["id"])
            assert row["premium_requested_at"]
            assert any(p["id"] == completed_state["session"]["id"] for p in overview["premium_requests"])
            issued_premium = await service.mark_premium_issued(0, completed_state["session"]["id"])
            assert issued_premium["newly_issued"] is True
            repeated_issue = await service.mark_premium_issued(0, completed_state["session"]["id"])
            assert repeated_issue["newly_issued"] is False
            final_state = await service.state(identity)
            assert final_state["premium"]["status"] == "issued" and final_state["premium"]["issued_at"]
            final_overview = await service.admin_overview()
            assert not any(p["id"] == completed_state["session"]["id"] for p in final_overview["premium_requests"])
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
    root = Path(__file__).resolve().parent.parent
    gift_asset = root / "static" / "bb-bike-gift-v1.webp"
    assert gift_asset.is_file()
    gift_bytes = gift_asset.read_bytes()
    assert 50_000 < len(gift_bytes) < 300_000
    assert gift_bytes[:4] == b"RIFF" and gift_bytes[8:12] == b"WEBP"
    assert production.mimetypes.guess_type(str(gift_asset))[0] == "image/webp"
    leaflet = (root / "static" / "vendor" / "leaflet-1.9.4.asset").read_bytes()
    assert hashlib.sha256(leaflet.rstrip(b"\n")).hexdigest() == "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a"
    invite_video = (root / "static" / production.QUEST_SHARE_VIDEO).read_bytes()
    assert 1_000_000 < len(invite_video) < 20_000_000
    assert invite_video[4:8] == b"ftyp"
    assert 0 < invite_video.find(b"moov") < invite_video.find(b"mdat")
    assert len(production.QUEST_SHARE_TEXT) <= 1024
    assert production.QUEST_SHARE_TEXT.startswith("Бибибайк КВЕСТ 💚")
    share_settings = production.load_settings()
    share_result = production.quest_share_result(share_settings, "quest_bot", "build abc123")
    assert share_result.mime_type == "video/mp4"
    assert share_result.video_url.endswith("/static/bbbike-quest-invite.mp4?v=abc123")
    assert share_result.caption == production.QUEST_SHARE_TEXT
    assert share_result.reply_markup.inline_keyboard[0][0].url == "https://t.me/quest_bot?startapp=quest"
    class SetupBot:
        name = ""
        commands = []
        short_description = ""
        description = ""
        menu_button = None
        writes = 0
        async def get_my_name(self): return type("Name", (), {"name": self.name})()
        async def set_my_name(self, name): self.name = name; self.writes += 1
        async def get_my_commands(self): return self.commands
        async def set_my_commands(self, commands): self.commands = commands; self.writes += 1
        async def get_my_short_description(self):
            return type("Short", (), {"short_description": self.short_description})()
        async def set_my_short_description(self, value): self.short_description = value; self.writes += 1
        async def get_my_description(self):
            return type("Description", (), {"description": self.description})()
        async def set_my_description(self, value): self.description = value; self.writes += 1
        async def get_chat_menu_button(self):
            return self.menu_button or type("Menu", (), {"text": None, "web_app": None})()
        async def set_chat_menu_button(self, menu_button): self.menu_button = menu_button; self.writes += 1
    setup_bot = SetupBot()
    await production.setup_bot_commands(setup_bot, share_settings)
    assert setup_bot.name == "Бибибайк КВЕСТ"
    # Повторная синхронизация читает значения, но не делает лишних Telegram writes.
    writes = setup_bot.writes
    await production.setup_bot_commands(setup_bot, share_settings)
    assert setup_bot.writes == writes
    assert setup_bot.menu_button.web_app.url == share_settings.webapp_url
    class FloodedNameBot(SetupBot):
        async def set_my_name(self, name):
            raise production.TelegramRetryAfter(
                method=SetMyName(name=name), message="Too Many Requests", retry_after=67611,
            )
    flooded_bot = FloodedNameBot()
    await production.setup_bot_commands(flooded_bot, share_settings)
    # Flood control одного поля не мешает синхронизировать остальные и не выходит наружу.
    assert flooded_bot.name == "" and flooded_bot.menu_button.web_app.url == share_settings.webapp_url
    webapp = (root / "index.html").read_text(encoding="utf-8")
    assert "/api/quest/share/invite" in webapp and "tg.shareMessage" in webapp
    assert "Поделиться квестом" in webapp and "Отправить приглашение с видео" in webapp
    admin_app = (root / "admin.html").read_text(encoding="utf-8")
    assert "copyPremiumRequest" in admin_app and "copySupportConversation" in admin_app
    assert "Копировать заявку" in admin_app and "Копировать всё обращение" in admin_app
    assert "Telegram ID:" in admin_app and "ID участника:" in admin_app
    assert "function useMapgl(){return !!mapglKey()&&!!window.mapgl&&!window.__mapglFailed}" in webapp
    assert "function initGlMap(" in webapp and "new mapgl.Map(node,opts)" in webapp
    assert "function startSplashMotion(" in webapp and "requestAnimationFrame(render)" in webapp
    assert "function primeRouteMap(" in webapp and "return rules('',true)" in webapp
    assert 'rel="preload" href="/static/bb-bike-gift-v1.webp"' in webapp
    assert "fallbackSrc='/static/bb-bike-scooter-cutout.png'" in webapp
    assert "renderDensity=Math.min(1.75" in webapp and "ctx.setTransform(renderDensity" in webapp
    assert "animation:gift-bike-ride 3s linear infinite" in webapp
    assert 'class="splash-bike-wrap"' in webapp and 'class="splash-headlight"' in webapp
    assert 'class="splash-motion" width="512" height="512"' in webapp
    assert "warmedFingerprint===stateFingerprint(state)?screenCache.get('partners'):null" in webapp
    assert 'id="map-loading-layer"' not in webapp and 'Загружаем карту 2ГИС' not in webapp
    assert 'runSplash();loadMapgl(()=>{});return refresh()' in webapp
    assert 'splashTimer=setTimeout(()=>finishSplashAfterMapTimeout(generation),SPLASH_MAX_MS)' in webapp
    assert "if(node)return;" in webapp and "mapAttemptId" in webapp
    assert "attemptId===mapAttemptId" in webapp
    assert "2gis.ru/directions/tab/" in webapp and "2gis.ru/routeSearch/rsType" not in webapp
    assert "reward_redeem_request_id" in production.SCHEMA
    assert all(name in production.SCHEMA for name in ("requested_at", "support_notified_at", "support_notification_claim"))
    draft = production.support_draft_url("https://t.me/bbbike_support", "Код KP-123")
    assert draft.startswith("https://t.me/bbbike_support?text=") and "KP-123" in draft
    assert "glMap.setStyleById(next)" in webapp and "id=\"map-theme-toggle\"" in webapp
    assert "coverMapPreview" not in webapp and 'class="cover-map"' not in webapp
    assert ".map-tone-dark canvas" not in webapp
    assert "supportOptions={failIfMajorPerformanceCaveat:false}" in webapp
    assert "if(compatAttempt>0){opts.webglVersion=1" in webapp
    assert "graphicsPreset:android?'light':'normal'" in webapp and "instance.on?.('idle'" in webapp
    assert "invalidtilekey|styleloaderror|webglcontextlost|context lost" in webapp
    assert "function clearRoute()" in webapp and "routeRequestToken++" in webapp
    assert '.map-chip{width:44px;height:44px' in webapp
    assert production.DEFAULT_MAPGL_LIGHT_STYLE == "c080bb6a-8134-4993-93a1-5b4d8c36a59b"
    assert production.DEFAULT_MAPGL_DARK_STYLE == "9643e8da-173b-4359-9fee-8a1fe58e68aa"
    settings = load_settings()
    admin_id = next(iter(settings.admin_ids))
    ticket = create_admin_ticket(admin_id, settings, now=1_000)
    assert validate_admin_ticket(ticket, settings, now=1_001) == admin_id
    assert validate_admin_ticket(ticket + "x", settings, now=1_001) is None
    assert validate_admin_ticket(ticket, settings, now=1_000 + settings.admin_ticket_ttl_sec + 1) is None
    for order in itertools.permutations(range(3)): await scenario(order)
    await production_reward_scenario()
    print("PASS: persistence, maps, 6 point orders, QR idempotency, password session, one-time rewards, 30-day subscription, CRM support and native video sharing")
if __name__ == "__main__": asyncio.run(main())
