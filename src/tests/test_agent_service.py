"""Agent service (step 1): creating agents, status, and float adjustments. In-memory fakes, no DB."""
import pytest

from src.models.schemas import Agent, Player
from src.repositories.agent_repository import (
    AgentAlreadyExists,
    AgentNotFound,
    AgentSuspended,
    CommissionWouldGoNegative,
    FloatWouldGoNegative,
)
from src.services.agent_service import (
    AgentAlreadyExistsError,
    AgentNotFoundError,
    AgentService,
    AgentSuspendedError,
    InsufficientCommissionError,
    InsufficientFloatError,
    InvalidAgentInputError,
    commission_percent_to_bps,
)


class FakeAgentRepo:
    def __init__(self):
        self.agents = {}
        self.ledger = []
        self.wallets = {}  # player_id -> {"balance": int, "won": int, "deposited": int}
        self.linked = {}  # player_id -> agent_id
        self.txns = []

    async def get_by_code(self, code):
        return next((a for a in self.agents.values() if a.agent_code == code), None)

    async def player_deposit_state(self, player_id):  # rewards hook: no DB in these tests
        return None

    async def link_player(self, player_id, agent_id):
        if player_id in self.linked:
            return False
        self.linked[player_id] = agent_id
        return True

    async def count_players(self, agent_id):
        return sum(1 for a in self.linked.values() if a == agent_id)

    async def list_players(self, agent_id, limit=100):
        return []

    async def list_ledger(self, agent_id, limit=100):
        return []

    async def topup_atomic(self, agent_id, target_player_id, amount, commission):
        agent = self.agents.get(agent_id)
        if agent is None:
            raise AgentNotFound(agent_id)
        if agent.status != "active":
            raise AgentSuspended(agent_id)
        if agent.float_balance < amount:
            raise FloatWouldGoNegative(agent_id)
        agent.float_balance -= amount
        agent.commission_balance += commission
        w = self.wallets.setdefault(target_player_id, {"balance": 0, "won": 0, "deposited": 0})
        w["balance"] += amount
        w["deposited"] += amount
        self.linked.setdefault(target_player_id, agent_id)
        self.txns.append(("agent_topup", target_player_id, amount))
        self.ledger.append((agent_id, -amount, agent.float_balance, "topup", commission))
        return {"agent": agent, "wallet_balance": w["balance"], "transaction_id": f"txn{len(self.txns)}"}

    async def cashout_commission_atomic(self, agent_id, amount):
        agent = self.agents.get(agent_id)
        if agent is None:
            raise AgentNotFound(agent_id)
        if agent.status != "active":
            raise AgentSuspended(agent_id)
        if agent.commission_balance < amount:
            raise CommissionWouldGoNegative(agent_id)
        agent.commission_balance -= amount
        w = self.wallets.setdefault(agent.player_id, {"balance": 0, "won": 0, "deposited": 0})
        w["balance"] += amount
        w["won"] += amount
        return {"agent": agent, "wallet_balance": w["balance"], "transaction_id": "cash"}

    async def get(self, agent_id):
        return self.agents.get(agent_id)

    async def get_by_player(self, player_id):
        return next((a for a in self.agents.values() if a.player_id == player_id), None)

    async def list_all(self, limit=200):
        return list(self.agents.values())

    async def create(self, player_id, commission_bps, created_by):
        if await self.get_by_player(player_id):
            raise AgentAlreadyExists(player_id)
        agent = Agent(player_id=player_id, agent_code=f"AG{len(self.agents):06d}",
                      commission_bps=commission_bps, created_by=created_by)
        self.agents[agent.id] = agent
        return agent

    async def set_status(self, agent_id, status):
        agent = self.agents.get(agent_id)
        if agent:
            agent.status = status
        return agent

    async def adjust_float_atomic(self, agent_id, delta, admin_id, note=""):
        agent = self.agents.get(agent_id)
        if agent is None:
            raise AgentNotFound(agent_id)
        if agent.float_balance + delta < 0:
            raise FloatWouldGoNegative(agent_id)
        agent.float_balance += delta
        self.ledger.append((agent_id, delta, agent.float_balance, admin_id, note))
        return agent


class FakePlayerRepo:
    def __init__(self, players):
        self.players = {p.telegram_id: p for p in players}

    async def get_by_telegram_id(self, telegram_id):
        return self.players.get(telegram_id)


@pytest.fixture
def repo():
    return FakeAgentRepo()


@pytest.fixture
def service(repo):
    players = [
        Player(id="tg_111", telegram_id="111", display_name="Abebe"),
        Player(id="tg_222", telegram_id="222", display_name="Banned", is_banned=True),
        Player(id="tg_333", telegram_id="333", display_name="Bot", is_bot=True),
    ]
    return AgentService(agent_repo=repo, player_repo=FakePlayerRepo(players))


