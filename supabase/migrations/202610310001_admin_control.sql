-- Admin control centre: draft/publish config, audit log, staff roles, player controls.
-- Idempotent: safe to run more than once.

-- 1. Draft / publish configuration (singleton row id = 'global')
CREATE TABLE IF NOT EXISTS control_config (
    id               TEXT PRIMARY KEY DEFAULT 'global',
    published        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- what players/services see
    previous         JSONB,                                -- the config before the last publish (one-step rollback)
    draft            JSONB,                                -- NULL = no unpublished changes
    version          INTEGER NOT NULL DEFAULT 0,
    published_by     TEXT,
    published_at     TIMESTAMPTZ,
    draft_by         TEXT,
    draft_updated_at TIMESTAMPTZ
);
INSERT INTO control_config (id) VALUES ('global') ON CONFLICT (id) DO NOTHING;

-- 2. Audit log: every admin action, who / what / when
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id      TEXT NOT NULL,            -- admin telegram_id ('system' for automatic actions)
    actor_role    TEXT,
    action        TEXT NOT NULL,            -- e.g. control.publish, player.ban, deposit.approve
    target_type   TEXT,                     -- player | transaction | admin | config | ...
    target_id     TEXT,
    summary       TEXT,
    before_json   JSONB,
    after_json    JSONB,
    ip            TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor   ON admin_audit_log (actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target  ON admin_audit_log (target_type, target_id, created_at DESC);

-- 3. Staff roles + per-admin extra permissions
ALTER TABLE admins ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'admin';
ALTER TABLE admins ADD COLUMN IF NOT EXISTS extra_permissions JSONB NOT NULL DEFAULT '[]'::jsonb;
UPDATE admins SET role = 'super' WHERE is_super_admin AND role = 'admin';

-- 4. Player controls: freeze, personal daily limits, staff notes
ALTER TABLE players ADD COLUMN IF NOT EXISTS is_frozen BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS frozen_reason TEXT;
ALTER TABLE players ADD COLUMN IF NOT EXISTS daily_deposit_limit INTEGER;      -- NULL = platform default
ALTER TABLE players ADD COLUMN IF NOT EXISTS daily_withdrawal_limit INTEGER;   -- NULL = platform default

CREATE TABLE IF NOT EXISTS player_notes (
    id          TEXT PRIMARY KEY,
    player_id   TEXT NOT NULL,
    admin_id    TEXT NOT NULL,
    note        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_player_notes_player ON player_notes (player_id, created_at DESC);
