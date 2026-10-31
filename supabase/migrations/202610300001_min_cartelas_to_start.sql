-- Rounds now start on a minimum number of CARTELAS (default 3), not players.
-- One player holding 3 cartelas starts the 30s countdown. Idempotent.
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS min_cartelas_to_start INTEGER NOT NULL DEFAULT 3 CHECK (min_cartelas_to_start >= 1);
ALTER TABLE game_rooms      ADD COLUMN IF NOT EXISTS min_cartelas_to_start INTEGER NOT NULL DEFAULT 3 CHECK (min_cartelas_to_start >= 1);

-- Apply the new rule to the live settings row and to any lobby that is open now.
UPDATE system_settings SET min_cartelas_to_start = 3, cards_per_player = 4 WHERE id = 'global';
UPDATE game_rooms SET min_cartelas_to_start = 3, cards_per_player = 4 WHERE state = 'waiting';
