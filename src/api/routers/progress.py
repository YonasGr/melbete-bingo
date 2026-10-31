"""Read-only cosmetic progress for the home screen: level + play-streak calendar.
Identity is verified from Telegram initData like the wallet routes."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, Query

from src.api.dependencies import _require_player_identity
from src.repositories.base import BaseRepository
from src.services.progress import level_for, streak_calendar

router = APIRouter(prefix="/api/progress", tags=["progress"])
EAT = timezone(timedelta(hours=3))


class _ProgressRepo(BaseRepository):
    async def rounds_and_wins(self, player_id: str) -> dict:
        row = await self._fetchrow(
            """SELECT GREATEST(COUNT(*) FILTER (WHERE type = 'game_stake')
                               - COUNT(*) FILTER (WHERE type = 'stake_refund'), 0) AS rounds,
                      COUNT(*) FILTER (WHERE type = 'win_payout') AS wins
               FROM transactions WHERE player_id = $1""", player_id)
        return row or {"rounds": 0, "wins": 0}

    async def streak(self, player_id: str):
        return await self._fetchrow(
            "SELECT streak_days, best_streak, last_play_day FROM engagement_state WHERE player_id = $1", player_id)


_repo = None


@router.get("")
async def get_progress(
    player_id: str = Query(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    global _repo
    _repo = _repo or _ProgressRepo()
    stats = await _repo.rounds_and_wins(player_id)
    st = await _repo.streak(player_id) or {}
    cal = streak_calendar(st.get("streak_days") or 0, st.get("last_play_day"), datetime.now(EAT).date())
    cal["best"] = int(st.get("best_streak") or 0)
    return {"rounds": int(stats["rounds"]), "wins": int(stats["wins"]), "level": level_for(stats["rounds"]), "streak": cal}
