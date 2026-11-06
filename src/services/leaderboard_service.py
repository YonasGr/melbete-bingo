"""Admin-controlled leaderboard campaigns.

The admin chooses each campaign's start and end date-time (in a timezone the admin picks) - nothing is derived from
calendar months. Players earn points for cartelas RESERVED while the campaign is running, plus an optional one-time
Telegram-channel reward that is verified by the bot. When a campaign ends nothing is paid automatically: an admin
approves or rejects the distribution. Approved rewards land in the winner's bonus balance and become withdrawable
after the wagering requirement (stakes x multiplier) has been met.

Pure helpers (parsing, phases, reward split) are module-level functions so they can be unit-tested without a database.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.models.schemas import Transaction, utcnow
from src.repositories.leaderboard_repository import LeaderboardRepository

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Africa/Addis_Ababa"
_FIXED_OFFSETS = {"Africa/Addis_Ababa": timezone(timedelta(hours=3), "EAT")}   # no DST: safe fallback without tzdata
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
ATTEMPTS_PER_MINUTE = 5


class LeaderboardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------------------------- pure helpers

def get_tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name or no tzdata installed
        if name in _FIXED_OFFSETS:
            return _FIXED_OFFSETS[name]
        raise LeaderboardError(f"Unknown timezone '{name}'. Use an IANA name such as Africa/Addis_Ababa.")


def parse_local(text, tz_name: str) -> datetime:
    """'2026-11-01T09:30' written in the admin's timezone -> an aware UTC datetime."""
    if not isinstance(text, str) or not text.strip():
        raise LeaderboardError("Please set both the start and the end date and time.")
    try:
        naive = datetime.fromisoformat(text.strip().replace("Z", ""))
    except ValueError:
        raise LeaderboardError("Dates must look like 2026-11-01 09:30.")
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=get_tz(tz_name)).astimezone(timezone.utc)


def to_local_text(dt: datetime, tz_name: str) -> str:
    return dt.astimezone(get_tz(tz_name)).strftime("%Y-%m-%dT%H:%M")


def campaign_phase(c: dict, now: datetime) -> str:
    """cancelled | scheduled | active | ended (waiting for the admin's decision) | distributed | rejected."""
    if c["cancelled"]:
        return "cancelled"
    if now < c["starts_at"]:
        return "scheduled"
    if now < c["ends_at"]:
        return "active"
    return {"approved": "distributed", "rejected": "rejected"}.get(c["distribution_status"], "ended")


def normalize_channel(raw) -> str:
    name = str(raw or "").strip()
    name = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", name, flags=re.I).lstrip("@").split("?")[0].strip("/")
    return name


def _whole(payload: dict, key: str, label: str, lo: int, hi: int, default=None) -> int:
    raw = payload.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise LeaderboardError(f"{label} is required.")
    try:
        f = float(raw)
        if f != int(f):
            raise ValueError
        n = int(f)
    except (TypeError, ValueError):
        raise LeaderboardError(f"{label} must be a whole number.")
    if not lo <= n <= hi:
        raise LeaderboardError(f"{label} must be between {lo:,} and {hi:,}.")
    return n


# The monthly reward ladder: 250 winners share 100,000 ETB. Each band is {"from": rank, "to": rank, "amount": ETB each}.
DEFAULT_REWARD_TIERS = [
    {"from": 1, "to": 1, "amount": 20000}, {"from": 2, "to": 2, "amount": 10000}, {"from": 3, "to": 3, "amount": 5000},
    {"from": 4, "to": 10, "amount": 1000}, {"from": 11, "to": 50, "amount": 600}, {"from": 51, "to": 100, "amount": 400},
    {"from": 101, "to": 200, "amount": 100}, {"from": 201, "to": 250, "amount": 80},
]


def parse_tiers(raw) -> Optional[list]:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, list) and raw else None


def tiers_total(tiers: list) -> int:
    return sum((int(t["to"]) - int(t["from"]) + 1) * int(t["amount"]) for t in tiers)


