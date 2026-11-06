from src.services.stake_math import plan_stake_debit


def test_any_mode_spends_real_first_then_bonus():
    split = plan_stake_debit(balance=100, won_balance=0, bonus_balance=30, amount=50, funding_mode="any")
    assert (split.from_bonus, split.from_real, split.from_won) == (0, 50, 0)
    assert split.funded_by == "real"


def test_any_mode_pure_bonus_and_pure_real():
    assert plan_stake_debit(0, 0, 50, 40, "any").funded_by == "bonus"
    assert plan_stake_debit(100, 0, 0, 40, "any").funded_by == "real"


def test_deposited_money_is_spent_before_winnings():
    # 100 balance of which 70 is withdrawable winnings -> 30 deposited
    split = plan_stake_debit(balance=100, won_balance=70, bonus_balance=0, amount=50, funding_mode="any")
    assert split.from_real == 50 and split.from_won == 20  # 30 deposited, then 20 of winnings
    assert plan_stake_debit(100, 70, 0, 30, "any").from_won == 0


def test_bonus_only_never_touches_real_balance():
    assert plan_stake_debit(1000, 0, 10, 20, "bonus_only") is None
    split = plan_stake_debit(1000, 0, 20, 20, "bonus_only")
    assert (split.from_bonus, split.from_real, split.funded_by) == (20, 0, "bonus")


def test_insufficient_funds_and_bad_amounts_return_none():
    assert plan_stake_debit(10, 0, 5, 20, "any") is None
    assert plan_stake_debit(10, 0, 5, 0, "any") is None
    assert plan_stake_debit(10, 0, 5, -5, "any") is None


def test_inconsistent_wallet_never_goes_negative_on_won():
    # won_balance > balance violates the invariant; the plan must still be sane
    split = plan_stake_debit(balance=10, won_balance=50, bonus_balance=0, amount=10, funding_mode="any")
    assert split.from_real == 10 and split.from_won == 10
