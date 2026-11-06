"""
Wallet service - all balance changes go through here, so every credit/debit
is paired with a `Transaction` audit record. No real money is ever handled
in code: deposits are approved by a human admin after reviewing a payment
screenshot/reference, and withdrawals are paid out manually by admins using
the ledger as their source of truth.

Two balances per wallet:
- `balance`: real money, fully withdrawable.
- `bonus_balance`: promotional credit (the registration bonus, goodwill
  credits, etc). Fully playable, but never withdrawable - this is shown to
  players plainly in the UI. This is what keeps the bonus program from
  bankrupting the house: it's an honest wagering restriction, not a secret
  thumb on the scale of any game. Every draw uses the same fair RNG for
  every player, bonus-funded or not.

When a stake is charged in the single real-money room, real balance is spent
first, then bonus credit covers any remainder (the published deterministic
funding rule). Whichever pool funded a card determines where
its winnings land: bonus-funded cards pay out to bonus_balance (still
locked), real-funded cards pay out to the real, withdrawable balance.
"""
import logging
from typing import List, Optional, Tuple

import asyncpg

from src.core.config import settings
from src.models.schemas import Transaction, Wallet, utcnow
from src.repositories.settings_repository import SettingsRepository
from src.repositories.transaction_repository import TransactionRepository
from src.repositories.wallet_repository import WalletRepository, WithdrawalRejected

logger = logging.getLogger(__name__)


class InsufficientBalanceError(Exception):
    pass


def _funding_parts(funded_by: str, amount: int) -> tuple[int, int]:
    """Decode real/bonus funding stored on a charged card.

    Unknown or legacy funding values deliberately fail closed to bonus.  A
    missing marker must never turn promotional funds into withdrawable money.
    """
    if funded_by.startswith("mixed:"):
        try:
            _, real, bonus = funded_by.split(":", 2)
            real_amount, bonus_amount = int(real), int(bonus)
            if real_amount >= 0 and bonus_amount >= 0 and real_amount + bonus_amount:
                return real_amount, bonus_amount
        except (TypeError, ValueError):
            pass
    return (amount, 0) if funded_by == "real" else (0, amount)


def funding_parts(funded_by: str, amount: int) -> tuple[int, int]:
    """Public wrapper around `_funding_parts` for other modules (e.g.
    RoomService, to total up a room's real vs bonus prize pool)."""
    return _funding_parts(funded_by, amount)


class TransactionNotFoundError(Exception):
    pass


class InvalidAmountError(Exception):
    pass


class WithdrawalAddressMissingError(Exception):
    pass


class DuplicateTransactionError(Exception):
    pass


