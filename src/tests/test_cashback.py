from datetime import date

from src.services.bonus_service import budget_alert_level, cashback_amount, cashback_days_due

TODAY = date(2026, 11, 6)


def test_cashback_is_a_rounded_down_percentage_of_deposits():
    assert cashback_amount(1000, 5) == 50
    assert cashback_amount(199, 5) == 9
    assert cashback_amount(19, 5) == 0
    assert cashback_amount(0, 5) == 0 and cashback_amount(500, 0) == 0


def test_only_finished_days_without_a_withdrawal_and_not_yet_paid_are_due():
    deposits = {date(2026, 11, 3): 100, date(2026, 11, 4): 200, date(2026, 11, 5): 300, TODAY: 400}
    due = cashback_days_due(deposits, withdrawal_days={date(2026, 11, 4)}, claimed_days={date(2026, 11, 3)}, today=TODAY)
    assert due == {date(2026, 11, 5): 300}   # 3rd already paid, 4th withdrew, today is still open


def test_daily_cap_limits_the_credit_but_zero_means_no_cap():
    assert cashback_amount(1000, 5, cap=30) == 30
    assert cashback_amount(1000, 5, cap=80) == 50
    assert cashback_amount(1000, 5, cap=0) == 50


def test_budget_alert_levels():
    assert budget_alert_level(79, 100) == 0
    assert budget_alert_level(80, 100) == 80
    assert budget_alert_level(100, 100) == 100 and budget_alert_level(150, 100) == 100
    assert budget_alert_level(5, 0) == 0
