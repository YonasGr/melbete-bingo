-- Cartela numbers grow from 1-500 to 1-1000.
-- Cartelas 1-500 keep exactly the same layouts (each card is generated from its own number);
-- 501-1000 are new. Only the default and the stored limit change here.
ALTER TABLE system_settings ALTER COLUMN cartela_max SET DEFAULT 1000;
ALTER TABLE game_rooms      ALTER COLUMN cartela_max SET DEFAULT 1000;

-- Live settings: raise the limit where it still holds the old default.
UPDATE system_settings SET cartela_max = 1000 WHERE cartela_max = 500;

-- Lobbies that are still open: widening the range is safe (taken numbers stay valid).
UPDATE game_rooms SET cartela_max = 1000 WHERE state = 'waiting' AND cartela_max = 500;
