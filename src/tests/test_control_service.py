"""ControlService: draft -> publish -> rollback, emergency switches, permissions, audit. Uses in-memory fakes."""
from datetime import datetime, timezone

import pytest

from src.services import control_logic as cl
from src.services.control_service import ControlError, ControlPermissionError, ControlService


class FakeControlRepo:
    def __init__(self):
        self.row = {"id": "global", "published": {}, "previous": None, "draft": None, "version": 0,
                    "published_by": None, "published_at": None, "draft_by": None, "draft_updated_at": None}

    async def get(self):
        return dict(self.row)

    async def save_draft(self, draft, admin_id):
        self.row.update(draft=draft, draft_by=admin_id, draft_updated_at=datetime.now(timezone.utc))
        return dict(self.row)

    async def discard_draft(self):
        self.row.update(draft=None, draft_by=None, draft_updated_at=None)
        return dict(self.row)

    async def publish(self, new_published, admin_id, previous):
        self.row.update(previous=previous, published=new_published, draft=None, version=self.row["version"] + 1,
                        published_by=admin_id, published_at=datetime.now(timezone.utc))
        return dict(self.row)

    async def set_published_direct(self, new_published, admin_id):
        self.row.update(previous=self.row["published"], published=new_published, version=self.row["version"] + 1,
                        published_by=admin_id, published_at=datetime.now(timezone.utc))
        return dict(self.row)


class FakeAudit:
    def __init__(self):
        self.entries = []

    async def add(self, actor_id, action, **kw):
        self.entries.append({"actor_id": actor_id, "action": action, **kw})


class FakeSettings:
    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return dict(self.data)


class FakeAdminService:
    PERMS = {
        "boss": cl.permissions_for("super", is_super=True),
        "mgr": cl.permissions_for("manager"),
        "fin": cl.permissions_for("finance"),
        "legacy": cl.permissions_for("admin"),
    }

    def __init__(self):
        self.live = {"payout_ratio": 0.8, "draw_interval": 2, "min_deposit": 50, "max_deposit": 70000,
                     "min_withdrawal": 200, "max_withdrawal": 10000, "cartela_min": 1, "cartela_max": 500}
        self.updates = []

    async def permissions_of(self, telegram_id):
        return self.PERMS.get(str(telegram_id), frozenset())

    async def get_admin(self, telegram_id):
        return None

    async def get_settings(self):
        return FakeSettings(self.live)

    async def check_settings_patch(self, fields):
        merged = {**self.live, **fields}
        if merged["min_deposit"] > merged["max_deposit"]:
            raise ValueError("Min deposit cannot be greater than Max deposit")

    async def update_settings(self, requester, fields):
        self.updates.append((requester, dict(fields)))
        self.live.update(fields)
        return FakeSettings(self.live)


class FakeAlerts:
    def __init__(self):
        self.sent = []

    def schedule(self, alert_type, data, **kw):
        self.sent.append((alert_type, data))


@pytest.fixture
def svc():
    admin = FakeAdminService()
    s = ControlService(repo=FakeControlRepo(), audit_repo=FakeAudit(), player_repo=object(), admin_service=admin,
                       alerts=FakeAlerts())
    s.fake_admin = admin
    return s


async def test_draft_is_invisible_until_published(svc):
    snap = await svc.save_draft("mgr", {"flags": {"deposits": False}})
    assert snap["has_draft"] and snap["draft"]["flags"]["deposits"] is False
    assert snap["published"]["flags"]["deposits"] is True
    assert await svc.flag_block("deposits") == ""            # players still see the published config
    svc.invalidate()
    await svc.publish("mgr")
    svc.invalidate()
    assert "Deposits" in await svc.flag_block("deposits")
    assert (await svc.snapshot("mgr"))["has_draft"] is False


async def test_only_publishers_can_publish_and_only_editors_can_draft(svc):
    with pytest.raises(ControlPermissionError):
        await svc.save_draft("fin", {"flags": {"deposits": False}})   # finance cannot edit config
    await svc.save_draft("legacy", {"flags": {"deposits": False}})   # legacy admin can draft...
    with pytest.raises(ControlPermissionError):
        await svc.publish("legacy")                                   # ...but not publish
    with pytest.raises(ControlPermissionError):
        await svc.publish("stranger")


async def test_publish_applies_settings_overlay_and_validates_first(svc):
    await svc.save_draft("mgr", {"settings": {"draw_interval": 3, "payout_ratio": 0.85}})
    snap = await svc.publish("mgr")
    assert svc.fake_admin.updates == [("mgr", {"draw_interval": 3, "payout_ratio": 0.85})]
    assert snap["live_settings"]["draw_interval"] == 3

    # a draft that breaks a cross-field rule is refused at draft time and never reaches the live settings
    with pytest.raises(ControlError):
        await svc.save_draft("mgr", {"settings": {"min_deposit": 90000}})
    assert len(svc.fake_admin.updates) == 1


