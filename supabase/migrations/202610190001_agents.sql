-- Agents (resellers): each agent holds an ETB float funded by the house, tops up players from it,
-- and earns a commission. Step 1: accounts, float and ledger. All amounts are integer ETB;
-- commission_bps is basis points (100 = 1%).
--
-- float_balance and commission_balance can never go negative (CHECK + guarded UPDATEs), and every
-- change is recorded in agent_ledger inside the same transaction.

CREATE TABLE IF NOT EXISTS agents (
    id                  TEXT PRIMARY KEY,
    player_id           TEXT NOT NULL UNIQUE,            -- "tg_<telegram_id>" of the agent's own account
    agent_code          TEXT NOT NULL UNIQUE,            -- short code used in the agent's player link
    status              TEXT NOT NULL DEFAULT 'active',  -- active | suspended
    commission_bps      INTEGER NOT NULL DEFAULT 0 CHECK (commission_bps BETWEEN 0 AND 10000),
    float_balance       INTEGER NOT NULL DEFAULT 0 CHECK (float_balance >= 0),
    commission_balance  INTEGER NOT NULL DEFAULT 0 CHECK (commission_balance >= 0),
    created_by          TEXT NOT NULL,                   -- admin telegram id
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_ledger (
    id                TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL REFERENCES agents (id),
    type              TEXT NOT NULL,                     -- float_adjust | topup | commission | ...
    float_delta       INTEGER NOT NULL DEFAULT 0,
    float_after       INTEGER NOT NULL,
    commission_delta  INTEGER NOT NULL DEFAULT 0,
    commission_after  INTEGER NOT NULL DEFAULT 0,
    player_id         TEXT,                              -- the player involved (top-ups)
    admin_id          TEXT,
    note              TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_ledger_agent ON agent_ledger (agent_id, created_at DESC);

-- Which agent (if any) a player belongs to.
ALTER TABLE players ADD COLUMN IF NOT EXISTS agent_id TEXT;
CREATE INDEX IF NOT EXISTS idx_players_agent_id ON players (agent_id) WHERE agent_id IS NOT NULL;