class WalletService:
    def __init__(
        self,
        wallet_repo: Optional[WalletRepository] = None,
        transaction_repo: Optional[TransactionRepository] = None,
        settings_repo: Optional[SettingsRepository] = None,
    ):
        self.wallet_repo = wallet_repo or WalletRepository()
        self.transaction_repo = transaction_repo or TransactionRepository()
        self.settings_repo = settings_repo or SettingsRepository()

    async def _limits(self):
        """Runtime-configurable limits, falling back to .env defaults if
        the admin has never touched settings yet (or Firestore isn't
        reachable, e.g. in unit tests using fakes)."""
        try:
            return await self.settings_repo.get_or_create()
        except Exception:
            return None

    @staticmethod
    def _control():
        from src.services.control_service import get_control_service

        return get_control_service()

    async def get_or_create_wallet(self, player_id: str) -> Wallet:
        wallet = await self.wallet_repo.get_by_player(player_id)
        if wallet:
            return wallet
        return await self.wallet_repo.create(Wallet(player_id=player_id))

    async def merge_wallets(self, from_player_id: str, to_player_id: str) -> Wallet:
        """Folds a source wallet's balances into a destination wallet -
        used when a web-only session (`web_<uuid>`) completes the
        "Link Telegram Account" flow, so any deposit/bonus/winnings the
        player already had aren't lost. Additive: the destination keeps
        whatever it already had, plus everything from the source. The
        source wallet is zeroed out (not deleted, for audit continuity).
        `free_credit_granted` is OR'd together so a player can't farm a
        second free credit by generating a fresh anonymous session and
        linking it in."""
        from_wallet = await self.get_or_create_wallet(from_player_id)
        to_wallet = await self.get_or_create_wallet(to_player_id)

        moved_playable = from_wallet.balance + from_wallet.bonus_balance
        if moved_playable == 0 and from_wallet.won_balance == 0:
            # Nothing to move financially - still fold in the free-credit
            # flag (see docstring) and stop, no transaction noise needed.
            if from_wallet.free_credit_granted and not to_wallet.free_credit_granted:
                await self.wallet_repo.update(to_player_id, {"free_credit_granted": True})
                to_wallet.free_credit_granted = True
            return to_wallet

        new_balance = to_wallet.balance + from_wallet.balance
        new_won = to_wallet.won_balance + from_wallet.won_balance
        new_bonus = to_wallet.bonus_balance + from_wallet.bonus_balance
        new_lifetime = to_wallet.lifetime_deposited + from_wallet.lifetime_deposited
        merged_free_credit = to_wallet.free_credit_granted or from_wallet.free_credit_granted

        await self.wallet_repo.update(to_player_id, {
            "balance": new_balance,
            "won_balance": new_won,
            "bonus_balance": new_bonus,
            "lifetime_deposited": new_lifetime,
            "free_credit_granted": merged_free_credit,
        })
        await self.wallet_repo.update(from_player_id, {"balance": 0, "won_balance": 0, "bonus_balance": 0})
        await self.transaction_repo.create(
            Transaction(
                player_id=to_player_id,
                type="account_link_merge",
                amount=moved_playable,
                balance_after=new_balance,
                status="completed",
                note=f"Merged web session {from_player_id} after linking a Telegram account",
            )
        )

        to_wallet.balance = new_balance
        to_wallet.won_balance = new_won
        to_wallet.bonus_balance = new_bonus
        to_wallet.lifetime_deposited = new_lifetime
        to_wallet.free_credit_granted = merged_free_credit
        return to_wallet

    # ------------------------------------------------------------------
    # Registration bonus
    # ------------------------------------------------------------------

    async def grant_free_credit_if_eligible(self, player_id: str) -> Optional[Transaction]:
        """First-time-user incentive: a one-off bonus credit (default 50
        ETB) so new players can try the game before depositing. Goes into
        `bonus_balance` (playable, not withdrawable). Only granted once."""
        s = await self._limits()
        amount = s.free_credit_amount if s else 50
        # One guarded UPDATE: concurrent callers cannot both win the grant.
        wallet = await self.wallet_repo.grant_free_credit_atomic(player_id, amount)
        if wallet is None:
            return None  # already granted
        return await self.transaction_repo.create(
            Transaction(
                player_id=player_id,
                type="free_credit",
                amount=amount,
                balance_after=wallet.bonus_balance,
                status="completed",
                note="First-time registration bonus (bonus balance - not withdrawable)",
            )
        )

    async def credit_engagement_bonus(self, player_id: str, amount: int, tx_type: str, note: str) -> Transaction:
        """House-funded engagement reward (streak reward, win-back free round). Goes to
        `bonus_balance`: playable, never withdrawable. `tx_type` is 'streak_reward' or 'winback_bonus'."""
        if amount <= 0:
            raise ValueError("bonus amount must be positive")
        wallet = await self.wallet_repo.apply_delta(player_id, bonus=amount)
        return await self.transaction_repo.create(
            Transaction(
                player_id=player_id, type=tx_type, amount=amount, balance_after=wallet.bonus_balance,
                status="completed", note=f"{note} (bonus balance - not withdrawable)",
            )
        )

    async def credit_referral_bonus(self, player_id: str, amount: int, referred_player_id: str) -> Transaction:
        """Credit referral reward to the inviter's bonus_balance."""
        wallet = await self.wallet_repo.apply_delta(player_id, bonus=amount)
        new_bonus = wallet.bonus_balance
        return await self.transaction_repo.create(
            Transaction(
                player_id=player_id,
                type="referral_bonus",
                amount=amount,
                balance_after=new_bonus,
                status="completed",
                note=f"Referred player ID: {referred_player_id} (bonus balance - not withdrawable)",
            )
        )

    # ------------------------------------------------------------------
    # Deposits (self-serve amount, admin-approved)
    # ------------------------------------------------------------------

    async def request_deposit(
        self,
        player_id: str,
        amount: int,
        payment_method: str,
        screenshot_file_id: Optional[str] = None,
        screenshot_url: Optional[str] = None,
        sms_text: Optional[str] = None,
        payment_verification_url: Optional[str] = None,
    ) -> Transaction:
        """Player says they sent `amount` via `payment_method`; balance is
        NOT changed yet - an admin verifies the payment and approves it."""
        s = await self._limits()
        if s and not getattr(s, "deposits_enabled", True):
            raise InvalidAmountError("Deposits are currently disabled by the system administrator.")
        min_dep = s.min_deposit if s else 50
        max_dep = s.max_deposit if s else 70000
        if amount < min_dep or amount > max_dep:
            raise InvalidAmountError(f"Deposit amount must be between {min_dep} and {max_dep} ETB")
        # Control Centre: feature flag / kill switch, frozen account, rolling 24h limit.
        blocked = await self._control().flag_block("deposits") or await self._control().player_money_block(player_id, "deposit", amount)
        if blocked:
            raise InvalidAmountError(blocked)

        if payment_verification_url:
            existing = await self.transaction_repo.get_by_payment_url(payment_verification_url)
            if existing:
                raise DuplicateTransactionError("This payment transaction has already been submitted.")

        try:
            txn = await self.transaction_repo.create(
                Transaction(
                    player_id=player_id,
                    type="top_up_pending",
                    amount=amount,
                    status="pending",
                    payment_method=payment_method,
                    screenshot_file_id=screenshot_file_id,
                    screenshot_url=screenshot_url,
                    sms_text=sms_text,
                    payment_verification_url=payment_verification_url,
                )
            )
        except asyncpg.UniqueViolationError:
            # Two simultaneous submissions of the same payment: the unique index
            # (uq_transactions_payment_verification_url) lets exactly one through.
            raise DuplicateTransactionError("This payment transaction has already been submitted.")
        try:
            from src.services.notifications import notify_admins_of_deposit

            ref = await notify_admins_of_deposit(txn)
            if ref:
                chat_id, message_id, is_photo = ref
                await self.transaction_repo.set_notification_ref(txn.id, chat_id, message_id, is_photo)
        except Exception:  # noqa: BLE001
            # Never let a notification hiccup block the deposit request
            # itself - the transaction is already saved and will still
            # show up in /pending and the admin panel either way.
            logger.exception("Failed to notify admins of deposit %s", txn.id)
        # Control Centre auto-approve rule (off by default): trusted players, small amounts, with a payment link.
        try:
            auto, why = await self._control().should_auto_approve(player_id, amount, bool(payment_verification_url))
            if auto:
                approved = await self.approve_topup(txn.id, "auto", source="auto")
                await self._control().audit(
                    "auto", "deposit.auto_approve", target_type="transaction", target_id=txn.id,
                    summary=f"{amount} ETB for {player_id}: {why}", after={"status": approved.status},
                )
                return approved
        except Exception:  # noqa: BLE001 - a failed auto-approve simply leaves the deposit for a human
            logger.exception("Auto-approve failed for deposit %s; left pending", txn.id)
        return txn

    async def _resolve_admin_label(self, admin_id: str) -> str:
        try:
            from src.repositories.player_repository import PlayerRepository

            admin_player = await PlayerRepository().get_by_telegram_id(admin_id)
            if admin_player:
                return admin_player.display_name
        except Exception:  # noqa: BLE001
            pass
        return admin_id

    async def _sync_notification(self, txn: Transaction, emoji: str, verb: str, admin_id: str, source: str, extra: str = "") -> None:
        """Edits the original admin-alert message (in the bot/group, if
        one was sent) to remove its buttons and show the resolution - so
        the group and the web admin panel can never show contradictory
        state for the same transaction."""
        try:
            from src.services.notifications import update_notification_message

            admin_label = await self._resolve_admin_label(admin_id)
            source_label = "Telegram" if source == "bot" else "web admin panel"
            line = f"{emoji} *{verb}* by {admin_label} (via {source_label})"
            if extra:
                line += f"\n{extra}"
            await update_notification_message(txn, line)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to sync notification message for transaction %s", txn.id)

    async def _award_first_deposit_in_tx(self, conn, txn: Transaction):
        """First-deposit reward hook, run inside the deposit-approval transaction.

        Policy: the cash deposit always succeeds, but a reward failure is never
        silent. The reward attempt runs in a savepoint (so a failure cannot poison
        the deposit) and, on error, leaves a durable audit row that can be re-run
        later via `BonusService.award_first_deposit` (idempotent per deposit ID).
        Expected outcomes (campaign paused, budget exhausted, ...) are returned,
        not raised.
        """
        if conn is None:  # in-memory repository (unit tests): no database, no reward
            return None
        from src.services.bonus_service import BonusService

        bonus = BonusService()
        try:
            async with conn.transaction():
                return await bonus.award_first_deposit_in_tx(conn, txn.player_id, txn.id, txn.amount)
        except Exception as exc:  # noqa: BLE001
            logger.exception("First-deposit bonus failed for deposit %s; cash deposit stays approved", txn.id)
            try:
                async with conn.transaction():
                    await bonus.record_award_failure(conn, txn.player_id, txn.id, repr(exc))
            except Exception:  # noqa: BLE001
                logger.exception("Could not record first-deposit bonus failure for deposit %s", txn.id)
            return None

    async def _credit_agent_commission_in_tx(self, conn, txn: Transaction):
        """Referral-agent reward hook, run inside the deposit-approval transaction. A failure here is
        logged and rolled back in a savepoint; it never blocks the player's cash deposit."""
        if conn is None:  # in-memory repository (unit tests): no database
            return None
        from src.repositories.agent_repository import AgentRepository

        try:
            async with conn.transaction():
                return await AgentRepository().credit_referral_commission_in_tx(
                    conn, txn.player_id, txn.id, txn.amount
                )
        except Exception:  # noqa: BLE001
            logger.exception("Agent commission failed for deposit %s; cash deposit stays approved", txn.id)
            return None

    async def approve_topup(self, transaction_id: str, admin_id: str, source: str = "web") -> Transaction:
        # One database transaction with row locks: concurrent approvals credit once,
        # and the first-deposit reward commits atomically with the cash credit.
        result = await self.wallet_repo.approve_topup_atomic(
            transaction_id, admin_id,
            on_first_deposit=self._award_first_deposit_in_tx,
            on_approved=self._credit_agent_commission_in_tx,
        )
        if result.txn is None:
            raise TransactionNotFoundError(transaction_id)
        txn = result.txn
        if not result.applied:
            return txn  # already resolved (or not a deposit): nothing credited here
        reward = result.extra or {}
        bonus_note = ""
        if reward.get("status") == "granted":
            bonus_note = f" First-deposit reward: +{reward['amount_etb']} bonus ETB ({reward['percentage']}%)."
        await self._sync_notification(txn, "✅", "Deposit approved", admin_id, source, f"{txn.amount} ETB credited.{bonus_note}")
        from src.services import player_notifications as pn

        pn.schedule_notify(
            txn.player_id, "deposit_approved",
            {"amount": txn.amount, "bonus_note": bonus_note.strip()}, dedupe_key=f"deposit:{txn.id}",
        )
        commission = result.approved_extra
        if commission is not None:  # the player is linked to an active agent: held new-player reward + tier refresh
            from src.services.agent_rewards import get_agent_reward_service

            await get_agent_reward_service().on_player_deposit(txn.player_id, txn.amount, txn.id)
        if commission and commission.get("earned"):
            pn.schedule_notify(
                commission["agent_player_id"], "agent_commission_earned",
                {"amount": commission["earned"]}, dedupe_key=f"agent_commission:{txn.id}",
            )
        return txn

    async def reject_topup(self, transaction_id: str, admin_id: str, note: str = "", source: str = "web") -> Transaction:
        txn = await self.transaction_repo.get(transaction_id)
        if not txn:
            raise TransactionNotFoundError(transaction_id)
        if txn.status != "pending":
            return txn
        await self.transaction_repo.update(
            transaction_id,
            {
                "type": "top_up_rejected",
                "status": "rejected",
                "admin_id": admin_id,
                "note": note,
                "resolved_at": utcnow(),
            },
        )
        txn.type = "top_up_rejected"
        txn.status = "rejected"
        await self._sync_notification(txn, "❌", "Deposit rejected", admin_id, source, note or "")
        from src.services import player_notifications as pn

        pn.schedule_notify(txn.player_id, "deposit_rejected", {"amount": txn.amount, "note": note}, dedupe_key=f"deposit:{txn.id}")
        return txn

    async def list_pending_topups(self) -> List[Transaction]:
        return await self.transaction_repo.list_pending_topups()

    async def list_deposit_history(self, limit: int = 200, status_filter: Optional[str] = None) -> List[Transaction]:
        return await self.transaction_repo.list_deposit_history(limit=limit, status_filter=status_filter)

    async def list_pending_withdrawals(self) -> List[Transaction]:
        return await self.transaction_repo.list_pending_withdrawals()

    async def list_withdrawal_history(self, limit: int = 200, status_filter: Optional[str] = None) -> List[Transaction]:
        return await self.transaction_repo.list_withdrawal_history(limit=limit, status_filter=status_filter)

    # ------------------------------------------------------------------
    # Withdrawals (Mini App only, admin-approved)
    # ------------------------------------------------------------------

    async def request_withdrawal(
        self,
        player_id: str,
        amount: int,
        payment_method: str,
        legal_name: str,
        withdrawal_address: str,
    ) -> Transaction:
        """Holds the funds immediately (debited from real balance only -
        bonus balance can never be withdrawn) so a player can't request the
        same money twice. Refunded automatically if an admin rejects it."""
        if not withdrawal_address:
            raise WithdrawalAddressMissingError("Set a withdrawal address for this method first")
        if not legal_name or len(legal_name.strip()) < 2:
            raise InvalidAmountError("Legal name is required")

        if amount <= 0:
            raise InvalidAmountError("Withdrawal amount must be positive")

        s = await self._limits()
        min_wd = s.min_withdrawal if s else 200
        max_wd = s.max_withdrawal if s else 10000
        if amount < min_wd or amount > max_wd:
            raise InvalidAmountError(f"Withdrawal amount must be between {min_wd} and {max_wd} ETB")
        # Control Centre: feature flag / kill switch, frozen account, rolling 24h limit.
        blocked = await self._control().flag_block("withdrawals") or await self._control().player_money_block(player_id, "withdrawal", amount)
        if blocked:
            raise InvalidAmountError(blocked)

        # Eligibility is re-checked inside the DB transaction on the locked wallet row
        # (see WalletRepository.hold_withdrawal_atomic); the debit and the pending
        # transaction commit together or not at all.
        try:
            txn = await self.wallet_repo.hold_withdrawal_atomic(
                Transaction(
                    player_id=player_id,
                    type="withdrawal_pending",
                    amount=-amount,
                    status="pending",
                    payment_method=payment_method,
                    legal_name=legal_name.strip(),
                    withdrawal_address=withdrawal_address,
                )
            )
        except WithdrawalRejected as e:
            if e.reason == "no_deposit":
                raise InsufficientBalanceError(
                    "Withdrawals require at least one approved real-money deposit. "
                    "Bonus credit and winnings generated from it stay in the bonus balance."
                )
            raise InsufficientBalanceError(
                f"Player {player_id} requested {amount} ETB but has less than that in withdrawable winnings. "
                f"Deposited funds are playable but not withdrawable - only game winnings can be withdrawn."
            )
        try:
            from src.services.notifications import notify_admins_of_withdrawal

            ref = await notify_admins_of_withdrawal(txn)
            if ref:
                chat_id, message_id, is_photo = ref
                await self.transaction_repo.set_notification_ref(txn.id, chat_id, message_id, is_photo)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify admins of withdrawal %s", txn.id)
        try:
            from src.repositories.player_repository import PlayerRepository
            from src.services.admin_alerts import get_alert_service

            player = await PlayerRepository().get_by_id(player_id)
            await get_alert_service().on_withdrawal_requested(
                player_id, player.display_name if player else player_id, amount, payment_method,
            )
        except Exception:  # noqa: BLE001 - an alert problem must never fail a withdrawal
            logger.warning("Withdrawal alert skipped for %s", txn.id, exc_info=True)
        return txn

    async def approve_withdrawal(self, transaction_id: str, admin_id: str, source: str = "web") -> Transaction:
        """Funds were already held at request time - approving just marks
        it paid, since the admin sends the money manually off-platform.
        Atomic and idempotent: a second concurrent approve/reject is a no-op."""
        txn, applied = await self.wallet_repo.resolve_withdrawal_atomic(transaction_id, admin_id, "approve")
        if txn is None:
            raise TransactionNotFoundError(transaction_id)
        if not applied:
            return txn
        from src.services import player_notifications as pn

        if txn.status == "rejected":  # legacy bonus-only request auto-converted to a refund
            await self._sync_notification(txn, "❌", "Withdrawal rejected & refunded", admin_id, source, txn.note or "")
            pn.schedule_notify(txn.player_id, "withdrawal_rejected", {"amount": abs(txn.amount), "note": txn.note or ""}, dedupe_key=f"withdrawal:{txn.id}")
        else:
            await self._sync_notification(txn, "✅", "Withdrawal marked paid", admin_id, source, f"{abs(txn.amount)} ETB sent.")
            pn.schedule_notify(txn.player_id, "withdrawal_approved", {"amount": abs(txn.amount)}, dedupe_key=f"withdrawal:{txn.id}")
        return txn

    async def reject_withdrawal(self, transaction_id: str, admin_id: str, note: str = "", source: str = "web") -> Transaction:
        """Refunds the held amount back to the player's real balance (exactly once)."""
        txn, applied = await self.wallet_repo.resolve_withdrawal_atomic(transaction_id, admin_id, "reject", note)
        if txn is None:
            raise TransactionNotFoundError(transaction_id)
        if not applied:
            return txn
        await self._sync_notification(txn, "❌", "Withdrawal rejected & refunded", admin_id, source, note or "")
        from src.services import player_notifications as pn

        pn.schedule_notify(txn.player_id, "withdrawal_rejected", {"amount": abs(txn.amount), "note": note}, dedupe_key=f"withdrawal:{txn.id}")
        return txn

    # ------------------------------------------------------------------
    # Game stakes / payouts
    # ------------------------------------------------------------------

    async def debit_for_stake(
        self, player_id: str, amount: int, room_id: str, funding_mode: str = "any"
    ) -> Tuple[Wallet, str]:
        """Charge the entry stake when a round starts.

        `funding_mode` enforces which lobby track this stake belongs to,
        so real money and bonus credit can never mix in the same prize
        pool:
          - "real_only": must come entirely from real `balance` (the
            real-money rooms). Raises InsufficientBalanceError if the
            player doesn't have enough real balance, even if they have
            plenty of bonus balance sitting unused.
          - "bonus_only": must come entirely from `bonus_balance` (the
            bonus room). Same fail-closed behavior in reverse.
          - "any": spends bonus balance first, then real balance, and can
            produce a mixed split. This is the configured mode for the single
            room so cartela-only promotional funds remain usable.

        Within real balance, spends the deposited portion before touching
        won_balance, so a player's withdrawable winnings are preserved as
        long as possible. Returns (wallet, funded_by) where funded_by is
        "bonus", "real", or a recorded mixed split - the caller must
        remember this per card so a win/refund is returned to the correct
        pools."""
        wallet = await self.get_or_create_wallet(player_id)

        if funding_mode == "real_only":
            wallet = await self.wallet_repo.atomic_debit_real(player_id, amount)
            if not wallet:
                current = await self.get_or_create_wallet(player_id)
                raise InsufficientBalanceError(
                    f"Player {player_id} has {current.balance} ETB real balance, needs {amount}"
                )
            await self.transaction_repo.create(
                Transaction(
                    player_id=player_id,
                    type="game_stake",
                    amount=-amount,
                    balance_after=wallet.balance,
                    status="completed",
                    note=f"room_id={room_id} funded_by=real",
                )
            )
            return wallet, "real"
        # "bonus_only" and "any": one locked, atomic charge (see
        # WalletRepository.debit_stake_atomic) so two simultaneous purchases can
        # never spend the same money or overwrite a concurrent deposit/payout.
        result = await self.wallet_repo.debit_stake_atomic(player_id, amount, funding_mode)
        if result is None:
            current = await self.get_or_create_wallet(player_id)
            if funding_mode == "bonus_only":
                raise InsufficientBalanceError(
                    f"Player {player_id} has {current.bonus_balance} ETB bonus balance, needs {amount}"
                )
            raise InsufficientBalanceError(
                f"Player {player_id} has {current.balance + current.bonus_balance} ETB total, needs {amount}"
            )
        wallet, split = result
        await self.transaction_repo.create(
            Transaction(
                player_id=player_id,
                type="game_stake",
                amount=-amount,
                balance_after=wallet.balance,
                status="completed",
                room_id=room_id,
                note=f"funded_by={split.funded_by}",
            )
        )
        return wallet, split.funded_by

    async def refund_stake(self, player_id: str, amount: int, room_id: str, funded_by: str = "real") -> Wallet:
        """Returns a stake to the exact pool it was charged from when a
        round ends with no winner - the inverse of `debit_for_stake`. This
        is NOT a win: `won_balance` is deliberately left untouched, so a
        refund never gets counted as winnings in stats or withdrawal
        eligibility. A no-winner round must cost a player nothing."""
        real_amount, bonus_amount = _funding_parts(funded_by, amount)
        wallet = await self.wallet_repo.apply_delta(player_id, balance=real_amount, bonus=bonus_amount)
        await self.transaction_repo.create(
            Transaction(
                player_id=player_id,
                type="stake_refund",
                amount=amount,
                balance_after=wallet.balance,
                status="completed",
                room_id=room_id,
                note=f"Round ended with no winner - stake refunded (funded_by={funded_by})",
            )
        )
        return wallet

    async def credit_payout(self, player_id: str, amount: int, room_id: str, funded_by: str = "real") -> Wallet:
        """Pays winnings to the pool that funded the winning card: real
        stakes win real, WITHDRAWABLE money (credited to both `balance`
        and `won_balance`); bonus-funded stakes win more bonus balance
        (playable, still not withdrawable). This is disclosed to players
        in the UI rather than hidden.

        NOTE: for prize-pool payouts (RoomService.claim_win), prefer
        `credit_payout_split` instead - a payout can legitimately contain
        BOTH real and bonus money at once (the pool is everyone's stakes
        mixed together, not just the winner's own), and splitting by the
        winner's own funding alone lets a small real stake "launder" a
        pot mostly filled by other players' bonus credit into fully
        withdrawable money. This method is kept for other real/bonus
        payouts that genuinely are single-sourced (e.g. referral bonus
        credit, admin adjustments modeled as a payout).
        """
        wallet = await self.get_or_create_wallet(player_id)
        stake_real, stake_bonus = _funding_parts(funded_by, amount)
        stake_total = stake_real + stake_bonus
        real_payout = round(amount * stake_real / stake_total) if stake_total else amount
        bonus_payout = amount - real_payout
        return await self.credit_payout_split(player_id, real_payout, bonus_payout, room_id, note=f"funded_by={funded_by}")

    async def credit_payout_split(
        self, player_id: str, real_amount: int, bonus_amount: int, room_id: str, note: str = ""
    ) -> Wallet:
        """Credit a payout using an EXPLICIT real/bonus split, computed by
        the caller from the actual composition of the money being paid
        out (e.g. a room's prize_pool_real / prize_pool_bonus), not from
        any one player's own stake. Only `real_amount` is ever
        withdrawable; `bonus_amount` always lands in bonus_balance no
        matter who wins."""
        wallet = await self.wallet_repo.apply_delta(
            player_id, balance=real_amount, won=real_amount, bonus=bonus_amount
        )
        await self.transaction_repo.create(
            Transaction(
                player_id=player_id,
                type="win_payout",
                amount=real_amount + bonus_amount,
                balance_after=wallet.balance,
                status="completed",
                room_id=room_id,
                note=note or f"real={real_amount} bonus={bonus_amount}",
            )
        )
        return wallet

    async def admin_adjust(self, player_id: str, amount: int, admin_id: str, note: str = "") -> Wallet:
        """Manual balance correction by an admin (e.g. reconciling a
        withdrawal, or a goodwill credit). Always affects real balance."""
        wallet = await self.wallet_repo.apply_delta(player_id, balance=amount)
        await self.transaction_repo.create(
            Transaction(
                player_id=player_id,
                type="admin_adjustment",
                amount=amount,
                balance_after=wallet.balance,
                status="completed",
                admin_id=admin_id,
                note=note,
            )
        )
        return wallet
