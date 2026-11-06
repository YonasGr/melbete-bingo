-- Admin-controlled leaderboard campaigns: the admin picks the start/end date-time (in a chosen timezone),
-- budget, winner count, points per cartela, wagering multiplier and an optional Telegram-channel reward.
-- Ending a campaign never pays anything: the admin approves or rejects the distribution. Idempotent.

CREATE TABLE IF NOT EXISTS leaderboard_campaigns (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    timezone             TEXT NOT NULL DEFAULT 'Africa/Addis_Ababa',
    starts_at            TIMESTAMPTZ NOT NULL,
    ends_at              TIMESTAMPTZ NOT NULL,
    budget_etb           INTEGER NOT NULL CHECK (budget_etb >= 1),
    winners_count        INTEGER NOT NULL CHECK (winners_count BETWEEN 1 AND 10000),
    points_per_cartela   INTEGER NOT NULL CHECK (points_per_cartela BETWEEN 1 AND 1000),
    wagering_multiplier  INTEGER NOT NULL DEFAULT 37 CHECK (wagering_multiplier BETWEEN 0 AND 1000),
    channel_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
    channel_username     TEXT NOT NULL DEFAULT '',
    channel_points       INTEGER NOT NULL DEFAULT 50 CHECK (channel_points BETWEEN 0 AND 10000),
    cancelled            BOOLEAN NOT NULL DEFAULT FALSE,
    distribution_status  TEXT NOT NULL DEFAULT 'none' CHECK (distribution_status IN ('none', 'approved', 'rejected')),
    decided_by           TEXT,
    decided_at           TIMESTAMPTZ,
    decision_note        TEXT NOT NULL DEFAULT '',
    total_distributed    INTEGER NOT NULL DEFAULT 0,
    created_by           TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends_at > starts_at)
);

-- Every point ever awarded (cartela points and channel points are separate sources).
CREATE TABLE IF NOT EXISTS leaderboard_points (
    id           TEXT PRIMARY KEY,
    campaign_id  TEXT NOT NULL REFERENCES leaderboard_campaigns (id),
    player_id    TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('cartela', 'channel')),
    points       INTEGER NOT NULL CHECK (points >= 0),
    dedupe_key   TEXT NOT NULL UNIQUE,            -- cartela:<card id>  /  channel:<campaign>:<player>
    note         TEXT NOT NULL DEFAULT '',
    revoked      BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_by   TEXT,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lb_points_campaign_player ON leaderboard_points (campaign_id, player_id);
-- the channel reward can be earned once per player per campaign (a revoked claim cannot be re-claimed)
CREATE UNIQUE INDEX IF NOT EXISTS uq_lb_channel_once ON leaderboard_points (campaign_id, player_id) WHERE source = 'channel';

-- Every channel verification attempt, successful or not, for admin review and rate limiting.
CREATE TABLE IF NOT EXISTS leaderboard_channel_attempts (
    id           TEXT PRIMARY KEY,
    campaign_id  TEXT NOT NULL,
    player_id    TEXT NOT NULL,
    result       TEXT NOT NULL,                   -- credited | not_member | unverifiable | duplicate
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lb_attempts ON leaderboard_channel_attempts (campaign_id, player_id, created_at DESC);

-- Approved rewards. The ETB lands in bonus_balance; after the player has wagered amount x multiplier
-- in stakes, up to `amount_etb` of their bonus balance becomes withdrawable.
CREATE TABLE IF NOT EXISTS leaderboard_rewards (
    id              TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL REFERENCES leaderboard_campaigns (id),
    player_id       TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    points          INTEGER NOT NULL,
    amount_etb      INTEGER NOT NULL CHECK (amount_etb >= 0),
    wager_required  INTEGER NOT NULL CHECK (wager_required >= 0),
    wagered         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'locked' CHECK (status IN ('locked', 'released')),
    credited_at     TIMESTAMPTZ,
    released_at     TIMESTAMPTZ,
    released_etb    INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_lb_rewards_player ON leaderboard_rewards (player_id, status);
