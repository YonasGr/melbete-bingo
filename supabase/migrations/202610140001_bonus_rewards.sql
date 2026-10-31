-- Melbete Bingo promotional rewards. Forward-only migration; real-money wallet columns are unchanged.
CREATE TABLE IF NOT EXISTS bonus_campaigns (
  reward_type TEXT PRIMARY KEY CHECK (reward_type IN ('welcome','referral','daily','first_deposit')),
  amount_etb INTEGER NOT NULL CHECK (amount_etb > 0),
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  daily_budget_etb INTEGER NOT NULL DEFAULT 500 CHECK (daily_budget_etb >= 0),
  total_budget_etb INTEGER NOT NULL DEFAULT 5000 CHECK (total_budget_etb >= 0),
  per_player_cap INTEGER NOT NULL DEFAULT 1 CHECK (per_player_cap > 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO bonus_campaigns(reward_type, amount_etb, enabled, daily_budget_etb, total_budget_etb, per_player_cap)
VALUES ('welcome',20,FALSE,500,5000,1), ('referral',5,FALSE,500,5000,20), ('daily',1,FALSE,500,5000,1), ('first_deposit',50,FALSE,5000,50000,1)
ON CONFLICT (reward_type) DO NOTHING;
CREATE TABLE IF NOT EXISTS bonus_claims (
  id BIGSERIAL PRIMARY KEY,
  player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
  reward_type TEXT NOT NULL REFERENCES bonus_campaigns(reward_type),
  local_reward_date DATE,
  status TEXT NOT NULL CHECK (status IN ('granted','denied','reversed')),
  amount_etb INTEGER NOT NULL DEFAULT 0 CHECK (amount_etb >= 0),
  ledger_id BIGINT,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(player_id, reward_type, local_reward_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS bonus_one_welcome_per_player
 ON bonus_claims(player_id) WHERE reward_type='welcome' AND status='granted';
CREATE UNIQUE INDEX IF NOT EXISTS bonus_one_first_deposit_per_player
 ON bonus_claims(player_id) WHERE reward_type='first_deposit' AND status='granted';
CREATE TABLE IF NOT EXISTS bonus_ledger (
  id BIGSERIAL PRIMARY KEY,
  player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
  amount_etb INTEGER NOT NULL CHECK (amount_etb <> 0),
  entry_type TEXT NOT NULL CHECK (entry_type IN ('grant','spend','reversal','adjustment')),
  reward_type TEXT,
  source_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bonus_ledger_player_created_idx ON bonus_ledger(player_id, created_at DESC);
CREATE TABLE IF NOT EXISTS bonus_campaign_state (
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id=1),
  globally_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO bonus_campaign_state(id, globally_enabled) VALUES (1,FALSE) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS bonus_admin_audit (
  id BIGSERIAL PRIMARY KEY,
  admin_telegram_id TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  before_json JSONB,
  after_json JSONB,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Preserve current wallet balance model, while making bonus credits auditable separately.
