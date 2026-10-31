"""In-memory stand-ins for WalletRepository's atomic operations, shared by test fakes.

A fake using this mixin must provide `self.wallets` (player_id -> Wallet) and an
async `create(wallet)`. These run with no `await` between check and write, so they
are atomic on a single event loop - real concurrency safety is a property of the
Postgres implementation and needs an integration test against a real database.
"""
from src.models.schemas import Wallet
from src.repositories.wallet_repository import WithdrawalRejected
from src.services.stake_math import plan_stake_debit


class AtomicWalletOps:
    async def _wallet_or_create(self, player_id):
        wallet = self.wallets.get(player_id)
        if wallet is None:
            wallet = await self.create(Wallet(player_id=player_id))
        return wallet

    async def apply_delta(self, player_id, balance=0, won=0, bonus=0):
        wallet = await self._wallet_or_create(player_id)
        wallet.balance += balance
        wallet.won_balance += won
        wallet.bonus_balance += bonus
        return wallet

    async def debit_stake_atomic(self, player_id, amount, funding_mode):
        wallet = await self._wallet_or_create(player_id)
        split = plan_stake_debit(wallet.balance, wallet.won_balance, wallet.bonus_balance, amount, funding_mode)
        if split is None:
            return None
        wallet.balance -= split.from_real
        wallet.bonus_balance -= split.from_bonus
        wallet.won_balance -= split.from_won
        return wallet, split

    async def hold_withdrawal_atomic(self, txn):
        """Mirror of WalletRepository.hold_withdrawal_atomic (no await between check and write)."""
        hold = -txn.amount
        wallet = self.wallets.get(txn.player_id)
        if wallet is None or wallet.lifetime_deposited <= 0:
            raise WithdrawalRejected("no_deposit")
        if wallet.won_balance < hold or wallet.balance < hold:
            raise WithdrawalRejected("insufficient_won")
        wallet.balance -= hold
        wallet.won_balance -= hold
        txn.balance_after = wallet.balance
        return await self.transaction_repo.create(txn)

    async def grant_free_credit_atomic(self, player_id, amount):
        wallet = await self._wallet_or_create(player_id)
        if wallet.free_credit_granted:
            return None
        wallet.bonus_balance += amount
        wallet.free_credit_granted = True
        return wallet

    async def resolve_withdrawal_atomic(self, transaction_id, admin_id, action, note=""):
        from src.models.schemas import utcnow

        txn = self.transaction_repo.transactions.get(transaction_id)
        if txn is None:
            return None, False
        if txn.type != "withdrawal_pending" or txn.status != "pending":
            return txn, False
        wallet = await self._wallet_or_create(txn.player_id)
        if action == "approve" and wallet.lifetime_deposited <= 0:
            action, note = "reject", "Rejected automatically: no approved real-money deposit"
        if action == "approve":
            txn.type, txn.status = "withdrawal_approved", "completed"
        else:
            refund = abs(txn.amount)
            wallet.balance += refund
            wallet.won_balance += refund
            txn.type, txn.status, txn.note = "withdrawal_rejected", "rejected", note
        txn.admin_id, txn.resolved_at = admin_id, utcnow()
        return txn, True
