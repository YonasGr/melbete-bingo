"""Pure arithmetic for splitting a stake across a wallet's pools.

Kept free of I/O and third-party imports so the database layer can run it
while holding a row lock, and so it can be unit-tested in isolation.
"""
from typing import NamedTuple, Optional


class StakeSplit(NamedTuple):
    from_bonus: int  # taken from bonus_balance
    from_real: int  # taken from balance (deposited money first, then winnings)
    from_won: int  # the part of from_real that came out of won_balance
    funded_by: str  # "bonus" | "real" | "mixed:<real>:<bonus>" (stored per card)


def plan_stake_debit(
    balance: int, won_balance: int, bonus_balance: int, amount: int, funding_mode: str
) -> Optional[StakeSplit]:
    """Work out how `amount` is paid, or return None if the wallet cannot cover it.

    - "bonus_only": paid entirely from bonus_balance.
    - anything else ("any"): real balance first, then bonus balance for the rest.
    Within real balance, deposited money is spent before won_balance, so a
    player's withdrawable winnings are preserved as long as possible.
    """
    if amount <= 0:
        return None
    if funding_mode == "bonus_only":
        if bonus_balance < amount:
            return None
        from_bonus, from_real = amount, 0
    else:
        if balance + bonus_balance < amount:
            return None
        from_real = min(balance, amount)
        from_bonus = amount - from_real
    deposited_portion = max(0, balance - won_balance)
    from_won = from_real - min(deposited_portion, from_real)
    if from_bonus == 0:
        funded_by = "real"
    elif from_real == 0:
        funded_by = "bonus"
    else:
        funded_by = f"mixed:{from_real}:{from_bonus}"
    return StakeSplit(from_bonus, from_real, from_won, funded_by)
