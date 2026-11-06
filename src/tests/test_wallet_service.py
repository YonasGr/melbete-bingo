"""
Tests for WalletService, focused on the deposited-vs-won balance split:
deposited money is playable but never withdrawable; only game winnings
(won_balance) can be withdrawn. This is a business-critical rule enforced
entirely server-side (see WalletService.request_withdrawal).
"""
import pytest

from src.tests.fake_wallet_ops import AtomicWalletOps
from src.models.schemas import SystemSettings, Transaction, Wallet, utcnow
from src.repositories.wallet_repository import TopupApproval
from src.services.wallet_service import (
    InsufficientBalanceError,
    InvalidAmountError,
    WalletService,
    WithdrawalAddressMissingError,
)


class FakeWalletRepository(AtomicWalletOps):
    def __init__(self):
        self.wallets = {}

    async def get_by_player(self, player_id):
        return self.wallets.get(player_id)

    async def create(self, wallet: Wallet):
        self.wallets[wallet.player_id] = wallet
        return wallet

    async def update(self, player_id, fields):
        wallet = self.wallets[player_id]
        for key, value in fields.items():
            setattr(wallet, key, value)

    async def atomic_debit_real(self, player_id, amount):
        wallet = self.wallets.get(player_id)
        if not wallet or wallet.balance < amount:
            return None
        wallet.balance -= amount
        wallet.won_balance = min(wallet.won_balance, wallet.balance)
        return wallet

    async def approve_topup_atomic(self, transaction_id, admin_id, on_first_deposit=None, on_approved=None):
        """In-memory stand-in for the Postgres transaction. There is no await between
        the status check and the credit, so it is atomic on a single event loop."""
        txn = self.transaction_repo.transactions.get(transaction_id)
        if txn is None:
            return TopupApproval(None, False)
        if txn.type != "top_up_pending" or txn.status != "pending":
            return TopupApproval(txn, False)
        wallet = self.wallets.get(txn.player_id)
        if wallet is None:
            wallet = self.wallets[txn.player_id] = Wallet(player_id=txn.player_id)
        is_first = wallet.lifetime_deposited == 0
        wallet.balance += txn.amount
        wallet.lifetime_deposited += txn.amount
        txn.type, txn.status = "top_up_approved", "completed"
        txn.balance_after, txn.admin_id, txn.resolved_at = wallet.balance, admin_id, utcnow()
        extra = await on_first_deposit(None, txn) if (is_first and on_first_deposit) else None
        if on_approved:
            await on_approved(None, txn)
        return TopupApproval(txn, True, extra)


class FakeTransactionRepository:
    def __init__(self):
        self.transactions = {}

    async def create(self, transaction):
        self.transactions[transaction.id] = transaction
        return transaction

    async def get(self, transaction_id):
        return self.transactions.get(transaction_id)

    async def update(self, transaction_id, fields):
        for k, v in fields.items():
            setattr(self.transactions[transaction_id], k, v)

    async def list_pending_topups(self):
        return [t for t in self.transactions.values() if t.type == "top_up_pending" and t.status == "pending"]

    async def list_pending_withdrawals(self):
        return [t for t in self.transactions.values() if t.type == "withdrawal_pending" and t.status == "pending"]


class FakeSettingsRepository:
    def __init__(self):
        self._settings = SystemSettings()

    async def get(self):
        return self._settings

    async def get_or_create(self):
        return self._settings

    async def update(self, fields):
        for k, v in fields.items():
            setattr(self._settings, k, v)
        return self._settings


@pytest.fixture
def wallets():
    wallet_repo = FakeWalletRepository()
    transaction_repo = FakeTransactionRepository()
    wallet_repo.transaction_repo = transaction_repo  # lets the fake emulate the atomic approval
    return WalletService(
        wallet_repo=wallet_repo,
        transaction_repo=transaction_repo,
        settings_repo=FakeSettingsRepository(),
    )


@pytest.mark.asyncio
async def test_deposit_does_not_increase_won_balance(wallets):
    txn = await wallets.request_deposit("player-1", 500, "telebirr")
    await wallets.approve_topup(txn.id, "admin-1")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.balance == 500
    assert wallet.won_balance == 0
    assert wallet.lifetime_deposited == 500


