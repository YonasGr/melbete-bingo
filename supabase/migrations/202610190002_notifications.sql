-- Player notifications: every event message a player gets (deposit approved, win, ...) is stored
-- here. The same rows are the in-app inbox (bell icon) and the record of the Telegram delivery.
-- dedupe_key makes one event produce one notification even if the code path runs twice.

CREATE TABLE IF NOT EXISTS notifications (
    id               TEXT PRIMARY KEY,
    player_id        TEXT NOT NULL,
    type             TEXT NOT NULL,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    data             JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key       TEXT,
    telegram_status  TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | blocked | skipped
    read_at          TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notifications_player ON notifications (player_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications (player_id) WHERE read_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_dedupe
    ON notifications (player_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
