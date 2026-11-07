"""
Data schemas for Melbete Bingo.

These are plain Pydantic models (not an ORM). Firestore is schemaless, so
these classes exist purely to give the rest of the codebase typed,
validated objects to work with; `src/repositories` is responsible for
converting to/from the dictionaries Firestore actually stores.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Player(BaseModel):
    """A registered Telegram user."""

    id: str = Field(default_factory=new_id)
    telegram_id: str
    display_name: str  # Telegram name - used everywhere, incl. after registration
    avatar_url: Optional[str] = None
    phone_normalized: Optional[str] = None  # last 9 digits of phone_number; unique per real account
    phone_number: Optional[str] = None  # from Telegram's shared-contact button
    # Full/real name used to reconcile payment screenshots. NOT collected at
    # registration (that used to cause drop-off) - it's asked for the first
    # time the player deposits or withdraws, via the wallet flows.
    payment_name: Optional[str] = None
    registration_state: str = "pending_contact"  # pending_contact | complete - sharing
    # contact via the Telegram button is now the only step needed to finish registering.
    # Saved withdrawal destinations, keyed by payment method id, e.g.
    # {"telebirr": "0912345678", "cbe": "1000123456789"}. Players must set
    # these once before they can request a withdrawal via that method.
    withdrawal_accounts: Dict[str, str] = Field(default_factory=dict)
    # When on, the Mini App submits the bingo claim for the player the
    # instant their card completes a valid pattern, without waiting for a
    # manual tap. Purely a client convenience flag, mirrored server-side so
    # it persists across sessions/devices.
    auto_bingo_enabled: bool = False
    agent_id: Optional[str] = None  # the reseller agent this player belongs to, if any
    promo_opt_out: bool = False  # True = no promotional messages (nudges, win-back, streak reminders)
    referred_by: Optional[str] = None  # telegram_id of the player who invited them
    # Number of referrals that have actually EARNED this player a bonus so
    # far (capped by SystemSettings.max_referrals_per_user - see
    # ReferralService). The full "who invited who" audit trail, including
    # any over-cap/unpaid referrals, lives in the Referral collection.
    referral_count: int = 0
    # Set by a super admin (e.g. via the admin panel) after repeated fraud -
    # most commonly fake/doctored deposit screenshots or claimed amounts
    # that don't match what was actually sent. A banned player is blocked
    # from the bot and the Mini App entirely (see require_admin/_require_
    # player_identity) until unbanned.
    is_banned: bool = False
    ban_reason: Optional[str] = None
    # Frozen = money movement (deposit / withdraw) is blocked, but the player can still sign in.
    is_frozen: bool = False
    frozen_reason: Optional[str] = None
    # Personal per-24h limits in ETB; None = use the platform default from the Control Centre.
    daily_deposit_limit: Optional[int] = None
    daily_withdrawal_limit: Optional[int] = None
    is_bot: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_registered(self) -> bool:
        return self.registration_state == "complete"


class GameRoom(BaseModel):
    """A Bingo session in the single active real-money room (real_10). At most one room per TRACK may be active (state in
    {waiting, running}) at any time - the 3 tracks run independently and
    concurrently, but within a track it's still one room at a time. See
    `RoomService`."""

    id: str = Field(default_factory=new_id)
    # Active lobby track: "real_10" (10 ETB real-money room).
    # Determines which wallet balance stakes are drawn from (real balance
    # for the real_* tracks, bonus_balance for "bonus") - see
    # WalletService.debit_for_stake's funding_mode. Real and bonus money
    # never share a room, so they can never share a prize pool either.
    track: str = "real_10"
    host_id: Optional[str] = None  # None: system/bot-owned global session
    variant: str = "75"  # only "75" is offered to hosts; kept for the pattern engine
    number_range_min: int = 1
    number_range_max: int = 75
    cards_per_player: int = 4
    pattern: Dict[str, Any] = Field(default_factory=dict)
    # waiting -> running -> ended. There is deliberately no separate
    # "finishing"/"verifying" state anymore: a claim is verified, paid out,
    # and recorded as a single atomic operation (see
    # RoomService.claim_win), so the round can only ever be in exactly one
    # of these three states - there's no in-between window for a client to
    # catch a room in and mishandle.
    state: str = "waiting"
    called_numbers: List[int] = Field(default_factory=list)
    draw_pool: List[int] = Field(default_factory=list)
    # One entry per winner (normally just one - the first valid claim wins
    # the round and ends it). Empty if the round ended with no winner
    # (pool exhausted / inactivity timeout) - see `end_reason`.
    winners: List[Dict[str, Any]] = Field(default_factory=list)
    # Why an "ended" round ended - purely informational/for transparency
    # (e.g. shown in the winner/round-summary screen and admin reports).
    end_reason: Optional[str] = None  # "winner" | "no_winner_pool_exhausted" | "no_winner_inactivity" | None
    # Deprecated, no longer written - kept only so older Firestore
    # documents from before this field was retired still deserialize.
    pending_winner: Optional[Dict[str, Any]] = None
    # Once an "ended" round has been explicitly acknowledged (a player
    # clicked "Go to Lobby" - see RoomService.advance_round), this flips to
    # True and the NEXT call to get_or_create_waiting_session() starts a
    # fresh round. Until then, this exact room keeps being "the" session
    # for anyone who polls or reloads, so nobody's celebration screen is
    # ever silently swapped out for an empty lobby.
    advanced: bool = False
    draw_interval: int = 2
    auto_draw: bool = True
    stake_amount: int = 10  # ETB cost to join, deducted from wallet on cartela selection
    prize_pool: int = 0  # sum of stakes collected so far this round
    # How much of `prize_pool` actually came from real (deposited) money
    # vs bonus credit - set once, from every charged card's funded_by, the
    # instant the round starts. This is what any payout must be split
    # against, NOT the winning card's own funding: the whole pot's money
    # is real money's money and bonus credit's money mixed together, and
    # a payout can only ever contain as much real, withdrawable money as
    # actually went into the pot - regardless of whose card happens to
    # win it.
    prize_pool_real: int = 0
    prize_pool_bonus: int = 0
    taken_cartelas: List[int] = Field(default_factory=list)  # cartela numbers already claimed this round
    # Admin-managed threshold. The admin service rejects values below two;
    # this model default keeps new deployments safe.
    min_players_to_start: int = 2  # legacy, no longer used to start a round
    # A round starts (30s countdown, then charge + draw) once this many
    # CARTELAS are reserved - even if they all belong to one player.
    min_cartelas_to_start: int = 2
    countdown_seconds: int = 30  # time players get to pick cartelas before auto-start
    # Cartela number range players can pick from.
    cartela_min: int = 1
    cartela_max: int = 500
    # Wall-clock time the countdown began (first cartela reserved this
    # round). Lets the server correctly resume a countdown - or an
    # in-progress draw loop - after a restart, instead of leaving the round
    # stuck forever because the in-memory background task died with the
    # old process.
    countdown_started_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def room_code(self) -> str:
        """Short, human-shareable code derived from the room id."""
        return self.id[:8].upper()


class Card(BaseModel):
    """A single bingo card (cartela) belonging to a player within a session."""

    id: str = Field(default_factory=new_id)
    room_id: str
    owner_id: str
    variant: str
    cartela_number: int  # 1-100, chosen by the player; determines the layout
    grid: List[List[Dict[str, Any]]]
    # Set to "real", "bonus", or a recorded mixed split only when the
    # round charges the stake.  Until then it is deliberately unfunded, so a
    # malformed/legacy card cannot default to a withdrawable payout.
    funded_by: str = "unfunded"
    created_at: datetime = Field(default_factory=utcnow)


class Wallet(BaseModel):
    """A player's play-money balance. Real money never touches the bot -
    admins reconcile top-ups/withdrawals manually against this ledger."""

    id: str = Field(default_factory=new_id)
    player_id: str
    balance: int = 0  # ETB, integer units - all real money (deposited + won combined)
    # Subset of `balance` that came from game winnings and is therefore
    # withdrawable. Deposited money itself is playable but NOT
    # withdrawable - only winnings are (see WalletService for the
    # enforcement). Invariant: 0 <= won_balance <= balance.
    won_balance: int = 0
    # Promotional balance (registration bonus, etc). Playable just like real
    # balance, but NOT withdrawable - this is disclosed to the player in the
    # UI, not hidden. Keeps the bonus program sustainable without needing to
    # secretly manipulate any player's odds of winning, which this app never
    # does: every draw is the same fair, verifiable RNG for everyone.
    bonus_balance: int = 0
    # Monotonically-increasing lifetime total of approved deposits, for
    # profile-display purposes only (never decreases, never used in any
    # withdrawal/spend calculation).
    lifetime_deposited: int = 0
    free_credit_granted: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Transaction(BaseModel):
    """An audit-logged change to a wallet's balance."""

    id: str = Field(default_factory=new_id)
    player_id: str
    type: str  # top_up_pending | top_up_approved | top_up_rejected | free_credit |
    # game_stake | win_payout | stake_refund | admin_adjustment | withdrawal_pending |
    # withdrawal_approved | withdrawal_rejected
    amount: int  # positive = credit, negative = debit
    balance_after: Optional[int] = None
    status: str = "completed"  # pending | completed | rejected
    payment_method: Optional[str] = None  # payment method id (e.g. "telebirr", "cbe")
    screenshot_file_id: Optional[str] = None  # Telegram file_id (bot-submitted deposits)
    screenshot_url: Optional[str] = None  # served URL (Mini-App-submitted deposits)
    room_id: Optional[str] = None
    admin_id: Optional[str] = None
    note: Optional[str] = None
    # Deposit-specific SMS text and extracted verification URL for duplicate protection.
    sms_text: Optional[str] = None
    payment_verification_url: Optional[str] = None
    # Withdrawal-specific fields (unused for deposits/game transactions).
    legal_name: Optional[str] = None
    withdrawal_address: Optional[str] = None  # phone/account number payout was sent to
    # Where the admin-alert message for this transaction was sent, so it
    # can be edited (buttons removed, resolution shown) no matter whether
    # the transaction actually gets resolved from the bot/group or the web
    # admin panel - both stay in sync on the exact same message.
    notification_chat_id: Optional[str] = None
    notification_message_id: Optional[int] = None
    notification_is_photo: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: Optional[datetime] = None


