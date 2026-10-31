from datetime import date, timedelta

from src.services.progress import level_for, streak_calendar


def test_levels():
    assert level_for(0)["name"] == "Bronze" and level_for(9)["name"] == "Bronze"
    assert level_for(10)["name"] == "Silver" and level_for(299)["name"] == "Diamond"
    top = level_for(300)
    assert top["name"] == "Crown" and top["next_at"] is None


def test_streak_calendar():
    today = date(2026, 10, 24)
    c = streak_calendar(3, today, today)
    assert [d["played"] for d in c["calendar"]] == [False] * 4 + [True] * 3 and c["days"] == 3
    assert streak_calendar(3, today - timedelta(days=1), today)["calendar"][-1]["played"] is False   # still alive, not played today
    assert streak_calendar(3, today - timedelta(days=1), today)["days"] == 3
    assert streak_calendar(5, today - timedelta(days=3), today)["days"] == 0                           # lapsed
    assert streak_calendar(0, None, today)["days"] == 0
