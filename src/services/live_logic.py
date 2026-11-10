"""Pure helpers for the live admin dashboard (no database, no framework)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def day_windows(now_utc: datetime, offset_hours: int = 3) -> dict:
    """UTC instants for 'today' and 'yesterday' as the operator sees them (local midnight)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc + timedelta(hours=offset_hours)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = local_midnight - timedelta(hours=offset_hours)
    return {
        "now": now_utc,
        "today_start": today,
        "yesterday_start": today - timedelta(days=1),
        "local_date": local.date(),
    }


def ggr(stakes: int, payouts: int, refunds: int = 0) -> int:
    """Platform revenue: stakes kept after paying winners and refunds."""
    return int(stakes) - int(payouts) - int(refunds)


def change_pct(current: float, previous: float) -> Optional[float]:
    """% change vs the previous period; None when there is nothing to compare with."""
    if not previous:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def age_minutes(then: Optional[datetime], now: datetime) -> Optional[int]:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((now - then).total_seconds() // 60))


def build_series(rows_stakes: list[dict], rows_deposits: list[dict], local_date, days: int = 7) -> list[dict]:
    """One entry per local day for the last `days` days (oldest first), zero-filled."""
    by_day: dict = {}
    for r in rows_stakes:
        d = by_day.setdefault(r["day"], {"stakes": 0, "payouts": 0, "refunds": 0, "deposits": 0})
        d["stakes"] = int(r.get("stakes") or 0)
        d["payouts"] = int(r.get("payouts") or 0)
        d["refunds"] = int(r.get("refunds") or 0)
    for r in rows_deposits:
        by_day.setdefault(r["day"], {"stakes": 0, "payouts": 0, "refunds": 0, "deposits": 0})["deposits"] = int(r.get("deposits") or 0)
    out = []
    for i in range(days - 1, -1, -1):
        day = local_date - timedelta(days=i)
        d = by_day.get(day, {"stakes": 0, "payouts": 0, "refunds": 0, "deposits": 0})
        out.append({"day": day.isoformat(), "revenue": ggr(d["stakes"], d["payouts"], d["refunds"]), "deposits": d["deposits"], "stakes": d["stakes"]})
    return out


def build_alerts(live: dict, control_cfg: dict) -> list[dict]:
    """Things an operator should look at right now. Each: {level, text, tab?}. Worst first."""
    alerts: list[dict] = []
    ks = control_cfg.get("kill_switch", {})
    mt = control_cfg.get("maintenance", {})
    if ks.get("enabled"):
        alerts.append({"level": "critical", "text": "Kill switch is ON: deposits, withdrawals and joining rounds are stopped.", "tab": "control"})
    if mt.get("enabled"):
        alerts.append({"level": "critical", "text": "Maintenance mode is ON: players see the maintenance screen.", "tab": "control"})

    pend = live.get("pending", {})
    dep, wd = pend.get("deposit", {}), pend.get("withdrawal", {})
    if dep.get("count") and (dep.get("oldest_minutes") or 0) >= 30:
        alerts.append({"level": "warning", "text": f"{dep['count']} deposit(s) waiting; the oldest for {dep['oldest_minutes']} min.", "tab": "deposits"})
    if wd.get("count") and (wd.get("oldest_minutes") or 0) >= 60:
        alerts.append({"level": "warning", "text": f"{wd['count']} withdrawal(s) waiting; the oldest for {wd['oldest_minutes']} min.", "tab": "withdrawals"})
    elif wd.get("count"):
        alerts.append({"level": "info", "text": f"{wd['count']} withdrawal(s) to process ({int(wd.get('amount', 0)):,} ETB).", "tab": "withdrawals"})

    today = live.get("today", {})
    if today.get("stakes", 0) > 0 and today.get("revenue", 0) < 0:
        alerts.append({"level": "warning", "text": f"Today's rounds are paying out more than they take in ({int(today['revenue']):,} ETB).", "tab": "economy"})

    flags = control_cfg.get("flags", {})
    off = [name for name, on in flags.items() if not on]
    if off and not ks.get("enabled"):
        alerts.append({"level": "info", "text": "Switched off for players: " + ", ".join(off).replace("_", " ") + ".", "tab": "control"})
    if control_cfg.get("money", {}).get("auto_approve", {}).get("enabled"):
        alerts.append({"level": "info", "text": "Auto-approve is ON for small deposits from trusted players.", "tab": "control"})
    if live.get("unpublished_changes"):
        alerts.append({"level": "info", "text": f"{live['unpublished_changes']} Control Center change(s) saved but not published.", "tab": "control", "action": "publish"})

    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(alerts, key=lambda a: order[a["level"]])
