"""Pure tests for the live dashboard logic."""
from datetime import date, datetime, timedelta, timezone

from src.services import control_logic as cl
from src.services import live_logic as ll


def test_day_windows_use_local_midnight():
    # 22:30 UTC on 30 Oct is 01:30 on 31 Oct in Ethiopia (UTC+3): "today" began 21:00 UTC on the 30th
    w = ll.day_windows(datetime(2026, 10, 30, 22, 30, tzinfo=timezone.utc), 3)
    assert w["today_start"] == datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
    assert w["yesterday_start"] == datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc)
    assert w["local_date"] == date(2026, 10, 31)
    # 10:00 UTC the same day is 13:00 local: same local date, window starts the previous 21:00 UTC
    w2 = ll.day_windows(datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc), 3)
    assert w2["today_start"] == datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)


def test_revenue_and_change():
    assert ll.ggr(1000, 800, 50) == 150
    assert ll.change_pct(150, 100) == 50.0
    assert ll.change_pct(50, 100) == -50.0
    assert ll.change_pct(10, 0) is None


def test_age_minutes():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    assert ll.age_minutes(now - timedelta(minutes=45), now) == 45
    assert ll.age_minutes(None, now) is None
    assert ll.age_minutes(now + timedelta(minutes=5), now) == 0


def test_series_is_zero_filled_oldest_first():
    today = date(2026, 10, 31)
    stakes = [{"day": date(2026, 10, 31), "stakes": 1000, "payouts": 800, "refunds": 0},
              {"day": date(2026, 10, 29), "stakes": 500, "payouts": 400, "refunds": 50}]
    deposits = [{"day": date(2026, 10, 30), "deposits": 700}]
    s = ll.build_series(stakes, deposits, today, 3)
    assert [x["day"] for x in s] == ["2026-10-29", "2026-10-30", "2026-10-31"]
    assert [x["revenue"] for x in s] == [50, 0, 200]
    assert [x["deposits"] for x in s] == [0, 700, 0]


def _live(**kw):
    base = {"pending": {"deposit": {"count": 0}, "withdrawal": {"count": 0}}, "today": {"stakes": 0, "revenue": 0}}
    base.update(kw)
    return base


def test_alerts_are_ordered_worst_first_and_quiet_when_fine():
    quiet = ll.build_alerts(_live(), cl.normalize_config({}))
    assert quiet == []
    cfg = cl.normalize_config({"kill_switch": {"enabled": True}, "flags": {"bonus_center": False}})
    live = _live(pending={"deposit": {"count": 2, "oldest_minutes": 40}, "withdrawal": {"count": 1, "oldest_minutes": 5, "amount": 900}},
                 today={"stakes": 1000, "revenue": -200}, unpublished_changes=3)
    alerts = ll.build_alerts(live, cfg)
    levels = [a["level"] for a in alerts]
    assert levels == sorted(levels, key=lambda l: {"critical": 0, "warning": 1, "info": 2}[l])
    texts = " ".join(a["text"] for a in alerts)
    assert "Kill switch" in texts and "deposit(s) waiting" in texts and "more than they take in" in texts and "not published" in texts
    assert "Switched off" not in texts          # the kill switch message already covers it


def test_alert_types_defaults_and_custom_choice():
    assert cl.effective_alert_types("super") == frozenset(cl.ALERT_TYPES)
    assert cl.effective_alert_types("finance") >= {"big_withdrawal", "fraud_flag"}
    assert "emergency" not in cl.effective_alert_types("support")
    assert cl.effective_alert_types("support", ["emergency"]) == frozenset({"emergency"})   # own choice replaces defaults
    assert cl.effective_alert_types("finance", []) == frozenset()                           # opted out of everything
    assert cl.effective_alert_types("nonsense") == cl.effective_alert_types("admin")
    assert cl.validate_alert_types(None) is None and cl.validate_alert_types(["emergency", "emergency"]) == ["emergency"]


def test_alert_config_bounds_and_messages():
    import pytest
    cfg = cl.normalize_config({"alerts": {"big_withdrawal_amount": 100, "failed_logins_before_alert": 5}})
    assert cfg["alerts"]["big_withdrawal_amount"] == 100 and cfg["alerts"]["enabled"] is True
    with pytest.raises(ValueError):
        cl.normalize_config({"alerts": {"failed_logins_before_alert": 0}})
    msg = cl.format_alert("big_withdrawal", {"amount": 12500, "player_name": "Abebe *x*", "method": "Telebirr", "threshold": 5000})
    assert "12,500 ETB" in msg and "Abebe *x*" in msg and "5,000 ETB" in msg   # plain text: no markdown to break
    assert "ON" in cl.format_alert("emergency", {"summary": "Kill switch ON", "actor": "1", "role": "manager"})
