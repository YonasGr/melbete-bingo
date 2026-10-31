"""
Bot service - manages 50 automated Ethiopian bot players, joining active rounds with random cartelas,
initiating rounds when enabled, and auto-claiming bingo wins.
"""
import asyncio
import random
import logging
from typing import List, Optional

from src.models.schemas import GameRoom, Card
from src.repositories import BotRepository, SettingsRepository
from src.services.game_service import PatternVerifier

logger = logging.getLogger(__name__)


class BotService:
    def __init__(self, bot_repo: Optional[BotRepository] = None, settings_repo: Optional[SettingsRepository] = None):
        self.bot_repo = bot_repo or BotRepository()
        self.settings_repo = settings_repo or SettingsRepository()

    async def ensure_bots_initialized(self) -> List:
        return await self.bot_repo.ensure_bots_seeded(50)

    async def fill_room_with_bots(self, room_service, room_id: str) -> None:
        """Fill the specified waiting room with bots, one at a time, at
        random intervals, each picking a random available cartela - and
        rotate through the full 50-bot pool so a different slice of bots
        tends to join each round instead of the same ones every time.
        Which track's bot toggle/count applies is read from the room's
        own track (real_10, real_50, or bonus) - each is independently
        configured in the admin panel."""
        room = await room_service.get_room(room_id)
        if room.state != "waiting":
            return

        s = await self.settings_repo.get_or_create()
        track_cfg = room_service._track_settings(s, room.track)
        if not track_cfg["bot_enabled"]:
            return

        await self.ensure_bots_initialized()
        target_max_bots = track_cfg["max_bots"]
        cards_in_room = await room_service.cards.list_by_room(room_id)
        existing_owner_ids = {c.owner_id for c in cards_in_room}
        existing_bot_count = len([c for c in cards_in_room if c.owner_id.startswith("bot_player_")])

        needed_bots = target_max_bots - existing_bot_count
        if needed_bots <= 0:
            return

        # Rotation: bots that haven't played in the longest time (or ever)
        # come first, so a fresh set of ~`needed_bots` bots from the pool
        # of 50 gets picked this round, and a different fresh set next
        # round, cycling through everyone before anyone repeats.
        rotation = await self.bot_repo.list_bots_for_rotation()
        bot_queue = [b for b in rotation if b.player_id not in existing_owner_ids][:needed_bots]
        random.shuffle(bot_queue)  # don't always offer them cartelas in the same rotation order either

        all_possible_cartelas = set(range(room.cartela_min, room.cartela_max + 1))
        joined_player_ids: List[str] = []

        for bot in bot_queue:
            # Random pause before THIS bot decides to join - independent
            # per bot, not a shared batch delay, so joins land scattered
            # across time instead of clumping together.
            await asyncio.sleep(random.uniform(1.5, 6.0))

            fresh_room = await room_service.get_room(room_id)
            if fresh_room.state != "waiting":
                break

            taken = set(fresh_room.taken_cartelas)
            available = list(all_possible_cartelas - taken)
            if not available:
                break

            cartela = random.choice(available)
            try:
                await room_service.select_cartela(room_id, bot.player_id, cartela)
                joined_player_ids.append(bot.player_id)
            except Exception as e:
                logger.warning(f"Bot {bot.player_id} failed to select cartela {cartela}: {e}")

        if joined_player_ids:
            await self.bot_repo.mark_played(joined_player_ids)

    async def check_and_claim_bot_wins(self, room_service, room_id: str) -> Optional[dict]:
        """Check if any bot in the running room has completed a winning pattern, and submit claim_win."""
        room = await room_service.get_room(room_id)
        if room.state != "running":
            return None

        cards = await room_service.cards.list_by_room(room_id)
        bot_cards = [c for c in cards if c.owner_id.startswith("bot_player_")]

        pattern_name = room.pattern.get("id", "any_line")

        for card in bot_cards:
            is_valid, _ = PatternVerifier.verify_claim(card.grid, room.called_numbers, pattern_name, room.variant)
            if is_valid:
                bot_rec = await self.bot_repo.get_by_player_id(card.owner_id)
                bot_name = bot_rec.display_name if bot_rec else "Bot Player"
                outcome = await room_service.claim_win(room_id, card.owner_id, card.id, player_name=bot_name)
                if outcome.won:
                    logger.info(f"Bot {bot_name} ({card.owner_id}) won room {room_id}!")
                    return outcome.winner

        return None