def validate_tiers(raw, budget: int) -> list:
    """Bands must start at rank 1, follow each other without gaps or overlaps, and never add up to more than the budget."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 60:
        raise LeaderboardError("Add between 1 and 60 reward bands.")
    out, expected = [], 1
    for i, t in enumerate(raw, start=1):
        if not isinstance(t, dict):
            raise LeaderboardError(f"Reward band {i} is not valid.")
        lo = _whole(t, "from", f"Band {i} first rank", 1, 10_000)
        hi = _whole(t, "to", f"Band {i} last rank", 1, 10_000)
        amount = _whole(t, "amount", f"Band {i} reward", 0, 100_000_000)
        if lo != expected:
            raise LeaderboardError(f"Band {i} must start at rank {expected:,} - ranks cannot have gaps or overlap.")
        if hi < lo:
            raise LeaderboardError(f"Band {i}: the last rank must not be lower than the first rank.")
        out.append({"from": lo, "to": hi, "amount": amount})
        expected = hi + 1
    total = tiers_total(out)
    if total > budget:
        raise LeaderboardError(f"The bands add up to {total:,} ETB, which is more than the {budget:,} ETB budget.")
    return out


def tier_for_rank(tiers: Optional[list], rank: int) -> Optional[dict]:
    for t in tiers or []:
        if t["from"] <= rank <= t["to"]:
            return t
    return None


def split_ladder(tiers: list, standings: list[dict]) -> list[dict]:
    """Each ranked player gets the amount of their band. Ranks nobody reached simply pay nothing."""
    out = []
    for i, w in enumerate(standings, start=1):
        t = tier_for_rank(tiers, i)
        if t is None:
            break
        if int(t["amount"]) > 0:
            out.append({"rank": i, "player_id": w["player_id"], "points": int(w["points"]), "amount": int(t["amount"]),
                        "display_name": w.get("display_name")})
    return out


def wagering_summary(rewards: list[dict], multiplier: int, estimated: int = 0) -> dict:
    """Player-facing wagering tracker. Required = reward x multiplier; only stakes charged after the credit count."""
    live = [r for r in rewards if r["status"] == "locked"]
    rows = live or rewards
    required = sum(int(r["wager_required"]) for r in rows)
    completed = sum(min(int(r["wagered"]), int(r["wager_required"])) for r in rows)
    if not rows:
        projected = max(0, int(estimated)) * int(multiplier)
        return {"status": "none", "multiplier": multiplier, "required": projected, "completed": 0, "remaining": projected,
                "percent": 0.0, "bonus_won": 0, "projected": True}
    status = "in_progress" if live else "eligible"
    return {"status": status, "multiplier": multiplier, "required": required, "completed": completed,
            "remaining": max(0, required - completed), "percent": round(100.0 * completed / required, 2) if required else 100.0,
            "bonus_won": sum(int(r.get("bonus_won", 0)) for r in rows), "projected": False}



def validate_campaign(payload: dict, base: Optional[dict] = None) -> dict:
    """Normalise admin input (merged over `base` when editing) into the database values."""
    p = dict(payload or {})
    tz_name = str(p.get("timezone") or (base or {}).get("timezone") or DEFAULT_TIMEZONE).strip()
    get_tz(tz_name)
    name = str(p.get("name", (base or {}).get("name", "")) or "").strip()
    if not 1 <= len(name) <= 80:
        raise LeaderboardError("Give the campaign a name (up to 80 characters).")
    starts = parse_local(p["starts_at"], tz_name) if p.get("starts_at") else (base or {}).get("starts_at")
    ends = parse_local(p["ends_at"], tz_name) if p.get("ends_at") else (base or {}).get("ends_at")
    if starts is None or ends is None:
        raise LeaderboardError("Please set both the start and the end date and time.")
    if ends <= starts:
        raise LeaderboardError("The end must be after the start.")
    if ends - starts > timedelta(days=366):
        raise LeaderboardError("A campaign can run for at most one year.")
    merged = {**(base or {}), **p}
    enabled = merged.get("channel_enabled", False)
    if not isinstance(enabled, bool):
        raise LeaderboardError("channel_enabled must be true or false.")
    channel = normalize_channel(merged.get("channel_username", ""))
    if enabled and not _CHANNEL_RE.match(channel):
        raise LeaderboardError("Enter the channel's public username, for example @melbete_news (5-32 letters, numbers or _).")
    if channel and not _CHANNEL_RE.match(channel):
        raise LeaderboardError("That channel username is not valid.")
    budget = _whole(merged, "budget_etb", "Reward budget", 1, 100_000_000)
    tiers = parse_tiers(merged.get("reward_tiers"))
    if merged.get("reward_tiers") is not None and tiers is None:
        raise LeaderboardError("Add at least one reward band.")
    if tiers is not None:
        tiers = validate_tiers(tiers, budget)
        winners = tiers[-1]["to"]                      # the ladder decides how many players win
    else:
        winners = _whole(merged, "winners_count", "Number of winners", 1, 10_000)
    return {
        "name": name, "timezone": tz_name, "starts_at": starts, "ends_at": ends,
        "budget_etb": budget, "winners_count": winners, "reward_tiers": tiers,
        "points_per_cartela": _whole(merged, "points_per_cartela", "Points per cartela", 1, 1_000),
        "wagering_multiplier": _whole(merged, "wagering_multiplier", "Wagering requirement", 0, 1_000),
        "channel_enabled": enabled, "channel_username": channel,
        "channel_points": _whole(merged, "channel_points", "Channel reward points", 0, 10_000, 50),
        "cancelled": bool(merged.get("cancelled", False)),
    }


def split_rewards(budget: int, standings: list[dict], winners_count: int) -> list[dict]:
    """Top N players share the budget in proportion to their points (rounded down, so never over budget)."""
    winners = standings[:winners_count]
    total = sum(int(w["points"]) for w in winners)
    if not winners or total <= 0:
        return []
    return [
        {"rank": i + 1, "player_id": w["player_id"], "points": int(w["points"]), "amount": budget * int(w["points"]) // total,
         "display_name": w.get("display_name")}
        for i, w in enumerate(winners)
    ]


def mask_name(name: Optional[str]) -> str:
    name = (name or "Player").strip() or "Player"
    return f"{name[:2]}{'*' * min(4, max(1, len(name) - 2))}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# --------------------------------------------------------------------------------------------- service

class LeaderboardService:
    def __init__(self, repo: Optional[LeaderboardRepository] = None):
        self.repo = repo or LeaderboardRepository()

    # ------------------------------------------------------------------ admin: campaigns
    def _admin_json(self, c: dict, now: datetime) -> dict:
        out = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in c.items()}
        out["reward_tiers"] = parse_tiers(c.get("reward_tiers"))
        out["phase"] = campaign_phase(c, now)
        out["starts_local"] = to_local_text(c["starts_at"], c["timezone"])
        out["ends_local"] = to_local_text(c["ends_at"], c["timezone"])
        return out

    async def admin_list(self) -> list[dict]:
        now = utcnow()
        return [self._admin_json(c, now) for c in await self.repo.list_campaigns()]

    async def create(self, admin_id: str, payload: dict) -> dict:
        values = validate_campaign(payload)
        now = utcnow()
        if values["ends_at"] <= now:
            raise LeaderboardError("The end date is already in the past.")
        clash = await self.repo.overlapping(values["starts_at"], values["ends_at"])
        if clash:
            raise LeaderboardError(f"These dates overlap the campaign '{clash['name']}'. Campaigns cannot overlap.")
        values["cancelled"] = False
        c = await self.repo.create_campaign(values, admin_id)
        logger.warning("Admin %s created leaderboard campaign %s", admin_id, c["id"])
        return self._admin_json(c, now)

    async def update(self, admin_id: str, campaign_id: str, payload: dict) -> dict:
        c = await self._get(campaign_id)
        now = utcnow()
        phase = campaign_phase(c, now)
        if phase == "cancelled" or phase in ("ended", "distributed", "rejected"):
            raise LeaderboardError("A finished campaign can no longer be edited.")
        if phase == "active":
            # Once players are earning points the rules are fixed; only the end date, name and channel reward move.
            allowed = {"name", "ends_at", "timezone", "channel_enabled", "channel_username", "channel_points"}
            locked = [k for k in payload if k not in allowed and k not in ("starts_at",)]
            if locked or ("starts_at" in payload and parse_local(payload["starts_at"], payload.get("timezone") or c["timezone"]) != c["starts_at"]):
                raise LeaderboardError("The campaign has started, so only its name, end date and channel reward can change.")
            payload = {k: v for k, v in payload.items() if k in allowed}
        values = validate_campaign(payload, base=c)
        if phase == "active" and values["ends_at"] <= now:
            raise LeaderboardError("The new end must be in the future.")
        clash = await self.repo.overlapping(values["starts_at"], values["ends_at"], exclude_id=campaign_id)
        if clash:
            raise LeaderboardError(f"These dates overlap the campaign '{clash['name']}'.")
        values.pop("cancelled", None)
        updated = await self.repo.update_campaign(campaign_id, values)
        logger.warning("Admin %s updated leaderboard campaign %s", admin_id, campaign_id)
        return self._admin_json(updated, now)

    async def extend(self, admin_id: str, campaign_id: str, new_end) -> dict:
        """Move the end date later. Allowed while the campaign runs, and also after it ended as long as the admin has
        not decided the rewards yet (the campaign then simply runs again). Points are never awarded retroactively for
        the time it was closed."""
        c = await self._get(campaign_id)
        now = utcnow()
        phase = campaign_phase(c, now)
        if phase not in ("scheduled", "active", "ended"):
            raise LeaderboardError("This campaign is finished (cancelled or already decided), so its end date can't be changed.")
        ends = parse_local(new_end, c["timezone"])
        if ends <= c["ends_at"]:
            raise LeaderboardError("The new end must be later than the current end. To end earlier, edit the campaign.")
        if ends <= now:
            raise LeaderboardError("The new end must be in the future.")
        if ends - c["starts_at"] > timedelta(days=366):
            raise LeaderboardError("A campaign can run for at most one year.")
        clash = await self.repo.overlapping(c["ends_at"], ends, exclude_id=campaign_id)
        if clash:
            raise LeaderboardError(f"Extending would overlap the campaign '{clash['name']}'.")
        updated = await self.repo.update_campaign(campaign_id, {"ends_at": ends})
        logger.warning("Admin %s extended leaderboard campaign %s to %s", admin_id, campaign_id, ends.isoformat())
        return self._admin_json(updated, now)

    async def cancel(self, admin_id: str, campaign_id: str) -> dict:
        c = await self._get(campaign_id)
        now = utcnow()
        if campaign_phase(c, now) not in ("scheduled", "active"):
            raise LeaderboardError("Only a scheduled or running campaign can be cancelled.")
        updated = await self.repo.update_campaign(campaign_id, {"cancelled": True})
        logger.warning("Admin %s cancelled leaderboard campaign %s", admin_id, campaign_id)
        return self._admin_json(updated, now)

    async def _get(self, campaign_id: str) -> dict:
        c = await self.repo.get_campaign(campaign_id)
        if c is None:
            raise LeaderboardError("Campaign not found.", 404)
        return c

    async def admin_detail_brief(self, campaign_id: str) -> dict:
        return self._admin_json(await self._get(campaign_id), utcnow())

    async def admin_detail(self, campaign_id: str) -> dict:
        c = await self._get(campaign_id)
        now = utcnow()
        standings = await self.repo.standings(campaign_id, max(c["winners_count"], 50))
        tiers = parse_tiers(c.get("reward_tiers"))
        preview = split_ladder(tiers, standings) if tiers else split_rewards(c["budget_etb"], standings, c["winners_count"])
        rewards = await self.repo.list_rewards(campaign_id)
        return {
            "campaign": self._admin_json(c, now),
            "participants": await self.repo.participant_count(campaign_id),
            "standings": [{"rank": i + 1, "player_id": s["player_id"], "name": s["display_name"], "points": s["points"],
                           "cartela_points": s["cartela_points"], "channel_points": s["channel_points"],
                           "cartelas": s["cartelas"], "reserved_etb": s["reserved_etb"]} for i, s in enumerate(standings[:50])],
            "preview": [{"rank": r["rank"], "player_id": r["player_id"], "name": r["display_name"], "points": r["points"], "amount": r["amount"]}
                        for r in preview[:50]],
            "preview_total": sum(r["amount"] for r in preview),
            "rewards": [{"rank": r["rank"], "player_id": r["player_id"], "name": r["display_name"], "points": r["points"],
                         "amount": r["amount_etb"], "wagered": r["wagered"], "wager_required": r["wager_required"], "status": r["status"],
                         "percent": round(100.0 * min(r["wagered"], r["wager_required"]) / r["wager_required"], 2) if r["wager_required"] else 100.0,
                         "bonus_won": r.get("bonus_won", 0), "released": r["released_etb"]}
                        for r in rewards[:250]],
            "channel_claims": [{**{k: (_iso(v) if isinstance(v, datetime) else v) for k, v in row.items()}} for row in await self.repo.channel_claims(campaign_id)],
            "failed_attempts": [{**{k: (_iso(v) if isinstance(v, datetime) else v) for k, v in row.items()}} for row in await self.repo.list_attempts(campaign_id, 50)],
        }

    # ------------------------------------------------------------------ admin: distribution decision
    async def approve(self, admin_id: str, campaign_id: str, note: str = "") -> dict:
        c = await self._get(campaign_id)
        now = utcnow()
        phase = campaign_phase(c, now)
        if phase == "distributed":
            return await self._finish_credits(c, admin_id)      # safe re-run: pays only rows not yet credited
        if phase != "ended":
            raise LeaderboardError("Rewards can only be approved after the campaign has ended, once.")
        claimed = await self.repo.decide(campaign_id, "approved", admin_id, str(note or "")[:200], now)
        if claimed is None:
            raise LeaderboardError("This campaign was already decided.")
        standings = await self.repo.standings(campaign_id, claimed["winners_count"])
        tiers = parse_tiers(claimed.get("reward_tiers"))
        planned = split_ladder(tiers, standings) if tiers else split_rewards(claimed["budget_etb"], standings, claimed["winners_count"])
        for r in planned:
            if r["amount"] <= 0:
                continue
            await self.repo.insert_reward(campaign_id, r["player_id"], r["rank"], r["points"], r["amount"],
                                          r["amount"] * claimed["wagering_multiplier"])
        logger.warning("Admin %s approved leaderboard distribution for %s", admin_id, campaign_id)
        return await self._finish_credits(claimed, admin_id)

    async def _finish_credits(self, c: dict, admin_id: str) -> dict:
        from src.api.dependencies import get_wallet_service
        from src.services import player_notifications as pn

        wallets = get_wallet_service()
        paid = 0
        for r in await self.repo.uncredited_rewards(c["id"]):
            # mark_credited is the once-only guard: if crediting then fails we log loudly instead of risking a double payment
            if not await self.repo.mark_credited(r["id"]):
                continue
            try:
                wallet = await wallets.wallet_repo.apply_delta(r["player_id"], bonus=r["amount_etb"])
                await wallets.transaction_repo.create(Transaction(
                    player_id=r["player_id"], type="leaderboard_reward", amount=r["amount_etb"], balance_after=wallet.bonus_balance,
                    status="completed",
                    note=f"{c['name']} - rank {r['rank']} (bonus balance; wager {r['wager_required']} ETB to withdraw)",
                ))
                pn.schedule_notify(r["player_id"], "leaderboard_reward",
                                   {"amount": r["amount_etb"], "rank": r["rank"], "campaign": c["name"], "wager_required": r["wager_required"]},
                                   dedupe_key=f"leaderboard:{r['id']}")
                paid += r["amount_etb"]
            except Exception:  # noqa: BLE001
                logger.exception("LEADERBOARD REWARD NOT CREDITED reward=%s player=%s amount=%s - credit manually", r["id"], r["player_id"], r["amount_etb"])
        rows = await self.repo.list_rewards(c["id"])
        total = sum(r["amount_etb"] for r in rows if r["credited_at"])
        await self.repo.set_total_distributed(c["id"], total)
        return {"credited_now": paid, "total_distributed": total, "winners": len(rows)}

    async def reject(self, admin_id: str, campaign_id: str, note: str = "") -> dict:
        c = await self._get(campaign_id)
        now = utcnow()
        if campaign_phase(c, now) != "ended":
            raise LeaderboardError("Only an ended campaign that has not been decided can be rejected.")
        claimed = await self.repo.decide(campaign_id, "rejected", admin_id, str(note or "")[:200], now)
        if claimed is None:
            raise LeaderboardError("This campaign was already decided.")
        logger.warning("Admin %s rejected leaderboard distribution for %s", admin_id, campaign_id)
        return self._admin_json(claimed, now)

    async def revoke_points(self, admin_id: str, points_id: str) -> None:
        if await self.repo.revoke_points(points_id, admin_id) is None:
            raise LeaderboardError("That claim was not found or is already revoked.", 404)
        logger.warning("Admin %s revoked leaderboard points entry %s", admin_id, points_id)

    # ------------------------------------------------------------------ points from play
    async def on_cards_charged(self, cards: list, stake: int) -> None:
        """Called after a round starts and these cartelas were charged. Awards cartela points for reservations made
        inside the campaign window and advances wagering on any locked leaderboard reward. Never raises."""
        try:
            now = utcnow()
            campaign = await self.repo.get_active(now)
            owners = set()
            for card in cards:
                owners.add(card.owner_id)
                if campaign and campaign["starts_at"] <= card.created_at < campaign["ends_at"]:
                    await self.repo.add_points(campaign["id"], card.owner_id, "cartela", campaign["points_per_cartela"],
                                               f"cartela:{card.id}", f"cartela {card.cartela_number}", stake_etb=int(stake))
                await self.repo.add_wagered(card.owner_id, int(stake))
            for owner in owners:
                await self._release_due(owner)
        except Exception:  # noqa: BLE001 - leaderboard bookkeeping must never affect a round
            logger.exception("Leaderboard hook failed")

    async def _release_due(self, player_id: str) -> None:
        from src.api.dependencies import get_wallet_service

        wallets = get_wallet_service()
        for r in await self.repo.due_release(player_id):
            if await self.repo.claim_release(r["id"]) is None:
                continue
            try:
                wallet = await wallets.get_or_create_wallet(player_id)
                # The reward itself stays play-only bonus; only what bonus-funded play won while it was locked turns withdrawable.
                moved = min(int(wallet.bonus_balance), int(r.get("bonus_won") or 0))
                if moved > 0:
                    wallet = await wallets.wallet_repo.apply_delta(player_id, balance=moved, won=moved, bonus=-moved)
                    await wallets.transaction_repo.create(Transaction(
                        player_id=player_id, type="leaderboard_release", amount=moved, balance_after=wallet.balance, status="completed",
                        note=f"Wagering of {r['wager_required']} ETB complete - {moved} ETB won from bonus play is now withdrawable",
                    ))
                await self.repo.finish_release(r["id"], moved)
            except Exception:  # noqa: BLE001
                logger.exception("Could not release leaderboard reward %s; will retry on the next round", r["id"])
                await self.repo.undo_release(r["id"])

    # ------------------------------------------------------------------ players
    async def player_view(self, player_id: str) -> dict:
        now = utcnow()
        c = await self.repo.get_active(now) or await self.repo.get_upcoming(now)
        if c is None:
            latest = await self.repo.get_latest_ended(now)
            c = latest if latest and now - latest["ends_at"] < timedelta(days=30) else None
        reward_rows = await self.repo.player_rewards(player_id)
        rewards = [{"campaign": r["campaign_name"], "rank": r["rank"], "amount": r["amount_etb"], "wagered": r["wagered"],
                    "wager_required": r["wager_required"], "status": r["status"], "released": r["released_etb"],
                    "bonus_won": r.get("bonus_won", 0)}
                   for r in reward_rows]
        if c is None:
            return {"campaign": None, "now": _iso(now), "rewards": rewards,
                    "wagering": wagering_summary(reward_rows, 0) if reward_rows else None}
        phase = campaign_phase(c, now)
        me = await self.repo.player_standing(c["id"], player_id)
        top = await self.repo.standings(c["id"], 10) if phase != "scheduled" else []
        history = await self.repo.player_history(c["id"], player_id) if phase != "scheduled" else []
        claimed = any(h["source"] == "channel" for h in history)
        tiers = parse_tiers(c.get("reward_tiers"))
        estimated = int(tier_for_rank(tiers, me["rank"])["amount"]) if (me and tiers and tier_for_rank(tiers, me["rank"])) else 0
        next_target = await self._next_target(c, tiers, me) if (me and phase == "active") else None
        return {
            "now": _iso(now),
            "campaign": {
                "id": c["id"], "name": c["name"], "phase": phase, "timezone": c["timezone"],
                "starts_at": _iso(c["starts_at"]), "ends_at": _iso(c["ends_at"]),
                "budget_etb": c["budget_etb"], "winners_count": c["winners_count"], "points_per_cartela": c["points_per_cartela"],
                "wagering_multiplier": c["wagering_multiplier"],
                "reward_tiers": tiers,
                "channel": {"enabled": bool(c["channel_enabled"]), "username": c["channel_username"] if c["channel_enabled"] else "",
                            "points": c["channel_points"], "claimed": claimed},
            },
            "me": {"points": me["points"], "cartela_points": me["cartela_points"], "channel_points": me["channel_points"], "rank": me["rank"],
                   "cartelas": me["cartelas"], "reserved_etb": me["reserved_etb"], "estimated_reward": estimated,
                   "next_target": next_target, "winning_zone": me["rank"] <= c["winners_count"]}
                  if me else {"points": 0, "cartela_points": 0, "channel_points": 0, "rank": None, "cartelas": 0, "reserved_etb": 0,
                              "estimated_reward": 0, "next_target": None, "winning_zone": False},
            "wagering": wagering_summary(reward_rows, c["wagering_multiplier"], estimated),
            "top": [{"rank": i + 1, "name": mask_name(t["display_name"]), "points": t["points"], "is_me": t["player_id"] == player_id} for i, t in enumerate(top)],
            "history": [{"source": h["source"], "points": h["points"], "entries": h["entries"], "at": _iso(h["last_at"]), "revoked": h["revoked"]} for h in history],
            "rewards": rewards,
        }

    async def _next_target(self, c: dict, tiers: Optional[list], me: dict) -> Optional[dict]:
        """The nearest better reward band: how many points (and cartelas) separate the player from its last place."""
        rank = int(me["rank"])
        if rank <= 1:
            return None
        band = tier_for_rank(tiers, rank) if tiers else None
        target_rank = band["from"] - 1 if band else (c["winners_count"] if rank > c["winners_count"] else rank - 1)
        if target_rank < 1:
            return None
        target = await self.repo.standing_at_rank(c["id"], target_rank)
        if target is None:
            return None
        need = max(0, int(target["points"]) - int(me["points"]))
        per = max(1, int(c["points_per_cartela"]))
        better = tier_for_rank(tiers, target_rank) if tiers else None
        return {"rank": target_rank, "amount": int(better["amount"]) if better else None, "points_needed": need,
                "cartelas_needed": math.ceil(need / per), "tie": need == 0}

    async def claim_channel(self, player_id: str) -> dict:
        from src.repositories.player_repository import PlayerRepository
        from src.services.notifications import check_channel_membership

        now = utcnow()
        c = await self.repo.get_active(now)
        if c is None:
            raise LeaderboardError("There is no campaign running right now.")
        if not c["channel_enabled"] or not c["channel_username"] or c["channel_points"] <= 0:
            raise LeaderboardError("The channel reward is not available in this campaign.")
        player = await PlayerRepository().get_by_id(player_id)
        if player is None or not str(player.telegram_id).isdigit() or player.id != f"tg_{player.telegram_id}":
            raise LeaderboardError("Open the app from Telegram to claim this reward.")
        if player.is_banned or player.is_bot:
            raise LeaderboardError("This account cannot earn leaderboard points.")
        if await self.repo.recent_attempts(c["id"], player_id, now - timedelta(minutes=1)) >= ATTEMPTS_PER_MINUTE:
            raise LeaderboardError("Too many attempts. Please wait a minute and try again.", 429)
        key = f"channel:{c['id']}:{player_id}"
        status, detail = await check_channel_membership(c["channel_username"], player.telegram_id)
        if status == "unverifiable":
            await self.repo.record_attempt(c["id"], player_id, "unverifiable", detail)
            raise LeaderboardError("We can't verify channel membership right now, so no points were added. Please try again later.", 503)
        if status != "member":
            await self.repo.record_attempt(c["id"], player_id, "not_member", detail)
            raise LeaderboardError(f"We couldn't find you in @{c['channel_username']} yet. Join the channel, then tap verify again.")
        if not await self.repo.add_points(c["id"], player_id, "channel", c["channel_points"], key, f"joined @{c['channel_username']}"):
            await self.repo.record_attempt(c["id"], player_id, "duplicate", "already claimed")
            raise LeaderboardError("You have already claimed this reward in this campaign.", 409)
        await self.repo.record_attempt(c["id"], player_id, "credited", detail)
        return {"points": c["channel_points"], "channel": c["channel_username"]}


_service: Optional[LeaderboardService] = None


def get_leaderboard_service() -> LeaderboardService:
    global _service
    if _service is None:
        _service = LeaderboardService()
    return _service
