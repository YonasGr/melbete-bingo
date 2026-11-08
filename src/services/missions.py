"""Daily missions: play N rounds today, claim a bonus. Bonus credit is play-only (never withdrawable), exactly like
streak rewards. Configured with ENGAGEMENT_MISSIONS ("3:20,5:40,10:80"); an empty value switches missions off."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.config import settings
from src.repositories.mission_repository import MissionRepository
from src.services.engagement_service import EAT, eat_day, parse_streak_rewards

logger = logging.getLogger(__name__)


class MissionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def day_start_utc(now: datetime) -> datetime:
    """Midnight of the current EAT day, as UTC."""
    local = now.astimezone(EAT)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


class MissionService:
    def __init__(self, repo: Optional[MissionRepository] = None):
        self.repo = repo or MissionRepository()

    @staticmethod
    def definitions() -> dict[int, int]:
        return dict(sorted(parse_streak_rewards(settings.engagement_missions).items()))   # same "a:b" format

    async def summary(self, player_id: str) -> dict:
        now = datetime.now(timezone.utc)
        defs = self.definitions()
        if not defs:
            return {"enabled": False, "missions": []}
        day = eat_day(now)
        played = await self.repo.rounds_played(player_id, day_start_utc(now))
        done = await self.repo.claimed(player_id, day)
        return {
            "enabled": True, "rounds_today": played,
            "missions": [{"rounds": r, "reward": a, "progress": min(played, r), "claimed": r in done, "claimable": played >= r and r not in done}
                         for r, a in defs.items()],
        }

    async def claim(self, player_id: str, rounds: int) -> dict:
        from src.api.dependencies import get_wallet_service

        from src.services.control_service import reward_blocked
        paused = await reward_blocked("missions", player_id=player_id)
        if paused:
            raise MissionError(paused, 403)
        defs = self.definitions()
        if rounds not in defs:
            raise MissionError("Unknown mission.")
        amount = defs[rounds]
        now = datetime.now(timezone.utc)
        day = eat_day(now)
        if await self.repo.rounds_played(player_id, day_start_utc(now)) < rounds:
            raise MissionError("Play more rounds today to unlock this reward.")
        cap = int(settings.engagement_missions_daily_budget_etb)
        if cap > 0 and await self.repo.paid_on(day) + amount > cap:
            raise MissionError("Today's mission rewards have run out. Try again tomorrow.", 409)
        if not await self.repo.claim(player_id, day, rounds, amount):      # once-only guard
            raise MissionError("You already claimed this mission today.", 409)
        try:
            await get_wallet_service().credit_engagement_bonus(player_id, amount, "mission_reward", f"Daily mission: play {rounds} rounds")
        except Exception:  # noqa: BLE001 - give the player the claim back rather than lose it
            logger.exception("Could not credit mission reward to %s", player_id)
            await self.repo.unclaim(player_id, day, rounds)
            raise MissionError("Could not credit the reward. Please try again.", 500)
        return {"amount": amount, "rounds": rounds}


_service: Optional[MissionService] = None


def get_mission_service() -> MissionService:
    global _service
    if _service is None:
        _service = MissionService()
    return _service