def test_commission_percent_conversion_and_limits():
    assert commission_percent_to_bps(5) == 500
    assert commission_percent_to_bps("12.5") == 1250
    assert commission_percent_to_bps(0) == 0
    for bad in (-1, 30.01, "abc", float("nan"), None):
        with pytest.raises(InvalidAgentInputError):
            commission_percent_to_bps(bad)


@pytest.mark.asyncio
async def test_create_agent_for_a_registered_player(service):
    agent = await service.create_agent("admin1", "111", 5)
    assert agent.player_id == "tg_111" and agent.commission_bps == 500
    assert agent.status == "active" and agent.float_balance == 0 and agent.commission_balance == 0


@pytest.mark.asyncio
async def test_create_agent_rejects_unknown_banned_bot_duplicate_and_bad_ids(service):
    with pytest.raises(AgentNotFoundError):
        await service.create_agent("admin1", "999", 5)
    with pytest.raises(InvalidAgentInputError):
        await service.create_agent("admin1", "222", 5)
    with pytest.raises(InvalidAgentInputError):
        await service.create_agent("admin1", "333", 5)
    with pytest.raises(InvalidAgentInputError):
        await service.create_agent("admin1", "not-a-number", 5)
    await service.create_agent("admin1", "111", 5)
    with pytest.raises(AgentAlreadyExistsError):
        await service.create_agent("admin1", "111", 5)


@pytest.mark.asyncio
async def test_suspend_and_activate(service):
    agent = await service.create_agent("admin1", "111", 5)
    assert (await service.set_status("admin1", agent.id, "suspended")).status == "suspended"
    assert (await service.set_status("admin1", agent.id, "active")).status == "active"
    with pytest.raises(InvalidAgentInputError):
        await service.set_status("admin1", agent.id, "deleted")
    with pytest.raises(AgentNotFoundError):
        await service.set_status("admin1", "missing", "active")


@pytest.mark.asyncio
async def test_float_add_remove_and_never_below_zero(service, repo):
    agent = await service.create_agent("admin1", "111", 5)
    assert (await service.adjust_float("admin1", agent.id, 5000, "Telebirr ref 1")).float_balance == 5000
    assert (await service.adjust_float("admin1", agent.id, -1200)).float_balance == 3800
    with pytest.raises(InsufficientFloatError):
        await service.adjust_float("admin1", agent.id, -3801)
    assert repo.agents[agent.id].float_balance == 3800  # the rejected adjustment changed nothing
    assert [row[1] for row in repo.ledger] == [5000, -1200]  # and left no ledger row


@pytest.mark.asyncio
async def test_float_adjustment_input_validation(service):
    agent = await service.create_agent("admin1", "111", 5)
    for bad in (0, 1.5, "100", None, True, 1_000_001, -1_000_001):
        with pytest.raises(InvalidAgentInputError):
            await service.adjust_float("admin1", agent.id, bad)
    with pytest.raises(AgentNotFoundError):
        await service.adjust_float("admin1", "missing", 100)


async def _funded_agent(service, float_amount=1000, percent=5):
    agent = await service.create_agent("admin1", "111", percent)
    await service.adjust_float("admin1", agent.id, float_amount)
    return agent


@pytest.fixture
def service_with_players(repo):
    players = [
        Player(id="tg_111", telegram_id="111", display_name="Abebe"),
        Player(id="tg_444", telegram_id="444", display_name="Kebede"),
        Player(id="tg_555", telegram_id="555", display_name="Banned", is_banned=True),
        Player(id="tg_666", telegram_id="666", display_name="Bot", is_bot=True),
    ]
    return AgentService(agent_repo=repo, player_repo=FakePlayerRepo(players))


@pytest.mark.asyncio
async def test_topup_moves_float_to_player_and_pays_commission(service_with_players, repo):
    svc = service_with_players
    agent = await _funded_agent(svc, 1000, 5)
    r = await svc.topup_player("tg_111", "444", 200)
    assert r["commission"] == 10  # 5% of 200, funded by the house
    a = repo.agents[agent.id]
    assert (a.float_balance, a.commission_balance) == (800, 10)
    assert repo.wallets["tg_444"] == {"balance": 200, "won": 0, "deposited": 200}  # playable, not withdrawable
    assert repo.linked["tg_444"] == agent.id


@pytest.mark.asyncio
async def test_commission_rounds_down_never_up(service_with_players, repo):
    svc = service_with_players
    agent = await _funded_agent(svc, 1000, 3)
    r = await svc.topup_player("tg_111", "444", 99)  # 2.97 -> 2
    assert r["commission"] == 2


@pytest.mark.asyncio
async def test_topup_cannot_exceed_float_and_changes_nothing(service_with_players, repo):
    svc = service_with_players
    agent = await _funded_agent(svc, 100, 5)
    with pytest.raises(InsufficientFloatError):
        await svc.topup_player("tg_111", "444", 101)
    assert repo.agents[agent.id].float_balance == 100
    assert "tg_444" not in repo.wallets