class PaymentMethod(BaseModel):
    """An admin-managed deposit/withdrawal channel (e.g. Telebirr, CBE Birr).
    Fully editable at runtime by admins - no redeploy needed to change an
    account number or turn a method on/off."""

    id: str = Field(default_factory=new_id)
    key: str  # stable machine key, e.g. "telebirr", "cbe"
    name: str  # display name, e.g. "Telebirr"
    account_info: str  # the number/handle players send money to, e.g. "0909823587"
    account_holder: Optional[str] = None  # name the account is registered under
    instructions: Optional[str] = None  # any extra instructions shown to players
    active: bool = True  # inactive methods are hidden from players
    supports_deposit: bool = True
    supports_withdrawal: bool = True
    display_order: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AdminUser(BaseModel):
    """A Telegram user granted admin access. The super admin (bootstrapped
    from the SUPER_ADMIN_TELEGRAM_ID env var) can add/remove other admins;
    regular admins can approve deposits/withdrawals but cannot manage other
    admins."""

    id: str = Field(default_factory=new_id)
    telegram_id: str
    is_super_admin: bool = False
    added_by: Optional[str] = None  # telegram_id of the admin who added this one
    # Staff role preset (super | manager | finance | support | admin) plus optional per-admin
    # extra permissions - see src/services/control_logic.py. "admin" = the pre-roles behaviour.
    role: str = "admin"
    extra_permissions: List[str] = Field(default_factory=list)
    # Telegram alert types this admin chose to receive; None = the role's defaults.
    alert_types: Optional[List[str]] = None
    created_at: datetime = Field(default_factory=utcnow)