@pytest.mark.asyncio
async def test_winning_increases_won_balance(wallets):
    await wallets.get_or_create_wallet("player-1")
    await wallets.credit_payout("player-1", 200, "room-1", funded_by="real")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.balance == 200
    assert wallet.won_balance == 200


@pytest.mark.asyncio
async def test_stake_uses_real_balance_before_bonus(wallets):
    wallet = await wallets.get_or_create_wallet("player-1")
    wallet.balance = 30
    wallet.bonus_balance = 50

    wallet, funded_by = await wallets.debit_for_stake("player-1", 40, "room-1")

    assert funded_by == "mixed:30:10"
    assert wallet.balance == 0
    assert wallet.bonus_balance == 40


@pytest.mark.asyncio
async def test_bonus_is_used_when_no_real_balance_exists(wallets):
    wallet = await wallets.get_or_create_wallet("player-1")
    wallet.bonus_balance = 50

    wallet, funded_by = await wallets.debit_for_stake("player-1", 40, "room-1")

    assert funded_by == "bonus"
    assert wallet.balance == 0
    assert wallet.bonus_balance == 10


@pytest.mark.asyncio
async def test_cannot_withdraw_more_than_winnings(wallets):
    # Deposit 500 (playable, not withdrawable), win 250 (withdrawable).
    txn = await wallets.request_deposit("player-1", 500, "telebirr")
    await wallets.approve_topup(txn.id, "admin-1")
    await wallets.credit_payout("player-1", 250, "room-1", funded_by="real")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.balance == 750
    assert wallet.won_balance == 250

    await wallets.wallet_repo.update("player-1", {})  # no-op, just confirming state
    # Player tries to withdraw the full 750 - must be rejected; the cap is
    # the 250 they actually won, not their total spendable balance.
    with pytest.raises(InsufficientBalanceError):
        await wallets.request_withdrawal("player-1", 750, "telebirr", "Test Player", "0912345678")

    # Exactly their winnings should succeed.
    wallet_txn = await wallets.request_withdrawal("player-1", 250, "telebirr", "Test Player", "0912345678")
    assert wallet_txn.status == "pending"
    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.won_balance == 0
    assert wallet.balance == 500  # deposited principal remains, untouched and unwithdrawable


@pytest.mark.asyncio
async def test_stake_spend_preserves_won_balance_until_deposited_runs_out(wallets):
    """Spending order: bonus -> deposited principal -> won_balance last,
    so a player's withdrawable winnings survive as long as possible."""
    txn = await wallets.request_deposit("player-1", 50, "telebirr")
    await wallets.approve_topup(txn.id, "admin-1")
    await wallets.credit_payout("player-1", 100, "room-1", funded_by="real")
    # balance = 150, won_balance = 100, deposited portion = 50

    wallet, funded_by = await wallets.debit_for_stake("player-1", 15, "room-2")
    assert funded_by == "real"
    assert wallet.balance == 135
    assert wallet.won_balance == 100  # untouched - the deposited portion easily covered the stake


@pytest.mark.asyncio
async def test_stake_spend_dips_into_won_balance_once_deposited_exhausted(wallets):
    txn = await wallets.request_deposit("player-1", 50, "telebirr")
    await wallets.approve_topup(txn.id, "admin-1")
    await wallets.credit_payout("player-1", 100, "room-1", funded_by="real")
    # balance = 150, won_balance = 100, deposited portion = 50

    wallet, funded_by = await wallets.debit_for_stake("player-1", 70, "room-2")
    assert funded_by == "real"
    assert wallet.balance == 80
    # 50 came from deposited portion, remaining 20 had to come from won_balance
    assert wallet.won_balance == 80


@pytest.mark.asyncio
async def test_rejected_withdrawal_refunds_won_balance(wallets):
    # Model an already-approved historical deposit without adding principal
    # to this focused refund scenario.
    await wallets.get_or_create_wallet("player-1")
    await wallets.wallet_repo.update("player-1", {"lifetime_deposited": 500})
    await wallets.credit_payout("player-1", 300, "room-1", funded_by="real")
    txn = await wallets.request_withdrawal("player-1", 200, "telebirr", "Test Player", "0912345678")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.won_balance == 100
    assert wallet.balance == 100

    await wallets.reject_withdrawal(txn.id, "admin-1", note="bad screenshot")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.won_balance == 300
    assert wallet.balance == 300