@pytest.mark.asyncio
async def test_topup_validation_and_blocked_targets(service_with_players):
    svc = service_with_players
    await _funded_agent(svc)
    for bad in (0, 9, 50_001, 12.5, "100", None, True):
        with pytest.raises(InvalidAgentInputError):
            await svc.topup_player("tg_111", "444", bad)
    with pytest.raises(InvalidAgentInputError):
        await svc.topup_player("tg_111", "abc", 100)
    with pytest.raises(AgentNotFoundError):
        await svc.topup_player("tg_111", "999", 100)  # unknown player
    for tid in ("111", "555", "666"):  # self, banned, bot
        with pytest.raises(InvalidAgentInputError):
            await svc.topup_player("tg_111", tid, 100)


@pytest.mark.asyncio
async def test_only_active_agents_can_top_up_or_cash_out(service_with_players, repo):
    svc = service_with_players
    agent = await _funded_agent(svc)
    await svc.topup_player("tg_111", "444", 100)
    await svc.set_status("admin1", agent.id, "suspended")
    with pytest.raises(AgentSuspendedError):
        await svc.topup_player("tg_111", "444", 100)
    with pytest.raises(AgentSuspendedError):
        await svc.cashout_commission("tg_111", 10)
    with pytest.raises(AgentNotFoundError):  # a normal player is not an agent
        await svc.topup_player("tg_444", "111", 100)


@pytest.mark.asyncio
async def test_commission_cashout_becomes_withdrawable_and_cannot_overdraw(service_with_players, repo):
    svc = service_with_players
    agent = await _funded_agent(svc, 10_000, 10)
    await svc.topup_player("tg_111", "444", 1000)  # +100 commission
    with pytest.raises(InsufficientCommissionError):
        await svc.cashout_commission("tg_111", 101)
    with pytest.raises(InvalidAgentInputError):
        await svc.cashout_commission("tg_111", 5)
    r = await svc.cashout_commission("tg_111", 60)
    assert repo.agents[agent.id].commission_balance == 40
    assert repo.wallets["tg_111"] == {"balance": 60, "won": 60, "deposited": 0}  # withdrawable via normal flow
    assert r["wallet_balance"] == 60


@pytest.mark.asyncio
async def test_deep_link_attaches_player_once_and_ignores_bad_codes(service_with_players, repo):
    svc = service_with_players
    agent = await _funded_agent(svc)
    assert await svc.link_player_by_code("tg_444", agent.agent_code.lower()) is True
    assert await svc.link_player_by_code("tg_444", agent.agent_code) is False  # already linked
    assert await svc.link_player_by_code("tg_555", "NOPE") is False
    assert await svc.link_player_by_code("tg_111", agent.agent_code) is False  # the agent themselves
    await svc.set_status("admin1", agent.id, "suspended")
    assert await svc.link_player_by_code("tg_666", agent.agent_code) is False


class FakeConn:
    """Scripted stand-in for an asyncpg connection, enough for credit_referral_commission_in_tx."""

    def __init__(self, agent_row):
        self.agent_row = agent_row
        self.executed = []

    async def fetchrow(self, sql, *args):
        if sql.lstrip().startswith("SELECT a.*"):
            return self.agent_row
        if sql.lstrip().startswith("UPDATE agents"):
            earned = args[1]
            self.agent_row = {**self.agent_row, "commission_balance": self.agent_row["commission_balance"] + earned}
            return self.agent_row
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.executed.append(args)


def _agent_row(bps, balance=0):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {"id": "a1", "player_id": "tg_111", "agent_code": "AGX", "status": "active", "commission_bps": bps,
            "float_balance": 700, "commission_balance": balance, "created_by": "admin1",
            "created_at": now, "updated_at": now}


@pytest.mark.asyncio
async def test_referral_commission_on_a_referred_players_deposit():
    from src.repositories.agent_repository import AgentRepository

    conn = FakeConn(_agent_row(500))  # 5%
    earned = await AgentRepository().credit_referral_commission_in_tx(conn, "tg_444", "dep1", 1000)
    assert earned == {"agent_player_id": "tg_111", "earned": 50}
    assert conn.agent_row["commission_balance"] == 50
    ledger = conn.executed[0]
    assert ledger[2] == "referral_commission" and ledger[5] == 50 and ledger[7] == "tg_444"  # type, delta, player


@pytest.mark.asyncio
async def test_referral_commission_rounds_down_and_skips_zero_and_unreferred():
    from src.repositories.agent_repository import AgentRepository

    assert await AgentRepository().credit_referral_commission_in_tx(FakeConn(_agent_row(300)), "tg_444", "d", 99) == {"agent_player_id": "tg_111", "earned": 2}
    assert await AgentRepository().credit_referral_commission_in_tx(FakeConn(_agent_row(300)), "tg_444", "d", 20) is None  # 0.6 -> 0
    assert await AgentRepository().credit_referral_commission_in_tx(FakeConn(None), "tg_555", "d", 1000) is None  # no active agent
