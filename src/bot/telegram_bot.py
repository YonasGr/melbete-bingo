"""
Melbete Bingo Telegram Bot.

Handles registration (just sharing their phone/contact - real/payment name
is collected later, only when they deposit or withdraw), the wallet
(balance, top-up with screenshot -> admin approval, one-time free credit),
and launching the live global game (the HTML5 Mini App at `/app`). Gameplay
itself happens in the Mini App, which talks to the same Postgres data via
the FastAPI backend (`src/api/main.py`).
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.core.config import settings
from src.models.schemas import Player
from src.repositories import LinkCodeRepository, PlayerRepository
from src.repositories.player_repository import PhoneAlreadyRegistered
from src.services.admin_service import AdminService
from src.services.broadcast_service import BroadcastService, BroadcastValidationError
from src.services.sms_parser import extract_verification_url
from src.services.wallet_service import DuplicateTransactionError, InvalidAmountError, TransactionNotFoundError, WalletService

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Ethiopian mobile numbers: optional +251/251 country code or a leading 0,
# followed by 9 digits (e.g. 0912345678, 251912345678, +251912345678).
_PHONE_RE = re.compile(r"^(?:\+?251|0)?[97]\d{8}$")


def _is_valid_phone_number(text: str) -> bool:
    """True if `text` looks like a real Ethiopian phone number - digits
    only (spaces/dashes stripped) matching the expected length/prefix.
    Rejects plain text or garbage input."""
    cleaned = re.sub(r"[\s\-]", "", text)
    return bool(_PHONE_RE.match(cleaned))


class MelbeteBingoBot:
    """Main Telegram Bot class."""

    def __init__(self):
        self.app: Application | None = None
        self._players: PlayerRepository | None = None
        self._wallets: WalletService | None = None
        self._admin_service: AdminService | None = None
        self._referral_service: "ReferralService | None" = None
        self._link_codes: LinkCodeRepository | None = None

    @property
    def players(self) -> PlayerRepository:
        # Created lazily so importing this module never requires a
        # live database connection (useful for tests/tooling).
        if self._players is None:
            self._players = PlayerRepository()
        return self._players

    @property
    def wallets(self) -> WalletService:
        if self._wallets is None:
            self._wallets = WalletService()
        return self._wallets

    @property
    def referral_service(self):
        if self._referral_service is None:
            from src.services.referral_service import ReferralService

            self._referral_service = ReferralService(player_repo=self.players, wallet_service=self.wallets)
        return self._referral_service

    @property
    def admin_service(self) -> AdminService:
        if self._admin_service is None:
            self._admin_service = AdminService()
        return self._admin_service

    @property
    def link_codes(self) -> LinkCodeRepository:
        if self._link_codes is None:
            self._link_codes = LinkCodeRepository()
        return self._link_codes

    def setup(self):
        """Build the bot application and register handlers."""
        # PTB Updater has compatibility issues on Python 3.14, so we build the
        # Application without an Updater and run a small async polling loop.
        self.app = Application.builder().token(settings.telegram_bot_token).updater(None).build()
        self.app.add_error_handler(self.error_handler)

        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("play", self.play_command))
        self.app.add_handler(CommandHandler("balance", self.balance_command))
        self.app.add_handler(CommandHandler("mute", self.mute_command))
        self.app.add_handler(CommandHandler("unmute", self.unmute_command))
        self.app.add_handler(CommandHandler("topup", self.topup_command))
        self.app.add_handler(CommandHandler("withdraw", self.withdraw_command))
        self.app.add_handler(CommandHandler("pending", self.pending_command))
        self.app.add_handler(CommandHandler("report", self.report_command))
        self.app.add_handler(CommandHandler("admin", self.admin_command))
        self.app.add_handler(CommandHandler("broadcast", self.broadcast_command))
        self.app.add_handler(CommandHandler(["cancelbroadcast", "cancel_broadcast", "stopbroadcast"], self.cancel_broadcast_command))
        self.app.add_handler(MessageHandler(filters.Caption() & filters.Regex(r"^/broadcast"), self.broadcast_caption_command))
        self.app.add_handler(CommandHandler("link", self.link_command))

        self.app.add_handler(MessageHandler(filters.CONTACT, self.contact_received))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.photo_received))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_received))

        self.app.add_handler(CallbackQueryHandler(self.button_callback))

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Keep one undeliverable chat from interrupting normal bot updates."""
        if isinstance(context.error, Forbidden):
            logger.info("Telegram delivery skipped: the bot was blocked or the chat is unavailable.")
            return

        logger.error("Unhandled Telegram update error", exc_info=context.error)

    def _game_url(self) -> str:
        base = (settings.public_base_url or "").rstrip("/")
        return f"{base}/app"

    def _admin_url(self) -> str:
        base = (settings.public_base_url or "").rstrip("/")
        return f"{base}/admin"

    async def _is_admin(self, user_id: int) -> bool:
        if user_id in settings.admin_ids:
            return True  # legacy env-based admins
        return await self.admin_service.is_admin(str(user_id))

    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin-only broadcast, with optional reply-photo and URL buttons.

        Usage: /broadcast News text | Button label | https://example.com
        Reply to an image with the same command to include that image.
        Repeat the final button pair up to three times.
        """
        if not update.effective_user or not await self._is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ This command is only available to admins.")
            return
        raw = " ".join(context.args).strip()
        replied = update.message.reply_to_message
        photo = replied.photo[-1].file_id if replied and replied.photo else None
        await self._do_broadcast(update, context, raw, photo)

    async def broadcast_caption_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin-only broadcast from a photo caption command."""
        if not update.effective_user or not await self._is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ This command is only available to admins.")
            return
        caption = (update.message.caption or "").strip()
        if caption.startswith("/broadcast"):
            raw = caption[len("/broadcast"):].strip()
        else:
            raw = caption
        photo = update.message.photo[-1].file_id if update.message.photo else None
        await self._do_broadcast(update, context, raw, photo)

    async def cancel_broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin-only broadcast cancellation command."""
        if not update.effective_user or not await self._is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ This command is only available to admins.")
            return
        cancelled = await BroadcastService(self.players).cancel()
        if cancelled:
            await update.message.reply_text("⛔ Broadcast cancellation requested.")
        else:
            await update.message.reply_text("ℹ️ No active broadcast is running.")

    async def _do_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, photo_file_id: Optional[str]):
        parts = [part.strip() for part in raw_text.split("|")]
        text = parts[0] if parts else ""
        button_parts = parts[1:]
        if len(button_parts) % 2:
            await update.message.reply_text("Button details must be label + URL pairs. Example:\n/broadcast Update | Open app | https://example.com")
            return
        buttons = [{"text": button_parts[i], "url": button_parts[i + 1]} for i in range(0, len(button_parts), 2)]

        cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Stop Broadcast", callback_data="cancel_broadcast")]])
        status_msg = await update.message.reply_text("⏳ Starting broadcast...", reply_markup=cancel_markup)

        last_update_time = 0.0

        async def on_progress(sent, total, delivered, failed):
            nonlocal last_update_time
            import time
            now = time.time()
            if now - last_update_time >= 2.0 or sent == total:
                last_update_time = now
                try:
                    await status_msg.edit_text(
                        f"⏳ Broadcasting: {sent}/{total} users processed (Delivered: {delivered}, Failed: {failed})...",
                        reply_markup=cancel_markup,
                    )
                except Exception:
                    pass

        async def run_background_broadcast():
            try:
                result = await BroadcastService(self.players).send(
                    text, photo=photo_file_id, buttons=buttons, bot=context.bot, on_progress=on_progress
                )
                row = await self.players._fetchrow("SELECT status FROM broadcast_progress WHERE id = 'active'")
                if row and row["status"] == "cancelled":
                    await status_msg.edit_text(
                        f"⛔ Broadcast cancelled\nDelivered: {result.delivered}/{result.attempted}\nFailed: {result.failed}"
                    )
                else:
                    await status_msg.edit_text(
                        f"✅ Broadcast complete\nDelivered: {result.delivered}/{result.attempted}\nUnavailable chats: {result.failed}"
                    )
            except BroadcastValidationError as e:
                await status_msg.edit_text(f"⚠️ {e}")
            except Exception as e:
                await status_msg.edit_text(f"❌ Broadcast failed: {e}")

        asyncio.create_task(run_background_broadcast())

    @staticmethod
    def _user_and_message(update):
        """`update` is a real `Update` when called from a CommandHandler,
        but a `CallbackQuery` when reused from a button tap - normalize
        both to (user, message) so handler bodies don't need to care."""
        if hasattr(update, "effective_user"):
            return update.effective_user, update.message
        return update.from_user, update.message

    # ------------------------------------------------------------------
    # Registration: contact -> free credit (that's it - real/payment name
    # is asked for later, at deposit/withdraw time, not during sign-up)
    # ------------------------------------------------------------------

    async def _send_welcome_banner(self, update: Update):
        """Brand welcome image for brand-new players. Cosmetic: never blocks onboarding."""
        if not update.message:
            return
        try:
            from pathlib import Path

            path = Path(__file__).resolve().parents[1] / "static" / "img" / "welcome-banner.jpg"
            with open(path, "rb") as f:
                await update.message.reply_photo(f, caption="Welcome to Melbete Bingo")
        except Exception:
            logger.warning("Could not send welcome banner", exc_info=True)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        logger.info(
            "Handling /start: update_id=%s user_id=%s chat_id=%s",
            update.update_id,
            user.id if user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        # Check for a referral or account-linking deep-link payload, e.g.
        # /start ref_12345678 or /start link_AB12CD
        ref_payload = None
        link_code = None
        agent_code = None
        if context.args:
            arg = context.args[0]
            if arg.startswith("ag_"):
                agent_code = arg[3:]  # a reseller agent's code
            elif arg.startswith("ref_"):
                ref_payload = arg[4:]  # the referrer's telegram_id
            elif arg.startswith("link_"):
                link_code = arg[5:]  # the web session's one-time link code

        player = await self.players.get_or_create(
            telegram_id=str(user.id),
            display_name=user.full_name or user.username or f"Player{user.id}",
        )

        if player.registration_state == "pending_contact":
            await self._send_welcome_banner(update)

        if link_code:
            await self._complete_account_link(update, link_code, player)

        if agent_code:
            try:
                from src.services.agent_service import AgentService

                await AgentService().link_player_by_code(player.id, agent_code)
            except Exception:
                logger.exception("Could not link player %s to agent code %r", player.id, agent_code)

        # Only ever link a referral for a genuinely brand-new player
        # (pending_contact = they have never shared contact yet, so this
        # is their very first /start) and never for a self-referral.
        # ReferralService also guards against double-linking on its own.
        if ref_payload and player.registration_state == "pending_contact" and not player.referred_by:
            new_ref = await self.referral_service.record_referral_click(ref_payload, player)
            if new_ref:
                # Refresh player
                player = await self.players.get_by_id(player.id)
                # Send notification to referrer
                referrer = await self.players.get_by_telegram_id(ref_payload)
                if referrer:
                    try:
                        friend_name = player.display_name or f"User {player.telegram_id}"
                        await context.bot.send_message(
                            chat_id=int(referrer.telegram_id),
                            text=(
                                f"🔔 *New Referral Link Click!*\n\n"
                                f"Your friend *{friend_name}* just started the bot using your invite link.\n"
                                f"• Status: *Pending registration* ⏳\n\n"
                                f"You will receive your bonus once they share their contact."
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.exception("Failed to notify referrer %s of click", ref_payload)

        await self._continue_registration_or_greet(update, player, context)

    async def link_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manual counterpart to the /start link_<code> deep link, for a
        player who already has this chat open and just wants to type the
        code the web app showed them."""
        user = update.effective_user
        if not context.args:
            await update.message.reply_text(
                "To link your web session to this Telegram account:\n\n"
                "1. On the web app, open *Profile → Link Telegram Account*\n"
                "2. Tap *Open Telegram* (or come back here and send the code it shows you)\n\n"
                "Send it as: `/link ABC123`",
                parse_mode="Markdown",
            )
            return
        player = await self.players.get_or_create(
            telegram_id=str(user.id),
            display_name=user.full_name or user.username or f"Player{user.id}",
        )
        await self._complete_account_link(update, context.args[0], player)
        await self._continue_registration_or_greet(update, player)

    async def _complete_account_link(self, update: Update, code: str, player: Player):
        """Finishes the web-to-Telegram account-linking flow started by
        `POST /api/link/create` in api/main.py: verifies the one-time
        code, folds the web session's wallet into this real Telegram
        account (see WalletService.merge_wallets), and marks the code
        `linked` with a fresh session token so the web page - which has
        been polling `GET /api/link/status/{code}` - picks it up and logs
        the browser in as this account from then on."""
        await update.message.reply_text("Browser account linking is temporarily disabled for security. Please use the official Telegram Mini App.")
        return
        record = await self.link_codes.get_by_code(code.strip().upper())
        if not record:
            await update.message.reply_text(
                "⚠️ That code isn't valid. Please generate a fresh one from "
                "the web app (Profile → Link Telegram Account) and try again."
            )
            return
        if record.status == "linked":
            await update.message.reply_text("✅ That code has already been used to link an account.")
            return
        expires_at = record.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at < datetime.now(timezone.utc):
            await update.message.reply_text(
                "⚠️ That code has expired (they only last a few minutes). "
                "Please generate a new one from the web app."
            )
            return

        linked_player_id = f"tg_{player.telegram_id}"
        session_token = None

        merged_wallet = await self.wallets.merge_wallets(record.web_player_id, linked_player_id)

        await self.link_codes.update(record.id, {
            "status": "linked",
            "telegram_id": player.telegram_id,
            "linked_player_id": linked_player_id,
            "session_token": session_token,
        })

        moved_note = ""
        if merged_wallet:
            moved_note = "\n\nAny balance from that browser session has been added to your account here."

        await update.message.reply_text(
            f"✅ *Linked!* Your web session is now connected to this Telegram account.{moved_note}\n\n"
            "You can close this chat and go back to the web page - it will log you in automatically.",
            parse_mode="Markdown",
        )

    async def _continue_registration_or_greet(self, update: Update, player: Player, context: ContextTypes.DEFAULT_TYPE = None):
        if player.registration_state == "pending_contact":
            referrer_name = None
            if player.referred_by:
                referrer = await self.players.get_by_telegram_id(player.referred_by)
                if referrer:
                    referrer_name = referrer.display_name

            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Share my contact", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )

            welcome_msg = "🎉 *Welcome to Melbete Bingo!*\n\n"
            if referrer_name:
                welcome_msg += f"You were invited by your friend *{referrer_name}*! 🤝\n\n"
            welcome_msg += (
                "Before you can play, please share your contact info - it's used "
                "to confirm your identity for payments and support."
            )

            await update.message.reply_text(
                welcome_msg,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

            if context:
                import urllib.parse
                bot_username = context.bot.username
                my_invite_link = f"https://t.me/{bot_username}?start=ref_{player.telegram_id}"
                s = await self.admin_service.get_settings()
                free_credit = s.free_credit_amount
                bonus_per_ref = s.referral_bonus

                invite_text = (
                    "🎮 Join me on Melbete Games! 🎪\n\n"
                    f"I'm playing Melbete Bingo, the ultimate live 75-ball bingo game! "
                    f"Play with real people, win awesome payouts, and claim your {free_credit} ETB FREE welcome credit immediately! 💰\n\n"
                    "👇 TAP HERE TO PLAY NOW AND CLAIM YOUR FREE CREDIT! 👇"
                )
                share_url = f"https://t.me/share/url?url={urllib.parse.quote(my_invite_link)}&text={urllib.parse.quote(invite_text)}"
                inline_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📤 Share Invite Link", url=share_url)
                ]])

                await update.message.reply_text(
                    f"🎁 While you sign up, here is *your own invite link* right away!\n"
                    f"Share it with friends to earn *{bonus_per_ref} ETB* bonus credit for each registration:\n\n"
                    f"🔗 `{my_invite_link}`",
                    parse_mode="Markdown",
                    reply_markup=inline_keyboard,
                )
        else:
            await self._send_main_menu(update, player, context)

    async def _send_main_menu(self, update: Update, player: Player, context: ContextTypes.DEFAULT_TYPE = None):
        wallet = await self.wallets.get_or_create_wallet(player.id)
        bonus_line = f"\n🎁 Bonus (playable only): *{wallet.bonus_balance} ETB*" if wallet.bonus_balance else ""
        s = await self.admin_service.get_settings()
        text = (
            f"👋 Welcome back, *{player.payment_name or player.display_name}*!\n\n"
            f"💰 Balance: *{wallet.balance} ETB*{bonus_line}\n"
            f"🏆 Winnings (withdrawable): *{wallet.won_balance} ETB*\n"
            f"🎯 Each round costs *{s.stake_amount} ETB* to join.\n\n"
            "Pick an option below:"
        )
        keyboard = [
            [InlineKeyboardButton("🎮 Play Now", callback_data="play_now")],
            [
                InlineKeyboardButton("💳 ገንዘብ ማስገባት", callback_data="topup"),
                InlineKeyboardButton("🏧 ገንዘብ ማውጣት", callback_data="withdraw"),
            ],
            [
                InlineKeyboardButton("💰 Balance", callback_data="balance"),
                InlineKeyboardButton("🤝 Referral", callback_data="referral"),
            ],
            [InlineKeyboardButton("❓ Help", callback_data="help")],
        ]
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def contact_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        contact = update.message.contact
        if not contact or str(contact.user_id) != str(user.id):
            await update.message.reply_text("⚠️ Please share *your own* contact using the button.", parse_mode="Markdown")
            return

        player = await self.players.get_or_create(telegram_id=str(user.id), display_name=user.full_name or f"Player{user.id}")
        try:
            await self.players.set_contact(player.id, contact.phone_number)
        except PhoneAlreadyRegistered:
            logger.warning("Registration blocked: phone already used by another account (player %s)", player.id)
            await update.message.reply_text(
                "⚠️ This phone number is already linked to another Melbete Bingo account.\n\n"
                "If that was you on a different Telegram account, please contact support and we will help you move over.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        player = await self.players.get_by_id(player.id)

        # Sharing contact is now the only registration step - go straight
        # to "you're registered" + bonus, instead of also asking for a
        # full/payment name (that used to trip people up and made many
        # of them drop off). We'll ask for their real/payment name later,
        # only when they actually deposit or withdraw money.
        msg = f"🎉 All set, *{player.display_name}*! You're registered.\n\nOpen the Bonus Center in the game to check whether your 20 ETB welcome reward is available. Bonus campaigns may be paused until approved. Bonus credit is non-withdrawable and cartelas only."
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

        # Qualify a referral only after verified onboarding. The reward is
        # credited by the transactional promotional ledger, never the legacy
        # general-wallet referral path.
        try:
            from src.services.bonus_service import BonusService
            award = await BonusService().qualify_referral(player.id, f"referral:{player.id}")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to process promotional referral for player %s", player.id)
            award = {"status": "error"}
        if award and award.get("status") == "granted":
            referrer = await self.players.get_by_id(award["referrer_player_id"])
            if referrer:
                try:
                    await context.bot.send_message(
                        chat_id=int(referrer.telegram_id),
                        text=f"🎉 Your referral {player.display_name} completed verification. You earned {award['amount_etb']} ETB bonus credit. It is non-withdrawable and can be used for Bingo cartelas only.",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to notify referrer %s", referrer.telegram_id)

        await self._send_main_menu(update, player, context)

    async def text_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Routes free-text input depending on what we're waiting on - e.g.
        a custom top-up amount, or the legal name during a withdrawal."""
        user = update.effective_user
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player:
            await self.start_command(update, context)
            return

        text = (update.message.text or "").strip()

        # Step: waiting for a top-up amount
        if context.user_data.get("topup_state") == "awaiting_amount":
            try:
                amount = int(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("እባክዎ ትክክለኛ አዎንታዊ ቁጥር (በብር) ይላኩ።")
                return
            s = await self.admin_service.get_settings()
            if amount < s.min_deposit or amount > s.max_deposit:
                await update.message.reply_text(
                    f"⚠️ የሚያስገቡት መጠን ከ{s.min_deposit} እስከ {s.max_deposit} ብር መሆን አለበት። እባክዎ እንደገና ይሞክሩ።"
                )
                return
            context.user_data["topup_amount"] = amount
            if player.payment_name:
                context.user_data["topup_state"] = "awaiting_sms"
                await update.message.reply_text(
                    f"📱 ደርሶናል - {amount} ብር።\n\n"
                    "አሁን ክፍያውን ከፈጸሙበት መተግበሪያ የደረሰዎትን ሙሉ *SMS መልእክት* ይለጥፉ።\n\n"
                    "የግብይት ማረጋገጫ አድራሻ ካለው አብረው ይላኩ፤ ከሌለውም ጥያቄዎ በአስተዳዳሪ ይረጋገጣል።",
                    parse_mode="Markdown",
                )
            else:
                # First-ever deposit: we don't have their real/payment name
                # on file yet, so grab it now.
                context.user_data["topup_state"] = "awaiting_payment_name"
                await update.message.reply_text(
                    f"👤 ደርሶናል - {amount} ብር።\n\n"
                    "ክፍያውን ለመላክ የተጠቀሙበት ስም ማን ነው (በደረሰኙ/በዝውውሩ ላይ በትክክል የሚታየው)? "
                    "ይህን የምንጠይቅዎ አንድ ጊዜ ብቻ ነው።",
                    parse_mode="Markdown",
                )
            return

        # Step: waiting for the payment name on a first-ever deposit
        if context.user_data.get("topup_state") == "awaiting_payment_name":
            if len(text) < 2:
                await update.message.reply_text("እባክዎ ለክፍያው የተጠቀሙበትን ስም ያስገቡ።")
                return
            await self.players.set_payment_name(player.id, text)
            context.user_data["topup_state"] = "awaiting_sms"
            await update.message.reply_text(
                "📱 አመሰግናለሁ! አሁን ክፍያውን ከፈጸሙበት መተግበሪያ የደረሰዎትን ሙሉ *SMS መልእክት* ይለጥፉ።\n\n"
                "የግብይት ማረጋገጫ አድራሻ ካለው አብረው ይላኩ፤ ከሌለውም ጥያቄዎ በአስተዳዳሪ ይረጋገጣል።",
                parse_mode="Markdown",
            )
            return

        # Step: waiting for the SMS message text
        if context.user_data.get("topup_state") == "awaiting_sms":
            url = extract_verification_url(text)
            existing = await self.wallets.transaction_repo.get_by_payment_url(url) if url else None
            if existing:
                context.user_data.pop("topup_state", None)
                context.user_data.pop("topup_amount", None)
                context.user_data.pop("topup_method", None)
                await update.message.reply_text(
                    "⚠️ ይህ ግብይት ከዚህ በፊት ለስርዓቱ ገብቷል።\n\n"
                    "እባክዎ የክፍያ ዝርዝርዎን ያረጋግጡ ወይም የተለየ ግብይት ይሞክሩ።"
                )
                return

            context.user_data["topup_sms"] = text
            context.user_data["topup_url"] = url
            context.user_data["topup_state"] = "awaiting_screenshot"
            await update.message.reply_text(
                "📸 የክፍያ SMS ደርሶናል!\n\n"
                "አሁን የክፍያ ማረጋገጫ *ስክሪንሾት* ይላኩ።\n\n"
                "⚠️ እውነተኛ እና ያልተቀየረ ስክሪንሾት መላክ ያስፈልጋል። ሀሰተኛ ስክሪንሾት ወይም "
                "የተጭበረበረ መጠን መላክ አካውንትዎ በቋሚነት እንዲታገድ ሊያደርግ ይችላል።",
                parse_mode="Markdown",
            )
            return

        # Step 1: waiting for withdrawal amount
        if context.user_data.get("withdraw_state") == "awaiting_amount":
            try:
                amount = int(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("እባክዎ ትክክለኛ አዎንታዊ ቁጥር (በብር) ይላኩ።")
                return
            s = await self.admin_service.get_settings()
            wallet = await self.wallets.get_or_create_wallet(player.id)
            if amount < s.min_withdrawal or amount > s.max_withdrawal:
                await update.message.reply_text(
                    f"⚠️ የሚያወጡት መጠን ከ{s.min_withdrawal} እስከ {s.max_withdrawal} ብር መሆን አለበት። እባክዎ እንደገና ይሞክሩ።"
                )
                return
            if amount > wallet.won_balance:
                await update.message.reply_text(
                    f"⚠️ ማውጣት የሚችሉት ካሸነፉት መጠን ({wallet.won_balance} ብር) ብቻ ነው። እባክዎ ያነሰ መጠን ይሞክሩ።"
                )
                return

            context.user_data["withdraw_amount"] = amount
            context.user_data["withdraw_state"] = "awaiting_address"
            method_key = context.user_data.get("withdraw_method")
            methods = await self.admin_service.list_payment_methods(active_only=True)
            method = next((m for m in methods if m.key == method_key), None)
            method_name = method.name if method else "ክፍያ"
            await update.message.reply_text(
                f"📱 ገንዘቡ እንዲላክልዎት የሚፈልጉትን *የ{method_name} ስልክ ቁጥር* ያስገቡ፦",
                parse_mode="Markdown",
            )
            return

        # Step 2: waiting for withdrawal phone number / address
        if context.user_data.get("withdraw_state") == "awaiting_address":
            if not _is_valid_phone_number(text):
                await update.message.reply_text(
                    "⚠️ እባክዎ ትክክለኛ የስልክ ቁጥር ብቻ ያስገቡ (ለምሳሌ 09XXXXXXXX)። ፊደላት ወይም ሌላ ጽሁፍ መያዝ የለበትም።"
                )
                return
            context.user_data["withdraw_address"] = re.sub(r"[\s\-]", "", text)
            context.user_data["withdraw_state"] = "awaiting_legal_name"
            method_key = context.user_data.get("withdraw_method")
            methods = await self.admin_service.list_payment_methods(active_only=True)
            method = next((m for m in methods if m.key == method_key), None)
            method_name = method.name if method else "ክፍያ"
            await update.message.reply_text(
                f"👤 አመሰግናለሁ! አሁን ደግሞ በ*{method_name}* ላይ የተመዘገበውን *ትክክለኛ ስም* (ልክ እንዳለው) ያስገቡ፦",
                parse_mode="Markdown",
            )
            return

        # Step 3: waiting for the exact name on the payment method, then submit
        if context.user_data.get("withdraw_state") == "awaiting_legal_name":
            if len(text) < 2:
                await update.message.reply_text("እባክዎ ትክክለኛውን ሙሉ ስም ያስገቡ።")
                return
            context.user_data["withdraw_legal_name"] = text
            # Save this as their payment/real name for future reference
            # (shown on the main menu, used for deposit reconciliation)
            # now that we no longer collect it during registration.
            await self.players.set_payment_name(player.id, text)

            amount = context.user_data.get("withdraw_amount")
            method_key = context.user_data.get("withdraw_method")
            address = context.user_data.get("withdraw_address")
            legal_name = text
            methods = await self.admin_service.list_payment_methods(active_only=True)
            method = next((m for m in methods if m.key == method_key), None)
            method_name = method.name if method else method_key

            try:
                txn = await self.wallets.request_withdrawal(player.id, amount, method_key, legal_name, address)
                await update.message.reply_text(
                    f"✅ የ*{amount} ብር* የገንዘብ ማውጣት ጥያቄ ገብቷል!\n\n"
                    f"በአስተዳደር ግምገማ ከተደረገ በኋላ ወደ {method_name} ቁጥርዎ ይላካል፦ `{address}`",
                    parse_mode="Markdown",
                )
            except Exception as e:
                await update.message.reply_text(f"❌ የገንዘብ ማውጣት ጥያቄ አልተሳካም: {e}")

            context.user_data.pop("withdraw_state", None)
            context.user_data.pop("withdraw_amount", None)
            context.user_data.pop("withdraw_method", None)
            context.user_data.pop("withdraw_address", None)
            context.user_data.pop("withdraw_legal_name", None)
            return

        # Not expecting text right now.
        if player.registration_state == "complete":
            await self._send_main_menu(update, player)

    # ------------------------------------------------------------------
    # Wallet / top-up
    # ------------------------------------------------------------------

    async def balance_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        user, message = self._user_and_message(update)
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player or player.registration_state != "complete":
            await message.reply_text("Please /start first to register.")
            return
        wallet = await self.wallets.get_or_create_wallet(player.id)
        text = f"💰 Real balance (withdrawable): *{wallet.balance} ETB*"
        if wallet.bonus_balance:
            text += f"\n🎁 Bonus balance (playable only): *{wallet.bonus_balance} ETB*"
        await message.reply_text(text, parse_mode="Markdown")

    async def _set_promos(self, update, opt_out: bool) -> None:
        user, message = self._user_and_message(update)
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player:
            await message.reply_text("Please /start first to register.")
            return
        await self.players.update(player.id, {"promo_opt_out": opt_out})
        await message.reply_text(
            "🔕 Promotional messages are off. You will still get messages about your deposits, withdrawals and wins. "
            "Send /unmute to turn them back on."
            if opt_out else
            "🔔 Promotional messages are on again. Send /mute any time to stop them."
        )

    async def mute_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        await self._set_promos(update, True)

    async def unmute_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        await self._set_promos(update, False)

    async def topup_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        user, message = self._user_and_message(update)
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player or player.registration_state != "complete":
            await message.reply_text("Please /start first to register.")
            return
        if player.is_banned:
            await message.reply_text(
                "🚫 አካውንትዎ ታግዷል፤ ገንዘብ ማስገባት አይችሉም። "
                "ይህ ስህተት ነው ብለው ካሰቡ እባክዎ ድጋፍን ያግኙ።"
            )
            return

        methods = await self.admin_service.list_payment_methods(active_only=True)
        methods = [m for m in methods if m.supports_deposit]

        # Group by name
        groups = {}
        for m in methods:
            groups.setdefault(m.name, []).append(m)

        if not groups:
            await message.reply_text("በአሁኑ ጊዜ ምንም የገንዘብ ማስገቢያ መንገድ አልተዘጋጀም።")
            return

        keyboard = []
        for name in sorted(groups.keys()):
            keyboard.append([InlineKeyboardButton(name, callback_data=f"topup_group_{name}")])

        s = await self.admin_service.get_settings()
        await message.reply_text(
            f"💳 *ገንዘብ ማስገባት*\n\n"
            f"በአንድ ጊዜ ዝቅተኛ *{s.min_deposit} ብር*፣ ከፍተኛ *{s.max_deposit} ብር* ማስገባት ይችላሉ።\n\n"
            "እባክዎ የሚከፍሉበትን መንገድ ይምረጡ፦",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def withdraw_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        user, message = self._user_and_message(update)
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player or player.registration_state != "complete":
            await message.reply_text("Please /start first to register.")
            return
        if player.is_banned:
            await message.reply_text(
                "🚫 አካውንትዎ ታግዷል፤ ገንዘብ ማውጣት አይችሉም። "
                "ይህ ስህተት ነው ብለው ካሰቡ እባክዎ ድጋፍን ያግኙ።"
            )
            return

        wallet = await self.wallets.get_or_create_wallet(player.id)
        s = await self.admin_service.get_settings()

        if wallet.won_balance < s.min_withdrawal:
            await message.reply_text(
                f"⚠️ ገንዘብ ለማውጣት ቢያንስ *{s.min_withdrawal} ብር* ማሸነፍ ይኖርብዎታል።\n"
                f"የአሁኑ ማሸነፊያ ሂሳብዎ: {wallet.won_balance} ብር",
                parse_mode="Markdown"
            )
            return

        methods = await self.admin_service.list_payment_methods(active_only=True)
        methods = [m for m in methods if m.supports_withdrawal]

        if not methods:
            await message.reply_text("በአሁኑ ጊዜ ምንም የገንዘብ ማውጫ መንገድ አልተዘጋጀም።")
            return

        # A withdrawal destination belongs to the player, not to an admin's
        # receiving account. Present each configured payment method directly;
        # do not expose account holders as a second "provider" choice.
        keyboard = [[InlineKeyboardButton(m.name, callback_data=f"withdraw_method_{m.key}")] for m in methods]

        await message.reply_text(
            f"🏧 *ማሸነፊያ ገንዘብ ማውጣት*\n\n"
            f"ማውጣት የሚችሉት: *{wallet.won_balance} ብር*\n"
            f"ዝቅተኛ: {s.min_withdrawal} ብር | ከፍተኛ: {s.max_withdrawal} ብር\n\n"
            "እባክዎ ገንዘቡን የሚቀበሉበትን መንገድ ይምረጡ፦",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def photo_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("topup_state") != "awaiting_screenshot":
            return  # not currently doing a top-up; ignore stray photos

        user = update.effective_user
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player:
            return

        amount = context.user_data.get("topup_amount")
        method = context.user_data.get("topup_method")
        sms_text = context.user_data.get("topup_sms")
        payment_verification_url = context.user_data.get("topup_url")
        photo = update.message.photo[-1]  # largest size

        try:
            txn = await self.wallets.request_deposit(
                player.id, amount, method, photo.file_id,
                sms_text=sms_text, payment_verification_url=payment_verification_url
            )
        except (InvalidAmountError, DuplicateTransactionError) as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        context.user_data.pop("topup_state", None)
        context.user_data.pop("topup_amount", None)
        context.user_data.pop("topup_method", None)
        context.user_data.pop("topup_sms", None)
        context.user_data.pop("topup_url", None)

        await update.message.reply_text(
            "✅ የገንዘብ ማስገባት ጥያቄዎ ለአስተዳደር ግምገማ ገብቷል። "
            "እንደጸደቀ ወዲያውኑ ማሳወቂያ ይደርስዎታል።"
        )

    async def _handle_topup_decision(self, query, context, approve: bool):
        admin_user = query.from_user
        if not await self._is_admin(admin_user.id):
            await query.answer("You're not authorized to do this.", show_alert=True)
            return

        txn_id = query.data.split("_", 2)[-1]
        existing = await self.wallets.transaction_repo.get(txn_id)
        if not existing:
            await query.answer("Transaction not found.", show_alert=True)
            return
        if existing.status != "pending":
            await query.answer(f"Already {existing.status} by another admin.", show_alert=True)
            return

        try:
            if approve:
                txn = await self.wallets.approve_topup(txn_id, str(admin_user.id), source="bot")
            else:
                txn = await self.wallets.reject_topup(txn_id, str(admin_user.id), source="bot")
        except TransactionNotFoundError:
            await query.answer("Transaction not found.", show_alert=True)
            return

        # Update the clicked message to remove buttons and show resolution
        admin_label = admin_user.full_name or admin_user.username or str(admin_user.id)
        status_text = "Approved" if approve else "Rejected"
        emoji = "✅" if approve else "❌"

        orig_text = query.message.caption or query.message.text or ""
        resolution_line = f"{orig_text}\n\n{emoji} *{status_text}* by {admin_label} (via Telegram)"

        try:
            if query.message.photo or query.message.caption:
                await query.edit_message_caption(
                    caption=resolution_line[:1024],
                    parse_mode="Markdown",
                    reply_markup=None
                )
            else:
                await query.edit_message_text(
                    text=resolution_line[:4096],
                    parse_mode="Markdown",
                    reply_markup=None
                )
        except Exception:
            logger.exception("Failed to update query message on decision")

        await query.answer("Done")

        player = await self.players.get_by_id(txn.player_id)
        if player:
            try:
                if approve:
                    wallet = await self.wallets.get_or_create_wallet(player.id)
                    await context.bot.send_message(
                        chat_id=int(player.telegram_id),
                        text=(
                            f"✅ የ*{txn.amount} ብር* ገንዘብ ማስገባትዎ ጸድቋል!\n"
                            f"💰 አዲሱ ሂሳብዎ: *{wallet.balance} ብር*"
                        ),
                        parse_mode="Markdown",
                    )
                else:
                    await context.bot.send_message(
                        chat_id=int(player.telegram_id),
                        text=(
                            "❌ የገንዘብ ማስገባት ጥያቄዎ ውድቅ ተደርጓል። "
                            "እባክዎ ድጋፍን ያግኙ ወይም ግልጽ ስክሪንሾት በመጠቀም እንደገና ይሞክሩ።"
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to notify player %s of top-up decision", player.telegram_id)

    async def _handle_withdrawal_decision(self, query, context, approve: bool):
        admin_user = query.from_user
        if not await self._is_admin(admin_user.id):
            await query.answer("You're not authorized to do this.", show_alert=True)
            return

        txn_id = query.data.split("_", 2)[-1]
        existing = await self.wallets.transaction_repo.get(txn_id)
        if not existing:
            await query.answer("Transaction not found.", show_alert=True)
            return
        if existing.status != "pending":
            await query.answer(f"Already {existing.status} by another admin.", show_alert=True)
            return

        try:
            if approve:
                txn = await self.wallets.approve_withdrawal(txn_id, str(admin_user.id), source="bot")
            else:
                txn = await self.wallets.reject_withdrawal(txn_id, str(admin_user.id), source="bot")
        except TransactionNotFoundError:
            await query.answer("Transaction not found.", show_alert=True)
            return

        # Update the clicked message to remove buttons and show resolution
        admin_label = admin_user.full_name or admin_user.username or str(admin_user.id)
        status_text = "Marked Paid" if approve else "Rejected"
        emoji = "✅" if approve else "❌"

        orig_text = query.message.caption or query.message.text or ""
        if "\n\n" in orig_text:
            orig_text = orig_text.split("\n\n")[0]

        resolution_line = f"{orig_text}\n\n{emoji} *{status_text}* by {admin_label} (via Telegram)"

        try:
            if query.message.photo or query.message.caption:
                await query.edit_message_caption(
                    caption=resolution_line,
                    parse_mode="Markdown",
                    reply_markup=None
                )
            else:
                await query.edit_message_text(
                    text=resolution_line,
                    parse_mode="Markdown",
                    reply_markup=None
                )
        except Exception:
            logger.exception("Failed to update query message on decision")

        await query.answer("Done")

        player = await self.players.get_by_id(txn.player_id)
        if player:
            try:
                if approve:
                    await context.bot.send_message(
                        chat_id=int(player.telegram_id),
                        text=(
                            f"✅ የ*{abs(txn.amount)} ብር* የገንዘብ ማውጣትዎ ወደ "
                            f"{txn.withdrawal_address} ተልኳል!"
                        ),
                        parse_mode="Markdown",
                    )
                else:
                    wallet = await self.wallets.get_or_create_wallet(player.id)
                    await context.bot.send_message(
                        chat_id=int(player.telegram_id),
                        text=(
                            f"❌ የገንዘብ ማውጣት ጥያቄዎ ውድቅ ተደርጎ "
                            f"*{abs(txn.amount)} ብር* ወደ ሂሳብዎ ተመልሷል።\n"
                            f"💰 ማውጣት የሚችሉት ሂሳብ: *{wallet.won_balance} ብር*"
                        ),
                        parse_mode="Markdown",
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to notify player %s of withdrawal decision", player.telegram_id)

    # ------------------------------------------------------------------
    # Referral
    # ------------------------------------------------------------------

    async def referral_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        user, message = self._user_and_message(update)
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player or player.registration_state != "complete":
            await message.reply_text("Please /start first to register.")
            return

        from src.services.notifications import get_bot_username

        bot_username = await get_bot_username()
        stats = await self.referral_service.get_stats(player, bot_username)

        if not stats["referral_link"]:
            await message.reply_text(
                "⚠️ Couldn't generate your referral link right now - please try again in a moment."
            )
            return

        text = (
            f"🤝 *Your Referral Link*\n\n"
            f"Share this link with friends - when they register and complete sign-up, "
            f"you'll earn *{stats['bonus_per_referral']} ETB* bonus credit each time!\n\n"
            f"🔗 `{stats['referral_link']}`\n\n"
            f"📊 *Your stats:*\n"
            f"• Total friends invited: *{stats.get('total_invited', 0)}* 👥\n"
            f"• Paid referrals: *{stats['referral_count']}/{stats['max_referrals']}* 🎉\n"
            f"• Slots remaining: *{stats['slots_remaining']}*\n"
            f"• Bonus per referral: *{stats['bonus_per_referral']} ETB*\n\n"
            f"_Note: Referral bonus is added to your bonus balance "
            "(playable but not directly withdrawable)._"
        )

        import urllib.parse
        invite_text = (
            "🎮 Join me on Melbete Games! 🎪\n\n"
            f"I'm playing Melbete Bingo, the ultimate live 75-ball bingo game! "
            f"Play with real people, win awesome payouts, and claim your {stats.get('free_credit_amount', 50)} ETB FREE welcome credit immediately! 💰\n\n"
            "👇 TAP HERE TO PLAY NOW AND CLAIM YOUR FREE CREDIT! 👇"
        )
        share_url = f"https://t.me/share/url?url={urllib.parse.quote(stats['referral_link'])}&text={urllib.parse.quote(invite_text)}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 Share with Friends", url=share_url)
        ]])

        await message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    # ------------------------------------------------------------------
    # Play / help
    # ------------------------------------------------------------------

    async def play_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        user, message = self._user_and_message(update)
        player = await self.players.get_by_telegram_id(str(user.id))
        if not player or player.registration_state != "complete":
            await message.reply_text("Please /start first to register.")
            return
        if player.is_banned:
            await message.reply_text(
                "🚫 Your account has been suspended and can't join games. "
                "Contact support if you believe this is a mistake."
            )
            return

        wallet = await self.wallets.get_or_create_wallet(player.id)
        total = wallet.balance + wallet.bonus_balance
        s = await self.admin_service.get_settings()
        if total < s.stake_amount:
            await message.reply_text(
                f"⚠️ You need at least {s.stake_amount} ETB to join a round "
                f"(your balance: {total} ETB). Use /topup to deposit funds.",
            )
            return

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎮 Open Melbete Games", web_app=WebAppInfo(url=self._game_url()))
        ]])
        await message.reply_text(
            "🎮 Tap below to open Melbete Games - pick Bingo to jump right into the game. If a round is "
            "already running, you'll see it live and can join the next one; otherwise pick your cartela "
            "number to join now!",
            reply_markup=keyboard,
        )

    async def help_command(self, update, context: ContextTypes.DEFAULT_TYPE):
        _, message = self._user_and_message(update)
        s = await self.admin_service.get_settings()
        help_text = f"""
📖 *Melbete Bingo - How to Play*

This is a *live global game* - everyone online plays the same round together.

*Commands:*
• /start - Register / main menu
• /play - Open the game
• /balance - Check your wallet balance
• /mute - Stop promotional messages (/unmute to turn them back on)
• /topup - Deposit funds (or use the Deposit button in-game)
• /help - Show this help

*How a round works:*
1. Open the game and pick your *cartela number* (1-100) - reserving it is free.
2. Pick 1-4 cartelas. As soon as 3 cartelas are reserved, a 30-second countdown starts. You don't need to wait for other players - even a single player with 3 cartelas can start a round, just like a real hall with one caller and any number of cards.
3. When the countdown ends, everyone who reserved a cartela is charged {s.stake_amount} ETB and the round starts.
4. Numbers are called out one by one.
5. *You* mark the numbers on your card yourself - turn on Auto-Bingo in the game to claim automatically the instant you complete a valid pattern, or tap 🎉 BINGO manually.
6. Bingo is any full row, full column, either diagonal through the center, OR all four corners.
7. If your claim is valid, the prize pool is paid to your wallet automatically. Deposits and withdrawals are both available from inside the game.

Good luck! 🍀
"""
        if update.effective_user and await self._is_admin(update.effective_user.id):
            help_text += (
                "\n*Admin broadcast:*\n"
                "• `/broadcast Your update`\n"
                "• `/broadcast Your update | Open app | https://example.com`\n"
                "• Reply to a photo with either command to send it with the announcement.\n"
                "You can add up to three label + URL button pairs.\n"
            )
        await message.reply_text(help_text, parse_mode="Markdown")

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------

    async def pending_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_admin(user.id):
            return
        pending_deposits = await self.wallets.list_pending_topups()
        pending_withdrawals = await self.wallets.list_pending_withdrawals()
        if not pending_deposits and not pending_withdrawals:
            await update.message.reply_text("No pending deposits or withdrawals. ✅")
            return
        for txn in pending_deposits:
            player = await self.players.get_by_id(txn.player_id)
            caption = (
                f"💳 {player.payment_name if player else txn.player_id} - {txn.amount} ETB via {txn.payment_method}\n"
                f"Transaction: `{txn.id}`"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{txn.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{txn.id}"),
            ]])
            if txn.screenshot_file_id:
                await update.message.reply_photo(txn.screenshot_file_id, caption=caption, parse_mode="Markdown", reply_markup=keyboard)
            else:
                await update.message.reply_text(caption, parse_mode="Markdown", reply_markup=keyboard)
        for txn in pending_withdrawals:
            player = await self.players.get_by_id(txn.player_id)
            caption = (
                f"🏧 {player.payment_name if player else txn.player_id} - {abs(txn.amount)} ETB via {txn.payment_method}\n"
                f"Legal name: {txn.legal_name}\nSend to: {txn.withdrawal_address}\n"
                f"Transaction: `{txn.id}`"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Mark Paid", callback_data=f"withdrawal_approve_{txn.id}"),
                InlineKeyboardButton("❌ Reject & Refund", callback_data=f"withdrawal_reject_{txn.id}"),
            ]])
            await update.message.reply_text(caption, parse_mode="Markdown", reply_markup=keyboard)

    async def report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_admin(user.id):
            return
        from src.services.room_service import RoomService

        room = await RoomService().get_or_create_waiting_session()
        pending_deposits = await self.wallets.list_pending_topups()
        pending_withdrawals = await self.wallets.list_pending_withdrawals()
        await update.message.reply_text(
            f"📊 *Current session*\n"
            f"State: {room.state}\n"
            f"Players: {len(room.taken_cartelas)}\n"
            f"Prize pool: {room.prize_pool} ETB\n"
            f"Called numbers: {len(room.called_numbers)}\n\n"
            f"⏳ Pending deposits: {len(pending_deposits)}\n"
            f"⏳ Pending withdrawals: {len(pending_withdrawals)}\n"
            f"(use /pending to review)",
            parse_mode="Markdown",
        )

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_admin(user.id):
            await update.message.reply_text("You're not authorized to use this.")
            return
        if not settings.public_base_url:
            await update.message.reply_text(
                "⚠️ PUBLIC_BASE_URL isn't configured on the server, so the admin panel link can't be built."
            )
            return
        # IMPORTANT: this must be a `web_app` button (WebAppInfo), not a
        # plain URL/link button - only WebApp buttons get Telegram's
        # signed initData populated, which is what /admin uses to verify
        # who you are. Tapping a plain link opens it in Telegram's in-app
        # browser with no signature at all, and you'd be denied access.
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛠️ Open Admin Panel", web_app=WebAppInfo(url=self._admin_url()))
        ]])
        await update.message.reply_text("Tap below to open the admin panel:", reply_markup=keyboard)

    # ------------------------------------------------------------------
    # Callback routing
    # ------------------------------------------------------------------

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data or ""

        if data.startswith("topup_approve_") or data.startswith("topup_reject_"):
            await self._handle_topup_decision(query, context, approve=data.startswith("topup_approve_"))
            return

        if data.startswith("withdrawal_approve_") or data.startswith("withdrawal_reject_"):
            await self._handle_withdrawal_decision(query, context, approve=data.startswith("withdrawal_approve_"))
            return

        if data == "cancel_broadcast":
            if not query.from_user or not await self._is_admin(query.from_user.id):
                await query.answer("⛔ Admin access required.", show_alert=True)
                return
            await query.answer("Cancelling broadcast...")
            await BroadcastService(self.players).cancel()
            return

        await query.answer()

        if data == "play_now":
            await self.play_command(query, context)
        elif data == "balance":
            await self.balance_command(query, context)
        elif data == "help":
            await self.help_command(query, context)
        elif data == "topup":
            await self.topup_command(query, context)
        elif data == "withdraw":
            await self.withdraw_command(query, context)
        elif data == "referral":
            await self.referral_command(query, context)
        elif data.startswith("topup_group_"):
            name = data.replace("topup_group_", "")
            methods = await self.admin_service.list_payment_methods(active_only=True)
            group = [m for m in methods if m.name == name and m.supports_deposit]

            if len(group) == 1:
                await self._show_topup_details(query, group[0], context)
            else:
                keyboard = [[InlineKeyboardButton(f"{m.account_holder} ({m.account_info})", callback_data=f"topup_method_{m.key}")] for m in group]
                await query.message.reply_text(
                    f"💳 *{name}* - ገንዘቡን የሚልኩበትን አካውንት ይምረጡ፦",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif data.startswith("topup_method_"):
            method_key = data.replace("topup_method_", "")
            methods = await self.admin_service.list_payment_methods(active_only=True)
            method = next((m for m in methods if m.key == method_key), None)
            if method:
                await self._show_topup_details(query, method, context)

        elif data.startswith("withdraw_method_"):
            method_key = data.replace("withdraw_method_", "")
            methods = await self.admin_service.list_payment_methods(active_only=True)
            method = next((m for m in methods if m.key == method_key and m.supports_withdrawal), None)
            if method:
                await self._start_withdraw_flow(query, method, context)

    async def _show_topup_details(self, query, method, context):
        s = await self.admin_service.get_settings()
        context.user_data["topup_method"] = method.key
        context.user_data["topup_state"] = "awaiting_amount"

        details = f"👤 ስም: *{method.account_holder or 'N/A'}*\n📱 ስልክ ቁጥር: `{method.account_info}`"
        if method.instructions:
            details += f"\n📝 {method.instructions}"

        await query.message.reply_text(
            f"💳 *{method.name}*\n\n{details}\n\n"
            f"1️⃣ ገንዘቡን ከላይ ወዳለው ቁጥር ይላኩ (ቁጥሩን ነክተው መቅዳት ይችላሉ)።\n"
            f"2️⃣ ከዚያ በኋላ የላኩትን *የብር መጠን* ይጻፉ "
            f"(ዝቅተኛ {s.min_deposit}, ከፍተኛ {s.max_deposit} ብር)፦",
            parse_mode="Markdown",
        )

    async def _start_withdraw_flow(self, query, method, context):
        context.user_data["withdraw_method"] = method.key
        context.user_data["withdraw_state"] = "awaiting_amount"
        s = await self.admin_service.get_settings()

        await query.message.reply_text(
            f"🏧 *በ{method.name} ገንዘብ ማውጣት*\n\n"
            f"ማውጣት የሚፈልጉትን *የብር መጠን* ያስገቡ "
            f"(ዝቅተኛ {s.min_withdrawal}, ከፍተኛ {s.max_withdrawal} ብር)፦",
            parse_mode="Markdown"
        )

    def run(self):
        """Run the bot with long-polling."""
        logger.info("Starting Melbete Bingo Bot...")
        asyncio.run(self._run_polling_without_updater())

    async def _run_polling_without_updater(self):
        if self.app is None:
            raise RuntimeError("Bot app is not initialized. Call setup() first.")

        await self.app.initialize()
        await self.app.start()
        offset = None

        try:
            # Make the persistent Menu Button (bottom-left icon) open the
            # real Mini App WebView too, not just the /play button.
            if settings.public_base_url:
                try:
                    await self.app.bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(text="Play", web_app=WebAppInfo(url=self._game_url()))
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to set chat menu button")

            # Ensure getUpdates can be used even if a webhook was configured before.
            await self.app.bot.delete_webhook(drop_pending_updates=False)

            while True:
                updates = await self.app.bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=Update.ALL_TYPES,
                )
                for update in updates:
                    offset = update.update_id + 1
                    logger.info(
                        "Received Telegram update: update_id=%s has_message=%s has_callback=%s",
                        update.update_id,
                        bool(update.message),
                        bool(update.callback_query),
                    )
                    await self.app.update_queue.put(update)
        finally:
            await self.app.stop()
            await self.app.shutdown()


bot = MelbeteBingoBot()
