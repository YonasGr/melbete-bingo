"""Safe, rate-limited Telegram broadcasts for platform announcements."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from src.core.config import settings
from src.repositories.player_repository import PlayerRepository

logger = logging.getLogger(__name__)
MAX_BUTTONS = 3
RECIPIENT_BATCH_SIZE = 25


class BroadcastValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BroadcastResult:
    attempted: int
    delivered: int
    failed: int


@dataclass(frozen=True)
class BroadcastPhoto:
    """A validated local image supplied by an authenticated administrator."""

    content: bytes
    filename: str


def validate_buttons(buttons: Optional[Iterable[dict]]) -> list[dict]:
    cleaned: list[dict] = []
    for item in buttons or []:
        text = str(item.get("text") or "").strip()
        url = str(item.get("url") or "").strip()
        if not text and not url:
            continue
        if not text or not url:
            raise BroadcastValidationError("Each button needs both a label and a URL")
        if len(text) > 64:
            raise BroadcastValidationError("Button labels must be 64 characters or fewer")
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise BroadcastValidationError("Button URLs must start with http:// or https://")
        cleaned.append({"text": text, "url": url})
    if len(cleaned) > MAX_BUTTONS:
        raise BroadcastValidationError(f"A broadcast can have at most {MAX_BUTTONS} buttons")
    return cleaned


class BroadcastService:
    def __init__(self, player_repo: Optional[PlayerRepository] = None):
        self.players = player_repo or PlayerRepository()

    async def cancel(self) -> bool:
        """Cancel the current active broadcast immediately."""
        res = await self.players._execute("""
            UPDATE broadcast_progress
            SET status = 'cancelled', updated_at = now()
            WHERE id = 'active' AND status IN ('running', 'cancelling')
        """)
        return res is not None

    async def send(self, text: str, *, photo: Optional[BroadcastPhoto | str] = None, buttons: Optional[Iterable[dict]] = None, bot: Optional[Bot] = None, on_progress = None) -> BroadcastResult:
        text = (text or "").strip()
        if not text and not photo:
            raise BroadcastValidationError("Write a message or attach an image")
        if len(text) > (1024 if photo else 4096):
            raise BroadcastValidationError("Message is too long for this broadcast format")
        clean_buttons = validate_buttons(buttons)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"])] for b in clean_buttons]) if clean_buttons else None
        sender = bot or Bot(token=settings.telegram_bot_token)
        attempted = delivered = failed = offset = 0
        batch_count = 0

        # Fetch total recipients to update progress accurately
        total_count = await self.players._fetchval("SELECT COUNT(*) FROM players WHERE telegram_id IS NOT NULL") or 0

        # Initialize progress row
        await self.players._execute("""
            INSERT INTO broadcast_progress (id, total_users, sent_users, delivered_users, failed_users, status, updated_at)
            VALUES ('active', $1, 0, 0, 0, 'running', now())
            ON CONFLICT (id) DO UPDATE SET
                total_users = $1,
                sent_users = 0,
                delivered_users = 0,
                failed_users = 0,
                status = 'running',
                updated_at = now()
        """, total_count)

        try:
            while True:
                # Check for cancellation signal before processing next batch
                current_status = await self.players._fetchval("SELECT status FROM broadcast_progress WHERE id = 'active'")
                if current_status in ('cancelled', 'cancelling'):
                    logger.info("Broadcast cancellation detected. Halting after %s recipients attempted.", attempted)
                    await self.players._execute("""
                        UPDATE broadcast_progress
                        SET sent_users = $1,
                            delivered_users = $2,
                            failed_users = $3,
                            status = 'cancelled',
                            updated_at = now()
                        WHERE id = 'active'
                    """, attempted, delivered, failed)
                    return BroadcastResult(attempted=attempted, delivered=delivered, failed=failed)

                recipients = await self.players.list_telegram_ids(limit=RECIPIENT_BATCH_SIZE, offset=offset)
                if not recipients:
                    break
                offset += len(recipients)
                attempted += len(recipients)
                batch_count += 1

                async def deliver(chat_id: str) -> bool:
                    try:
                        if photo:
                            if isinstance(photo, str):
                                photo_param = photo
                            else:
                                photo_param = InputFile(photo.content, filename=photo.filename)
                            await sender.send_photo(
                                chat_id=chat_id,
                                photo=photo_param,
                                caption=text or None,
                                reply_markup=markup,
                            )
                        else:
                            await sender.send_message(chat_id=chat_id, text=text, reply_markup=markup)
                        return True
                    except Forbidden as exc:
                        logger.info("Broadcast could not reach Telegram user %s: User blocked bot or forbidden (%s)", chat_id, exc)
                        return False
                    except (NetworkError, TimedOut) as exc:
                        logger.warning("Broadcast network error for Telegram user %s (%s)", chat_id, exc)
                        return False
                    except BadRequest as exc:
                        logger.warning("Broadcast bad request for Telegram user %s (%s)", chat_id, exc)
                        return False
                    except RetryAfter as exc:
                        logger.warning("Broadcast rate limited for Telegram user %s (retry after %ss)", chat_id, exc.retry_after)
                        return False
                    except TelegramError as exc:
                        logger.warning("Broadcast failed for Telegram user %s: %s", chat_id, exc)
                        return False
                    except Exception as exc:
                        logger.exception("Broadcast failed for Telegram user %s due to unexpected error: %s", chat_id, exc)
                        return False

                results = await asyncio.gather(*(deliver(recipient) for recipient in recipients))
                delivered += sum(results)
                failed += len(results) - sum(results)

                # Update database progress metrics every 5 batches (~5x DB load reduction)
                if batch_count % 5 == 0:
                    await self.players._execute("""
                        UPDATE broadcast_progress
                        SET sent_users = $1,
                            delivered_users = $2,
                            failed_users = $3,
                            updated_at = now()
                        WHERE id = 'active'
                    """, attempted, delivered, failed)

                # Trigger the real-time progress callback if supplied
                if on_progress:
                    try:
                        await on_progress(attempted, total_count, delivered, failed)
                    except Exception:
                        logger.exception("Error in broadcast progress callback")

                if len(recipients) == RECIPIENT_BATCH_SIZE:
                    await asyncio.sleep(1)

            # Check if broadcast was cancelled before marking complete
            final_status = await self.players._fetchval("SELECT status FROM broadcast_progress WHERE id = 'active'")
            if final_status in ('cancelled', 'cancelling'):
                await self.players._execute("""
                    UPDATE broadcast_progress
                    SET sent_users = $1,
                        delivered_users = $2,
                        failed_users = $3,
                        status = 'cancelled',
                        updated_at = now()
                    WHERE id = 'active'
                """, attempted, delivered, failed)
                return BroadcastResult(attempted=attempted, delivered=delivered, failed=failed)

            # Flush final metrics and mark complete
            await self.players._execute("""
                UPDATE broadcast_progress
                SET sent_users = $1,
                    delivered_users = $2,
                    failed_users = $3,
                    status = 'completed',
                    updated_at = now()
                WHERE id = 'active'
            """, attempted, delivered, failed)

        except Exception as exc:
            # Check if broadcast was marked cancelled
            current_status = await self.players._fetchval("SELECT status FROM broadcast_progress WHERE id = 'active'")
            if current_status != 'cancelled':
                await self.players._execute("""
                    UPDATE broadcast_progress
                    SET sent_users = $1,
                        delivered_users = $2,
                        failed_users = $3,
                        status = 'failed',
                        updated_at = now()
                    WHERE id = 'active'
                """, attempted, delivered, failed)
            raise exc

        return BroadcastResult(attempted=attempted, delivered=delivered, failed=failed)
