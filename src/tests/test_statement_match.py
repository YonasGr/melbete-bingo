"""Statement matching for deposit approval: pure functions, no DB."""
import pytest

from src.services.statement_match import StatementError, match_deposits, parse_statement

CSV = """Date,Receipt No,Details,Credit Amount,Debit
2026-10-20,CJ12ABC3DE,From Abebe,"1,000.00",
2026-10-20,CJ99XYZ8QQ,From Sara,500.00,
2026-10-21,CJ55LMN2PP,From Kebede,250,
2026-10-21,TOTAL,,,
2026-10-21,CJ00OUT111,Payment,,300.00
"""


def _dep(i, amount, text):
    return {"id": i, "player_id": f"tg_{i}", "amount": amount, "text": text}


def test_parse_finds_columns_and_skips_debits_and_totals():
    p = parse_statement(CSV)
    assert (p["ref_column"], p["amount_column"]) == ("Receipt No", "Credit Amount")
    assert [(r["ref"], r["amount"]) for r in p["rows"]] == [("CJ12ABC3DE", 1000), ("CJ99XYZ8QQ", 500), ("CJ55LMN2PP", 250)]
    assert p["skipped"] == 2


def test_parse_supports_semicolons_overrides_and_errors():
    p = parse_statement("ref;amt\nAB1234CD;10,5\n", ref_column="ref", amount_column="amt")
    assert p["rows"][0]["amount"] == 105   # commas are thousands separators; decimal-comma exports are not supported
    with pytest.raises(StatementError):
        parse_statement("")
    with pytest.raises(StatementError):
        parse_statement("a,b\n1,2\n")                       # no recognisable columns
    with pytest.raises(StatementError):
        parse_statement(CSV, ref_column="Nope")


def test_match_buckets():
    rows = parse_statement(CSV)["rows"]
    deposits = [
        _dep("d1", 1000, "Dear customer you received 1,000 ETB ref CJ12ABC3DE thanks"),   # matched
        _dep("d2", 900, "txn CJ99XYZ8QQ paid"),                                            # claims 900, paid 500
        _dep("d3", 1000, "again CJ12ABC3DE"),                                              # same credit as d1
        _dep("d4", 300, "no reference here"),                                              # unmatched
    ]
    r = match_deposits(rows, deposits)
    assert [m["id"] for m in r["matched"]] == ["d1"]
    assert r["amount_mismatch"][0]["id"] == "d2" and r["amount_mismatch"][0]["statement_amount"] == 500
    assert r["duplicate_claim"][0]["id"] == "d3" and r["duplicate_claim"][0]["already_used_by"] == "d1"
    assert [u["id"] for u in r["unmatched"]] == ["d4"]
    assert [u["ref"] for u in r["unclaimed"]] == ["CJ55LMN2PP"]   # money arrived, nobody claimed it yet


def test_reference_must_be_a_whole_token_and_long_enough():
    rows = [{"ref": "CJ12ABC3DE", "amount": 100, "line": 2}, {"ref": "123", "amount": 100, "line": 3}]
    assert match_deposits(rows, [_dep("d", 100, "xCJ12ABC3DEx")])["unmatched"]       # glued to other letters
    assert match_deposits(rows, [_dep("d", 100, "ref 123 paid")])["unmatched"]       # too short to trust
    assert match_deposits(rows, [_dep("d", 100, "ref cj12abc3de paid")])["matched"]  # case-insensitive
