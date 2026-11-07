from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.services.control_logic import normalize_config, normalize_promotions, public_view
from src.services.payout_ratio import effective_payout_ratio
from src.services.promotions import multiplier_at, weighted_deposits

FRIDAY_EVENING = datetime(2026, 11, 6, 19, 30)   # a Friday
MONDAY_NOON = datetime(2026, 11, 2, 12, 0)

FRIDAY_2X = {"name": "Friday x2", "enabled": True, "multiplier": 2.0, "days": [False, False, False, False, True, False, False],
             "start_date": "", "end_date": "", "start_time": "00:00", "end_time": "00:00"}


def test_weekday_promotion_boosts_only_matching_days():
    assert multiplier_at([FRIDAY_2X], FRIDAY_EVENING) == 2.0
    assert multiplier_at([FRIDAY_2X], MONDAY_NOON) == 1.0
    assert multiplier_at([{**FRIDAY_2X, "enabled": False}], FRIDAY_EVENING) == 1.0


def test_time_window_and_overnight_window():
    evening = {**FRIDAY_2X, "days": [True] * 7, "start_time": "18:00", "end_time": "22:00"}
    assert multiplier_at([evening], FRIDAY_EVENING) == 2.0 and multiplier_at([evening], MONDAY_NOON) == 1.0
    night = {**evening, "start_time": "22:00", "end_time": "02:00"}
    assert multiplier_at([night], datetime(2026, 11, 6, 23, 0)) == 2.0 and multiplier_at([night], datetime(2026, 11, 7, 1, 0)) == 2.0
    assert multiplier_at([night], datetime(2026, 11, 7, 12, 0)) == 1.0


def test_date_range_and_best_multiplier_wins():
    ranged = {**FRIDAY_2X, "days": [True] * 7, "start_date": "2026-11-05", "end_date": "2026-11-06", "multiplier": 3.0}
    assert multiplier_at([ranged, FRIDAY_2X], FRIDAY_EVENING) == 3.0
    assert multiplier_at([ranged], datetime(2026, 11, 7, 12, 0)) == 1.0


def test_weighted_deposits_boosts_each_deposit_by_its_own_time():
    rows = [(FRIDAY_EVENING, 100), (MONDAY_NOON, 100), (datetime(2026, 11, 6, 8, 0), 50)]
    assert weighted_deposits(rows, [FRIDAY_2X]) == {date(2026, 11, 6): 300, date(2026, 11, 2): 100}
    assert weighted_deposits(rows, []) == {date(2026, 11, 6): 150, date(2026, 11, 2): 100}


def test_promotions_are_validated_and_shown_only_while_active():
    cfg = normalize_config({"promotions": [FRIDAY_2X]})
    assert cfg["promotions"][0]["multiplier"] == 2.0 and cfg["promotions"][0]["applies"] == ["cashback"]
    for bad in ({**FRIDAY_2X, "multiplier": 9}, {**FRIDAY_2X, "name": ""}, {**FRIDAY_2X, "days": [False] * 7},
                {**FRIDAY_2X, "start_date": "2026-12-01", "end_date": "2026-11-01"}, {**FRIDAY_2X, "start_time": "25:00"}):
        with pytest.raises(ValueError):
            normalize_promotions([bad])
    live = public_view(cfg, datetime(2026, 11, 6, 17, 0, tzinfo=__import__("datetime").timezone.utc))   # 20:00 in Ethiopia, Friday
    quiet = public_view(cfg, datetime(2026, 11, 2, 9, 0, tzinfo=__import__("datetime").timezone.utc))
    assert [p["multiplier"] for p in live["promotions"]] == [2.0] and quiet["promotions"] == []


def test_room_uses_its_own_payout_only_when_switched_on():
    s = SimpleNamespace(payout_ratio=0.8, payout_custom_real_50=True, payout_ratio_real_50=0.9,
                        payout_custom_real_10=False, payout_ratio_real_10=0.7)
    assert effective_payout_ratio(s, "real_50") == 0.9
    assert effective_payout_ratio(s, "real_10") == 0.8
    assert effective_payout_ratio(s, None) == 0.8
