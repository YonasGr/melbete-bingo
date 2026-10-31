"""Pure helpers for the daily wallet reconciliation (no I/O, unit-tested)."""
from datetime import datetime, timedelta, timezone
from typing import Optional

EAT = timezone(timedelta(hours=3))


def seconds_until(hour_eat: int, now: Optional[datetime] = None) -> float:
    """Seconds until the next `hour_eat`:00 Ethiopia time."""
    now = (now or datetime.now(EAT)).astimezone(EAT)
    target = now.replace(hour=hour_eat, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def persistent(first: list, second: list, key: str = "player_id") -> list:
    """Keep only findings present in both reads, so a transaction in flight never raises a false alarm."""
    seen = {r[key] for r in second}
    return [r for r in first if r[key] in seen]


def format_report(r: dict, today: str) -> tuple[str, bool]:
    """(message, has_problems). Critical = impossible balances; 'review' = ledger totals that disagree."""
    crit = r["negative"] + r["won_over_balance"]
    problems = bool(crit)
    t, p = r["totals"], r["pending"]
    lines = [
        f"Melbete Bingo daily reconciliation - {today}",
        "ALERT: balance problems found" if problems else "OK: no balance problems",
        f"Player money: {t['real_total']} ETB (withdrawable {t['withdrawable']}), bonus {t['bonus_total']}, wallets {t['wallets']}",
        f"Pending: {p['topups']} deposits, {p['withdrawals']} withdrawals ({p['withdrawal_amount']} ETB), {p['stale']} older than 24h",
    ]
    for row in r["negative"][:10]:
        lines.append(f"NEGATIVE {row['player_id']}: balance {row['balance']}, won {row['won_balance']}, bonus {row['bonus_balance']}")
    for row in r["won_over_balance"][:10]:
        lines.append(f"WON > BALANCE {row['player_id']}: won {row['won_balance']} > balance {row['balance']}")
    if r["deposit_ledger"]:
        lines.append(f"Review: {len(r['deposit_ledger'])} wallet(s) where lifetime deposits differ from approved top-ups:")
        for row in r["deposit_ledger"][:5]:
            lines.append(f"  {row['player_id']}: wallet {row['wallet_total']} vs ledger {row['ledger_total']}")
    return "\n".join(lines), problems
