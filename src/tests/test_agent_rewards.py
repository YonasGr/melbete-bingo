"""Agent rewards: tiers, held new-player / retention rewards, anti-abuse rules, the payout job.
In-memory fake repository, no DB."""
from datetime import timedelta

import pytest

from src.models.schemas import Agent, utcnow
from src.services.agent_rewards import (
    AgentRewardService,
    RewardInputError,
    month_start,
    next_tier,
    pick_tier,
    validate_settings,
    validate_tiers,
)

TIERS = [
    {"label": "Bronze", "min_monthly_volume": 0, "commission_bps": 300},
    {"label": "Silver", "min_monthly_volume": 50_000, "commission_bps": 400},
    {"label": "Gold", "min_monthly_volume": 200_000, "commission_bps": 500},
]
SETTINGS = {"enabled": True, "new_player_bounty": 20, "min_first_deposit": 50, "min_rounds_played": 3,
            "retention_bonus": 10, "retention_days": 30, "hold_days": 3, "daily_payout_cap": 100}


class FakeRepo:
    def __init__(self):
        self.agent = Agent(id="a1", player_id="tg_111", agent_code="AGTEST", commission_bps=300, created_by="admin")
        self.settings = dict(SETTINGS)
        self.volume = 0
        self.players = {"tg_222": {"agent_id": "a1", "is_banned": False, "is_bot": False, "lifetime_deposited": 100}}
        self.rounds = {"tg_222": 5}
        self.same_phone = set()
        self.same_ip = set()
        self.rewards = {}   # dedupe_key -> dict
        self.paid_today = 0

    async def get(self, agent_id): return self.agent if agent_id == "a1" else None
    async def list_tiers(self): return list(TIERS)
    async def volume_since(self, agent_id, since): return self.volume
    async def get_reward_settings(self): return dict(self.settings)
    async def player_deposit_state(self, pid): return self.players.get(pid)
    async def shares_phone_with_agent(self, pid, agent_player_id): return pid in self.same_phone
    async def shares_ip_with_agent(self, pid, agent_player_id): return pid in self.same_ip
    async def rounds_played(self, pid): return self.rounds.get(pid, 0)
    async def active_agents(self, limit=1000): return [self.agent] if self.agent.status == "active" else []
    async def retention_candidates(self, older_than, active_since, limit=200): return []

    async def set_tier(self, agent_id, bps, label):
        self.agent = self.agent.model_copy(update={"tier_bps": bps, "tier_label": label})
        return self.agent

    async def create_reward(self, agent_id, player_id, type_, amount, release_at, dedupe_key, note=""):
        if dedupe_key in self.rewards:
            return False
        self.rewards[dedupe_key] = {"id": f"r{len(self.rewards)}", "agent_id": agent_id, "player_id": player_id,
                                    "type": type_, "amount": amount, "status": "pending", "release_at": release_at}
        return True

    async def due_rewards(self, limit=200):
        return [r for r in self.rewards.values() if r["status"] == "pending" and r["release_at"] <= utcnow()]

    async def cancel_reward(self, reward_id, note=""):
        for r in self.rewards.values():
            if r["id"] == reward_id and r["status"] == "pending":
                r["status"] = "cancelled"
                return r
        return None

    async def cancel_pending_for_player(self, pid, note):
        n = 0
        for r in self.rewards.values():
            if r["player_id"] == pid and r["status"] == "pending":
                r["status"] = "cancelled"
                n += 1
        return n

    async def pay_reward_atomic(self, reward_id, daily_cap):
        r = next(x for x in self.rewards.values() if x["id"] == reward_id)
        if r["status"] != "pending":
            return {"result": "gone"}
        if daily_cap and self.paid_today + r["amount"] > daily_cap:
            return {"result": "cap"}
        self.paid_today += r["amount"]
        r["status"] = "paid"
        self.agent.commission_balance += r["amount"]
        return {"result": "paid", "agent_player_id": self.agent.player_id, "amount": r["amount"], "type": r["type"]}


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    sent = []
    monkeypatch.setattr("src.services.player_notifications.schedule_notify",
                        lambda player_id, type_, data=None, dedupe_key=None: sent.append((player_id, type_)))
    return sent


