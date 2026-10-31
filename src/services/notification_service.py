"""
Admin notification service - the single place that pings admins about new
deposits (and other events). Both the Telegram bot and the FastAPI Mini App
backend use this so a deposit submitted from *either* path reaches admins
through the exact same pipeline, with no second/divergent notification path.
"""
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from src.core.config import settings
from src.models.schemas import PaymentMethod, Player, Transaction


class AdminNotifier:
    """Best-effort Telegram alerts to the admin chat / admin ids. If the bot
    token is missing (e.g. local tests) every method becomes a quiet no-op."""

    def __init__(self, bot: Optional[Bot] = None):
        self._bot = bot

    @property
    def bot(self) -> Optional[Bot]:
        if self._bot is None and settings.telegram_bot_token:
            self._bot = Bot(settings.telegram_bot_token)
        return self._bot

    @property
    def _targets(self) -> list[str]:
        if settings.admin_chat_id:
            return [settings.admin_chat_id]
        return [str(a) for a in settings.admin_ids if a]

    async def notify_new_deposit(
        self, txn: Transaction, player: Optional[Player], method: Optional[PaymentMethod]
    ) -> None:
        """Alert admins that a top-up request is awaiting approval. Works for
        both bot-submitted deposits (screenshot_file_id) and Mini App
        deposits (screenshot_url), falling back to a plain text message."""
        if not self.bot:
            return
        name = (player.payment_name or player.display_name) if player else txn.player_id
        phone = player.phone_number if player else None
        method_label = method.name if method else txn.payment_method
        caption = (
            "💳 *New top-up request*\n\n"
            f"Player: {name} (`{player.telegram_id if player else txn.player_id}`)\n"
            f"Phone: {phone or 'N/A'}\n"
            f"Amount: {txn.amount} ETB\n"
            f"Method: {method_label}\n"
            f"Transaction: `{txn.id}`"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{txn.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{txn.id}"),
        ]])
        for chat_id in self._targets:
            if not chat_id:
                continue
            try:
                if txn.screenshot_file_id:
                    await self.bot.send_photo(
                        chat_id=chat_id, photo=txn.screenshot_file_id,
                        caption=caption, parse_mode="Markdown", reply_markup=keyboard,
                    )
                elif txn.screenshot_url:
                    filename = txn.screenshot_url.split("/")[-1]
                    from src.services.screenshot_storage import get_screenshot
                    content, mime = get_screenshot(filename)
                    if content:
                        import io
                        photo_file = io.BytesIO(content)
                        photo_file.name = filename
                        await self.bot.send_photo(
                            chat_id=chat_id, photo=photo_file,
                            caption=caption, parse_mode="Markdown", reply_markup=keyboard,
                        )
                    else:
                        await self.bot.send_photo(
                            chat_id=chat_id, photo=txn.screenshot_url,
                            caption=caption, parse_mode="Markdown", reply_markup=keyboard,
                        )
                else:
                    await self.bot.send_message(
                        chat_id=chat_id, text=caption, parse_mode="Markdown", reply_markup=keyboard
                    )
            except Exception:  # noqa: BLE001
                logger = __import__("logging").getLogger(__name__)
                logger.exception("Failed to notify admin chat %s of deposit %s", chat_id, txn.id)


_notifier: Optional[AdminNotifier] = None


def get_admin_notifier() -> AdminNotifier:
    global _notifier
    if _notifier is None:
        _notifier = AdminNotifier()
    return _notifier
