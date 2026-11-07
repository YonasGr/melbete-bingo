"""Agent (reseller) service - step 1: create agents, suspend/activate them, manage their float.

An agent's float is ETB the house has funded (cash or Telebirr, settled outside the app). It is only
ever changed by an admin through `adjust_float`, one atomic DB transaction with a ledger row, and it
can never go below zero. Player top-ups and commission build on this in later steps.
"""
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.models.schemas import Agent
from src.repositories.agent_repository import (
    AgentAlreadyExists,
    AgentNotFound,
    AgentRepository,
    AgentSuspended,
    CommissionWouldGoNegative,
    FloatWouldGoNegative,
)
from src.repositories.player_repository import PlayerRepository

logger = logging.getLogger(__name__)

MAX_COMMISSION_BPS = 3000  # 30% hard cap; lower it here if the business wants a tighter limit
MIN_TOPUP = 10  # ETB per player top-up
MAX_TOPUP = 50_000
MIN_CASHOUT = 10  # ETB of commission per cash-out
MAX_FLOAT_ADJUSTMENT = 1_000_000  # ETB per single adjustment - guards against a typo'd extra zero


MAX_APPLICATION_NOTE = 500
MIN_APPLICATION_NOTE = 10

_TG_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TG_INVITE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def normalize_telegram_address(raw: str) -> str:
    """Turn what an applicant typed (@name, t.me/name, https://t.me/name, t.me/+invite, t.me/joinchat/invite)
    into one canonical https://t.me/... link. Raises InvalidAgentInputError when it is not a Telegram address."""
    text = str(raw or "").strip()
    if not text:
        raise InvalidAgentInputError("Please enter your Telegram group or channel link")
    if len(text) > 200:
        raise InvalidAgentInputError("That Telegram link is too long")
    text = re.sub(r"^(https?://)?((www\.)?(t\.me|telegram\.me|telegram\.dog)/)?", "", text, flags=re.I)
    text = text.split("?")[0].split("#")[0].strip().strip("/")
    if text.startswith("@"):
        text = text[1:]
    if text.startswith("+"):
        code = text[1:]
        if _TG_INVITE.match(code):
            return f"https://t.me/+{code}"
    elif text.lower().startswith("joinchat/"):
        code = text.split("/", 1)[1].strip("/")
        if _TG_INVITE.match(code):
            return f"https://t.me/joinchat/{code}"
    elif "/" not in text and _TG_USERNAME.match(text):
        return f"https://t.me/{text}"
    raise InvalidAgentInputError("Enter a valid Telegram group or channel link, like https://t.me/yourchannel or @yourchannel")


DEFAULT_APPLICATION_COMMISSION_PERCENT = 3   # the admin can change it when approving


