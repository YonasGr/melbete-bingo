"""Guard: every state-changing admin endpoint must write an audit row.

A new POST/PUT/DELETE in src/api/routers/admin.py that forgets `_audit(...)` fails this test, so the audit log
cannot silently develop gaps again. (control.py endpoints audit inside ControlService.)"""
import ast
from pathlib import Path

ADMIN_ROUTER = Path(__file__).resolve().parents[1] / "api" / "routers" / "admin.py"

# Deliberately not audited: logging out changes nothing; statement matching only reads and reports.
NOT_AUDITED = {"admin_logout", "admin_reconcile_deposits"}


def _mutating_routes():
    for node in ast.walk(ast.parse(ADMIN_ROUTER.read_text())):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            verbs = [d.func.attr for d in node.decorator_list
                     if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("post", "put", "delete", "patch")]
            if verbs:
                yield node.name, ast.unparse(node)


def test_every_mutating_admin_endpoint_is_audited():
    missing = sorted(name for name, src in _mutating_routes() if name not in NOT_AUDITED and "_audit(" not in src)
    assert not missing, f"admin endpoints that change state but write no audit row: {missing}"


def test_allowlist_has_no_stale_names():
    names = {name for name, _ in _mutating_routes()}
    assert NOT_AUDITED <= names


def test_money_decisions_and_exports_are_audited():
    sources = dict(_mutating_routes())
    for name in ("admin_approve_deposit", "admin_reject_deposit", "admin_approve_withdrawal", "admin_reject_withdrawal",
                 "admin_approve_matched_deposits", "admin_adjust_agent_float", "admin_reset_user_balance"):
        assert "_audit(" in sources[name], name
    text = ADMIN_ROUTER.read_text()
    assert 'f"export.{kind}s"' in text   # CSV exports record who pulled player and payout data
