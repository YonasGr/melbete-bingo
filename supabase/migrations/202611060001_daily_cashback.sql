-- Daily cashback: a player who deposited on a day and did NOT withdraw that same day gets a percentage of that
-- day's approved deposits back as bonus credit (non-withdrawable, cartelas only). Days are Ethiopia time.
-- bonus_campaigns.amount_etb holds the percentage for this campaign (5 = 5%). Idempotent.

ALTER TABLE bonus_campaigns DROP CONSTRAINT IF EXISTS bonus_campaigns_reward_type_check;
ALTER TABLE bonus_campaigns ADD CONSTRAINT bonus_campaigns_reward_type_check
  CHECK (reward_type IN ('welcome','referral','daily','first_deposit','cashback'));

INSERT INTO bonus_campaigns(reward_type, amount_etb, enabled, daily_budget_etb, total_budget_etb, per_player_cap)
VALUES ('cashback', 5, TRUE, 50000, 1000000, 1)
ON CONFLICT (reward_type) DO NOTHING;