async def test_unchanged_settings_are_not_part_of_the_draft(svc):
    snap = await svc.save_draft("mgr", {"settings": {"draw_interval": 2}})   # same as live
    assert snap["draft"]["settings"] == {}
    assert snap["changes"] == []


async def test_rollback_restores_config_and_settings_and_can_be_undone(svc):
    await svc.save_draft("mgr", {"flags": {"play": False}, "settings": {"draw_interval": 5}})
    await svc.publish("mgr")
    assert svc.fake_admin.live["draw_interval"] == 5
    await svc.rollback("mgr")
    svc.invalidate()
    assert svc.fake_admin.live["draw_interval"] == 2
    assert await svc.flag_block("play") == ""
    await svc.rollback("mgr")                                   # roll forward again
    svc.invalidate()
    assert svc.fake_admin.live["draw_interval"] == 5
    assert await svc.flag_block("play") != ""


async def test_rollback_needs_something_to_roll_back_to(svc):
    with pytest.raises(ControlError):
        await svc.rollback("mgr")


async def test_emergency_switches_are_instant_and_survive_a_stale_draft(svc):
    await svc.save_draft("mgr", {"flags": {"bonus_center": False}})        # draft made while everything is normal
    await svc.set_emergency("mgr", kill_switch={"enabled": True, "reason": "Investigating"})
    assert await svc.flag_block("deposits") == "Investigating"             # instant, no publish needed
    assert await svc.flag_block("withdrawals") == "Investigating"
    await svc.publish("mgr")                                                # the old draft must not undo it
    svc.invalidate()
    assert await svc.flag_block("deposits") == "Investigating"
    assert "Bonus" in await svc.flag_block("bonus_center")                 # and its own change did go live
    await svc.set_emergency("mgr", kill_switch={"enabled": False})
    svc.invalidate()
    assert await svc.flag_block("deposits") == ""


async def test_emergency_requires_permission_and_maintenance_message_is_public(svc):
    with pytest.raises(ControlPermissionError):
        await svc.set_emergency("fin", maintenance={"enabled": True})
    await svc.set_emergency("mgr", maintenance={"enabled": True, "message": "Back at 9", "eta": "21:00"})
    view = await svc.public()
    assert view["maintenance"] == {"enabled": True, "message": "Back at 9", "eta": "21:00"}
    assert "version" in view


async def test_paused_track_blocks_joining_only_that_track(svc):
    await svc.save_draft("mgr", {"games": {"tracks": {"real_10": {"open": False, "pause_message": "Short break"}}}})
    await svc.publish("mgr")
    svc.invalidate()
    assert await svc.any_play_restriction() is True
    assert await svc.track_block("real_10") == "Short break"
    assert await svc.track_block("bonus") == ""


async def test_audit_trail_records_who_what(svc):
    await svc.save_draft("mgr", {"flags": {"deposits": False}})
    await svc.publish("mgr")
    await svc.set_emergency("boss", maintenance={"enabled": True})
    actions = [(e["actor_id"], e["action"]) for e in svc.audit_repo.entries]
    assert actions == [("mgr", "control.draft_save"), ("mgr", "control.publish"), ("boss", "control.emergency")]
    publish = svc.audit_repo.entries[1]
    assert {"path": "flags.deposits", "from": True, "to": False} in publish["after"]["changes"]


async def test_failed_audit_write_never_blocks_the_action(svc):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    svc.audit_repo.add = boom
    await svc.save_draft("mgr", {"flags": {"deposits": False}})   # must not raise
    await svc.publish("mgr")


async def test_emergency_and_publish_alert_the_admins(svc):
    await svc.set_emergency("boss", kill_switch={"enabled": True, "reason": "x"})
    await svc.save_draft("mgr", {"flags": {"deposits": False}})
    await svc.publish("mgr")
    await svc.rollback("mgr")
    kinds = [k for k, _ in svc._alerts.sent]
    assert kinds == ["emergency", "control_publish", "control_publish"]
    assert "Kill switch ON" in svc._alerts.sent[0][1]["summary"]


async def test_alert_scheduling_failure_never_breaks_the_action(svc):
    class Boom:
        def schedule(self, *a, **k):
            raise RuntimeError("no loop")

    svc._alerts = Boom()
    await svc.set_emergency("boss", maintenance={"enabled": True})      # must not raise
