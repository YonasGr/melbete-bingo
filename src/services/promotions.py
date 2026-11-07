"""Timed promotions (Control Center -> Promotions): a multiplier on cashback for deposits made inside a window.

A promotion matches a deposit when it is enabled, the deposit's Ethiopia-time weekday is ticked, the date is inside
the optional start/end dates and the time of day is inside the optional window. The best matching multiplier wins
(promotions never stack)."""
from datetime import date, datetime, time


def _hm(value: str, default: time) -> time:
    try:
        h, m = str(value).split(":")
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return default


def promo_matches(promo: dict, local_dt: datetime) -> bool:
    if not promo.get("enabled") or "cashback" not in (promo.get("applies") or ["cashback"]):
        return False
    days = promo.get("days") or [True] * 7
    if len(days) == 7 and not days[local_dt.weekday()]:
        return False
    d = local_dt.date()
    if promo.get("start_date") and d < date.fromisoformat(promo["start_date"]):
        return False
    if promo.get("end_date") and d > date.fromisoformat(promo["end_date"]):
        return False
    start, end = _hm(promo.get("start_time", "00:00"), time(0, 0)), _hm(promo.get("end_time", "00:00"), time(0, 0))
    if start == end:                      # no window = all day
        return True
    t = local_dt.time()
    return (start <= t < end) if start < end else (t >= start or t < end)   # overnight windows wrap past midnight


def multiplier_at(promos: list, local_dt: datetime) -> float:
    best = 1.0
    for p in promos or []:
        if promo_matches(p, local_dt):
            best = max(best, float(p.get("multiplier", 1.0)))
    return best


def weighted_deposits(rows, promos: list) -> dict:
    """rows: [(local_datetime, amount)] -> {local_date: boosted deposit total (whole ETB, rounded down)}."""
    out: dict = {}
    for local_dt, amount in rows:
        out[local_dt.date()] = out.get(local_dt.date(), 0.0) + float(amount) * multiplier_at(promos, local_dt)
    return {d: int(total) for d, total in out.items()}


def active_now(promos: list, local_dt: datetime) -> list:
    return [p for p in promos or [] if promo_matches(p, local_dt)]
