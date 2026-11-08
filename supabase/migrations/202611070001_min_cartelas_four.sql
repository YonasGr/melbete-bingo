-- Round countdown starts once 4 cartelas are taken (previously 2, with a code floor of 3).
ALTER TABLE system_settings ALTER COLUMN min_cartelas_to_start SET DEFAULT 4;
ALTER TABLE game_rooms      ALTER COLUMN min_cartelas_to_start SET DEFAULT 4;
UPDATE system_settings SET min_cartelas_to_start = 4 WHERE id = 'global';
UPDATE game_rooms      SET min_cartelas_to_start = 4 WHERE state = 'waiting';