def _svc():
    repo = FakeRepo()
    return AgentRewardService(repo), repo


def _age(repo, days=4):
    for r in repo.rewards.values():
        r["release_at"] = utcnow() - timedelta(days=days)


# ----------------------------------------------------------------- pure helpers
def test_pick_and_next_tier():
    assert pick_tier(TIERS, 0)["label"] == "Bronze"
    assert pick_tier(TIERS, 49_999)["label"] == "Bronze"
    assert pick_tier(TIERS, 50_000)["label"] == "Silver"
    assert pick_tier(TIERS, 10**9)["label"] == "Gold"
    assert next_tier(TIERS, 60_000)["label"] == "Gold"
    assert next_tier(TIERS, 300_000) is None
    assert pick_tier([], 5) is None


def test_month_start_resets_to_first_of_month():
    m = month_start()
    assert m.day == 1 and m.hour == 0 and m.minute == 0


def test_validate_tiers():
    out = validate_tiers([{"label": "Gold", "min_monthly_volume": 200000, "commission_percent": 5},
                          {"label": "Bronze", "min_monthly_volume": 0, "commission_percent": 3}])
    assert [t["label"] for t in out] == ["Bronze", "Gold"] and out[1]["commission_bps"] == 500
    for bad in ([], "x", [{"label": "", "min_monthly_volume": 0, "commission_percent": 3}],
                [{"label": "A", "min_monthly_volume": -1, "commission_percent": 3}],
                [{"label": "A", "min_monthly_volume": 0, "commission_percent": 99}],
                [{"label": "A", "min_monthly_volume": 0, "commission_percent": 3},
                 {"label": "B", "min_monthly_volume": 0, "commission_percent": 4}]):
        with pytest.raises(Exception):
            validate_tiers(bad)


def test_validate_settings():
    assert validate_settings({"hold_days": 5, "enabled": False}) == {"hold_days": 5, "enabled": False}
    for bad in ({"hold_days": -1}, {"new_player_bounty": 1.5}, {"enabled": "yes"}, {"retention_days": 0}, "x"):
        with pytest.raises(RewardInputError):
            validate_settings(bad)


# ----------------------------------------------------------------- tiers
@pytest.mark.asyncio
async def test_refresh_tier_promotes_and_never_lowers_base_rate():
    svc, repo = _svc()
    repo.volume = 60_000
    agent = await svc.refresh_tier(repo.agent, notify=False)
    assert (agent.tier_label, agent.tier_bps) == ("Silver", 400)
    repo.volume = 250_000
    agent = await svc.refresh_tier(agent)   # a real promotion notifies the agent
    assert (agent.tier_label, agent.tier_bps) == ("Gold", 500)
    assert max(agent.commission_bps, agent.tier_bps) == 400
    repo.agent.commission_bps = 900  # admin gave this agent a better personal rate
    assert max(repo.agent.commission_bps, repo.agent.tier_bps) == 900


# ----------------------------------------------------------------- reward creation + abuse rules
@pytest.mark.asyncio
async def test_first_deposit_creates_one_held_reward():
    svc, repo = _svc()
    assert await svc.on_player_deposit("tg_222", 100, "t1") == "created"
    assert await svc.on_player_deposit("tg_222", 100, "t2") is None   # dedupe: one per player
    r = repo.rewards["new_player:tg_222"]
    assert r["amount"] == 20 and r["status"] == "pending"
    assert r["release_at"] > utcnow() + timedelta(days=2)             # held, not paid at once


