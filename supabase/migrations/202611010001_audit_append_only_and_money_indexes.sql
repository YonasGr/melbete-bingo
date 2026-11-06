-- Audit log becomes append-only, and the finance export / daily reconciliation queries get an index.

-- 1. admin_audit_log: no UPDATE, DELETE or TRUNCATE from the application role. This protects against bugs and a
--    compromised app credential; a database owner can still drop the trigger, which is itself a visible schema change.
CREATE OR REPLACE FUNCTION admin_audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'admin_audit_log is append-only (% is not allowed)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_admin_audit_append_only ON admin_audit_log;
CREATE TRIGGER trg_admin_audit_append_only
    BEFORE UPDATE OR DELETE ON admin_audit_log
    FOR EACH ROW EXECUTE FUNCTION admin_audit_log_append_only();

DROP TRIGGER IF EXISTS trg_admin_audit_no_truncate ON admin_audit_log;
CREATE TRIGGER trg_admin_audit_no_truncate
    BEFORE TRUNCATE ON admin_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION admin_audit_log_append_only();

-- 2. TransactionRepository.list_for_export() / daily_money_summary() filter by type and a created_at/resolved_at range.
CREATE INDEX IF NOT EXISTS idx_transactions_type_created ON transactions (type, created_at DESC);
