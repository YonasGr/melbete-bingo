"""AdminAlertService: routing, thresholds, cooldowns, failed-login tracking, delivery failures."""
import asyncio
from types import SimpleNamespace

import pytest

from src.services import control_logic as cl
from src.services.admin_alerts import AdminAlertService, FailedLoginTracker


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.sent.append((chat_id, text))


class FakeAdminRepo:
    def __init__(self, admins):
        self.admins = admins

    async def list_all(self):
        return self.admins


def admin(tid, role="admin", sup=False, types=None):
    return SimpleNamespace(telegram_id=tid, role=role, is_super_admin=sup, alert_types=types)


def make(admins, cfg=None, bot=None, flagged=(), group="", env=()):
    config = cl.normalize_config(cfg or {})
    bot = bot or FakeBot()

    async def get_cfg():
        return config

    async def get_bot():
        return bot

    async def get_flagged():
        return set(flagged)

    svc = AdminAlertService(admin_repo=FakeAdminRepo(admins), bot_getter=get_bot, config_getter=get_cfg,
                            flagged_getter=get_flagged, group_chat_id=group, env_admin_ids=list(env))
    return svc, bot


async def drain(svc):
    await asyncio.gather(*list(svc._tasks))


async def test_routing_follows_role_defaults_and_personal_choice():
    staff = [admin("1", "super", True), admin("2", "finance"), admin("3", "support"), admin("4", "finance", types=["emergency"])]
    svc, _ = make(staff)
    assert await svc.recipients("big_withdrawal") == ["1", "2"]          # support + the opted-out finance admin excluded
    assert await svc.recipients("emergency") == ["1", "4"]
    assert await svc.recipients("fraud_flag") == ["1", "2", "3"]


async def test_group_chat_and_env_admins():
    svc, _ = make([admin("1", "super", True)], group="-100123", env=[777])
    assert await svc.recipients("big_withdrawal") == ["1", "777", "-100123"]
    assert await svc.recipients("staff_change") == ["1"]                  # not a group type, env admin default lacks it


async def test_big_withdrawal_threshold_and_master_switch():
    svc, bot = make([admin("1", "super", True)], {"alerts": {"big_withdrawal_amount": 1000}})
    await svc.on_withdrawal_requested("p1", "Abebe", 999, "Telebirr"); await drain(svc)
    assert bot.sent == []
    await svc.on_withdrawal_requested("p1", "Abebe", 1500, "Telebirr"); await drain(svc)
    assert len(bot.sent) == 1 and "1,500 ETB" in bot.sent[0][1]

    off, bot2 = make([admin("1", "super", True)], {"alerts": {"enabled": False, "big_withdrawal_amount": 1}})
    await off.on_withdrawal_requested("p1", "Abebe", 5000, None); await drain(off)
    assert bot2.sent == []

    never, bot3 = make([admin("1", "super", True)], {"alerts": {"big_withdrawal_amount": 0}})
    await never.on_withdrawal_requested("p1", "Abebe", 10**6, None); await drain(never)
    assert bot3.sent == []                                                  # 0 = never


async def test_flagged_player_alert_once_per_day_per_player():
    svc, bot = make([admin("1", "super", True)], {"alerts": {"big_withdrawal_amount": 0}}, flagged={"bad"})
    await svc.on_withdrawal_requested("good", "Good", 100, None); await drain(svc)
    assert bot.sent == []
    await svc.on_withdrawal_requested("bad", "Bad", 100, None); await drain(svc)
    await svc.on_withdrawal_requested("bad", "Bad", 200, None); await drain(svc)
    assert len(bot.sent) == 1 and "Flagged player" in bot.sent[0][1]


async def test_duplicate_alerts_are_suppressed_by_cooldown():
    svc, bot = make([admin("1", "super", True)])
    for _ in range(3):
        svc.schedule("emergency", {"summary": "Kill switch ON", "actor": "9"}, dedupe_key="ks", cooldown=60)
    await drain(svc)
    assert len(bot.sent) == 1


def test_failed_login_tracker_threshold_window_and_cooldown():
    t = FailedLoginTracker(window=600)
    assert t.record("ip", 3, now=0) is None and t.record("ip", 3, now=10) is None
    assert t.record("ip", 3, now=20) == 3          # third attempt fires
    assert t.record("ip", 3, now=30) is None       # then quiet for 15 minutes
    t.record("ip", 3, now=1000); t.record("ip", 3, now=1001)
    assert t.record("ip", 3, now=1002) == 3        # 15 min later a fresh burst alerts again
    t2 = FailedLoginTracker(window=600)
    t2.record("x", 3, now=0); t2.record("x", 3, now=100)
    assert t2.record("x", 3, now=900) is None      # old attempts fell out of the window


async def test_failed_login_alert_and_delivery_failure_is_contained():
    bot = FakeBot(fail_for={"2"})
    svc, bot = make([admin("1", "super", True), admin("2", "manager")], {"alerts": {"failed_logins_before_alert": 2}}, bot=bot)
    await svc.on_failed_login("203.0.113.9", "555", True); await drain(svc)
    assert bot.sent == []
    await svc.on_failed_login("203.0.113.9", "555", True); await drain(svc)
    assert [c for c, _ in bot.sent] == ["1"]        # admin 2 had blocked the bot: logged, others still delivered
    res = await svc.send("test", "hi", only_to=["2", "1"])
    assert res["sent"] == 1 and res["failed"] == 1 and "2:" in res["errors"][0]


async def test_no_bot_configured_is_reported_not_raised():
    async def no_bot():
        return None

    svc = AdminAlertService(admin_repo=FakeAdminRepo([]), bot_getter=no_bot, config_getter=lambda: asyncio.sleep(0, cl.normalize_config({})),
                            group_chat_id="", env_admin_ids=[])
    res = await svc.send("test", "hi", only_to=["1"])
    assert res["sent"] == 0 and "not configured" in res["errors"][0]