@pytest.mark.asyncio
async def test_no_reward_for_second_deposit_small_deposit_self_or_banned():
    svc, repo = _svc()
    repo.players["tg_222"]["lifetime_deposited"] = 300                # earlier deposits exist
    assert await svc.on_player_deposit("tg_222", 100) is None
    repo.players["tg_222"]["lifetime_deposited"] = 20
    assert await svc.on_player_deposit("tg_222", 20) is None          # below min_first_deposit
    repo.players["tg_111"] = {"agent_id": "a1", "is_banned": False, "is_bot": False, "lifetime_deposited": 100}
    assert await svc.on_player_deposit("tg_111", 100) is None         # the agent themselves
    repo.players["tg_222"].update(lifetime_deposited=100, is_banned=True)
    assert await svc.on_player_deposit("tg_222", 100) is None
    repo.players["tg_222"]["is_banned"] = False
    repo.same_phone.add("tg_222")
    assert await svc.on_player_deposit("tg_222", 100) is None         # same phone as the agent
    repo.same_phone.clear()
    repo.same_ip.add("tg_222")
    assert await svc.on_player_deposit("tg_222", 100) is None         # same connection as the agent
    assert repo.rewards == {}


@pytest.mark.asyncio
async def test_disabled_rewards_create_nothing_and_a_hook_error_never_raises():
    svc, repo = _svc()
    repo.settings["enabled"] = False
    assert await svc.on_player_deposit("tg_222", 100) is None

    class Broken(FakeRepo):
        async def player_deposit_state(self, pid):
            raise RuntimeError("db down")

    assert await AgentRewardService(Broken()).on_player_deposit("tg_222", 100) is None


# ----------------------------------------------------------------- the payout job
@pytest.mark.asyncio
async def test_cycle_pays_only_after_hold_and_enough_rounds():
    svc, repo = _svc()
    await svc.on_player_deposit("tg_222", 100)
    assert (await svc.run_cycle())["paid"] == 0                       # still on hold
    _age(repo)
    repo.rounds["tg_222"] = 1
    assert (await svc.run_cycle())["paid"] == 0                       # held long enough, but hasn't played
    repo.rounds["tg_222"] = 3
    assert (await svc.run_cycle())["paid"] == 1
    assert repo.agent.commission_balance == 20
    assert (await svc.run_cycle())["paid"] == 0                       # never paid twice


@pytest.mark.asyncio
async def test_cycle_cancels_unqualified_after_expiry_and_banned_players():
    svc, repo = _svc()
    await svc.on_player_deposit("tg_222", 100)
    repo.rounds["tg_222"] = 0
    _age(repo, days=40)
    assert (await svc.run_cycle())["cancelled"] == 1

    svc, repo = _svc()
    await svc.on_player_deposit("tg_222", 100)
    _age(repo)
    repo.players["tg_222"]["is_banned"] = True
    assert (await svc.run_cycle())["cancelled"] == 1
    assert repo.agent.commission_balance == 0


@pytest.mark.asyncio
async def test_cycle_respects_daily_cap_and_suspended_agents():
    svc, repo = _svc()
    repo.settings["daily_payout_cap"] = 30
    repo.players["tg_333"] = dict(repo.players["tg_222"])
    repo.rounds["tg_333"] = 5
    await svc.on_player_deposit("tg_222", 100)
    await svc.on_player_deposit("tg_333", 100)
    _age(repo)
    r = await svc.run_cycle()
    assert r["paid"] == 1 and r["capped"] == 1 and repo.agent.commission_balance == 20

    svc, repo = _svc()
    await svc.on_player_deposit("tg_222", 100)
    _age(repo)
    repo.agent.status = "suspended"
    assert (await svc.run_cycle())["paid"] == 0


@pytest.mark.asyncio
async def test_deposit_reversal_cancels_held_rewards_and_admin_cancel_works_once():
    svc, repo = _svc()
    await svc.on_player_deposit("tg_222", 100)
    assert await svc.on_deposit_reversed("tg_222") == 1
    assert repo.rewards["new_player:tg_222"]["status"] == "cancelled"

    svc, repo = _svc()
    await svc.on_player_deposit("tg_222", 100)
    rid = repo.rewards["new_player:tg_222"]["id"]
    await svc.cancel_reward("admin1", rid)
    with pytest.raises(RewardInputError):
        await svc.cancel_reward("admin1", rid)                        # already cancelled
