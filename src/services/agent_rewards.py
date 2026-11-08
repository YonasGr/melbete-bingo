"""Agent rewards: monthly-volume commission tiers, new-player and retention rewards, anti-abuse
checks, and the background job that pays held rewards.

How the money flows
-------------------
* Tier: an agent's volume this calendar month (their own top-ups + approved deposits of their
  players) picks a tier. The tier rate is stored on agents.tier_bps and the rate PAID is
  max(commission_bps, tier_bps) - so tiers can only ever raise an agent's rate.
* New-player reward: created PENDING when a linked player makes their first deposit. It is paid
  only after the hold period AND once the player has really played a few rounds.
* Retention reward: created once a rewarded player is still playing `retention_days` later.
* Every reward has a unique dedupe_key, so a retry can never pay twice, and the daily payout cap
  is enforced inside the same database transaction that pays.
* Nothing here can raise into a deposit or top-up: the hooks log and swallow errors.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.models.schemas import Agent, utcnow
from src.repositories.agent_repository import AgentRepository

logger = logging.getLogger(__name__)

MAX_TIERS = 10
RETENTION_ACTIVE_WINDOW_DAYS = 7      # a retained player must have played within this many days
UNQUALIFIED_EXPIRY_DAYS = 30          # a held reward the player never qualified for is cancelled after this
SUSPICIOUS_TOPUPS_PER_DAY = 5         # same agent -> same player this many times in 24h gets flagged
LOOP_MINUTES = int(os.environ.get("AGENT_REWARDS_LOOP_MINUTES", "60"))

_SETTING_LIMITS = {  # name -> (min, max)
    "new_player_bounty": (0, 10_000),
    "min_first_deposit": (0, 100_000),
    "min_rounds_played": (0, 100),
    "retention_bonus": (0, 10_000),
    "retention_days": (1, 365),
    "hold_days": (0, 60),
    "daily_payout_cap": (0, 1_000_000),
}


class RewardInputError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def month_start(now: Optional[datetime] = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def pick_tier(tiers: list[dict], volume: int) -> Optional[dict]:
    """The tier with the highest threshold the volume has reached (None if below every tier)."""
    best = None
    for t in sorted(tiers, key=lambda t: t["min_monthly_volume"]):
        if volume >= t["min_monthly_volume"]:
            best = t
    return best


def next_tier(tiers: list[dict], volume: int) -> Optional[dict]:
    for t in sorted(tiers, key=lambda t: t["min_monthly_volume"]):
        if t["min_monthly_volume"] > volume:
            return t
    return None


def validate_tiers(raw) -> list[dict]:
    from src.services.agent_service import InvalidAgentInputError, commission_percent_to_bps

    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_TIERS):
        raise RewardInputError(f"Provide between 1 and {MAX_TIERS} tiers")
    out, seen = [], set()
    for t in raw:
        if not isinstance(t, dict):
            raise RewardInputError("Each tier must be an object")
        label = str(t.get("label") or "").strip()[:20]
        vol = t.get("min_monthly_volume")
        if not label:
            raise RewardInputError("Every tier needs a name")
        if isinstance(vol, bool) or not isinstance(vol, int) or not (0 <= vol <= 100_000_000):
            raise RewardInputError("Monthly volume must be a whole number of ETB")
        if vol in seen:
            raise RewardInputError("Two tiers cannot have the same volume threshold")
        seen.add(vol)
        try:
            bps = commission_percent_to_bps(t.get("commission_percent"))
        except InvalidAgentInputError as e:
            raise RewardInputError(e.message)
        out.append({"label": label, "min_monthly_volume": vol, "commission_bps": bps})
    return sorted(out, key=lambda t: t["min_monthly_volume"])


def validate_settings(raw) -> dict:
    if not isinstance(raw, dict):
        raise RewardInputError("Invalid settings")
    out: dict = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise RewardInputError("enabled must be true or false")
        out["enabled"] = raw["enabled"]
    for name, (lo, hi) in _SETTING_LIMITS.items():
        if name in raw:
            v = raw[name]
            if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
                raise RewardInputError(f"{name.replace('_', ' ')} must be a whole number between {lo} and {hi}")
            out[name] = v
    return out


class AgentRewardService:
    def __init__(self, agents: Optional[AgentRepository] = None):
        self.agents = agents or AgentRepository()

    # ---------------------------------------------------------------- admin config
    async def get_settings(self) -> dict:
        return await self.agents.get_reward_settings()

    async def update_settings(self, admin_id: str, raw) -> dict:
        values = validate_settings(raw)
        result = await self.agents.update_reward_settings(values)
        logger.warning("Admin %s changed agent reward settings: %s", admin_id, values)
        return result

    async def get_tiers(self) -> list[dict]:
        return await self.agents.list_tiers()

    async def set_tiers(self, admin_id: str, raw) -> list[dict]:
        tiers = validate_tiers(raw)
        result = await self.agents.replace_tiers(tiers)
        logger.warning("Admin %s replaced agent tiers: %s", admin_id, tiers)
        return result

    # ---------------------------------------------------------------- tiers
    async def refresh_tier(self, agent: Agent, notify: bool = True) -> Agent:
        """Recompute this agent's tier from this month's volume. Notifies the agent on a promotion."""
        tiers = await self.agents.list_tiers()
        if not tiers:
            return agent
        now = utcnow()
        volume = await self.agents.volume_since(agent.id, month_start(now))
        tier = pick_tier(tiers, volume)
        bps, label = (tier["commission_bps"], tier["label"]) if tier else (0, "")
        if bps == agent.tier_bps and label == agent.tier_label:
            return agent
        updated = await self.agents.set_tier(agent.id, bps, label) or agent
        if notify and agent.tier_label and label and bps > agent.tier_bps:  # a real promotion, not the first assignment
            from src.services import player_notifications as pn

            pn.schedule_notify(agent.player_id, "agent_tier_up", {"label": label, "percent": bps / 100},
                               dedupe_key=f"agent_tier:{agent.id}:{now:%Y-%m}:{label}")
        return updated

    # ---------------------------------------------------------------- reward creation
    async def on_player_deposit(self, player_id: str, amount: int, txn_id: str = "") -> Optional[str]:
        """Call AFTER a deposit / top-up has committed. Creates the held new-player reward for the
        player's agent if this is the player's first deposit. Never raises."""
        try:
            return await self._on_player_deposit(player_id, int(amount), txn_id)
        except Exception:  # noqa: BLE001 - a reward problem must never affect a deposit
            logger.exception("Agent reward hook failed for player %s", player_id)
            return None

    async def _on_player_deposit(self, player_id: str, amount: int, txn_id: str) -> Optional[str]:
        from src.services.control_service import reward_blocked
        if await reward_blocked("agent_rewards"):
            return None   # new agent rewards are paused
        state = await self.agents.player_deposit_state(player_id)
        if not state or not state.get("agent_id"):
            return None
        agent = await self.agents.get(state["agent_id"])
        if agent is None or agent.status != "active":
            return None
        await self.refresh_tier(agent)
        if not agent.rewards_enabled:
            return None   # admin switched this agent's rewards off (commission on top-ups is unaffected)
        settings = await self.agents.get_reward_settings()
        if not settings["enabled"] or settings["new_player_bounty"] <= 0:
            return None
        if state.get("is_banned") or state.get("is_bot") or agent.player_id == player_id:
            return None
        if amount < settings["min_first_deposit"] or int(state["lifetime_deposited"]) > amount:
            return None  # below the minimum, or not the player's first deposit
        if await self.agents.shares_phone_with_agent(player_id, agent.player_id):
            logger.warning("Agent %s: no new-player reward, %s shares the agent's phone number", agent.id, player_id)
            return None
        if await self.agents.shares_ip_with_agent(player_id, agent.player_id):
            logger.warning("Agent %s: no new-player reward, %s uses the agent's connection", agent.id, player_id)
            return None
        release_at = utcnow() + timedelta(days=settings["hold_days"])
        created = await self.agents.create_reward(
            agent.id, player_id, "new_player", settings["new_player_bounty"], release_at,
            f"new_player:{player_id}", f"first deposit {amount} ETB {txn_id}".strip(),
        )
        return "created" if created else None

    async def on_deposit_reversed(self, player_id: str) -> int:
        """A deposit was reversed / the player was banned: cancel every reward still on hold for them."""
        try:
            return await self.agents.cancel_pending_for_player(player_id, "deposit reversed or player banned")
        except Exception:  # noqa: BLE001
            logger.exception("Could not cancel agent rewards for %s", player_id)
            return 0

    async def cancel_reward(self, admin_id: str, reward_id: str) -> dict:
        row = await self.agents.cancel_reward(reward_id, f"cancelled by admin {admin_id}")
        if row is None:
            raise RewardInputError("That reward is not on hold (already paid, cancelled or unknown)")
        logger.warning("Admin %s cancelled agent reward %s", admin_id, reward_id)
        return row

    # ---------------------------------------------------------------- the periodic job
    async def run_cycle(self) -> dict:
        now = utcnow()
        settings = await self.agents.get_reward_settings()
        result = {"tiers_changed": 0, "retention_created": 0, "paid": 0, "cancelled": 0, "capped": 0}

        for agent in await self.agents.active_agents():
            if (await self.refresh_tier(agent)) is not agent:
                result["tiers_changed"] += 1

        if not settings["enabled"]:
            return result

        from src.services.control_service import reward_blocked
        new_paused = bool(await reward_blocked("agent_rewards"))
        pay_paused = bool(await reward_blocked("agent_rewards", payout=True))

        if settings["retention_bonus"] > 0 and not new_paused:
            for c in await self.agents.retention_candidates(
                now - timedelta(days=settings["retention_days"]), now - timedelta(days=RETENTION_ACTIVE_WINDOW_DAYS),
            ):
                if await self.agents.create_reward(
                    c["agent_id"], c["player_id"], "retention", settings["retention_bonus"], now,
                    f"retention:{c['player_id']}", f"still playing after {settings['retention_days']} days",
                ):
                    result["retention_created"] += 1

        for rw in ([] if pay_paused else await self.agents.due_rewards()):   # "stop everything" holds payouts too
            outcome = await self._settle(rw, settings, now)
            if outcome == "paid":
                result["paid"] += 1
            elif outcome == "cancelled":
                result["cancelled"] += 1
            elif outcome == "cap":
                result["capped"] += 1
        return result

    async def _settle(self, rw: dict, settings: dict, now: datetime) -> str:
        try:
            agent = await self.agents.get(rw["agent_id"])
            if agent is None or agent.status != "active" or not agent.rewards_enabled:
                return "skipped"   # stays on hold; paid later if the agent is re-enabled
            reason = await self._disqualify(rw, agent, settings)
            if reason:
                await self.agents.cancel_reward(rw["id"], reason)
                return "cancelled"
            if rw["type"] == "new_player":
                played = await self.agents.rounds_played(rw["player_id"])
                if played < settings["min_rounds_played"]:
                    if rw["release_at"] + timedelta(days=UNQUALIFIED_EXPIRY_DAYS) < now:
                        await self.agents.cancel_reward(rw["id"], "player never reached the required rounds")
                        return "cancelled"
                    return "waiting"
            paid = await self.agents.pay_reward_atomic(rw["id"], settings["daily_payout_cap"])
            if paid["result"] == "paid":
                from src.services import player_notifications as pn

                pn.schedule_notify(paid["agent_player_id"], "agent_reward_paid",
                                   {"amount": paid["amount"], "reward_type": paid["type"]},
                                   dedupe_key=f"agent_reward:{rw['id']}")
                return "paid"
            return paid["result"]
        except Exception:  # noqa: BLE001 - one bad reward must not stop the others
            logger.exception("Could not settle agent reward %s", rw.get("id"))
            return "error"

    async def _disqualify(self, rw: dict, agent: Agent, settings: dict) -> Optional[str]:
        pid = rw.get("player_id")
        if not pid:
            return None
        if pid == agent.player_id:
            return "agent rewarding their own account"
        state = await self.agents.player_deposit_state(pid)
        if state is None or state.get("is_banned") or state.get("is_bot"):
            return "player banned or removed"
        if state.get("agent_id") != agent.id:
            return "player no longer linked to this agent"
        if await self.agents.shares_phone_with_agent(pid, agent.player_id):
            return "player shares the agent's phone number"
        if await self.agents.shares_ip_with_agent(pid, agent.player_id):
            return "player uses the same connection as the agent"
        return None

    # ---------------------------------------------------------------- read models
    async def agent_summary(self, agent: Agent) -> dict:
        """Everything the agent page needs for the rewards section."""
        now = utcnow()
        tiers = await self.agents.list_tiers()
        volume = await self.agents.volume_since(agent.id, month_start(now))
        nxt = next_tier(tiers, volume)
        board = await self.agents.leaderboard(now - timedelta(days=7), 10)
        settings = await self.agents.get_reward_settings()
        return {
            "enabled": settings["enabled"],
            "monthly_volume": volume,
            "tier": {"label": agent.tier_label, "percent": agent.tier_bps / 100},
            "effective_percent": max(agent.commission_bps, agent.tier_bps) / 100,
            "next_tier": ({"label": nxt["label"], "percent": nxt["commission_bps"] / 100,
                           "remaining": nxt["min_monthly_volume"] - volume} if nxt else None),
            "tiers": [{"label": t["label"], "min_monthly_volume": t["min_monthly_volume"],
                       "percent": t["commission_bps"] / 100} for t in tiers],
            "totals": await self.agents.reward_totals(agent.id),
            "rewards": [self._public_reward(r) for r in await self.agents.list_rewards(agent.id, None, 30)],
            "rules": {"new_player_bounty": settings["new_player_bounty"], "hold_days": settings["hold_days"],
                      "min_first_deposit": settings["min_first_deposit"], "min_rounds_played": settings["min_rounds_played"],
                      "retention_bonus": settings["retention_bonus"], "retention_days": settings["retention_days"]},
            "leaderboard": [{"rank": i + 1, "agent_code": self._mask(b["agent_code"]), "volume": b["volume"],
                             "is_me": b["id"] == agent.id} for i, b in enumerate(board)],
        }

    async def admin_overview(self) -> dict:
        now = utcnow()
        return {
            "settings": await self.agents.get_reward_settings(),
            "tiers": [{"label": t["label"], "min_monthly_volume": t["min_monthly_volume"],
                       "commission_percent": t["commission_bps"] / 100} for t in await self.agents.list_tiers()],
            "rewards": [self._admin_reward(r) for r in await self.agents.list_rewards(None, None, 100)],
            "flags": await self.agents.suspicious_topups(now - timedelta(hours=24), SUSPICIOUS_TOPUPS_PER_DAY),
        }

    @staticmethod
    def _mask(code: str) -> str:
        """Other agents are shown as AG****XY so the board motivates without exposing identities."""
        return f"{code[:2]}****{code[-2:]}" if len(code) > 4 else code

    @staticmethod
    def _public_reward(r: dict) -> dict:
        return {"type": r["type"], "amount": int(r["amount"]), "status": r["status"],
                "release_at": r["release_at"].isoformat(), "created_at": r["created_at"].isoformat()}

    @staticmethod
    def _admin_reward(r: dict) -> dict:
        return {"id": r["id"], "agent_code": r["agent_code"], "player_id": r["player_id"], "type": r["type"],
                "amount": int(r["amount"]), "status": r["status"], "note": r["note"],
                "release_at": r["release_at"].isoformat(), "created_at": r["created_at"].isoformat()}


_service: Optional[AgentRewardService] = None


def get_agent_reward_service() -> AgentRewardService:
    global _service
    if _service is None:
        _service = AgentRewardService()
    return _service


async def agent_rewards_loop() -> None:
    """Background task started at app startup. Never crashes the app: errors are logged, then it waits."""
    if LOOP_MINUTES <= 0:
        return
    while True:
        await asyncio.sleep(LOOP_MINUTES * 60)
        try:
            from src.core import loop_lock

            if not await loop_lock.try_acquire("agent_rewards", max(120, LOOP_MINUTES * 120)):
                continue
            result = await get_agent_reward_service().run_cycle()
            if any(result.values()):
                logger.info("Agent rewards cycle: %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("Agent rewards cycle failed")