@pytest.mark.asyncio
async def test_no_deposit_cannot_withdraw_legacy_won_balance(wallets):
    """A historical/misclassified bonus payout cannot be cashed out."""
    wallet = await wallets.get_or_create_wallet("player-1")
    await wallets.wallet_repo.update("player-1", {"balance": 300, "won_balance": 300})

    with pytest.raises(InsufficientBalanceError, match="approved real-money deposit"):
        await wallets.request_withdrawal("player-1", 200, "telebirr", "Test Player", "0912345678")


@pytest.mark.asyncio
async def test_unknown_funding_payout_fails_closed_to_bonus(wallets):
    await wallets.credit_payout("player-1", 250, "room-1", funded_by="unfunded")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.balance == 0
    assert wallet.won_balance == 0
    assert wallet.bonus_balance == 250


@pytest.mark.asyncio
async def test_bonus_win_never_becomes_withdrawable(wallets):
    """Bonus-funded wins stay locked in bonus_balance - they must never
    leak into won_balance regardless of how the rest of the wallet math
    changes."""
    wallet = await wallets.get_or_create_wallet("player-1")
    await wallets.wallet_repo.update("player-1", {"bonus_balance": 50})
    await wallets.credit_payout("player-1", 40, "room-1", funded_by="bonus")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.bonus_balance == 90
    assert wallet.won_balance == 0
    assert wallet.balance == 0


# ---------------------------------------------------------------------
# merge_wallets - folding a web-only session into a linked Telegram
# account (see /api/link/* + telegram_bot.py's _complete_account_link).
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_wallets_moves_everything_additively(wallets):
    # Web session already has some money in every bucket.
    await wallets.request_deposit("web_abc", 300, "telebirr")
    txn = (await wallets.transaction_repo.list_pending_topups())[0]
    await wallets.approve_topup(txn.id, "admin-1")
    await wallets.credit_payout("web_abc", 150, "room-1", funded_by="real")

    # Telegram account already had its own balance too.
    await wallets.request_deposit("tg_999", 100, "telebirr")
    txn2 = (await wallets.transaction_repo.list_pending_topups())[0]
    await wallets.approve_topup(txn2.id, "admin-1")

    merged = await wallets.merge_wallets("web_abc", "tg_999")

    assert merged.balance == 300 + 150 + 100
    assert merged.won_balance == 150  # only the payout counted as winnings
    assert merged.lifetime_deposited == 300 + 100

    # Source wallet is zeroed out, not deleted.
    source = await wallets.get_or_create_wallet("web_abc")
    assert source.balance == 0
    assert source.won_balance == 0
    assert source.bonus_balance == 0


@pytest.mark.asyncio
async def test_merge_wallets_combines_bonus_balances(wallets):
    await wallets.grant_free_credit_if_eligible("web_abc")  # +50 bonus by default fallback
    merged = await wallets.merge_wallets("web_abc", "tg_999")
    assert merged.bonus_balance == 50
    assert merged.free_credit_granted is True


@pytest.mark.asyncio
async def test_merge_wallets_free_credit_flag_prevents_double_grant(wallets):
    """A player can't farm a second free credit by generating a fresh
    anonymous web session and linking it into an account that already
    got one."""
    await wallets.grant_free_credit_if_eligible("tg_999")  # already has it
    await wallets.merge_wallets("web_new_session", "tg_999")

    merged = await wallets.get_or_create_wallet("tg_999")
    assert merged.free_credit_granted is True
    # Attempting to grant again must be a no-op (already granted).
    result = await wallets.grant_free_credit_if_eligible("tg_999")
    assert result is None


@pytest.mark.asyncio
async def test_merge_wallets_with_nothing_to_move_is_a_cheap_noop(wallets):
    """A brand new, completely empty web session merging in should not
    blow up and should not create bogus transaction records."""
    merged = await wallets.merge_wallets("web_empty", "tg_999")
    assert merged.balance == 0
    assert merged.bonus_balance == 0
    assert merged.won_balance == 0


@pytest.mark.asyncio
async def test_merge_wallets_preserves_destination_existing_funds(wallets):
    await wallets.request_deposit("tg_999", 1000, "telebirr")
    txn = (await wallets.transaction_repo.list_pending_topups())[0]
    await wallets.approve_topup(txn.id, "admin-1")

    merged = await wallets.merge_wallets("web_empty_session", "tg_999")
    assert merged.balance == 1000


