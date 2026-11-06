"""
Room service - the business logic for the live global Bingo session:
finding/creating the single active round, letting players pick a cartela
number (which both charges their stake and generates their card), drawing
numbers, and verifying claims. Kept separate from the FastAPI routes so it
can be unit-tested and reused (e.g. from the Telegram bot) without needing
an HTTP request.

Only ONE round is ever active at a time (state "waiting" or "running").
Anyone who wants to join while a round is running has to wait for it to
finish; `get_or_create_waiting_session` is how callers always find "the"
current global game.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from src.core.config import settings
from src.models.schemas import Card, Claim, GameRoom
from src.repositories import CardRepository, ClaimRepository, DrawLogRepository, RoomRepository
from src.repositories.settings_repository import SettingsRepository
from src.services.game_service import CardGenerator, DrawEngine, PatternVerifier
from src.services.wallet_service import InsufficientBalanceError, WalletService, funding_parts

logger = logging.getLogger(__name__)


@dataclass
class ClaimOutcome:
    is_valid: bool
    message: str
    won: bool
    card: Optional[Card]
    payout: int = 0
    winner: Optional[dict] = None
    pending: bool = False


class RoomNotFoundError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class CartelaTakenError(Exception):
    pass


class TooManyCartelasError(Exception):
    pass


class RoomService:
    # Once the lobby countdown reaches this many seconds remaining, all
    # cartela activity (picking or releasing) is locked - the round is
    # about to charge everyone and start, so a pick/release landing in
    # that last instant could otherwise race charge_and_start() and leave
    # a player charged for a cartela they never actually got to keep, or
    # vice versa. Enforced here (not just in the UI) since a client can
    # always bypass a disabled button and call the API directly.
    LOBBY_LOCK_SECONDS = 5

    def _countdown_remaining_seconds(self, room: GameRoom) -> Optional[float]:
        """Seconds left in the lobby countdown, or None if no countdown is
        running yet (nobody has reserved a cartela this round)."""
        if not room.countdown_started_at:
            return None
        started_at = room.countdown_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        return max(room.countdown_seconds - elapsed, 0)

    def _assert_lobby_not_locked(self, room: GameRoom) -> None:
        remaining = self._countdown_remaining_seconds(room)
        if remaining is not None and remaining <= self.LOBBY_LOCK_SECONDS:
            raise InvalidStateError(
                "The round is about to start - cartela selection is locked for the final "
                f"{self.LOBBY_LOCK_SECONDS} seconds."
            )

    def __init__(
        self,
        room_repo: Optional[RoomRepository] = None,
        card_repo: Optional[CardRepository] = None,
        claim_repo: Optional[ClaimRepository] = None,
        draw_log_repo: Optional[DrawLogRepository] = None,
        wallet_service: Optional[WalletService] = None,
        settings_repo: Optional[SettingsRepository] = None,
    ):
        self.rooms = room_repo or RoomRepository()
        self.cards = card_repo or CardRepository()
        self.claims = claim_repo or ClaimRepository()
        self.draw_logs = draw_log_repo or DrawLogRepository()
        self.wallets = wallet_service or WalletService()
        self.settings_repo = settings_repo or SettingsRepository()
        # In-process locks serializing the "who wins the round" race in
        # claim_win. Fine under the existing single-process assumption
        # this codebase already makes elsewhere (see _active_draw_loops in
        # api/main.py).
        self._room_locks: dict = {}
        # Optional async callable(list_of_player_ids) -> {player_id: display_name}; set by the API
        # layer so shared-win results carry every winner's name. Falls back to the player id.
        self.name_resolver = None
        # Optional async callable(set_of_player_ids), set by the API layer: called (in the background,
        # never blocking or failing the round) once a round has started with these players charged.
        self.on_round_started = None
        self._hook_tasks: set = set()
        # Per-player locks preventing the double-select race when someone
        # taps a cartela button faster than the DB round-trip.
        self._player_locks: dict = {}

    async def _payout_ratio(self) -> float:
        s = await self.settings_repo.get_or_create()
        return s.payout_ratio

    async def get_payout_ratio(self) -> float:
        """Public accessor - fraction of the prize pool actually paid to a
        winner after the platform's cut. Used by the API layer to show
        players the real payout instead of the raw total pool."""
        return await self._payout_ratio()

    # ------------------------------------------------------------------
    # Global session lookup / creation
    # ------------------------------------------------------------------

    # Which lobby track uses which wallet balance and which settings
    # fields. "real_10" is the original/default track and reuses the
    # original flat settings fields for backward compatibility; real_50
    # and bonus use the new per-track fields added alongside this.
    TRACKS = ("real_10",)

    def _track_settings(self, s, track: str) -> dict:
        if track == "real_50":
            return {
                "stake_amount": s.stake_amount_real_50,
                "bot_enabled": s.bot_enabled_real_50,
                "max_bots": s.max_bots_real_50,
                "funding_mode": "real_only",
            }
        if track == "bonus":
            return {
                "stake_amount": s.stake_amount_bonus,
                "bot_enabled": s.bot_enabled_bonus,
                "max_bots": s.max_bots_bonus,
                "funding_mode": "bonus_only",
            }
        # "real_10" / unknown track name -> the original default track.
        # Deliberately NOT "or"-ing with the legacy stake_amount /
        # bot_system_enabled / max_bots_to_join fields here: the 20260726
        # migration backfills these _real_10 columns from the legacy ones
        # exactly once, at migration time, so they always start correct.
        # From then on the _real_10 column is the sole source of truth -
        # falling back to the legacy field on every read would mean an
        # admin explicitly turning real_10 bots OFF via the new per-track
        # toggle could be silently overridden back ON by a stale legacy
        # bot_system_enabled=True sitting untouched in the same row.
        return {
            "stake_amount": s.stake_amount_real_10,
            "bot_enabled": s.bot_enabled_real_10,
            "max_bots": s.max_bots_real_10,
                # Real balance is spent before bonus credit.
            # The prize pool is still split by actual funding source at settlement.
            "funding_mode": "any",
        }

    async def _new_waiting_room(self, track: str = "real_10") -> GameRoom:
        s = await self.settings_repo.get_or_create()
        track_cfg = self._track_settings(s, track)
        draw_interval = s.draw_interval
        stake_amount = track_cfg["stake_amount"]
        countdown_seconds = s.lobby_countdown_seconds
        cards_per_player = s.cards_per_player
        min_players = s.min_players_to_start
        min_cartelas = s.min_cartelas_to_start
        cartela_min = s.cartela_min
        cartela_max = s.cartela_max

        # Draw pool is always based on the 75-ball bingo algorithm (1-75), NOT cartela range!
        draw_pool, seed = DrawEngine.initialize_draw_pool(1, 75)

        room = GameRoom(
            track=track,
            host_id=None,
            variant="75",
            number_range_min=1,
            number_range_max=75,
            cards_per_player=cards_per_player,
            pattern={"id": "any_line", "variant": "75"},
            state="waiting",
            called_numbers=[],
            draw_pool=draw_pool,
            winners=[],
            draw_interval=draw_interval,
            auto_draw=True,
            stake_amount=stake_amount,
            prize_pool=0,
            taken_cartelas=[],
            min_players_to_start=min_players,
            min_cartelas_to_start=min_cartelas,
            countdown_seconds=countdown_seconds,
            cartela_min=cartela_min,
            cartela_max=cartela_max,
        )
        # The repository takes a database-level advisory lock before checking
        # and inserting. This closes the multi-worker/redeploy race where two
        # callers both saw no latest room and each created a lobby.
        # Unit-test/in-memory repositories keep the original small API; the
        # production Postgres repository supplies the atomic method.
        created = (
            await self.rooms.create_if_no_active(room)
            if hasattr(self.rooms, "create_if_no_active")
            else await self._create_room_for_legacy_repository(room)
        )
        if created:
            await self.draw_logs.create_for_room(room.id, seed)
            return room
        if hasattr(self.rooms, "get_active"):
            active = await self.rooms.get_active(track)
        else:
            candidate = await self.rooms.get_latest(track)
            active = candidate if candidate and candidate.state in {"waiting", "running"} else None
        if active:
            return active
        # The only expected path here is a transient deletion between the
        # transaction and read; retry through the same guarded operation.
        return await self._new_waiting_room(track)

    async def _create_room_for_legacy_repository(self, room: GameRoom) -> bool:
        if hasattr(self.rooms, "get_active"):
            existing = await self.rooms.get_active(room.track)
        else:
            candidate = await self.rooms.get_latest(room.track)
            existing = candidate if candidate and candidate.state in {"waiting", "running"} else None
        if existing:
            return False
        await self.rooms.create(room)
        return True

    async def get_or_create_waiting_session(self, track: str = "real_10") -> GameRoom:
        """Return the current session for this track that players should
        see: the active round if one is waiting/running, or a round that
        just ended but hasn't been explicitly advanced past yet (see
        `advance_round`) - otherwise a brand new waiting round.

        Each of the 3 lobby tracks (real_10, real_50, bonus) has its own
        independent, concurrently-running session - this is scoped to
        exactly one track, never across tracks.

        Deliberately NOT time-based: an "ended" round stays "the" session
        for EVERY caller (a poll, a fresh page load, a reconnect after the
        app was backgrounded - all of it) until a player actually clicks
        past it. That's what guarantees nobody's winner screen is ever
        silently swapped out for an empty lobby."""
        if track not in self.TRACKS:
            raise ValueError(f"Unsupported Bingo room track: {track}")
        active = await self.rooms.get_active(track) if hasattr(self.rooms, "get_active") else None
        if active:
            return active
        latest = await self.rooms.get_latest(track)
        if latest is None:
            return await self._new_waiting_room(track)
        if latest.state == "cancelled":
            # Nobody was ever charged and there's nothing to show - safe to
            # move straight past without waiting for anyone to click anything.
            return await self._new_waiting_room(track)
        if latest.state == "ended" and latest.advanced:
            return await self._new_waiting_room(track)
        return latest

    async def advance_round(self, room_id: str) -> GameRoom:
        """Called when a player is done looking at an ended round's
        summary (they clicked "Go to Lobby" - always a deliberate click,
        never a timer). Idempotent and race-safe when several players
        close their own screens at slightly different times: whoever gets
        here first actually creates the next round; everyone after that
        just gets handed the round that already exists."""
        # The check, advance flag, and creation of the next room must be one
        # critical section. Without it, two players leaving the results
        # screen together could both observe `advanced=False` and create two
        # waiting rooms; the later poll then made the winner screen appear to
        # return or jump unpredictably.
        lock = self._get_room_lock(room_id)
        async with lock:
            room = await self.rooms.get(room_id)
            track = room.track if room else "real_10"
            latest = await self.rooms.get_latest(track)
            if latest and latest.id == room_id and latest.state == "ended" and not latest.advanced:
                await self.rooms.update(room_id, {"advanced": True})
                return await self._new_waiting_room(track)
            # Someone else already advanced past this round (or it isn't even
            # the latest round anymore) - hand back the single current room.
            return latest or await self._new_waiting_room(track)

    async def auto_advance_track(self, track: str, min_age_seconds: float) -> Optional[Tuple[GameRoom, GameRoom]]:
        """Keep a track's rounds running non-stop. Called by the server's round cycler.

        - No round / cancelled round / already-advanced round -> make sure a fresh waiting lobby exists.
        - A finished round nobody has clicked past yet -> once its winners are paid and it has been
          showing for `min_age_seconds`, start the next waiting round (same path as a player's
          "Go to Lobby", so it is idempotent and race-safe against players clicking at the same time).

        Returns (finished_room, next_room) only when this call actually moved a finished round on."""
        latest = await self.rooms.get_latest(track)
        if latest is None or latest.state != "ended" or latest.advanced:
            await self.get_or_create_waiting_session(track)
            return None
        if any(w.get("paid") is False for w in latest.winners):
            latest = await self.pay_unpaid_winners(latest.id)
            if any(w.get("paid") is False for w in latest.winners):
                return None  # never move on while a payout is still outstanding
        updated = latest.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).total_seconds() < min_age_seconds:
            return None
        next_room = await self.advance_round(latest.id)
        if next_room.id == latest.id:
            return None
        return latest, next_room

    async def get_room(self, room_id: str) -> GameRoom:
        room = await self.rooms.get(room_id)
        if not room:
            raise RoomNotFoundError(room_id)
        return room

    @asynccontextmanager
    async def _exclusive_room(self, room_id: str):
        """Use PostgreSQL locking in production; retain lightweight fakes."""
        if hasattr(self.rooms, "exclusive_room_lock"):
            async with self.rooms.exclusive_room_lock(room_id):
                yield
        else:
            lock = self._get_room_lock(room_id)
            async with lock:
                yield

    # ------------------------------------------------------------------
    # Joining - picking a cartela number just RESERVES it. No money moves
    # yet; everyone gets charged together the instant the lobby countdown
    # ends and the round actually starts (see `charge_and_start`). This is
    # what lets the live cartela board update in real time for free: a
    # player can look, pick, and even change their mind (by not being
    # charged) right up until the round locks in.
    # ------------------------------------------------------------------

    async def select_cartela(self, room_id: str, player_id: str, cartela_number: int) -> Tuple[GameRoom, Card]:
        # ── Critical section: per-player lock ──────────────────────────
        # Prevents double-spend race when a player taps rapidly. Each
        # player gets their own lock so they never block each other.
        lock = self._get_player_lock(player_id)
        async with lock:
            # These three reads are independent of each other - fire them
            # together instead of one-at-a-time. Each Firestore round
            # trip is real, felt latency on a "tap a number" interaction
            # that needs to feel instant; running them concurrently cuts
            # that wait roughly to the slowest single call instead of the
            # sum of all three.
            room, existing, wallet = await asyncio.gather(
                self.get_room(room_id),
                self.cards.list_by_room_and_owner(room_id, player_id),
                self.wallets.get_or_create_wallet(player_id),
            )
            if room.state != "waiting":
                raise InvalidStateError("Round has already started")

            self._assert_lobby_not_locked(room)

            if cartela_number in room.taken_cartelas:
                raise CartelaTakenError(f"Cartela {cartela_number} is already taken")

            if len(existing) >= room.cards_per_player:
                raise TooManyCartelasError(
                    f"You can play with at most {room.cards_per_player} cartela(s) per round"
                )

            # Soft balance check so players can't reserve a card they clearly
            # can't afford - the real charge still happens at round start.
            # Each extra cartela costs its own stake, so a player about to hold
            # N cartelas needs N stakes covered in total. Checked against
            # only the balance this track's stakes are actually funded
            # from (real balance for real_10/real_50, bonus_balance for
            # bonus) - a big bonus balance doesn't let someone reserve a
            # real-money cartela they can't pay for, and vice versa.
            required = room.stake_amount * (len(existing) + 1)
            available = wallet.bonus_balance if room.track == "bonus" else wallet.balance
            if available < required:
                raise InsufficientBalanceError(
                    f"Player {player_id} has {available}, needs {required}"
                )

            grid = CardGenerator.generate_card(room.variant, cartela_number=cartela_number, cartela_min=room.cartela_min, cartela_max=room.cartela_max)
            card = Card(
                room_id=room_id,
                owner_id=player_id,
                variant=room.variant,
                cartela_number=cartela_number,
                grid=grid,
            )

            taken = room.taken_cartelas + [cartela_number]
            fields = {"taken_cartelas": taken}
            min_cartelas = max(int(getattr(room, "min_cartelas_to_start", 3) or 1), 1)
            if len(taken) >= min_cartelas and not room.countdown_started_at:
                # The lobby has enough cartelas (they may all belong to one
                # player) - the 30s clock starts now.
                room.countdown_started_at = datetime.now(timezone.utc)
                fields["countdown_started_at"] = room.countdown_started_at

            # Two independent writes to two different documents - same
            # reasoning as the reads above, run them together.
            await asyncio.gather(
                self.cards.create(card),
                self.rooms.update(room_id, fields),
            )
            room.taken_cartelas = taken

        return room, card

    async def release_cartela(self, room_id: str, player_id: str, card_id: Optional[str] = None) -> GameRoom:
        """Lets a player change their mind before the round starts - drops
        their reservation(s) with no financial consequence since nothing
        was ever charged. If `card_id` is given, only that one cartela is
        released (useful when a player holds 2 and wants to swap just
        one); otherwise every cartela they hold this round is released."""
        room = await self.get_room(room_id)
        if room.state != "waiting":
            raise InvalidStateError("Round already started")
        self._assert_lobby_not_locked(room)
        cards = await self.cards.list_by_room_and_owner(room_id, player_id)
        if card_id:
            cards = [c for c in cards if c.id == card_id]
        for card in cards:
            await self.cards.delete(card.id)
            if card.cartela_number in room.taken_cartelas:
                room.taken_cartelas.remove(card.cartela_number)
        fields = {"taken_cartelas": room.taken_cartelas}
        min_cartelas = max(int(getattr(room, "min_cartelas_to_start", 3) or 1), 1)
        if len(room.taken_cartelas) < min_cartelas:
            # Not enough cartelas any more - stop the clock; it restarts
            # from the full 30s when the lobby reaches the minimum again.
            fields["countdown_started_at"] = None
            room.countdown_started_at = None
        await self.rooms.update(room_id, fields)
        return room

    async def cancel_round(self, room_id: str) -> GameRoom:
        """Mark an unstartable lobby cancelled, then let the next lookup
        create a fresh waiting lobby. No refunds are needed."""
        await self.rooms.update(
            room_id,
            {"taken_cartelas": [], "prize_pool": 0, "state": "cancelled", "countdown_started_at": None},
        )
        room = await self.get_room(room_id)
        cards = await self.cards.list_by_room(room_id)
        for card in cards:
            await self.cards.delete(card.id)
        return room

    # ------------------------------------------------------------------
    # Running the round
    # ------------------------------------------------------------------

    async def charge_and_start(self, room_id: str) -> Tuple[Optional[GameRoom], list]:
        """Called once the lobby countdown ends. Charges every reserved
        cartela's stake in one pass; anyone who can no longer afford it
        (balance changed mid-countdown) is dropped from the round instead
        of blocking everyone else. Returns (room, dropped_player_ids) - room
        is None if literally nobody could be charged (round is cancelled).
        """
        async with self._exclusive_room(room_id):
            return await self._charge_and_start_locked(room_id)

    async def _charge_and_start_locked(self, room_id: str) -> Tuple[Optional[GameRoom], list]:
        room = await self.get_room(room_id)
        if room.state != "waiting":
            raise InvalidStateError("Game already started")

        cards = await self.cards.list_by_room(room_id)
        s = await self.settings_repo.get_or_create()
        required_cartelas = max(int(getattr(room, "min_cartelas_to_start", None) or s.min_cartelas_to_start), 1)
        if len(cards) < required_cartelas:
            # A countdown can expire after a player leaves or loses funds.
            # Preserve the cancelled marker so the next lookup rotates this
            # empty/unstartable lobby instead of exposing it indefinitely.
            if cards:
                await self.cancel_round(room_id)
            return None, []
        charged_cartelas: list[int] = []
        charged_cards: list[tuple[Card, str]] = []
        dropped: list[str] = []
        funding_mode = self._track_settings(s, room.track)["funding_mode"]
        for card in cards:
            try:
                _, funded_by = await self.wallets.debit_for_stake(
                    card.owner_id, room.stake_amount, room_id, funding_mode=funding_mode
                )
                await self.cards.update(card.id, {"funded_by": funded_by})
                charged_cartelas.append(card.cartela_number)
                charged_cards.append((card, funded_by))
            except InsufficientBalanceError:
                await self.cards.delete(card.id)
                dropped.append(card.owner_id)

        charged_players = {card.owner_id for card, _ in charged_cards}
        if len(charged_cartelas) < required_cartelas:
            # A player can lose balance during the countdown and drop below
            # the minimum number of cartelas; restore every charged stake to
            # its original pool and reset the lobby instead.
            for card, funded_by in charged_cards:
                await self.wallets.refund_stake(card.owner_id, room.stake_amount, room_id, funded_by)
            await self.cancel_round(room_id)
            return None, dropped

        if not charged_cartelas:
            await self.cancel_round(room_id)
            return None, dropped

        prize_pool = room.stake_amount * len(charged_cartelas)
        pool_real = 0
        pool_bonus = 0
        for _card, funded_by in charged_cards:
            real_part, bonus_part = funding_parts(funded_by, room.stake_amount)
            pool_real += real_part
            pool_bonus += bonus_part
        await self.rooms.update(
            room_id,
            {
                "taken_cartelas": charged_cartelas,
                "prize_pool": prize_pool,
                "prize_pool_real": pool_real,
                "prize_pool_bonus": pool_bonus,
                "state": "running",
            },
        )
        room.taken_cartelas = charged_cartelas
        room.prize_pool = prize_pool
        room.prize_pool_real = pool_real
        room.prize_pool_bonus = pool_bonus
        room.state = "running"
        self._fire_round_started(charged_players)
        return room, dropped

    def _fire_round_started(self, player_ids) -> None:
        hook = self.on_round_started
        if hook is None or not player_ids:
            return

        async def _run() -> None:
            try:
                await hook(set(player_ids))
            except Exception:  # noqa: BLE001 - engagement bookkeeping must never affect a round
                logger.exception("on_round_started hook failed")

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            return
        self._hook_tasks.add(task)
        task.add_done_callback(self._hook_tasks.discard)

    async def draw_number(self, room_id: str) -> Tuple[Optional[int], int]:
        """Draw the next number for a room. Returns (number, sequence)."""
        async with self._exclusive_room(room_id):
            return await self._draw_number_locked(room_id)

    async def _draw_number_locked(self, room_id: str) -> Tuple[Optional[int], int]:
        room = await self.get_room(room_id)
        if room.state != "running":
            raise InvalidStateError("Game not running")

        pool = room.draw_pool

        # Pure fair draw: the pool was already shuffled once with a
        # cryptographically-secure seed in initialize_draw_pool, so every
        # remaining number has an equal chance of being drawn next. No
        # code anywhere is allowed to look at who would win before a
        # number is drawn and reorder the pool in response - that would
        # make the outcome predetermined instead of random, for bots and
        # real players alike, and for real-money and bonus-funded cards
        # alike. The winner is decided purely by chance plus who calls
        # claim_win() first (see the lock/race handling there).
        number = DrawEngine.draw_number(pool)
        if number is None:
            return None, len(room.called_numbers)

        called_numbers = room.called_numbers + [number]
        await self.rooms.update(room_id, {"called_numbers": called_numbers, "draw_pool": pool})

        draw_log = await self.draw_logs.get_by_room(room_id)
        if draw_log:
            await self.draw_logs.append_draw(draw_log.id, len(called_numbers), number)

        return number, len(called_numbers)

    async def claim_win(self, room_id: str, player_id: str, card_id: str, player_name: Optional[str] = None) -> "ClaimOutcome":
        """Verify a bingo claim and, if it's genuinely valid AND wins the
        settlement race against any other simultaneous claim, pay it out
        and end the round - all as a single atomic, synchronous operation.
        There is no in-between state to catch: by the time this returns
        with `won=True`, the money is already in the winner's wallet and
        `room.state == "ended"` with the full winner record recorded.

        Returns a ClaimOutcome:
        - not is_valid: the pattern genuinely isn't complete yet (no
          money moved, round keeps running).
        - is_valid and not won: the pattern is complete, but the round had
          already ended (someone else's claim - or this same player's
          own duplicate/double-tap claim - got there first).
        - is_valid and won: this call ended the round and paid out the
          winner right now.
        """
        room = await self.get_room(room_id)
        card = await self.cards.get(card_id)
        if not card:
            raise RoomNotFoundError(f"Card {card_id} not found")

        # Any card - bot-owned or a real player's - is judged purely on
        # whether its pattern is actually complete against the numbers
        # actually called, and on the order claims land in (see the lock
        # below). No account is special-cased to win or blocked from
        # winning based on who owns the card or how it was funded.

        if room.state != "running":
            # Round is already over one way or another - tell the caller
            # clearly whether it was THEM that already won (duplicate
            # claim / double-tap) or somebody else, without re-verifying
            # a pattern against a round that no longer exists.
            already_won_by_me = any(w.get("player_id") == player_id for w in room.winners)
            duplicate_multi_winner = already_won_by_me and len(room.winners) > 1
            msg = (
                "Your win is already confirmed - check the results screen!"
                if already_won_by_me
                else "This round has already ended."
            )
            return ClaimOutcome(True, msg, duplicate_multi_winner, card,
                                payout=next((int(w.get("payout", 0)) for w in room.winners
                                             if w.get("player_id") == player_id), 0))

        pattern_name = room.pattern.get("id", "any_line")
        is_valid, message = PatternVerifier.verify_claim(
            card.grid, room.called_numbers, pattern_name, room.variant
        )
        if not is_valid:
            return ClaimOutcome(False, message, False, card)

        # Attach the authoritatively-marked grid (not the stored, mostly-
        # unmarked one) so whoever displays this card shows the actual
        # winning hits, not a blank template.
        card.grid = PatternVerifier.compute_effective_grid(card.grid, room.called_numbers)

        if await self.is_multi_winner_round(room_id):
            return await self._claim_multi_winner(room_id, player_id, card, player_name)

        lock = self._get_room_lock(room_id)
        async with lock:
            # Re-check under the lock - another request may have already
            # ended the round in the gap since we read `room` above. This
            # is the ONLY race that matters, and it's now fully closed:
            # nothing after this point can be missed by a poller, because
            # there is no intermediate state to poll into - the very next
            # line either pays out and ends the round, or bails out.
            fresh_room = await self.get_room(room_id)
            if fresh_room.state != "running":
                already_won_by_me = any(w.get("player_id") == player_id for w in fresh_room.winners)
                msg = (
                    "Your win is already confirmed - check the results screen!"
                    if already_won_by_me
                    else "Another player already won this round."
                )
                return ClaimOutcome(True, msg, False, card)

            ratio = await self._payout_ratio()
            payout = round(fresh_room.prize_pool * ratio)

            # Split the payout by how the WHOLE pool was actually funded,
            # not by whichever pool the winning card itself came from.
            # This is the fix that stops a small real-money stake from
            # "laundering" a pot mostly filled by other players' bonus
            # credit into fully withdrawable money - the winner can never
            # receive more real, withdrawable money than actually went
            # into the pool as real money in the first place.
            pool_real, pool_bonus, real_payout, bonus_payout = self._split_real_bonus(fresh_room, payout)

            winner_record = {
                "player_id": player_id,
                # Persist the visible name with the immutable result. Room
                # summaries intentionally don't ship all players on every
                # live refresh, so relying on that list made the results UI
                # fall back to the generic "Player" label.
                "player_name": player_name or player_id,
                "card_id": card_id,
                "cartela_number": card.cartela_number,
                # Stored as a normal nested 5x5 array - unlike Firestore,
                # Postgres JSONB has no restriction on arrays containing
                # arrays, so no flattening workaround is needed here.
                "grid": card.grid,
                "pattern": pattern_name,
                "payout": payout,
                "prize_pool": fresh_room.prize_pool,
                "payout_ratio": ratio,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            winners = fresh_room.winners + [winner_record]

            # Credit the wallet FIRST so it's correct the instant this
            # call returns - fully synchronous, no delay, no separate
            # background step that could crash or be skipped.
            await self.wallets.credit_payout_split(
                player_id, real_payout, bonus_payout, room_id,
                note=f"pool_real={pool_real} pool_bonus={pool_bonus}",
            )
            await self.rooms.update(room_id, {
                "winners": winners,
                "state": "ended",
                "end_reason": "winner",
            })

        claim = Claim(
            room_id=room_id,
            player_id=player_id,
            card_id=card_id,
            claimed_pattern=pattern_name,
            status="accepted",
            verification_message="Bingo! Valid winning pattern.",
            verified_at=datetime.now(timezone.utc),
        )
        await self.claims.create(claim)

        draw_log = await self.draw_logs.get_by_room(room_id)
        if draw_log:
            await self.draw_logs.finalize(draw_log.id)

        from src.services import player_notifications as pn

        pn.schedule_notify(
            player_id, "win", {"payout": payout, "cartela_number": card.cartela_number, "shared_with": 1},
            dedupe_key=f"win:{room_id}:{player_id}",
        )
        return ClaimOutcome(True, message, True, card, payout=payout, winner=winner_record)


    # ------------------------------------------------------------------
    # Multi-winner rounds (players > settings.multi_winner_min_players)
    #
    # Rule: there is no claim window. After every drawn number the server checks EVERY card in
    # the room. All players whose BINGO is completed by that same last called number win together:
    # one share per player (even if two of their cartelas complete), the prize is split equally,
    # paid at once, and the round ends. Usually that is one winner; sometimes several cards
    # complete on the same number. Players do not need to press claim (auto-BINGO or not).
    # ------------------------------------------------------------------

    async def is_multi_winner_round(self, room_id: str) -> bool:
        threshold = settings.multi_winner_min_players
        if threshold <= 0:
            return False
        cards = await self.cards.list_by_room(room_id)
        return len({c.owner_id for c in cards}) > threshold

    @staticmethod
    def _split_real_bonus(room: GameRoom, payout: int) -> Tuple[int, int, int, int]:
        """(pool_real, pool_bonus, real_payout, bonus_payout) for a payout of `payout`.

        Split by how the WHOLE pool was funded, never by the winning card's own stake, so a small
        real stake can't launder a bonus-funded pot into withdrawable money. No composition on
        record -> fail closed to bonus."""
        pool_real = getattr(room, "prize_pool_real", 0) or 0
        pool_bonus = getattr(room, "prize_pool_bonus", 0) or 0
        pool_total = pool_real + pool_bonus
        real_payout = round(payout * pool_real / pool_total) if pool_total > 0 else 0
        return pool_real, pool_bonus, real_payout, payout - real_payout

    async def _claim_multi_winner(self, room_id, player_id, card, player_name) -> "ClaimOutcome":
        """A valid manual claim in a multi-winner round just triggers the same settlement the
        draw loop runs after every number; it never makes the claimer win alone."""
        names = {player_id: player_name} if player_name else None
        await self.settle_round_winners(room_id, names=names)
        room = await self.get_room(room_id)
        mine = next((w for w in room.winners if w.get("player_id") == player_id), None)
        if mine is not None:
            return ClaimOutcome(True, "Bingo! Valid winning pattern.", True, card,
                                payout=int(mine.get("payout", 0)), winner=mine)
        return ClaimOutcome(True, "This round has already ended.", False, card)

    async def settle_round_winners(self, room_id: str, names: Optional[dict] = None) -> Optional[GameRoom]:
        """Check every card against the numbers called so far. If any card is complete, all
        players with a complete card win together on this same last called number: split the
        prize equally, pay, and end the round. Returns the ended room, or None if the round is
        not running or nobody has BINGO yet. Safe to call repeatedly / concurrently."""
        async with self._exclusive_room(room_id):
            room = await self.get_room(room_id)
            if room.state != "running":
                return None

            pattern_name = room.pattern.get("id", "any_line")
            called = room.called_numbers
            cards = await self.cards.list_by_room(room_id)
            found: dict = {}  # owner_id -> (card, effective grid); one share per player
            for card in sorted(cards, key=lambda c: c.cartela_number):
                if card.owner_id in found:
                    continue
                grid = PatternVerifier.compute_effective_grid(card.grid, called)
                if PatternVerifier.verify_pattern(grid, pattern_name, room.variant):
                    found[card.owner_id] = (card, grid)
            if not found:
                return None

            resolved = dict(names or {})
            missing = [o for o in found if o not in resolved]
            if missing and self.name_resolver is not None:
                try:
                    resolved.update(await self.name_resolver(missing))
                except Exception:  # noqa: BLE001 - names are cosmetic, never block a payout
                    logger.exception("Could not resolve winner names for room %s", room_id)

            ratio = await self._payout_ratio()
            total = round(room.prize_pool * ratio)
            n = len(found)
            share = total // n  # ETB left over from rounding down stays with the house
            now = datetime.now(timezone.utc).isoformat()
            winning_number = called[-1] if called else None
            winners = [
                {
                    "player_id": owner_id,
                    "player_name": resolved.get(owner_id) or owner_id,
                    "card_id": card.id,
                    "cartela_number": card.cartela_number,
                    "grid": grid,
                    "pattern": pattern_name,
                    "payout": share,
                    "shared_with": n,
                    "winning_number": winning_number,
                    "prize_pool": room.prize_pool,
                    "payout_ratio": ratio,
                    "timestamp": now,
                    "paid": False,
                }
                for owner_id, (card, grid) in found.items()
            ]

            # End the round FIRST with the split frozen into the winner records, then pay and flag
            # each winner. A crash mid-way can leave a winner unpaid (visible as paid=False, fixable
            # with pay_unpaid_winners) but can never pay anyone twice or draw another number.
            await self.rooms.update(room_id, {"winners": winners, "state": "ended", "end_reason": "winner"})
            room.winners, room.state, room.end_reason = winners, "ended", "winner"
            await self._pay_winners(room, winners)

            for w in winners:
                await self.claims.create(Claim(
                    room_id=room_id, player_id=w["player_id"], card_id=w["card_id"], claimed_pattern=pattern_name,
                    status="accepted", verification_message="Bingo! Valid winning pattern.",
                    verified_at=datetime.now(timezone.utc),
                ))

        draw_log = await self.draw_logs.get_by_room(room_id)
        if draw_log:
            await self.draw_logs.finalize(draw_log.id)
        return room

    async def _pay_winners(self, room: GameRoom, winners: list) -> None:
        for w in winners:
            if w.get("paid"):
                continue
            pool_real, pool_bonus, real_payout, bonus_payout = self._split_real_bonus(room, int(w["payout"]))
            await self.wallets.credit_payout_split(
                w["player_id"], real_payout, bonus_payout, room.id,
                note=f"shared_win 1/{w.get('shared_with', 1)} pool_real={pool_real} pool_bonus={pool_bonus}",
            )
            w["paid"] = True
            await self.rooms.update(room.id, {"winners": winners})
            from src.services import player_notifications as pn

            pn.schedule_notify(
                w["player_id"], "win",
                {"payout": w["payout"], "cartela_number": w.get("cartela_number"), "shared_with": w.get("shared_with", 1)},
                dedupe_key=f"win:{room.id}:{w['player_id']}",
            )

    async def pay_unpaid_winners(self, room_id: str) -> GameRoom:
        """Recovery tool: pay any winner of an ended round still flagged paid=False (e.g. after a
        crash mid-payout). Idempotent - already-paid winners are skipped."""
        async with self._exclusive_room(room_id):
            room = await self.get_room(room_id)
            if room.state == "ended" and any(w.get("paid") is False for w in room.winners):
                await self._pay_winners(room, room.winners)
            return room

    async def end_round_without_winner(self, room_id: str, reason: str) -> GameRoom:
        """NOT called automatically anywhere anymore - the draw loop never
        ends a round on its own; a round only ends when a player wins it
        (see `claim_win`). Kept available as a manual/admin tool for the
        rare case a round genuinely needs to be torn down early (e.g. a
        misconfigured pattern makes winning impossible). Refunds every
        charged stake back to whichever pool (real/bonus) it came from -
        a round nobody won must never cost anyone anything - then ends
        the round. Idempotent: only actually runs (and only ever refunds
        once) while the room is still "running"."""
        room = await self.get_room(room_id)
        if room.state == "running":
            cards = await self.cards.list_by_room(room_id)
            for card in cards:
                funded_by = getattr(card, "funded_by", "real")
                await self.wallets.refund_stake(card.owner_id, room.stake_amount, room_id, funded_by=funded_by)
            await self.rooms.update(room_id, {"state": "ended", "end_reason": reason})
            room.state = "ended"
            room.end_reason = reason
        return room

    def _get_player_lock(self, player_id: str) -> "asyncio.Lock":
        lock = self._player_locks.get(player_id)
        if lock is None:
            lock = asyncio.Lock()
            self._player_locks[player_id] = lock
        return lock

    def _get_room_lock(self, room_id: str) -> "asyncio.Lock":
        lock = self._room_locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            self._room_locks[room_id] = lock
        return lock
