from datetime import datetime

from src.services.reconciliation_report import EAT, format_report, persistent, seconds_until


def test_seconds_until_rolls_to_tomorrow():
    now = datetime(2026, 10, 24, 2, 0, tzinfo=EAT)
    assert seconds_until(3, now) == 3600
    assert seconds_until(3, datetime(2026, 10, 24, 4, 0, tzinfo=EAT)) == 23 * 3600


def test_persistent_drops_transient_findings():
    a = [{"player_id": "x"}, {"player_id": "y"}]
    assert persistent(a, [{"player_id": "y"}]) == [{"player_id": "y"}]


def _base():
    return {"negative": [], "won_over_balance": [], "deposit_ledger": [],
            "totals": {"real_total": 100, "withdrawable": 40, "bonus_total": 5, "wallets": 3},
            "pending": {"topups": 1, "withdrawals": 2, "withdrawal_amount": 300, "stale": 0}}


def test_report_ok_and_alert():
    text, bad = format_report(_base(), "24 Oct 2026")
    assert not bad and text.splitlines()[1].startswith("OK")
    r = _base(); r["negative"] = [{"player_id": "tg_1", "balance": -5, "won_balance": 0, "bonus_balance": 0}]
    text, bad = format_report(r, "24 Oct 2026")
    assert bad and "NEGATIVE tg_1" in text
