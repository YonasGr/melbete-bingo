-- A round now starts once 2 cartelas are reserved (was 3). Idempotent.
ALTER TABLE system_settings ALTER COLUMN min_cartelas_to_start SET DEFAULT 2;
ALTER TABLE game_rooms      ALTER COLUMN min_cartelas_to_start SET DEFAULT 2;
UPDATE system_settings SET min_cartelas_to_start = 2 WHERE id = 'global';
UPDATE game_rooms SET min_cartelas_to_start = 2 WHERE state = 'waiting';
