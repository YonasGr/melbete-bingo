"""
Shared admin-notification pipeline.

The bot process and the API process run as separate containers, so they
can't share in-memory state - but they share the same Telegram bot token
and the same Postgres-backed services. This module is the SINGLE place
that knows how to alert admins about a pending deposit or withdrawal,
using a lightweight `telegram.Bot` client (not the polling `Application`)
that either process can call directly.

This is what makes Mini-App-submitted deposits/withdrawals reach admins
exactly the same way bot-submitted ones do: both `WalletService.request_deposit`
and `WalletService.request_withdrawal` call straight into this module, so
there is only ever ONE notification code path, regardless of where the
request came from.

IMPORTANT: a raw `telegram.Bot` instance must be initialized
(`await bot.initialize()`) before it can make any network call - skipping
this raises `RuntimeError: This HTTPXRequest is not initialized!` on the
very first request. That bug used to live here silently (caught by a
broad except and logged), which meant every admin notification silently
failed regardless of how correctly ADMIN_CHAT_ID was configured. The
module-level `_get_bot()` below initializes exactly once per process and
reuses that same instance for the process's whole lifetime.
"""
import asyncio
import logging
from typing import Iterable, Optional, Tuple

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from src.core.config import settings
from src.models.schemas import Transaction
from src.repositories import AdminRepository, PlayerRepository
from src.repositories.payment_method_repository import PaymentMethodRepository

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None
_bot_lock = asyncio.Lock()


async def _get_bot() -> Optional[Bot]:
    global _bot
    if not settings.telegram_bot_token:
        return None
    if _bot is None:
        async with _bot_lock:
            if _bot is None:  # re-check inside the lock - avoid a double-init race
                bot = Bot(token=settings.telegram_bot_token)
                await bot.initialize()
                _bot = bot
    return _bot


async def _admin_chat_targets() -> Iterable[str]:
    """Every chat a notification should go to: the shared admin group IF
    configured AND every individual admin's DM. Sending to both ensures
    that even if the group chat is the primary channel, individual admins
    get notified even when the group doesn't (e.g. notification was
    dismissed, or the admin hasn't joined the group yet)."""
    targets: set[str] = set()

    # Always include the shared admin group if configured
    if settings.admin_chat_id:
        targets.add(settings.admin_chat_id)

    # Always include every individual admin — env-configured ones AND
    # anyone the super admin has added dynamically via the admin panel.
    try:
        admins = await AdminRepository().list_all()
        targets.update(a.telegram_id for a in admins)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load dynamic admin list for notification")
    # Legacy env-based admins still honored
    targets.update(str(a) for a in settings.admin_ids if a)
    return [t for t in targets if t]


async def _payment_method_name(key: Optional[str]) -> str:
    if not key:
        return "N/A"
    try:
        method = await PaymentMethodRepository().get_by_key(key)
        return method.name if method else key
    except Exception:  # noqa: BLE001
        return key


async def _send(
    caption: str, keyboard: InlineKeyboardMarkup, photo_file_id: Optional[str] = None
) -> Optional[Tuple[str, int, bool]]:
    """Sends to every admin target. Returns (chat_id, message_id, is_photo)
    for the FIRST successful send, so the caller can persist it on the
    transaction and later edit that exact message when it's resolved -
    from either the bot/group OR the web admin panel. With ADMIN_CHAT_ID
    set (the recommended setup), there's exactly one target, so this
    single reference is authoritative; with the DM-fallback (multiple
    targets, no shared group), only the first admin's copy stays in sync -
    an inherent limit of a fan-out with no single source of truth."""
    bot = await _get_bot()
    if not bot:
        logger.warning("TELEGRAM_BOT_TOKEN not configured - skipping admin notification")
        return None
    targets = await _admin_chat_targets()
    if not targets:
        logger.warning("No admin chat/ids configured - deposit/withdrawal request has no one to notify")
        return None

    targets = list(targets)
    first_ref: Optional[Tuple[str, int, bool]] = None
    for chat_id in targets:
        try:
            caption_trunc = caption[:1024]
            if photo_file_id:
                msg = await bot.send_photo(
                    chat_id=chat_id, photo=photo_file_id, caption=caption_trunc, parse_mode="Markdown", reply_markup=keyboard
                )
            else:
                msg = await bot.send_message(
                    chat_id=chat_id, text=caption_trunc, parse_mode="Markdown", reply_markup=keyboard
                )
            if first_ref is None:
                first_ref = (str(chat_id), msg.message_id, bool(photo_file_id))
            logger.info("Notification sent to chat %s (msg_id=%s)", chat_id, msg.message_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to notify chat %s - check: (1) bot is a member of the group, "
                "(2) bot has Send Messages permission, (3) chat_id is correct "
                "(group IDs are negative, e.g. -1001234567890)",
                chat_id,
            )
    if first_ref is None:
        logger.error(
            "All %d admin notification targets FAILED - no message delivered. Targets: %s",
            len(targets), targets,
        )
    return first_ref


