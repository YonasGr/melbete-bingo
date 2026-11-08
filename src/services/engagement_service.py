"""Player engagement: play streaks, "round starting" nudges, and win-back free rounds.

Design rules (this touches house money and players' attention, so it is deliberately restrained):
- Every reward is house-funded BONUS balance: playable, never withdrawable.
- Every "send / pay" decision is a guarded UPDATE in the repository, so it happens at most once per
  cooldown even with overlapping jobs or several workers.
- Promotional messages skip players who muted them, never go out during quiet hours (EAT), and each
  kind has a per-player cooldown. Win-back has a hard daily ETB budget.
- Nothing here can influence a draw or a result.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from src.core.config import settings as app_settings

logger = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))  # Ethiopia time: streak days and quiet hours follow the players' day


def parse_streak_rewards(raw: str) -> dict[int, int]:
    """'3:10,7:30' -> {3: 10, 7: 30}. Malformed or non-positive entries are ignored (never raise)."""
    rewards: dict[int, int] = {}
    for part in (raw or "").split(","):
        days, _, amount = part.strip().partition(":")
        try:
            d, a = int(days), int(amount)
        except ValueError:
            continue
        if d >= 1 and a >= 1:
            rewards[d] = a
    return rewards


def eat_day(now: datetime) -> date:
    return now.astimezone(EAT).date()


class EngagementService:
    def __init__(self, repo=None, wallets=None, cfg=None, notify: Optional[Callable] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        if repo is None:
            from src.repositories.engagement_repository import EngagementRepository

            repo = EngagementRepository()
        self.repo = repo
        self._wallets = wallets
        self.cfg = cfg or app_settings
        self._notify = notify
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._last_nudge_batch: dict[str, datetime] = {}
        self._last_nudge_sent: dict[str, datetime] = {}
        self._last_room_players: dict[str, set[str]] = {}

    # -- plumbing ----------------------------------------------------------------------------

    @property
    def wallets(self):
        if self._wallets is None:
            from src.services.wallet_service import WalletService

            self._wallets = WalletService()
        return self._wallets

    def notify(self, player_id: str, type_: str, data: dict, dedupe_key: Optional[str] = None) -> None:
        if self._notify is not None:
            self._notify(player_id, type_, data, dedupe_key)
            return
        from src.services import player_notifications as pn

        pn.schedule_notify(player_id, type_, data, dedupe_key)

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        hour = (now or self._now()).astimezone(EAT).hour
        start, end = self.cfg.engagement_quiet_start_hour, self.cfg.engagement_quiet_end_hour
        return (hour >= start or hour < end) if start > end else (start <= hour < end)

    # -- streaks -----------------------------------------------------------------------------

    async def record_round_played(self, player_ids) -> None:
        """Called once a round has started with these players charged. Updates each player's streak
        and pays the streak reward when a milestone is reached (once per streak run)."""
        rewards = parse_streak_rewards(self.cfg.engagement_streak_rewards)
        now = self._now()
        from src.services.control_service import reward_blocked
        streak_paused = bool(await reward_blocked("streak_winback"))
        day = eat_day(now)
        for pid in set(player_ids):
            try:
                state = await self.repo.record_play(pid, day, now)
                streak = int(state["streak_days"])
                amount = rewards.get(streak)
                if amount and not streak_paused and not await reward_blocked("streak_winback", player_id=pid) and await self.repo.claim_streak_reward(pid, streak):
                    # The claim above is the once-only guard; if paying fails we log loudly rather than
                    # risk paying the same milestone twice.
                    await self.wallets.credit_engagement_bonus(pid, amount, "streak_reward", f"{streak}-day play streak")
                    self.notify(pid, "streak_reward", {"days": streak, "amount": amount}, dedupe_key=f"streak:{pid}:{day}:{streak}")
            except Exception:  # noqa: BLE001 - one player's failure must not stop the others
                logger.exception("Could not record play / streak for %s", pid)
            await self._low_balance_nudge(pid, now, day)

    async def _low_balance_nudge(self, pid: str, now: datetime, day: date) -> None:
        """After a round: if the player can no longer afford another cartela, say so - at most once per EAT
        day (the notification's dedupe key), never in quiet hours, and never to players who muted promos
        (PlayerNotifier drops promo types for them). Never raises: it must not affect a round."""
        threshold = int(getattr(self.cfg, "engagement_low_balance_threshold_etb", 0) or 0)
        if threshold <= 0 or self.in_quiet_hours(now):
            return
        try:
            wallet = await self.wallets.get_or_create_wallet(pid)
            total = int(wallet.balance) + int(getattr(wallet, "bonus_balance", 0) or 0)
            if total < threshold:
                self.notify(pid, "low_balance", {"balance": total, "threshold": threshold}, dedupe_key=f"low_balance:{pid}:{day}")
        except Exception:  # noqa: BLE001
            logger.exception("Low-balance check failed for %s", pid)

    # -- "round starting" nudge --------------------------------------------------------------

    async def nudge_round_starting(self, track: str, stake: int, players_in_room: list[str], seconds: int) -> int:
        """A new lobby just opened: tell recent players who are not in it. Bounded by a per-player
        cooldown, a per-track batch gap, a recipient cap and quiet hours. Returns how many were sent."""
        cfg = self.cfg
        now = self._now()
        if cfg.engagement_nudge_max_recipients <= 0 or self.in_quiet_hours(now):
            return 0
        last = self._last_nudge_batch.get(track)
        if last and now - last < timedelta(minutes=cfg.engagement_nudge_batch_gap_minutes):
            return 0
        self._last_nudge_batch[track] = now
        excluded = set(players_in_room) | self._last_room_players.get(track, set())
        if players_in_room:
            self._last_room_players[track] = set(players_in_room)
        candidates = await self.repo.nudge_candidates(
            now - timedelta(days=14), now - timedelta(hours=cfg.engagement_nudge_cooldown_hours),
            list(excluded), cfg.engagement_nudge_max_recipients,
        )
        cutoff = now - timedelta(hours=cfg.engagement_nudge_cooldown_hours)
        sent = 0
        for row in candidates:
            pid = row["player_id"]
            previous = self._last_nudge_sent.get(pid)
            if previous and now - previous < timedelta(hours=cfg.engagement_nudge_cooldown_hours):
                continue
            if await self.repo.claim_nudge(pid, cutoff, now):
                self.notify(pid, "room_filling",
                            {"stake": stake, "players": max(len(players_in_room), 1), "seconds": seconds},
                            dedupe_key=f"nudge:{pid}:{now:%Y%m%d%H%M}")
                self._last_nudge_sent[pid] = now
                sent += 1
        return sent

    # -- periodic job: win-back + streak reminders ------------------------------------------

    async def run_cycle(self) -> dict:
        """One pass of the background job. Safe to run often: every action is claim-guarded."""
        now = self._now()
        if self.in_quiet_hours(now):
            return {"winback": 0, "reminders": 0, "skipped": "quiet_hours"}
        return {"winback": await self._run_winback(now), "reminders": await self._run_streak_reminders(now)}

    async def _run_winback(self, now: datetime) -> int:
        cfg = self.cfg
        from src.services.control_service import reward_blocked
        if await reward_blocked("streak_winback"):
            return 0
        amount = cfg.engagement_winback_bonus_etb
        if amount <= 0 or cfg.engagement_winback_days <= 0:
            return 0
        day_start = now.astimezone(EAT).replace(hour=0, minute=0, second=0, microsecond=0)
        remaining = cfg.engagement_winback_daily_budget_etb - await self.repo.winback_paid_since(day_start)
        if remaining < amount:
            return 0  # today's budget is used up
        cooldown_cutoff = now - timedelta(days=cfg.engagement_winback_cooldown_days)
        candidates = await self.repo.lapsed_candidates(
            now - timedelta(days=cfg.engagement_winback_days), now - timedelta(days=90),
            cooldown_cutoff, remaining // amount,
        )
        paid = 0
        for row in candidates:
            pid = row["player_id"]
            if not await self.repo.claim_winback(pid, cooldown_cutoff, now):
                continue
            try:
                await self.wallets.credit_engagement_bonus(pid, amount, "winback_bonus", "Welcome-back free round")
            except Exception:  # noqa: BLE001
                logger.exception("Win-back bonus for %s failed; releasing the claim so it can be retried", pid)
                await self.repo.release_winback(pid, None)
                continue
            self.notify(pid, "winback_bonus", {"amount": amount}, dedupe_key=f"winback:{pid}:{now:%Y%m%d}")
            paid += 1
        return paid

    async def _run_streak_reminders(self, now: datetime) -> int:
        rewards = parse_streak_rewards(self.cfg.engagement_streak_rewards)
        hour = now.astimezone(EAT).hour
        if not rewards or not (16 <= hour < 21):  # one gentle reminder, in the evening only
            return 0
        today = eat_day(now)
        sent = 0
        for row in await self.repo.streak_reminder_candidates(today, 200):
            pid, streak = row["player_id"], int(row["streak_days"])
            if not await self.repo.claim_streak_reminder(pid, today):
                continue
            nxt = min((d for d in rewards if d > streak), default=None)
            self.notify(pid, "streak_at_risk",
                        {"streak": streak, "next_days": nxt, "next_amount": rewards.get(nxt) if nxt else None},
                        dedupe_key=f"streak_reminder:{pid}:{today}")
            sent += 1
        return sent


_service: Optional[EngagementService] = None


def get_engagement_service() -> EngagementService:
    global _service
    if _service is None:
        _service = EngagementService()
    return _service


async def engagement_loop() -> None:
    """Background task started at app startup. Never crashes the app: errors are logged, then it waits."""
    minutes = app_settings.engagement_loop_minutes
    if minutes <= 0:
        return
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            from src.core import loop_lock

            # One instance at a time (rolling deploys briefly run two). The lease outlives one period.
            if not await loop_lock.try_acquire("engagement", max(120, minutes * 120)):
                continue
            result = await get_engagement_service().run_cycle()
            if result.get("winback") or result.get("reminders"):
                logger.info("Engagement cycle: %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("Engagement cycle failed")