class AgentError(Exception):
    """Base class; `message` is safe to show to the admin."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AgentNotFoundError(AgentError):
    pass


class AgentAlreadyExistsError(AgentError):
    pass


class InvalidAgentInputError(AgentError):
    pass


class InsufficientFloatError(AgentError):
    pass


class AgentSuspendedError(AgentError):
    pass


class InsufficientCommissionError(AgentError):
    pass


def commission_percent_to_bps(percent) -> int:
    """12.5 -> 1250. Accepts int/float/str; rejects NaN, negatives and anything over the cap."""
    try:
        value = Decimal(str(percent))
    except (InvalidOperation, ValueError):
        raise InvalidAgentInputError("Commission must be a number")
    if not value.is_finite():
        raise InvalidAgentInputError("Commission must be a number")
    bps = int((value * 100).to_integral_value())
    if bps < 0 or bps > MAX_COMMISSION_BPS:
        raise InvalidAgentInputError(f"Commission must be between 0 and {MAX_COMMISSION_BPS / 100:g}%")
    return bps


class AgentService:
    def __init__(self, agent_repo: Optional[AgentRepository] = None, player_repo: Optional[PlayerRepository] = None):
        self.agents = agent_repo or AgentRepository()
        self.players = player_repo or PlayerRepository()

    async def create_agent(self, admin_id: str, telegram_id: str, commission_percent) -> Agent:
        telegram_id = str(telegram_id or "").strip()
        if not telegram_id.isdigit():
            raise InvalidAgentInputError("telegram_id must be the player's numeric Telegram ID")
        bps = commission_percent_to_bps(commission_percent)
        player = await self.players.get_by_telegram_id(telegram_id)
        if player is None:
            raise AgentNotFoundError("No player with that Telegram ID - they must open the bot once first")
        if getattr(player, "is_bot", False):
            raise InvalidAgentInputError("A bot account cannot be an agent")
        if getattr(player, "is_banned", False):
            raise InvalidAgentInputError("A banned player cannot be an agent")
        try:
            agent = await self.agents.create(player.id, bps, admin_id)
        except AgentAlreadyExists:
            raise AgentAlreadyExistsError("This player is already an agent")
        logger.warning("Admin %s created agent %s for player %s (%d bps)", admin_id, agent.id, player.id, bps)
        return agent

    async def update_commission(self, admin_id: str, agent_id: str, commission_percent) -> Agent:
        """Change the agent's BASE rate. The paid rate stays max(base, tier), so tiers still apply on top."""
        bps = commission_percent_to_bps(commission_percent)
        agent = await self.agents.set_commission(agent_id, bps)
        if agent is None:
            raise AgentNotFoundError("Agent not found")
        logger.warning("Admin %s set commission of agent %s to %d bps", admin_id, agent_id, bps)
        return agent

    async def set_rewards_enabled(self, admin_id: str, agent_id: str, enabled) -> Agent:
        """Per-agent switch for new-player / retention rewards (the commission on top-ups is unaffected)."""
        if not isinstance(enabled, bool):
            raise InvalidAgentInputError("enabled must be true or false")
        agent = await self.agents.set_rewards_enabled(agent_id, enabled)
        if agent is None:
            raise AgentNotFoundError("Agent not found")
        logger.warning("Admin %s set rewards_enabled=%s for agent %s", admin_id, enabled, agent_id)
        return agent

    # ------------------------------------------------------------------
    # Registration requests: a player applies, an admin approves (-> becomes an agent) or rejects
    # ------------------------------------------------------------------

    async def apply(self, player_id: str, channel_link: str = "", note: str = "") -> dict:
        channel_link = normalize_telegram_address(channel_link)
        note = str(note or "").strip()
        if len(note) < MIN_APPLICATION_NOTE:
            raise InvalidAgentInputError(f"Please describe your group or channel (at least {MIN_APPLICATION_NOTE} characters)")
        if len(note) > MAX_APPLICATION_NOTE:
            raise InvalidAgentInputError(f"Please keep the description under {MAX_APPLICATION_NOTE} characters")
        player = await self.players.get_by_id(player_id)
        if player is None:
            raise AgentNotFoundError("Player not found")
        if getattr(player, "is_bot", False) or getattr(player, "is_banned", False):
            raise InvalidAgentInputError("This account cannot become an agent")
        if await self.agents.get_by_player(player_id):
            raise AgentAlreadyExistsError("You are already an agent")
        app = await self.agents.create_application(player_id, note, channel_link)
        if app is None:
            raise InvalidAgentInputError("You already have a request waiting for review")
        logger.info("Player %s applied to become an agent", player_id)
        return app

    async def my_application(self, player_id: str) -> Optional[dict]:
        return await self.agents.latest_application(player_id)

    async def list_applications(self, status: Optional[str] = "pending") -> list[dict]:
        if status not in (None, "pending", "approved", "rejected"):
            raise InvalidAgentInputError("status must be pending, approved or rejected")
        return await self.agents.list_applications(status)

    async def review_application(self, admin_id: str, application_id: str, approve: bool,
                                 commission_percent=None, note: str = "") -> dict:
        existing = await self.agents.get_application(application_id)
        if existing is None:
            raise AgentNotFoundError("Request not found")
        note = str(note or "").strip()[:200]
        bps = None
        if approve:
            bps = commission_percent_to_bps(DEFAULT_APPLICATION_COMMISSION_PERCENT if commission_percent in (None, "") else commission_percent)
            player = await self.players.get_by_id(existing["player_id"])
            if player is None or getattr(player, "is_bot", False) or getattr(player, "is_banned", False):
                raise InvalidAgentInputError("This player can no longer become an agent")
        claimed = await self.agents.resolve_application(application_id, "approved" if approve else "rejected", admin_id, note)
        if claimed is None:
            raise InvalidAgentInputError("This request was already reviewed")
        agent = None
        if approve:
            try:
                agent = await self.agents.create(existing["player_id"], bps, admin_id)
            except AgentAlreadyExists:
                agent = await self.agents.get_by_player(existing["player_id"])
        try:
            from src.services import player_notifications as pn

            if approve:
                pn.schedule_notify(existing["player_id"], "agent_application_approved", {"percent": bps / 100},
                                   dedupe_key=f"agent_application:{application_id}")
            else:
                pn.schedule_notify(existing["player_id"], "agent_application_rejected", {"note": note},
                                   dedupe_key=f"agent_application:{application_id}")
        except Exception:  # noqa: BLE001 - a notification problem must not undo the decision
            logger.exception("Could not notify %s about their agent request", existing["player_id"])
        logger.warning("Admin %s %s agent request %s", admin_id, "approved" if approve else "rejected", application_id)
        return {"application": claimed, "agent": agent}

    async def list_agents(self) -> list[Agent]:
        return await self.agents.list_all()

    async def get_agent(self, agent_id: str) -> Agent:
        agent = await self.agents.get(agent_id)
        if agent is None:
            raise AgentNotFoundError("Agent not found")
        return agent

    async def set_status(self, admin_id: str, agent_id: str, status: str) -> Agent:
        if status not in ("active", "suspended"):
            raise InvalidAgentInputError("status must be 'active' or 'suspended'")
        agent = await self.agents.set_status(agent_id, status)
        if agent is None:
            raise AgentNotFoundError("Agent not found")
        logger.warning("Admin %s set agent %s to %s", admin_id, agent_id, status)
        return agent

    async def adjust_float(self, admin_id: str, agent_id: str, amount, note: str = "") -> Agent:
        """Positive = the house funds the agent; negative = take float back. Never below zero."""
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise InvalidAgentInputError("amount must be a whole number of ETB")
        if amount == 0 or abs(amount) > MAX_FLOAT_ADJUSTMENT:
            raise InvalidAgentInputError(f"amount must be non-zero and at most {MAX_FLOAT_ADJUSTMENT} ETB")
        try:
            agent = await self.agents.adjust_float_atomic(agent_id, amount, admin_id, (note or "")[:200])
        except AgentNotFound:
            raise AgentNotFoundError("Agent not found")
        except FloatWouldGoNegative:
            raise InsufficientFloatError("That would take the agent's float below zero")
        logger.warning("Admin %s adjusted float of agent %s by %+d (now %d)", admin_id, agent_id, amount, agent.float_balance)
        return agent

    # ------------------------------------------------------------------
    # Agent self-service (the agent acts as themselves, identified by their player id)
    # ------------------------------------------------------------------

    async def get_by_player(self, player_id: str) -> Agent:
        agent = await self.agents.get_by_player(player_id)
        if agent is None:
            raise AgentNotFoundError("You are not an agent")
        return agent

    async def topup_player(self, agent_player_id: str, target_telegram_id, amount) -> dict:
        """The agent pays `amount` ETB of their float into a player's wallet. The agent earns
        floor(amount * commission_bps / 10000) ETB commission, funded by the house."""
        if isinstance(amount, bool) or not isinstance(amount, int) or not (MIN_TOPUP <= amount <= MAX_TOPUP):
            raise InvalidAgentInputError(f"Top-up must be a whole number between {MIN_TOPUP} and {MAX_TOPUP} ETB")
        target_telegram_id = str(target_telegram_id or "").strip()
        if not target_telegram_id.isdigit():
            raise InvalidAgentInputError("Enter the player's numeric Telegram ID")
        agent = await self.get_by_player(agent_player_id)
        if agent.status != "active":
            raise AgentSuspendedError("Your agent account is suspended")
        player = await self.players.get_by_telegram_id(target_telegram_id)
        if player is None:
            raise AgentNotFoundError("No player with that Telegram ID - they must open the bot once first")
        if player.id == agent.player_id:
            raise InvalidAgentInputError("You cannot top up your own account")
        if getattr(player, "is_bot", False) or getattr(player, "is_banned", False):
            raise InvalidAgentInputError("That account cannot receive top-ups")
        commission = amount * max(agent.commission_bps, agent.tier_bps) // 10000  # tier can only raise the base rate
        try:
            result = await self.agents.topup_atomic(agent.id, player.id, amount, commission)
        except AgentNotFound:
            raise AgentNotFoundError("You are not an agent")
        except AgentSuspended:
            raise AgentSuspendedError("Your agent account is suspended")
        except FloatWouldGoNegative:
            raise InsufficientFloatError("Not enough float - ask the house to top up your float")
        logger.info("Agent %s topped up %s with %d ETB (commission %d)", agent.id, player.id, amount, commission)
        result["commission"] = commission
        result["player_name"] = player.display_name
        # Held new-player reward + tier refresh. Best-effort: never raises into the top-up.
        from src.services.agent_rewards import AgentRewardService

        await AgentRewardService(self.agents).on_player_deposit(player.id, amount, result["transaction_id"])
        from src.services import player_notifications as pn

        pn.schedule_notify(player.id, "agent_topup_received", {"amount": amount},
                           dedupe_key=f"agent_topup:{result['transaction_id']}")
        return result

    async def cashout_commission(self, agent_player_id: str, amount) -> dict:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < MIN_CASHOUT:
            raise InvalidAgentInputError(f"Cash-out must be a whole number of at least {MIN_CASHOUT} ETB")
        agent = await self.get_by_player(agent_player_id)
        try:
            result = await self.agents.cashout_commission_atomic(agent.id, amount)
        except AgentNotFound:
            raise AgentNotFoundError("You are not an agent")
        except AgentSuspended:
            raise AgentSuspendedError("Your agent account is suspended")
        except CommissionWouldGoNegative:
            raise InsufficientCommissionError("You do not have that much commission")
        return result

    async def dashboard(self, agent_player_id: str) -> dict:
        agent = await self.get_by_player(agent_player_id)
        return {
            "agent": agent,
            "player_count": await self.agents.count_players(agent.id),
            "ledger": await self.agents.list_ledger(agent.id, 50),
        }

    async def list_players(self, agent_player_id: str) -> list[dict]:
        agent = await self.get_by_player(agent_player_id)
        return await self.agents.list_players(agent.id)

    async def link_player_by_code(self, player_id: str, agent_code: str) -> bool:
        """Deep link /start ag_<CODE>: attach a player to the agent unless they already have one,
        are the agent themselves, or the agent is suspended. Never raises for bad codes."""
        agent = await self.agents.get_by_code(str(agent_code or "").strip().upper())
        if agent is None or agent.status != "active" or agent.player_id == player_id:
            return False
        return await self.agents.link_player(player_id, agent.id)

    async def ledger_for_admin(self, agent_id: str) -> list:
        await self.get_agent(agent_id)
        return await self.agents.list_ledger(agent_id, 200)
