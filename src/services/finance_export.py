"""Pure helpers for the finance CSV export (no I/O, unit-tested).

Player names, payout accounts and admin notes are user-supplied, and finance staff open these files
in Excel / Sheets - so every text cell is neutralised against formula injection (CSV injection)."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

EAT = timezone(timedelta(hours=3))
MAX_EXPORT_DAYS = 366
MAX_EXPORT_ROWS = 50_000

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

DEPOSIT_COLUMNS = [
    ("id", "Transaction ID"), ("created_at", "Requested (EAT)"), ("resolved_at", "Resolved (EAT)"),
    ("status", "Status"), ("player_id", "Player ID"), ("player_name", "Player name"),
    ("amount", "Amount (ETB)"), ("payment_method", "Method"), ("payment_verification_url", "Receipt link"),
    ("reviewed_by", "Reviewed by"), ("note", "Note"),
]
WITHDRAWAL_COLUMNS = [
    ("id", "Transaction ID"), ("created_at", "Requested (EAT)"), ("resolved_at", "Resolved (EAT)"),
    ("status", "Status"), ("player_id", "Player ID"), ("player_name", "Player name"),
    ("amount", "Amount (ETB)"), ("payment_method", "Method"), ("legal_name", "Account holder"),
    ("withdrawal_address", "Payout account"), ("reviewed_by", "Reviewed by"), ("note", "Note"),
]


class ExportRangeError(ValueError):
    """The requested date range is malformed or too large."""


def csv_safe(value) -> str:
    """Text for one CSV cell. A leading = + - @ (or tab/CR) would be run as a formula by spreadsheets, so
    it gets a leading apostrophe. Numbers are written as numbers by the caller, never through here."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_LEADERS) else text


def eat_iso(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S")


def status_of(txn_type: str) -> str:
    if txn_type.endswith("_approved"):
        return "approved"
    if txn_type.endswith("_rejected"):
        return "rejected"
    return "pending"


def parse_range(start: Optional[str], end: Optional[str], today: Optional[date] = None) -> tuple[datetime, datetime, str, str]:
    """(start_utc, end_utc_exclusive, start_label, end_label) for an inclusive EAT date range.
    Defaults to the last 30 days. Raises ExportRangeError on bad input or a range over MAX_EXPORT_DAYS."""
    today = today or datetime.now(EAT).date()
    try:
        d_end = date.fromisoformat(end) if end else today
        d_start = date.fromisoformat(start) if start else d_end - timedelta(days=29)
    except ValueError:
        raise ExportRangeError("Dates must be YYYY-MM-DD")
    if d_start > d_end:
        raise ExportRangeError("Start date is after end date")
    if (d_end - d_start).days + 1 > MAX_EXPORT_DAYS:
        raise ExportRangeError(f"Choose a range of at most {MAX_EXPORT_DAYS} days")
    lo = datetime(d_start.year, d_start.month, d_start.day, tzinfo=EAT).astimezone(timezone.utc)
    hi = (datetime(d_end.year, d_end.month, d_end.day, tzinfo=EAT) + timedelta(days=1)).astimezone(timezone.utc)
    return lo, hi, d_start.isoformat(), d_end.isoformat()


def build_csv(rows: Iterable[dict], kind: str) -> str:
    """CSV text (UTF-8 BOM first so Excel reads Amharic names correctly) for 'deposit' or 'withdrawal' rows
    shaped like the admin history endpoints return them (amount already positive)."""
    columns = DEPOSIT_COLUMNS if kind == "deposit" else WITHDRAWAL_COLUMNS
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([label for _, label in columns])
    for r in rows:
        line = []
        for key, _ in columns:
            v = r.get(key)
            if key in ("created_at", "resolved_at"):
                line.append(eat_iso(v))
            elif key == "amount":
                line.append(int(v or 0))
            else:
                line.append(csv_safe(v))
        writer.writerow(line)
    return out.getvalue()
