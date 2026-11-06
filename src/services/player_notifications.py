"""Event notifications to players: stored for the in-app inbox AND sent to the player's Telegram.

Call `schedule_notify(player_id, type, data, dedupe_key=...)` AFTER the money/game transaction has
committed. It runs in the background and can never raise or delay the caller - a notification
problem must not affect a payout. Each event should pass a dedupe_key (e.g. "deposit:<txn id>") so
retries or double code paths still produce one message.

Delivery is best-effort: the inbox row is always written first; the Telegram send then records
sent / blocked (player blocked the bot) / failed on that row. Transactional messages are always
sent (promotions and opt-outs belong to the broadcast system, not here).
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from src.models.schemas import new_id

logger = logging.getLogger(__name__)


def _etb(n: Any) -> str:
    try:
        return f"{int(n):,} ETB"
    except (TypeError, ValueError):
        return "0 ETB"


# type -> (emoji, title builder, body builder). Builders only read the `data` dict.
TEMPLATES: dict[str, tuple[str, Callable[[dict], str], Callable[[dict], str]]] = {
    "deposit_approved": (
        "✅", lambda d: "Deposit approved",
        lambda d: f"{_etb(d.get('amount'))} was added to your balance." + (f" {d['bonus_note']}" if d.get("bonus_note") else ""),
    ),
    "deposit_rejected": (
        "❌", lambda d: "Deposit rejected",
        lambda d: f"Your deposit of {_etb(d.get('amount'))} could not be approved." + (f" Reason: {d['note']}" if d.get("note") else ""),
    ),
    "deposit_received": (
        "📥", lambda d: "Deposit received",
        lambda d: f"We received your deposit request of {_etb(d.get('amount'))}. An admin will review it shortly and you will get a message when it is approved.",
    ),
    "withdrawal_received": (
        "⏳", lambda d: "Withdrawal requested",
        lambda d: f"Your withdrawal of {_etb(d.get('amount'))} is waiting for review. The amount is held until it is paid or refunded to you.",
    ),
    "withdrawal_approved": (
        "💸", lambda d: "Withdrawal paid",
        lambda d: f"Your withdrawal of {_etb(d.get('amount'))} has been approved and sent.",
    ),
    "withdrawal_rejected": (
        "↩️", lambda d: "Withdrawal rejected",
        lambda d: f"Your withdrawal of {_etb(d.get('amount'))} was rejected and refunded to your balance." + (f" Reason: {d['note']}" if d.get("note") else ""),
    ),
    "win": (
        "🏆",
        lambda d: "You won!" if int(d.get("shared_with") or 1) <= 1 else "You won - shared prize!",
        lambda d: (f"Cartela #{d['cartela_number']}: " if d.get("cartela_number") else "")
        + f"{_etb(d.get('payout'))} was added to your balance."
        + (f" Your balance is now {_etb(d.get('balance'))}." if d.get("balance") is not None else "")
        + (f" Prize shared with {int(d['shared_with']) - 1} other winner(s)." if int(d.get("shared_with") or 1) > 1 else ""),
    ),
    "bonus_granted": (
        "🎁", lambda d: "Bonus received",
        lambda d: f"{_etb(d.get('amount'))} bonus was added to your balance." + (f" {d['reason']}" if d.get("reason") else ""),
    ),
    "agent_topup_received": (
        "💰", lambda d: "Top-up received",
        lambda d: f"Your agent topped up {_etb(d.get('amount'))} to your balance.",
    ),
    "agent_tier_up": (
        "⭐", lambda d: f"You reached {d.get('label')} tier",
        lambda d: f"Your commission rate is now {d.get('percent')}% for the rest of this month. Keep going!",
    ),
    "agent_reward_paid": (
        "🎉", lambda d: "Agent reward paid",
        lambda d: f"{_etb(d.get('amount'))} was added to your commission balance ({'new player' if d.get('reward_type') == 'new_player' else 'player retention'} reward).",
    ),
    "agent_application_approved": (
        "🎉", lambda d: "You are now a Melbete agent",
        lambda d: f"Your agent request was approved. Open the Agent portal from your profile to start. Your commission is {d.get('percent')}%.",
    ),
    "agent_application_rejected": (
        "ℹ️", lambda d: "Agent request update",
        lambda d: "Your agent request was not approved this time." + (f" Note: {d.get('note')}" if d.get("note") else ""),
    ),
    "leaderboard_reward": (
        "🏆", lambda d: f"You won {_etb(d.get('amount'))} in the leaderboard!",
        lambda d: f"You finished #{d.get('rank')} in {d.get('campaign')}. The reward is in your bonus balance; wager {_etb(d.get('wager_required'))} to make it withdrawable.",
    ),
    "referral_reward": (
        "🤝", lambda d: "Referral reward",
        lambda d: f"{_etb(d.get('amount'))} bonus - someone you invited joined Melbete Bingo.",
    ),
    "streak_reward": (
        "🔥", lambda d: f"{d.get('days')}-day streak reward!",
        lambda d: f"You played {d.get('days')} days in a row. {_etb(d.get('amount'))} bonus was added to your balance (playable, not withdrawable).",
    ),
    "streak_at_risk": (
        "🔥", lambda d: f"Keep your {d.get('streak')}-day streak",
        lambda d: f"Play a round today to make it {int(d.get('streak') or 0) + 1} days in a row."
        + (f" Reach {d['next_days']} days for a {_etb(d.get('next_amount'))} bonus." if d.get("next_days") else ""),
    ),
    "room_filling": (
        "🎯", lambda d: "A round is starting",
        lambda d: f"A {_etb(d.get('stake'))} round opens in about {d.get('seconds', 30)} seconds with {d.get('players', 1)} player(s) in. Join now!",
    ),
    "low_balance": (
        "👛", lambda d: "Your balance is running low",
        lambda d: f"You have {_etb(d.get('balance'))} left, which is below {_etb(d.get('threshold'))}. Top up to keep playing - deposits are usually approved within minutes.",
    ),
    "winback_bonus": (
        "🎁", lambda d: "A free round is waiting",
        lambda d: f"We added {_etb(d.get('amount'))} bonus to your balance (playable, not withdrawable). Come play a round!",
    ),
    "agent_commission_earned": (
        "💼", lambda d: "Commission earned",
        lambda d: f"You earned {_etb(d.get('amount'))} commission from a deposit by one of your players.",
    ),
}


# Promotional types: skipped entirely for players who opted out, and they carry the /mute hint.
PROMO_TYPES = {"room_filling", "winback_bonus", "streak_at_risk", "low_balance"}
# Types whose Telegram message gets an "Open Melbete Bingo" button.
OPEN_APP_TYPES = PROMO_TYPES | {"streak_reward"}


def render(type_: str, data: dict) -> tuple[str, str, str]:
    """(emoji, title, body) for a notification type; unknown types fall back to a generic message."""
    emoji, title_fn, body_fn = TEMPLATES.get(type_, ("🔔", lambda d: "Notification", lambda d: str(d.get("message", ""))))
    return emoji, title_fn(data), body_fn(data)


class PlayerNotifier:
    """Writes the inbox row, then tries Telegram. `repo`, `players` and `sender` are injectable so the
    logic is testable without a database or a bot."""

    def __init__(self, repo=None, players=None, sender: Optional[Callable[..., Awaitable[str]]] = None):
        if repo is None:
            from src.repositories.notification_repository import NotificationRepository

            repo = NotificationRepository()
        if players is None:
            from src.repositories.player_repository import PlayerRepository

            players = PlayerRepository()
        self.repo = repo
        self.players = players
        self.sender = sender or self._telegram_send

    @staticmethod
    async def _telegram_send(chat_id: int, text: str, open_app: bool = False) -> str:
        """-> 'sent' | 'blocked' | 'failed' | 'skipped'."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        from telegram.error import Forbidden

        from src.core.config import settings
        from src.services.notifications import _get_bot

        bot = await _get_bot()
        if bot is None:
            return "skipped"
        markup = None
        base = (settings.public_base_url or "").rstrip("/")
        if open_app and base:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Open Melbete Bingo", web_app=WebAppInfo(url=f"{base}/app"))]])
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            return "sent"
        except Forbidden:
            return "blocked"  # the player blocked the bot or deleted the chat
        except Exception:  # noqa: BLE001
            logger.exception("Telegram notification to %s failed", chat_id)
            return "failed"

    async def notify(self, player_id: str, type_: str, data: Optional[dict] = None,
                     dedupe_key: Optional[str] = None, send_telegram: bool = True) -> Optional[str]:
        """Returns the notification id, or None if it was a duplicate or could not be stored."""
        data = dict(data or {})
        emoji, title, body = render(type_, data)
        promo = type_ in PROMO_TYPES
        player = None
        if promo:
            try:
                player = await self.players.get_by_id(player_id)
            except Exception:  # noqa: BLE001
                logger.exception("Could not load player %s for a promotional notification", player_id)
                return None
            if player is None or getattr(player, "promo_opt_out", False):
                return None  # opted out (or unknown): send nothing, not even an inbox entry
        try:
            row = await self.repo.create(new_id(), player_id, type_, title, body, data, dedupe_key,
                                         datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            logger.exception("Could not store %s notification for %s", type_, player_id)
            return None
        if row is None:
            return None  # same event already notified
        if not send_telegram:
            await self._set_status(row["id"], "skipped")
            return row["id"]
        status = "skipped"
        try:
            player = player or await self.players.get_by_id(player_id)
            telegram_id = getattr(player, "telegram_id", None) if player else None
            if player and not getattr(player, "is_bot", False) and str(telegram_id or "").isdigit():
                text = f"{emoji} {title}\n{body}" + ("\n\nSend /mute to stop promotional messages." if promo else "")
                status = await self.sender(int(telegram_id), text, open_app=type_ in OPEN_APP_TYPES)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram delivery of %s to %s failed", type_, player_id)
            status = "failed"
        await self._set_status(row["id"], status)
        return row["id"]

    async def _set_status(self, notification_id: str, status: str) -> None:
        try:
            await self.repo.set_telegram_status(notification_id, status)
        except Exception:  # noqa: BLE001
            logger.exception("Could not record delivery status for notification %s", notification_id)


_notifier: Optional[PlayerNotifier] = None
_background: set = set()


def get_notifier() -> PlayerNotifier:
    global _notifier
    if _notifier is None:
        _notifier = PlayerNotifier()
    return _notifier


def schedule_notify(player_id: str, type_: str, data: Optional[dict] = None, dedupe_key: Optional[str] = None) -> None:
    """Fire-and-forget. Safe to call from anywhere inside a running event loop; never raises."""
    try:
        task = asyncio.get_running_loop().create_task(get_notifier().notify(player_id, type_, data, dedupe_key))
    except RuntimeError:
        return  # no running loop (e.g. a sync test): nothing to do
    _background.add(task)  # keep a reference so the task is not garbage-collected mid-flight
    task.add_done_callback(_background.discard)
