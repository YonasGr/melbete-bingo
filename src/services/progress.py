"""Cosmetic player progress: level from rounds played, plus the 7-day play-streak calendar.
Pure functions, no I/O. Levels unlock card frames only - they carry no cash value."""
from datetime import date, timedelta
from typing import Optional

LEVELS = [("Bronze", 0), ("Silver", 10), ("Gold", 30), ("Platinum", 75), ("Diamond", 150), ("Crown", 300)]


def level_for(rounds: int) -> dict:
    rounds = max(0, int(rounds or 0))
    idx = max(i for i, (_, at) in enumerate(LEVELS) if rounds >= at)
    last = idx + 1 >= len(LEVELS)
    return {
        "index": idx, "name": LEVELS[idx][0], "rounds": rounds, "from": LEVELS[idx][1],
        "next_at": None if last else LEVELS[idx + 1][1], "next_name": None if last else LEVELS[idx + 1][0],
    }


def streak_calendar(streak_days: int, last_play_day: Optional[date], today: date, days: int = 7) -> dict:
    """The streak counts only if the last play was today or yesterday (EAT); otherwise it has lapsed."""
    active = bool(last_play_day) and int(streak_days or 0) > 0 and (today - last_play_day).days <= 1
    played = {last_play_day - timedelta(days=i) for i in range(int(streak_days))} if active else set()
    cal = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        cal.append({"date": d.isoformat(), "played": d in played})
    return {"days": int(streak_days) if active else 0, "calendar": cal}
