-- ============================================================================
-- Melbete Bingo - Supabase (Postgres) schema
-- ============================================================================
-- Run this once against a fresh Supabase project (SQL Editor -> New query
-- -> paste this whole file -> Run). It is idempotent: re-running it is safe
-- and will not duplicate tables/data (CREATE TABLE IF NOT EXISTS throughout).
--
-- This replaces the previous Firestore data model 1:1 - one table per
-- former collection, same field names, so the migration only changes how
-- data is stored, never what is stored. See MIGRATING_TO_SUPABASE.md for
-- the full story and setup steps.
--
-- Design notes:
--   * All ids are TEXT, not the built-in UUID type. The app already
--     generates its own ids (uuid4 strings, or deterministic ones like
--     "tg_123456789" / "web_<uuid>" / "global") - TEXT keeps every id
--     format valid without a translation layer.
--   * JSONB is used for anything that was a nested object/array in
--     Firestore (grids, patterns, winners, etc). Unlike Firestore, Postgres
--     has no "nested array" restriction, so the flattening workaround
--     the app used for card grids is gone entirely on the DB side (kept
--     only in the API layer for backward-compatible response shape).
--   * Every table has an index on whatever the app actually queries by -
--     see the comment above each index for which repository method it
--     serves.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- players
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS players (
    id                  TEXT PRIMARY KEY,               -- "tg_<telegram_id>"
    telegram_id         TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    avatar_url          TEXT,
    phone_number        TEXT,
    payment_name        TEXT,
    registration_state  TEXT NOT NULL DEFAULT 'pending_contact',
    withdrawal_accounts JSONB NOT NULL DEFAULT '{}'::jsonb,
    auto_bingo_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    referred_by         TEXT,
    referral_count      INTEGER NOT NULL DEFAULT 0,
    is_banned           BOOLEAN NOT NULL DEFAULT FALSE,
    ban_reason          TEXT,
    is_bot              BOOLEAN NOT NULL DEFAULT FALSE,
    agent_id            TEXT,                           -- agents.id, see migrations/202610190001
    promo_opt_out       BOOLEAN NOT NULL DEFAULT FALSE,  -- see migrations/202610190003
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- PlayerRepository.list_all(): most-recently-registered first (admin panel)
CREATE INDEX IF NOT EXISTS idx_players_created_at ON players (created_at DESC);

-- ----------------------------------------------------------------------------
-- game_rooms
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS game_rooms (
    id                    TEXT PRIMARY KEY,
    track                 TEXT NOT NULL DEFAULT 'real_10',
    host_id               TEXT,
    variant               TEXT NOT NULL DEFAULT '75',
    number_range_min      INTEGER NOT NULL DEFAULT 1,
    number_range_max      INTEGER NOT NULL DEFAULT 75,
    cards_per_player      INTEGER NOT NULL DEFAULT 4,
    pattern               JSONB NOT NULL DEFAULT '{}'::jsonb,
    state                 TEXT NOT NULL DEFAULT 'waiting',   -- waiting | running | ended | cancelled
    called_numbers        JSONB NOT NULL DEFAULT '[]'::jsonb,
    draw_pool             JSONB NOT NULL DEFAULT '[]'::jsonb,
    winners               JSONB NOT NULL DEFAULT '[]'::jsonb,
    end_reason            TEXT,
    pending_winner        JSONB,                              -- deprecated, kept for old rows
    advanced              BOOLEAN NOT NULL DEFAULT FALSE,
    draw_interval         INTEGER NOT NULL DEFAULT 2,
    auto_draw              BOOLEAN NOT NULL DEFAULT TRUE,
    stake_amount          INTEGER NOT NULL DEFAULT 10,
    prize_pool            INTEGER NOT NULL DEFAULT 0,
    prize_pool_real       INTEGER NOT NULL DEFAULT 0,
    prize_pool_bonus      INTEGER NOT NULL DEFAULT 0,
    taken_cartelas        JSONB NOT NULL DEFAULT '[]'::jsonb,
    min_players_to_start  INTEGER NOT NULL DEFAULT 1,
    min_cartelas_to_start INTEGER NOT NULL DEFAULT 3 CHECK (min_cartelas_to_start >= 1),
    countdown_seconds     INTEGER NOT NULL DEFAULT 30,
    cartela_min           INTEGER NOT NULL DEFAULT 1,
    cartela_max           INTEGER NOT NULL DEFAULT 500,
    countdown_started_at  TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- RoomRepository.get_latest() / list_recent(): newest round first
CREATE INDEX IF NOT EXISTS idx_game_rooms_created_at ON game_rooms (created_at DESC);

-- ----------------------------------------------------------------------------
-- cards
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cards (
    id              TEXT PRIMARY KEY,
    room_id         TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    variant         TEXT NOT NULL,
    cartela_number  INTEGER NOT NULL,
    grid            JSONB NOT NULL,          -- 5x5 nested array of cell objects - no encoding trick needed
    funded_by       TEXT NOT NULL DEFAULT 'unfunded', -- set at stake charge; unknown values fail closed to bonus
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- CardRepository.list_by_room()
CREATE INDEX IF NOT EXISTS idx_cards_room ON cards (room_id);
-- CardRepository.list_by_room_and_owner()
CREATE INDEX IF NOT EXISTS idx_cards_room_owner ON cards (room_id, owner_id);

-- ----------------------------------------------------------------------------
-- wallets  (1:1 with players, keyed directly by player_id)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wallets (
    id                  TEXT PRIMARY KEY,
    player_id           TEXT NOT NULL UNIQUE,
    balance             INTEGER NOT NULL DEFAULT 0,
    won_balance         INTEGER NOT NULL DEFAULT 0,
    bonus_balance       INTEGER NOT NULL DEFAULT 0,
    lifetime_deposited  INTEGER NOT NULL DEFAULT 0,
    free_credit_granted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- WalletRepository.get_by_player() is the only lookup - the UNIQUE
-- constraint above already gives it an index for free.

-- ----------------------------------------------------------------------------
-- transactions  (the wallet audit ledger)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id                        TEXT PRIMARY KEY,
    player_id                 TEXT NOT NULL,
    type                      TEXT NOT NULL,
    amount                    INTEGER NOT NULL,
    balance_after             INTEGER,
    status                    TEXT NOT NULL DEFAULT 'completed',
    payment_method            TEXT,
    screenshot_file_id        TEXT,
    screenshot_url            TEXT,
    room_id                   TEXT,
    admin_id                  TEXT,
    note                      TEXT,
    legal_name                TEXT,
    withdrawal_address        TEXT,
    notification_chat_id      TEXT,
    notification_message_id   BIGINT,
    notification_is_photo     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at                TIMESTAMPTZ
);
-- TransactionRepository.list_pending_topups() / list_pending_withdrawals()
CREATE INDEX IF NOT EXISTS idx_transactions_type_status ON transactions (type, status);
-- TransactionRepository.list_by_player()
CREATE INDEX IF NOT EXISTS idx_transactions_player ON transactions (player_id, created_at DESC);

-- ----------------------------------------------------------------------------
-- payment_methods
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_methods (
    id                  TEXT PRIMARY KEY,
    key                 TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    account_info        TEXT NOT NULL,
    account_holder      TEXT,
    instructions        TEXT,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    supports_deposit    BOOLEAN NOT NULL DEFAULT TRUE,
    supports_withdrawal BOOLEAN NOT NULL DEFAULT TRUE,
    display_order       INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- admins
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admins (
    id              TEXT PRIMARY KEY,
    telegram_id     TEXT NOT NULL UNIQUE,   -- AdminRepository keys documents by telegram_id directly
    is_super_admin  BOOLEAN NOT NULL DEFAULT FALSE,
    added_by        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- system_settings  (singleton row, id is always 'global')
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    id                              TEXT PRIMARY KEY DEFAULT 'global',
    stake_amount                    INTEGER NOT NULL DEFAULT 10,
    free_credit_amount              INTEGER NOT NULL DEFAULT 50,
    lobby_countdown_seconds         INTEGER NOT NULL DEFAULT 30,
    draw_interval                   INTEGER NOT NULL DEFAULT 2,
    min_deposit                     INTEGER NOT NULL DEFAULT 50,
    max_deposit                     INTEGER NOT NULL DEFAULT 70000,
    min_withdrawal                  INTEGER NOT NULL DEFAULT 200,
    max_withdrawal                  INTEGER NOT NULL DEFAULT 10000,
    payout_ratio                    DOUBLE PRECISION NOT NULL DEFAULT 0.8,
    referral_bonus                  INTEGER NOT NULL DEFAULT 20,
    max_referrals_per_user          INTEGER NOT NULL DEFAULT 3,
    cards_per_player                INTEGER NOT NULL DEFAULT 4,
    min_players_to_start            INTEGER NOT NULL DEFAULT 5 CHECK (min_players_to_start >= 2),
    min_cartelas_to_start           INTEGER NOT NULL DEFAULT 3 CHECK (min_cartelas_to_start >= 1),
    cartela_min                     INTEGER NOT NULL DEFAULT 1,
    cartela_max                     INTEGER NOT NULL DEFAULT 500,
    game_inactivity_timeout_minutes INTEGER NOT NULL DEFAULT 0,
    support_contact                 TEXT NOT NULL DEFAULT '@melbete_support_1',
    bot_system_enabled              BOOLEAN NOT NULL DEFAULT FALSE,
    deposits_enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    max_bots_to_join                INTEGER NOT NULL DEFAULT 10,
    stake_amount_real_10            INTEGER NOT NULL DEFAULT 10,
    stake_amount_real_50            INTEGER NOT NULL DEFAULT 50,
    stake_amount_bonus              INTEGER NOT NULL DEFAULT 10,
    bot_enabled_real_10             BOOLEAN NOT NULL DEFAULT FALSE,
    bot_enabled_real_50             BOOLEAN NOT NULL DEFAULT FALSE,
    bot_enabled_bonus               BOOLEAN NOT NULL DEFAULT FALSE,
    max_bots_real_10                INTEGER NOT NULL DEFAULT 10,
    max_bots_real_50                INTEGER NOT NULL DEFAULT 10,
    max_bots_bonus                  INTEGER NOT NULL DEFAULT 10,
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- bot_players
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot_players (
    id              TEXT PRIMARY KEY,
    player_id       TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    avatar_url      TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_played_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bot_players_last_played_at
    ON bot_players (last_played_at ASC NULLS FIRST);

-- ----------------------------------------------------------------------------
-- referrals
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS referrals (
    id            TEXT PRIMARY KEY,
    referrer_id   TEXT NOT NULL,
    referred_id   TEXT NOT NULL UNIQUE,   -- a player can only ever be referred once
    bonus_amount  INTEGER NOT NULL DEFAULT 0,
    awarded       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    awarded_at    TIMESTAMPTZ
);
-- ReferralRepository.list_by_referrer()
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_id);
-- ReferralRepository.list_all(): newest first (admin panel)
CREATE INDEX IF NOT EXISTS idx_referrals_created_at ON referrals (created_at DESC);

-- ----------------------------------------------------------------------------
-- claims  (win-verification audit records)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
    id                    TEXT PRIMARY KEY,
    room_id               TEXT NOT NULL,
    player_id             TEXT NOT NULL,
    card_id               TEXT NOT NULL,
    claimed_pattern       TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    verification_message  TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at           TIMESTAMPTZ
);

-- ----------------------------------------------------------------------------
-- draw_logs  (RNG fairness audit trail)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS draw_logs (
    id          TEXT PRIMARY KEY,
    room_id     TEXT NOT NULL,
    seed        TEXT NOT NULL,
    sequence    JSONB NOT NULL DEFAULT '[]'::jsonb,
    final_hash  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- DrawLogRepository.get_by_room()
CREATE INDEX IF NOT EXISTS idx_draw_logs_room ON draw_logs (room_id);

-- ----------------------------------------------------------------------------
-- link_codes  (web <-> Telegram account linking, one-time short-lived codes)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS link_codes (
    id                TEXT PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    web_player_id     TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    telegram_id       TEXT,
    linked_player_id  TEXT,
    session_token     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ
);

-- ----------------------------------------------------------------------------
-- Backfill for deployments that ran an earlier version of this schema
-- before player banning existed (safe to re-run; no-ops once applied).
-- ----------------------------------------------------------------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS is_banned  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS ban_reason TEXT;

-- ============================================================================
-- Done. No seed data here on purpose - SettingsRepository.get_or_create(),
-- AdminService.ensure_super_admin_bootstrapped(), and
-- no payment-method seed data is present. Payment methods are configured by
-- an admin and read only from this database.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- broadcast_progress (admin broadcast status)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS broadcast_progress (
    id              TEXT PRIMARY KEY,
    total_users     INTEGER NOT NULL DEFAULT 0,
    sent_users      INTEGER NOT NULL DEFAULT 0,
    delivered_users INTEGER NOT NULL DEFAULT 0,
    failed_users    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'idle',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO broadcast_progress (id, total_users, sent_users, delivered_users, failed_users, status)
VALUES ('active', 0, 0, 0, 0, 'idle')
ON CONFLICT (id) DO NOTHING;


-- Promotional bonus rewards (global campaign switch defaults OFF).
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

-- ----------------------------------------------------------------------------
-- Wallet hardening (idempotency keys, unique payment URL) - see migrations/202610160001
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS idempotency_keys (
    player_id     TEXT NOT NULL,
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress | completed
    response      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, scope, key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at ON idempotency_keys (created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_payment_verification_url
    ON transactions (payment_verification_url)
    WHERE payment_verification_url IS NOT NULL;

-- ----------------------------------------------------------------------------
-- Agents (resellers) - see migrations/202610190001_agents.sql
-- ----------------------------------------------------------------------------
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

CREATE INDEX IF NOT EXISTS idx_players_agent_id ON players (agent_id) WHERE agent_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- Agent rewards - see migrations/202610240002_agent_rewards.sql
-- ----------------------------------------------------------------------------

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

-- ----------------------------------------------------------------------------
-- Player notifications / inbox - see migrations/202610190002_notifications.sql
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- Engagement (streaks, nudges, win-back) - see migrations/202610190003_engagement.sql
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engagement_state (
    player_id                 TEXT PRIMARY KEY,
    last_played_at            TIMESTAMPTZ,
    streak_days               INTEGER NOT NULL DEFAULT 0,
    best_streak               INTEGER NOT NULL DEFAULT 0,
    last_play_day             DATE,                      -- Ethiopia (EAT) calendar day of the last played round
    last_rewarded_streak      INTEGER NOT NULL DEFAULT 0,  -- highest streak milestone already paid in this run
    last_nudge_at             TIMESTAMPTZ,
    last_winback_at           TIMESTAMPTZ,
    last_streak_reminder_day  DATE,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_engagement_last_played ON engagement_state (last_played_at);



-- Lease locks: one API instance per background loop. See migrations/202610240001
CREATE TABLE IF NOT EXISTS loop_locks (
    name       TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

-- ----------------------------------------------------------------------------
-- Multi-account signals - see migrations/202610240003_abuse_signals.sql
-- ----------------------------------------------------------------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS phone_normalized TEXT;

UPDATE players
   SET phone_normalized = right(regexp_replace(phone_number, '\D', '', 'g'), 9)
 WHERE phone_normalized IS NULL
   AND phone_number IS NOT NULL
   AND length(regexp_replace(phone_number, '\D', '', 'g')) >= 9;

CREATE INDEX IF NOT EXISTS idx_players_phone_norm ON players (phone_normalized) WHERE phone_normalized IS NOT NULL;

-- Enforce uniqueness in the database too - but only if existing data has no duplicates, so this
-- migration can never fail half-way. Otherwise the app still blocks NEW duplicates and the admin
-- "Abuse review" lists the old ones; clean them up, then re-run this migration to add the index.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM players WHERE phone_normalized IS NOT NULL AND NOT is_bot
               GROUP BY phone_normalized HAVING count(*) > 1) THEN
        RAISE NOTICE 'Duplicate phone numbers exist: unique index uq_players_phone_norm NOT created yet';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_players_phone_norm
            ON players (phone_normalized) WHERE phone_normalized IS NOT NULL AND NOT is_bot;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS player_ip_log (
    player_id   TEXT NOT NULL,
    ip_hash     TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (player_id, ip_hash)
);
CREATE INDEX IF NOT EXISTS idx_player_ip_log_hash ON player_ip_log (ip_hash);

-- ----------------------------------------------------------------------------
-- admin control centre (see migrations/202610310001_admin_control.sql)
-- ----------------------------------------------------------------------------

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

-- per-admin Telegram alert subscriptions (migrations/202610310002_admin_alerts.sql)
ALTER TABLE admins ADD COLUMN IF NOT EXISTS alert_types JSONB;
