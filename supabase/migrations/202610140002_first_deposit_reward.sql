-- First approved deposit reward: amount_etb on this campaign is a percentage (50 = 50%).
-- Safe to apply after 202610140001_bonus_rewards.sql on existing installations.
ALTER TABLE bonus_campaigns DROP CONSTRAINT IF EXISTS bonus_campaigns_reward_type_check;
ALTER TABLE bonus_campaigns ADD CONSTRAINT bonus_campaigns_reward_type_check
  CHECK (reward_type IN ('welcome','referral','daily','first_deposit'));
INSERT INTO bonus_campaigns(reward_type, amount_etb, enabled, daily_budget_etb, total_budget_etb, per_player_cap)
VALUES ('first_deposit',50,FALSE,5000,50000,1)
ON CONFLICT (reward_type) DO NOTHING;
CREATE UNIQUE INDEX IF NOT EXISTS bonus_one_first_deposit_per_player
  ON bonus_claims(player_id) WHERE reward_type='first_deposit' AND status='granted';