class SystemSettings(BaseModel):
    """Singleton document (id is always "global") holding all the runtime-
    tunable knobs a super admin can change without redeploying."""

    id: str = "global"
    stake_amount: int = 10
    free_credit_amount: int = 50
    lobby_countdown_seconds: int = 30
    # Seconds between each ball being drawn once a round starts. This is
    # the SINGLE source of truth for draw pacing - the bot, the Mini App,
    # and every new room all derive it from here (via SettingsRepository),
    # never from a hardcoded constant.
    draw_interval: int = 2
    min_deposit: int = 50
    max_deposit: int = 70000
    min_withdrawal: int = 200
    max_withdrawal: int = 10000
    payout_ratio: float = 0.8  # fraction of the prize pool paid to the winner
    referral_bonus: int = 20  # ETB credited to the REFERRER's bonus balance per successful referral
    max_referrals_per_user: int = 3  # bonus-earning cap; further referrals still tracked, just unpaid
    # Max cartelas a single player may hold in one round (admin-tunable).
    # Passed to every new GameRoom on creation so the frontend constraint
    # is authoritative and there is no separate hardcoded constant.
    cards_per_player: int = 4
    # Minimum players needed for a round to auto-start. Default 5 ensures
    # a lively game; set to 1 for testing via the admin panel.
    min_players_to_start: int = 5  # legacy, no longer used to start a round
    # Cartelas (not players) needed before the lobby countdown begins.
    min_cartelas_to_start: int = 2
    # Cartela number range players can pick from (1-500 by default).
    # Admin-tunable so the game can be shrunk or expanded without redeploy.
    cartela_min: int = 1
    cartela_max: int = 500
    # Currently unused. Base rule as of this version: a round only ever
    # ends because a player won it - never on a timer, regardless of how
    # long it's been idle. Field kept (rather than removed) purely so
    # existing Firestore documents and the admin settings form don't need
    # a migration; the draw loop no longer reads it.
    game_inactivity_timeout_minutes: int = 0
    support_contact: str = "@melbete_support_1"
    bot_system_enabled: bool = False  # legacy/global fallback, superseded by per-track fields below
    deposits_enabled: bool = True
    max_bots_to_join: int = 10  # legacy/global fallback, superseded by per-track fields below

    # ------------------------------------------------------------------
    # Per-track settings: the lobby offers 3 independent, concurrently
    # running rooms - a 10 birr real-money room, a 50 birr real-money
    # room, and a bonus-balance-only room. Each is tuned and toggled
    # independently in the admin panel.
    # ------------------------------------------------------------------
    stake_amount_real_10: int = 10  # kept in sync with legacy `stake_amount`
    stake_amount_real_50: int = 50
    stake_amount_bonus: int = 10
    bot_enabled_real_10: bool = False
    bot_enabled_real_50: bool = False
    bot_enabled_bonus: bool = False
    # Per-room payout (winner share). The switch decides whether the room uses its own ratio or the shared payout_ratio.
    payout_custom_real_10: bool = False
    payout_custom_real_50: bool = False
    payout_custom_bonus: bool = False
    payout_ratio_real_10: float = 0.8
    payout_ratio_real_50: float = 0.8
    payout_ratio_bonus: float = 0.8
    max_bots_real_10: int = 10
    max_bots_real_50: int = 10
    max_bots_bonus: int = 10
    updated_at: datetime = Field(default_factory=utcnow)


