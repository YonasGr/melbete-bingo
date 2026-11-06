"""Finance CSV export helpers: formula-injection safety, EAT dates, date-range limits."""
from datetime import date, datetime, timezone

import pytest

from src.services import finance_export as fx


def test_csv_safe_neutralises_formula_leaders():
    for evil in ('=HYPERLINK("http://x")', "+251911223344", "-1+1", "@SUM(A1)", "\tcmd", "\rcmd"):
        assert fx.csv_safe(evil).startswith("'")
    assert fx.csv_safe("Abebe Bekele") == "Abebe Bekele"
    assert fx.csv_safe("አበበ") == "አበበ"
    assert fx.csv_safe(None) == ""


def test_status_of_maps_transaction_types():
    assert fx.status_of("top_up_approved") == "approved"
    assert fx.status_of("withdrawal_rejected") == "rejected"
    assert fx.status_of("withdrawal_pending") == "pending"


def test_eat_iso_converts_utc_to_ethiopia_time():
    assert fx.eat_iso(datetime(2026, 10, 31, 21, 30, tzinfo=timezone.utc)) == "2026-11-01 00:30:00"
    assert fx.eat_iso(None) == ""


def test_parse_range_is_inclusive_in_eat():
    lo, hi, a, b = fx.parse_range("2026-10-01", "2026-10-31")
    assert (a, b) == ("2026-10-01", "2026-10-31")
    assert lo == datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)   # 00:00 EAT on the 1st
    assert hi == datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)   # 00:00 EAT on Nov 1 (exclusive)


def test_parse_range_defaults_to_last_30_days():
    _, _, a, b = fx.parse_range(None, None, today=date(2026, 11, 1))
    assert (a, b) == ("2026-10-03", "2026-11-01")


@pytest.mark.parametrize("start,end", [("nope", None), ("2026-10-05", "2026-10-01"), ("2024-01-01", "2026-01-01")])
def test_parse_range_rejects_bad_input(start, end):
    with pytest.raises(fx.ExportRangeError):
        fx.parse_range(start, end)


def test_build_csv_has_bom_header_and_safe_cells():
    row = {"id": "t1", "created_at": datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc), "resolved_at": None,
           "status": "approved", "player_id": "tg_1", "player_name": "=cmd|' /C calc'!A0", "amount": 500,
           "payment_method": "telebirr", "payment_verification_url": "", "reviewed_by": "123", "note": None}
    text = fx.build_csv([row], "deposit")
    assert text.startswith("\ufeffTransaction ID,Requested (EAT)")
    line = text.splitlines()[1]
    assert "2026-11-01 00:00:00" in line
    assert "'=cmd" in line and ",500," in line


def test_build_csv_withdrawal_columns_include_payout_account():
    text = fx.build_csv([], "withdrawal")
    assert "Payout account" in text and "Account holder" in text
    assert "Receipt link" not in text