# ---------------------------------------------------------------------------
# Deposit approval: single credit + first-deposit reward hook
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approving_same_deposit_twice_credits_once(wallets):
    txn = await wallets.request_deposit("player-1", 500, "telebirr")
    await wallets.approve_topup(txn.id, "admin-1")
    again = await wallets.approve_topup(txn.id, "admin-2", source="bot")

    wallet = await wallets.get_or_create_wallet("player-1")
    assert wallet.balance == 500
    assert wallet.lifetime_deposited == 500
    assert again.status == "completed"
    assert again.admin_id == "admin-1"  # the first approver wins; the second changes nothing


@pytest.mark.asyncio
async def test_concurrent_approvals_credit_once(wallets):
    import asyncio

    txn = await wallets.request_deposit("player-1", 300, "telebirr")
    await asyncio.gather(
        wallets.approve_topup(txn.id, "admin-1"),
        wallets.approve_topup(txn.id, "admin-2", source="bot"),
    )
    assert (await wallets.get_or_create_wallet("player-1")).balance == 300


@pytest.mark.asyncio
async def test_approve_topup_unknown_transaction_raises(wallets):
    from src.services.wallet_service import TransactionNotFoundError

    with pytest.raises(TransactionNotFoundError):
        await wallets.approve_topup("does-not-exist", "admin-1")


@pytest.mark.asyncio
async def test_approve_topup_ignores_non_deposit_transactions(wallets):
    """A pending withdrawal shares status='pending' with a pending deposit; it must never be credited."""
    await wallets.get_or_create_wallet("player-1")
    withdrawal = Transaction(player_id="player-1", type="withdrawal_pending", amount=-100, status="pending")
    await wallets.transaction_repo.create(withdrawal)

    result = await wallets.approve_topup(withdrawal.id, "admin-1")

    assert result.type == "withdrawal_pending" and result.status == "pending"
    assert (await wallets.get_or_create_wallet("player-1")).balance == 0


@pytest.mark.asyncio
async def test_first_deposit_hook_runs_only_for_first_deposit(wallets, monkeypatch):
    calls = []

    async def fake_hook(conn, txn):
        calls.append(txn.id)
        return {"status": "granted", "amount_etb": 100, "percentage": 50}

    monkeypatch.setattr(wallets, "_award_first_deposit_in_tx", fake_hook)

    first = await wallets.request_deposit("player-1", 200, "telebirr")
    await wallets.approve_topup(first.id, "admin-1")
    second = await wallets.request_deposit("player-1", 200, "telebirr")
    await wallets.approve_topup(second.id, "admin-1")

    assert calls == [first.id]  # not for the second deposit, and not twice for the first


class _FakeSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False  # never swallow the exception, like a real savepoint


class _FakeConn:
    def transaction(self):
        return _FakeSavepoint()


@pytest.mark.asyncio
async def test_reward_failure_does_not_fail_deposit_and_is_audited(wallets, monkeypatch):
    from src.services.bonus_service import BonusService

    recorded = []

    async def boom(self, conn, player_id, deposit_id, amount):
        raise RuntimeError("database hiccup")

    async def record(self, conn, player_id, deposit_id, error):
        recorded.append((player_id, deposit_id, error))

    monkeypatch.setattr(BonusService, "award_first_deposit_in_tx", boom)
    monkeypatch.setattr(BonusService, "record_award_failure", record)

    txn = Transaction(player_id="player-1", type="top_up_approved", amount=200)
    result = await wallets._award_first_deposit_in_tx(_FakeConn(), txn)

    assert result is None  # no exception escapes, so the deposit transaction still commits
    assert len(recorded) == 1 and recorded[0][:2] == ("player-1", txn.id)
    assert "database hiccup" in recorded[0][2]


# ---------------------------------------------------------------------------
# Stake charging / payouts go through the atomic repository operations
# ---------------------------------------------------------------------------
async def _seed(wallets, player_id, balance=0, won=0, bonus=0):
    wallet = await wallets.get_or_create_wallet(player_id)
    wallet.balance, wallet.won_balance, wallet.bonus_balance = balance, won, bonus
    return wallet