class BotPlayer(BaseModel):
    """An automated bot player profile."""

    id: str = Field(default_factory=new_id)
    player_id: str
    display_name: str
    avatar_url: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    # When this bot last joined a room. None means it has never played.
    # Used to rotate through the full bot pool (least-recently-used
    # first) instead of the same bots tending to get picked every round.
    last_played_at: Optional[datetime] = None


class Referral(BaseModel):
    """One referral relationship: `referrer_id` (internal player id, e.g.
    "tg_123") invited `referred_id`. Created the moment a brand-new
    player's very first /start carries a `ref_<telegram_id>` deep-link
    payload - one record per referred player, regardless of whether it
    ends up earning a bonus (the referrer's per-user cap may already be
    reached). This is the single source of truth for "who invited who" -
    replaces reconstructing it by scanning every player's `referred_by`
    field, which doesn't scale and can't reliably answer "when" or
    "was this one actually paid"."""

    id: str = Field(default_factory=new_id)
    referrer_id: str
    referred_id: str
    bonus_amount: int = 0  # 0 until awarded (or if this referral was over the referrer's cap)
    awarded: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    awarded_at: Optional[datetime] = None


class Claim(BaseModel):
    """A player's claim that they have a winning card."""

    id: str = Field(default_factory=new_id)
    room_id: str
    player_id: str
    card_id: str
    claimed_pattern: str
    status: str = "pending"  # pending | accepted | rejected
    verification_message: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    verified_at: Optional[datetime] = None


class DrawLog(BaseModel):
    """Audit trail of the RNG seed and draw order for a room."""

    id: str = Field(default_factory=new_id)
    room_id: str
    seed: str
    sequence: List[Dict[str, Any]] = Field(default_factory=list)
    final_hash: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class LinkCode(BaseModel):
    """A short-lived, one-time code that lets someone using the web
    version outside Telegram (an anonymous `web_<uuid>` session, with no
    initData to verify) prove they own a real Telegram account and
    "log in" as that account going forward. Flow: the web page requests
    a code, the player sends/deep-links it to the bot, the bot verifies
    it and marks this record `linked` - see `/api/link/*` in api/main.py
    and `start_command`'s `link_<code>` payload handling in the bot."""

    id: str = Field(default_factory=new_id)
    code: str
    web_player_id: str
    status: str = "pending"  # pending | linked | expired
    telegram_id: Optional[str] = None
    linked_player_id: Optional[str] = None  # "tg_<telegram_id>" once linked
    session_token: Optional[str] = None  # issued once linked; the web page's new long-lived credential
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None  # set explicitly at creation (short TTL)


class Agent(BaseModel):
    """A reseller: funded with an ETB float by the house, tops up players from it, earns commission."""

    id: str = Field(default_factory=new_id)
    player_id: str  # "tg_<telegram_id>"
    agent_code: str
    status: str = "active"  # active | suspended
    commission_bps: int = 0  # basis points: 100 = 1%
    float_balance: int = 0  # ETB the agent can still spend on player top-ups
    commission_balance: int = 0  # ETB earned, withdrawable through the normal approval flow
    tier_bps: int = 0  # rate earned from this month's volume; the rate paid is max(commission_bps, tier_bps)
    tier_label: str = ""
    rewards_enabled: bool = True  # False = this agent earns no new-player / retention rewards (commission is unaffected)
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AgentLedgerEntry(BaseModel):
    id: str = Field(default_factory=new_id)
    agent_id: str
    type: str  # float_adjust | topup | commission | ...
    float_delta: int = 0
    float_after: int = 0
    commission_delta: int = 0
    commission_after: int = 0
    player_id: Optional[str] = None
    admin_id: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
