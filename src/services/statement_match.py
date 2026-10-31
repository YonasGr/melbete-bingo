"""Match a payment-provider statement (Telebirr / bank CSV export) against pending deposit requests.

Why: today an admin approves each deposit by eyeballing a screenshot. With a statement the system can
check each request against what actually arrived:

* matched         - the statement has a credit whose reference appears in the player's SMS AND the
                    amount is the same. Safe to approve in bulk.
* amount_mismatch - the reference is in the statement but for a DIFFERENT amount (player claimed more
                    than they paid). Never auto-approved.
* duplicate_claim - the same statement credit is already backing an older request.
* unmatched       - no statement credit carries a reference found in the player's SMS.
* unclaimed       - statement credits nobody has asked for yet (a player may still submit them).

Pure functions, no database: the admin endpoints load the pending deposits and call these.
"""
import csv
import io
import re
from typing import Optional

MIN_REF_LEN = 6        # shorter "references" are too likely to match by accident
MAX_ROWS = 5000

_REF_STRONG = re.compile(r"ref|receipt|reciept|transaction|txn", re.I)
_REF_WEAK = re.compile(r"(^|[^a-z])id($|[^a-z])", re.I)
_AMOUNT_PRIORITY = ("credit", "amount", "paid", "total")


class StatementError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _parse_amount(raw) -> Optional[int]:
    """Whole ETB of a credit. Strips thousands separators and currency text; debits/zero -> None."""
    s = re.sub(r"[^\d.\-]", "", str(raw or ""))
    if s in ("", "-", ".", "-."):
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return int(round(value)) if value > 0 else None


def _pick(headers: list[str], override: Optional[str], kind: str) -> str:
    if override:
        for h in headers:
            if h.strip().lower() == str(override).strip().lower():
                return h
        raise StatementError(f"There is no column called '{override}'. Columns found: {', '.join(headers)}")
    if kind == "ref":
        for rx in (_REF_STRONG, _REF_WEAK):
            for h in headers:
                if rx.search(h):
                    return h
    else:
        for word in _AMOUNT_PRIORITY:
            for h in headers:
                if word in h.lower():
                    return h
    raise StatementError(
        f"Could not find the {'reference' if kind == 'ref' else 'amount'} column automatically. "
        f"Columns found: {', '.join(headers)}. Type the right column name in the box."
    )


def parse_statement(text: str, ref_column: Optional[str] = None, amount_column: Optional[str] = None) -> dict:
    text = (text or "").strip("\ufeff \r\n\t")
    if not text:
        raise StatementError("The statement is empty")
    first = text.splitlines()[0]
    delimiter = max(",;\t|", key=first.count) if any(d in first for d in ",;\t|") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
    if len(headers) < 2:
        raise StatementError("Could not read columns. Paste the CSV including its header row.")
    ref_col = _pick(headers, ref_column, "ref")
    amt_col = _pick(headers, amount_column, "amount")
    rows, skipped = [], 0
    for line_no, row in enumerate(reader, start=2):
        if len(rows) + skipped >= MAX_ROWS:
            raise StatementError(f"Too many rows (limit {MAX_ROWS}). Split the statement by date.")
        ref = str(row.get(ref_col) or "").strip()
        amount = _parse_amount(row.get(amt_col))
        if not ref or amount is None:
            skipped += 1            # debits, totals, blank lines
            continue
        rows.append({"ref": ref, "amount": amount, "line": line_no})
    return {"rows": rows, "ref_column": ref_col, "amount_column": amt_col, "skipped": skipped}


def _contains_ref(text: str, ref: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(ref.upper())}(?![A-Z0-9])", text.upper()) is not None


def match_deposits(rows: list[dict], deposits: list[dict]) -> dict:
    """`deposits` must be oldest-first: when two requests claim the same credit the older one wins.
    Each deposit is {"id", "player_id", "amount", "text"} where text = SMS + link + note."""
    usable = [r for r in rows if len(r["ref"]) >= MIN_REF_LEN]
    used: dict[int, str] = {}        # statement line -> deposit id that claimed it
    referenced: set[int] = set()
    out = {"matched": [], "amount_mismatch": [], "duplicate_claim": [], "unmatched": [], "unclaimed": []}
    for d in deposits:
        text = d.get("text") or ""
        hits = [r for r in usable if _contains_ref(text, r["ref"])]
        base = {"id": d["id"], "player_id": d["player_id"], "amount": d["amount"]}
        if not hits:
            out["unmatched"].append(base)
            continue
        referenced.update(r["line"] for r in hits)
        exact = [r for r in hits if r["amount"] == d["amount"]]
        if not exact:
            out["amount_mismatch"].append({**base, "statement_amount": hits[0]["amount"], "ref": hits[0]["ref"]})
            continue
        free = [r for r in exact if r["line"] not in used]
        if not free:
            out["duplicate_claim"].append({**base, "ref": exact[0]["ref"], "already_used_by": used[exact[0]["line"]]})
            continue
        used[free[0]["line"]] = d["id"]
        out["matched"].append({**base, "ref": free[0]["ref"]})
    out["unclaimed"] = [{"ref": r["ref"], "amount": r["amount"]} for r in usable if r["line"] not in referenced][:200]
    return out
