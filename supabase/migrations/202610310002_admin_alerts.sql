-- Per-admin Telegram alert subscriptions. NULL = use the role's defaults.
ALTER TABLE admins ADD COLUMN IF NOT EXISTS alert_types JSONB;
