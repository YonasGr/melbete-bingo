-- Agent rewards: monthly-volume commission tiers, new-player / retention rewards held until they
-- can be verified, and a per-agent daily payout cap. All amounts are integer ETB.
--
-- agents.commission_bps stays the admin-set BASE rate. agents.tier_bps is the rate earned from this
-- month's volume. The rate that is paid is GREATEST(commission_bps, tier_bps), so turning tiers on
-- never lowers an existing agent's rate.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS tier_bps   INTEGER NOT NULL DEFAULT 0 CHECK (tier_bps BETWEEN 0 AND 10000);
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tier_label TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS agent_tiers (
    id                 TEXT PRIMARY KEY,
    label              TEXT NOT NULL,
    min_monthly_volume INTEGER NOT NULL UNIQUE CHECK (min_monthly_volume >= 0),
    commission_bps     INTEGER NOT NULL CHECK (commission_bps BETWEEN 0 AND 10000),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single settings row (id = 'global'), editable from the admin panel.
CREATE TABLE IF NOT EXISTS agent_reward_settings (
    id                   TEXT PRIMARY KEY,
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    new_player_bounty    INTEGER NOT NULL DEFAULT 20  CHECK (new_player_bounty >= 0),
    min_first_deposit    INTEGER NOT NULL DEFAULT 50  CHECK (min_first_deposit >= 0),
    min_rounds_played    INTEGER NOT NULL DEFAULT 3   CHECK (min_rounds_played >= 0),
    retention_bonus      INTEGER NOT NULL DEFAULT 10  CHECK (retention_bonus >= 0),
    retention_days       INTEGER NOT NULL DEFAULT 30  CHECK (retention_days >= 1),
    hold_days            INTEGER NOT NULL DEFAULT 3   CHECK (hold_days >= 0),
    daily_payout_cap     INTEGER NOT NULL DEFAULT 2000 CHECK (daily_payout_cap >= 0),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO agent_reward_settings (id) VALUES ('global') ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_tiers (id, label, min_monthly_volume, commission_bps) VALUES
    ('tier_bronze', 'Bronze', 0,      300),
    ('tier_silver', 'Silver', 50000,  400),
    ('tier_gold',   'Gold',   200000, 500)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_rewards (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents (id),
    player_id   TEXT,                                   -- the referred player the reward is about
    type        TEXT NOT NULL,                          -- new_player | retention | milestone
    amount      INTEGER NOT NULL CHECK (amount > 0),
    status      TEXT NOT NULL DEFAULT 'pending',        -- pending | paid | cancelled
    release_at  TIMESTAMPTZ NOT NULL,
    dedupe_key  TEXT NOT NULL UNIQUE,                   -- one reward per event, however often it is retried
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_agent_rewards_agent  ON agent_rewards (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_rewards_due    ON agent_rewards (release_at) WHERE status = 'pending';
