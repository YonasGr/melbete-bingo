-- Monthly leaderboard: fixed reward ladder per rank band, tie-break by ETB reserved, and the
-- "winnings from bonus play become withdrawable after wagering" ledger. Idempotent.

-- NULL = legacy campaign (budget shared in proportion to points). Otherwise a JSON list of
-- {"from": 1, "to": 1, "amount": 20000} bands that must start at rank 1 and have no gaps.
ALTER TABLE leaderboard_campaigns ADD COLUMN IF NOT EXISTS reward_tiers JSONB;

-- ETB value of the reserved cartela behind each cartela point row (used for the tie-break and the player dashboard).
ALTER TABLE leaderboard_points ADD COLUMN IF NOT EXISTS stake_etb INTEGER NOT NULL DEFAULT 0 CHECK (stake_etb >= 0);

-- Winnings paid into the bonus balance from bonus-funded play while this reward was still locked.
-- When the wagering requirement is met only this amount (never the reward itself) turns withdrawable.
ALTER TABLE leaderboard_rewards ADD COLUMN IF NOT EXISTS bonus_won INTEGER NOT NULL DEFAULT 0 CHECK (bonus_won >= 0);
