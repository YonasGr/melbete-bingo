"""Per-reward Start/Stop, timers, master pause (pure logic in control_logic)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.services import control_logic as cl


def cfg(**items):
    c = cl.normalize_config({})
    for k, v in items.items():
        c["rewards"]["items"][k].update(v)
    return cl.normalize_config(c)


def test_default_everything_runs():
    c = cl.normalize_config({})
    assert all(cl.reward_state(c, k)["state"] == "on" for k in cl.REWARDS)


def test_stop_new_vs_stop_all():
    c = cfg(welcome={"enabled": False, "mode": "new"}, agent_rewards={"enabled": False, "mode": "all"})
    w, a = cl.reward_state(c, "welcome"), cl.reward_state(c, "agent_rewards")
    assert w["new_blocked"] and not w["payout_blocked"]
    assert a["new_blocked"] and a["payout_blocked"]
    assert not cl.reward_state(c, "daily")["new_blocked"]          # others untouched


def test_master_pause_blocks_all_and_resume_restores_items():
    c = cfg(daily={"enabled": False})
    c["rewards"]["master"] = {"paused": True, "mode": "all", "reason": "audit"}
    c = cl.normalize_config(c)
    assert cl.reward_state(c, "welcome")["payout_blocked"] and cl.reward_state(c, "welcome")["message"] == "audit"
    c["rewards"]["master"]["paused"] = False
    c = cl.normalize_config(c)
    assert cl.reward_state(c, "welcome")["state"] == "on"
    assert cl.reward_state(c, "daily")["state"] == "stopped"       # its own switch is exactly as before


def test_timer_window_uses_ethiopia_time_when_no_offset():
    now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)         # 15:00 in Addis
    c = cfg(daily={"start_at": "2026-11-07T16:00:00", "stop_at": "2026-11-07T20:00:00"})
    assert cl.reward_state(c, "daily", now)["state"] == "scheduled"
    assert cl.reward_state(c, "daily", now + timedelta(hours=2))["state"] == "on"
    assert cl.reward_state(c, "daily", now + timedelta(hours=6))["state"] == "ended"


def test_validation():
    with pytest.raises(ValueError):
        cl.normalize_config({"rewards": {"items": {"nope": {}}}})
    with pytest.raises(ValueError):
        cfg(daily={"start_at": "2026-11-07T20:00:00+03:00", "stop_at": "2026-11-07T10:00:00+03:00"})
    with pytest.raises(ValueError):
        cfg(daily={"mode": "bogus"})


def test_weekly_window_runs_past_midnight_in_ethiopia_time():
    c = cfg(daily={"windows": [{"days": [False, False, False, False, True, False, False], "from": "22:00", "to": "02:00"}]})
    fri_night = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)       # 23:00 Fri in Addis
    sat_early = datetime(2026, 11, 6, 22, 30, tzinfo=timezone.utc)      # 01:30 Sat in Addis (still Friday's window)
    sat_noon = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
    assert cl.reward_state(c, "daily", fri_night)["state"] == "on"
    assert cl.reward_state(c, "daily", sat_early)["state"] == "on"
    assert cl.reward_state(c, "daily", sat_noon)["state"] == "off_window"


def test_new_item_fields_validate_and_default():
    c = cl.normalize_config({})
    it = c["rewards"]["items"]["welcome"]
    assert it["windows"] == [] and it["spike_limit"] == 0 and it["elig"] == {"new_players_days": 0, "min_deposits_etb": 0}
    with pytest.raises(ValueError):
        cfg(daily={"windows": [{"days": [True] * 7, "from": "25:00", "to": "02:00"}]})


def test_experience_defaults_and_validation():
    e = cl.normalize_config({})["experience"]
    assert e["tiers"]["big_win_etb"] == 500 and e["splash"]["seconds"] == 3 and e["voice"]["mode"] == "player"
    assert cl.public_view(cl.normalize_config({}))["experience"] == e
    with pytest.raises(ValueError):
        cl.normalize_config({"experience": {"tiers": {"big_win_etb": 900, "jackpot_etb": 100}}})
    with pytest.raises(ValueError):
        cl.normalize_config({"experience": {"countdown": {"warn_seconds": 3, "hot_seconds": 8}}})
    with pytest.raises(ValueError):
        cl.normalize_config({"experience": {"voice": {"mode": "loud"}}})


def test_public_view_lists_only_rewards_players_should_hear_about():
    c = cfg(daily={"enabled": False, "reason": "Back Monday"}, cashback={"stop_at": "2099-01-01T00:00:00+03:00"})
    pub = cl.public_view(c)["rewards"]
    assert pub["daily"]["state"] == "stopped" and pub["daily"]["message"] == "Back Monday"
    assert pub["cashback"]["state"] == "on" and pub["cashback"]["ends_at"]
    assert "welcome" not in pub
