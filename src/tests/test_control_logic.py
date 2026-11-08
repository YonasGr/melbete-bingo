"""Pure-logic tests for the admin Control Centre (no database needed)."""
from datetime import datetime, timezone

import pytest

from src.services import control_logic as cl


def test_defaults_round_trip_and_everything_is_on():
    cfg = cl.normalize_config({})
    assert cfg["flags"] == {name: True for name in cl.FLAGS}
    assert cfg["maintenance"]["enabled"] is False
    assert cfg["kill_switch"]["enabled"] is False
    assert all(t["open"] for t in cfg["games"]["tracks"].values())
    assert cl.normalize_config(cfg) == cfg  # idempotent


def test_unknown_flag_track_setting_and_text_are_rejected():
    for bad in (
        {"flags": {"nope": True}},
        {"games": {"tracks": {"real_999": {"open": False}}}},
        {"settings": {"not_a_setting": 1}},
        {"content": {"texts": {"mystery": "x"}}},
    ):
        with pytest.raises(ValueError):
            cl.normalize_config(bad)


def test_settings_overlay_ranges_and_cross_field_rules():
    assert cl.normalize_config({"settings": {"payout_ratio": "0.8", "stake_amount_real_10": 25}})["settings"] == {
        "payout_ratio": 0.8, "stake_amount_real_10": 25,
    }
    with pytest.raises(ValueError):
        cl.normalize_config({"settings": {"payout_ratio": 0.2}})          # commission above 50%
    with pytest.raises(ValueError):
        cl.normalize_config({"settings": {"lobby_countdown_seconds": 0}})
    with pytest.raises(ValueError):
        cl.normalize_config({"settings": {"min_deposit": 500, "max_deposit": 100}})


def test_auto_approve_needs_a_limit_and_banner_needs_a_title():
    with pytest.raises(ValueError):
        cl.normalize_config({"money": {"auto_approve": {"enabled": True, "deposit_max": 0}}})
    with pytest.raises(ValueError):
        cl.normalize_config({"content": {"banner": {"enabled": True}}})
    with pytest.raises(ValueError):
        cl.normalize_config({"content": {"announcement": {"enabled": True}}})


def test_diff_lists_only_changed_leaves():
    old = cl.normalize_config({})
    new = cl.normalize_config({"flags": {"deposits": False}, "settings": {"draw_interval": 3}})
    paths = {c["path"]: (c["from"], c["to"]) for c in cl.diff_config(old, new)}
    assert paths == {"flags.deposits": (True, False), "settings.draw_interval": (None, 3)}
    assert cl.diff_config(old, old) == []


def test_flags_kill_switch_and_track_pause():
    cfg = cl.normalize_config({"flags": {"withdrawals": False}})
    assert cl.flag_state(cfg, "withdrawals")[0] is False
    assert cl.flag_state(cfg, "deposits")[0] is True

    paused = cl.normalize_config({"games": {"tracks": {"real_10": {"open": False, "pause_message": "Back at 8pm"}}}})
    assert cl.track_state(paused, "real_10") == (False, "Back at 8pm")
    assert cl.track_state(paused, "bonus")[0] is True

    killed = cl.normalize_config({"kill_switch": {"enabled": True, "reason": "Investigating"}})
    for flag in ("deposits", "withdrawals", "play"):
        assert cl.flag_state(killed, flag) == (False, "Investigating")
    assert cl.flag_state(killed, "bonus_center")[0] is True
    assert cl.track_state(killed, "real_10")[0] is False


def test_daily_limit_prefers_personal_over_platform():
    cfg = cl.normalize_config({"money": {"daily_deposit_limit": 5000}})
    assert cl.daily_limit(cfg, "deposit", None) == 5000
    assert cl.daily_limit(cfg, "deposit", 1000) == 1000
    assert cl.daily_limit(cfg, "withdrawal", None) == 0  # 0 = unlimited


def test_auto_approve_is_conservative():
    cfg = cl.normalize_config({"money": {"auto_approve": {"enabled": True, "deposit_max": 500, "min_prior_approved": 2}}})
    ok = dict(amount=300, prior_approved=2, has_verification_url=True)
    assert cl.decide_auto_approve(cfg, **ok)[0] is True
    assert cl.decide_auto_approve(cfg, **{**ok, "amount": 501})[0] is False
    assert cl.decide_auto_approve(cfg, **{**ok, "prior_approved": 1})[0] is False
    assert cl.decide_auto_approve(cfg, **{**ok, "has_verification_url": False})[0] is False
    assert cl.decide_auto_approve(cfg, **ok, is_banned=True)[0] is False
    assert cl.decide_auto_approve(cfg, **ok, is_frozen=True)[0] is False
    assert cl.decide_auto_approve(cfg, **ok, abuse_flagged=True)[0] is False
    assert cl.decide_auto_approve(cl.normalize_config({}), **ok)[0] is False  # off by default


def test_withdrawal_review_threshold():
    cfg = cl.normalize_config({"money": {"withdrawal_review_above": 3000}})
    assert cl.withdrawal_needs_senior(cfg, 3001) is True
    assert cl.withdrawal_needs_senior(cfg, -3001) is True   # held withdrawals are stored negative
    assert cl.withdrawal_needs_senior(cfg, 3000) is False
    assert cl.withdrawal_needs_senior(cl.normalize_config({}), 10**7) is False


def test_role_permissions():
    assert cl.permissions_for("super") == frozenset(cl.PERMISSIONS)
    assert cl.permissions_for("support", is_super=True) == frozenset(cl.PERMISSIONS)
    assert "money.approve" in cl.permissions_for("finance")
    assert "control.publish" not in cl.permissions_for("finance")
    assert "players.ban" in cl.permissions_for("support")
    assert "money.approve" not in cl.permissions_for("support")
    assert "control.publish" in cl.permissions_for("manager")
    assert "games.manage" in cl.permissions_for("manager")
    assert "games.manage" not in cl.permissions_for("support")
    assert "games.manage" in cl.permissions_for("super")
    # legacy admins keep what they had: approvals + view, plus drafting - but cannot publish or ban
    legacy = cl.permissions_for("admin")
    assert {"money.approve", "players.view", "control.edit"} <= legacy
    assert not ({"control.publish", "players.ban", "staff.manage"} & legacy)
    # extra grants add to a role; unknown strings are ignored
    assert "players.ban" in cl.permissions_for("finance", ["players.ban", "bogus"])
    assert cl.permissions_for(None) == cl.permissions_for("admin")
    with pytest.raises(ValueError):
        cl.validate_extra_permissions(["bogus"])


def test_public_view_hides_rules_and_expired_announcements():
    cfg = cl.normalize_config({
        "money": {"daily_deposit_limit": 1, "auto_approve": {"enabled": True, "deposit_max": 5}},
        "content": {
            "announcement": {"enabled": True, "body": "Hi", "ends_at": "2026-01-01T00:00:00+00:00"},
            "banner": {"enabled": True, "title": "Bonus!", "cta_label": "Go"},
            "texts": {"deposit_intro": "Send to the number below."},
        },
        "flags": {"bonus_center": False},
    })
    view = cl.public_view(cfg, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert view["announcement"] is None                      # ended
    assert view["banner"]["title"] == "Bonus!"
    assert view["texts"] == {"deposit_intro": "Send to the number below."}
    assert view["flags"]["bonus_center"] is False and "bonus_center" in view["flag_messages"]
    assert "money" not in view and "settings" not in view
    live = cl.public_view(cfg, now=datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert live["announcement"]["body"] == "Hi"