@pytest.mark.asyncio
async def test_mixed_stake_spends_real_first_and_records_split(wallets):
    await _seed(wallets, "p1", balance=100, bonus=30)
    wallet, funded_by = await wallets.debit_for_stake("p1", 50, "room-1", funding_mode="any")
    assert funded_by == "real"
    assert (wallet.balance, wallet.bonus_balance) == (50, 30)


@pytest.mark.asyncio
async def test_two_simultaneous_stakes_cannot_spend_the_same_bonus(wallets):
    import asyncio

    await _seed(wallets, "p1", bonus=100)
    results = await asyncio.gather(
        wallets.debit_for_stake("p1", 60, "room-1", funding_mode="bonus_only"),
        wallets.debit_for_stake("p1", 60, "room-1", funding_mode="bonus_only"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, InsufficientBalanceError) for r in results) == 1
    assert (await wallets.get_or_create_wallet("p1")).bonus_balance == 40


@pytest.mark.asyncio
async def test_insufficient_stake_reports_total_and_changes_nothing(wallets):
    await _seed(wallets, "p1", balance=10, bonus=5)
    with pytest.raises(InsufficientBalanceError):
        await wallets.debit_for_stake("p1", 50, "room-1", funding_mode="any")
    wallet = await wallets.get_or_create_wallet("p1")
    assert (wallet.balance, wallet.bonus_balance) == (10, 5)


@pytest.mark.asyncio
async def test_refund_returns_each_pool_without_creating_winnings(wallets):
    await _seed(wallets, "p1", balance=100, bonus=30)
    _, funded_by = await wallets.debit_for_stake("p1", 50, "room-1", funding_mode="any")
    wallet = await wallets.refund_stake("p1", 50, "room-1", funded_by)
    assert (wallet.balance, wallet.bonus_balance, wallet.won_balance) == (100, 30, 0)


@pytest.mark.asyncio
async def test_payout_split_only_real_part_is_withdrawable(wallets):
    wallet = await wallets.credit_payout_split("p1", real_amount=70, bonus_amount=30, room_id="room-1")
    assert (wallet.balance, wallet.won_balance, wallet.bonus_balance) == (70, 70, 30)


@pytest.mark.asyncio
async def test_referral_and_admin_credits_land_in_the_right_pool(wallets):
    await wallets.credit_referral_bonus("p1", 20, "friend-1")
    await wallets.admin_adjust("p1", 15, "admin-1", note="goodwill")
    wallet = await wallets.get_or_create_wallet("p1")
    assert (wallet.bonus_balance, wallet.balance, wallet.won_balance) == (20, 15, 0)


@pytest.mark.asyncio
async def test_withdrawal_hold_debits_and_records_in_one_step(wallets):
    await wallets.wallet_repo.create(Wallet(player_id="p9", balance=500, won_balance=300, lifetime_deposited=200))
    txn = await wallets.request_withdrawal("p9", 250, "telebirr", "Test Player", "0912345678")
    w = await wallets.get_or_create_wallet("p9")
    assert (w.balance, w.won_balance) == (250, 50)
    assert txn.balance_after == 250 and txn.amount == -250 and txn.status == "pending"


@pytest.mark.asyncio
async def test_withdrawal_rejects_non_positive_amount(wallets):
    with pytest.raises(InvalidAmountError):
        await wallets.request_withdrawal("p9", 0, "telebirr", "Test Player", "0912345678")
    with pytest.raises(InvalidAmountError):
        await wallets.request_withdrawal("p9", -500, "telebirr", "Test Player", "0912345678")


@pytest.mark.asyncio
async def test_rejecting_a_withdrawal_twice_refunds_once(wallets):
    await wallets.wallet_repo.create(Wallet(player_id="p11", balance=500, won_balance=300, lifetime_deposited=100))
    txn = await wallets.request_withdrawal("p11", 200, "telebirr", "Test Player", "0912345678")
    await wallets.reject_withdrawal(txn.id, "admin-1")
    await wallets.reject_withdrawal(txn.id, "admin-2")
    await wallets.approve_withdrawal(txn.id, "admin-3")  # late approve of a rejected request: no-op
    w = await wallets.get_or_create_wallet("p11")
    assert (w.balance, w.won_balance) == (500, 300)