async def _build_deposit_caption(txn: Transaction) -> str:
    player = await PlayerRepository().get_by_id(txn.player_id)
    player_label = (player.payment_name or player.display_name) if player else txn.player_id
    phone = (player.phone_number if player else None) or "N/A"
    method_name = await _payment_method_name(txn.payment_method)
    caption = (
        f"💳 *New deposit request*\n\n"
        f"Player: {player_label}"
        + (f" (`{player.telegram_id}`)" if player else "")
        + f"\nPhone: {phone}\n"
        f"Amount: {txn.amount} ETB\n"
        f"Method: {method_name}\n"
        f"Transaction: `{txn.id}`"
    )
    if txn.payment_verification_url:
        caption += f"\n🔗 Verify: {txn.payment_verification_url}"
    if txn.sms_text:
        caption += f"\n\n📱 *SMS:*\n`{txn.sms_text}`"
    if txn.screenshot_url and not txn.screenshot_file_id:
        base = settings.public_base_url or ""
        caption += f"\n\nScreenshot: {base}{txn.screenshot_url}"
    return caption


async def _build_withdrawal_caption(txn: Transaction) -> str:
    player = await PlayerRepository().get_by_id(txn.player_id)
    player_label = (player.payment_name or player.display_name) if player else txn.player_id
    method_name = await _payment_method_name(txn.payment_method)
    caption = (
        f"🏧 *New withdrawal request*\n\n"
        f"Player: {player_label}"
        + (f" (`{player.telegram_id}`)" if player else "")
        + f"\nLegal name: {txn.legal_name}\n"
        f"Amount: {abs(txn.amount)} ETB\n"
        f"Method: {method_name}\n"
        f"Send to: {txn.withdrawal_address}\n"
        f"Transaction: `{txn.id}`"
    )
    return caption


async def notify_admins_of_deposit(txn: Transaction) -> Optional[Tuple[str, int, bool]]:
    caption = await _build_deposit_caption(txn)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{txn.id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{txn.id}"),
    ]])
    return await _send(caption, keyboard, photo_file_id=txn.screenshot_file_id)


async def notify_admins_of_withdrawal(txn: Transaction) -> Optional[Tuple[str, int, bool]]:
    caption = await _build_withdrawal_caption(txn)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Mark Paid", callback_data=f"withdrawal_approve_{txn.id}"),
        InlineKeyboardButton("❌ Reject & Refund", callback_data=f"withdrawal_reject_{txn.id}"),
    ]])
    return await _send(caption, keyboard)


async def update_notification_message(txn: Transaction, resolution_line: str) -> None:
    """Edits the original deposit/withdrawal alert to remove the
    Approve/Reject buttons and show how it was resolved - called after
    EITHER the bot/group buttons OR the web admin panel resolves a
    transaction, so both surfaces always agree on its current state and
    nobody can be misled by a stale button. No-op if this transaction has
    no stored message reference (e.g. no admin chat was configured when
    it was created)."""
    if not txn.notification_chat_id or not txn.notification_message_id:
        return
    bot = await _get_bot()
    if not bot:
        return
    try:
        if txn.type.startswith("top_up"):
            base_caption = await _build_deposit_caption(txn)
        elif txn.type.startswith("withdrawal"):
            base_caption = await _build_withdrawal_caption(txn)
        else:
            base_caption = ""

        full_text = f"{base_caption}\n\n{resolution_line}" if base_caption else resolution_line

        if txn.notification_is_photo:
            await bot.edit_message_caption(
                chat_id=txn.notification_chat_id,
                message_id=txn.notification_message_id,
                caption=full_text[:1024],
                parse_mode="Markdown",
                reply_markup=None,
            )
        else:
            await bot.edit_message_text(
                chat_id=txn.notification_chat_id,
                message_id=txn.notification_message_id,
                text=full_text[:4096],
                parse_mode="Markdown",
                reply_markup=None,
            )
    except Exception:  # noqa: BLE001
        logger.warning("Could not edit notification message for transaction %s", txn.id, exc_info=True)


_bot_username: Optional[str] = None


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        try:
            bot = await _get_bot()
            if bot:
                me = await bot.get_me()
                _bot_username = me.username
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch bot username dynamically")
    return _bot_username or ""


async def check_channel_membership(channel_username: str, telegram_id: str) -> tuple[str, str]:
    """Ask Telegram (never the client) whether this user is in the channel.

    Returns ("member" | "not_member" | "unverifiable", detail). Only "member" may credit points. The bot must be
    an administrator of the channel to see its members; any error - bot not in the channel, channel not found,
    network problem - is "unverifiable", so a misconfiguration can never hand out points.
    """
    bot = await _get_bot()
    if bot is None:
        return "unverifiable", "bot is not configured"
    try:
        member = await bot.get_chat_member(chat_id=f"@{channel_username.lstrip('@')}", user_id=int(telegram_id))
    except Exception as e:  # noqa: BLE001 - every failure means "could not verify"
        text = str(e)
        logger.warning("Channel membership check for @%s failed: %s", channel_username, text)
        return "unverifiable", text[:200]
    status = getattr(member, "status", "")
    if status in ("creator", "administrator", "member"):
        return "member", status
    if status == "restricted":
        return ("member", status) if getattr(member, "is_member", False) else ("not_member", status)
    return "not_member", status or "unknown"
