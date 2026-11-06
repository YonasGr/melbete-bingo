-- A player holds between 1 and 4 cartelas per round. Idempotent.
UPDATE system_settings SET cards_per_player = 4 WHERE cards_per_player > 4;
UPDATE system_settings SET cards_per_player = 4 WHERE id = 'global';
UPDATE game_rooms SET cards_per_player = 4 WHERE state = 'waiting';
ALTER TABLE system_settings ALTER COLUMN cards_per_player SET DEFAULT 4;
ALTER TABLE game_rooms ALTER COLUMN cards_per_player SET DEFAULT 4;
