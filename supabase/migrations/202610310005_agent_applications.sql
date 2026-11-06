-- Agent registration requests (a player applies from the Mini App, an admin approves or rejects) and a
-- per-agent switch for the reward programme. Idempotent.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS rewards_enabled BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS agent_applications (
    id           TEXT PRIMARY KEY,
    player_id    TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by  TEXT,
    review_note  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at  TIMESTAMPTZ
);
-- one open application per player
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_applications_pending ON agent_applications (player_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_agent_applications_status ON agent_applications (status, created_at DESC);
