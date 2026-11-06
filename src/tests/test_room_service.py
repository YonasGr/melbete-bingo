"""
Tests for RoomService (the live global session).

These use simple in-memory fakes instead of real Firestore repositories,
so the full flow (reserve cartela -> charge & start -> draw -> claim ->
payout) can be tested offline without any cloud credentials.
"""
import asyncio

import pytest
from datetime import datetime, timedelta, timezone

from src.tests.fake_wallet_ops import AtomicWalletOps
from src.models.schemas import Card, DrawLog, GameRoom, SystemSettings, Wallet
from src.services.room_service import (
    CartelaTakenError,
    InvalidStateError,
    RoomNotFoundError,
    RoomService,
    TooManyCartelasError,
)
from src.services.wallet_service import InsufficientBalanceError, WalletService


class FakeRoomRepository:
    def __init__(self):
        self.rooms = {}

    async def get(self, room_id):
        return self.rooms.get(room_id)

    async def create(self, room: GameRoom):
        self.rooms[room.id] = room
        return room

    async def update(self, room_id, fields):
        room = self.rooms[room_id]
        for key, value in fields.items():
            setattr(room, key, value)

    async def get_latest(self, track: str = "real_10"):
        candidates = [r for r in self.rooms.values() if getattr(r, "track", "real_10") == track]
        if not candidates:
            return None
        return sorted(candidates, key=lambda r: r.created_at)[-1]


class FakeCardRepository:
    def __init__(self):
        self.cards = {}
        self.list_by_room_calls = 0

    async def get(self, card_id):
        return self.cards.get(card_id)

    async def create(self, card: Card):
        self.cards[card.id] = card
        return card

    async def list_by_room(self, room_id):
        self.list_by_room_calls += 1
        return [c for c in self.cards.values() if c.room_id == room_id]

    async def list_by_room_and_owner(self, room_id, owner_id):
        return [c for c in self.cards.values() if c.room_id == room_id and c.owner_id == owner_id]

    async def update(self, card_id, fields):
        card = self.cards[card_id]
        for key, value in fields.items():
            setattr(card, key, value)

    async def delete(self, card_id):
        self.cards.pop(card_id, None)


class FakeClaimRepository:
    def __init__(self):
        self.claims = []

    async def create(self, claim):
        self.claims.append(claim)
        return claim


class FakeDrawLogRepository:
    def __init__(self):
        self.logs = {}

    async def create_for_room(self, room_id, seed):
        log = DrawLog(room_id=room_id, seed=seed)
        self.logs[log.id] = log
        return log

    async def get_by_room(self, room_id):
        for log in self.logs.values():
            if log.room_id == room_id:
                return log
        return None

    async def append_draw(self, log_id, draw_number, value):
        self.logs[log_id].sequence.append({"draw_number": draw_number, "value": value})

    async def finalize(self, log_id):
        self.logs[log_id].final_hash = "fake-hash"


class FakeWalletRepository(AtomicWalletOps):
    def __init__(self, starting_balance: int = 1000):
        self.wallets = {}
        self.starting_balance = starting_balance

    async def get_by_player(self, player_id):
        return self.wallets.get(player_id)

    async def create(self, wallet: Wallet):
        wallet.balance = self.starting_balance
        self.wallets[wallet.player_id] = wallet
        return wallet

    async def update(self, player_id, fields):
        wallet = self.wallets[player_id]
        for key, value in fields.items():
            setattr(wallet, key, value)

    async def atomic_debit_real(self, player_id, amount):
        wallet = self.wallets.get(player_id)
        if not wallet or wallet.balance < amount:
            return None
        wallet.balance -= amount
        wallet.won_balance = min(wallet.won_balance, wallet.balance)
        return wallet


class FakeTransactionRepository:
    def __init__(self):
        self.transactions = []

    async def create(self, transaction):
        self.transactions.append(transaction)
        return transaction

    async def update(self, transaction_id, fields):
        for t in self.transactions:
            if t.id == transaction_id:
                for k, v in fields.items():
                    setattr(t, k, v)

    async def list_pending_topups(self):
        return [t for t in self.transactions if t.type == "top_up_pending" and t.status == "pending"]

    async def list_pending_withdrawals(self):
        return [t for t in self.transactions if t.type == "withdrawal_pending" and t.status == "pending"]


class FakeSettingsRepository:
    def __init__(self):
        self._settings = SystemSettings()

    async def get(self):
        return self._settings

    async def get_or_create(self):
        return self._settings

    async def update(self, fields):
        for k, v in fields.items():
            setattr(self._settings, k, v)
        return self._settings


@pytest.fixture
def wallet_service():
    return WalletService(
        wallet_repo=FakeWalletRepository(),
        transaction_repo=FakeTransactionRepository(),
        settings_repo=FakeSettingsRepository(),
    )


@pytest.fixture
def service(wallet_service):
    return RoomService(
        room_repo=FakeRoomRepository(),
        card_repo=FakeCardRepository(),
        claim_repo=FakeClaimRepository(),
        draw_log_repo=FakeDrawLogRepository(),
        wallet_service=wallet_service,
        settings_repo=FakeSettingsRepository(),
    )


async def claim_and_finalize(service, room_id, player_id, card_id):
    """Test helper name kept for minimal diff noise, but claiming is now a
    single atomic call - no separate finalize/close steps exist anymore.
    Returns (is_valid, message, payout) - payout is 0 if the claim wasn't
    valid or didn't win the settlement race."""
    outcome = await service.claim_win(room_id, player_id, card_id)
    return outcome.is_valid, outcome.message, outcome.payout


@pytest.mark.asyncio
async def test_get_or_create_waiting_session(service):
    room = await service.get_or_create_waiting_session()
    assert room.state == "waiting"
    assert room.variant == "75"
    assert len(room.draw_pool) == 75
    assert room.cartela_min == 1
    assert room.cartela_max == 500
    # Minimum cartelas to start is configured in SystemSettings (default 2)
    assert room.min_cartelas_to_start == 2

    # Calling again while still waiting returns the SAME room - only one
    # global session may exist at a time.
    room2 = await service.get_or_create_waiting_session()
    assert room2.id == room.id


@pytest.mark.asyncio
async def test_select_cartela_reserves_without_charging(service, wallet_service):
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 42)

    assert card.cartela_number == 42
    # The center cell is a free space; its value must stay inside the 1-75
    # ball range (None), never the cartela number (which can exceed 75).
    assert card.grid[2][2]["free"] is True
    assert card.grid[2][2]["value"] is None
    assert room.taken_cartelas == [42]
    # Nothing is charged yet - only reserved.
    assert room.prize_pool == 0

    wallet = await wallet_service.get_or_create_wallet("player-1")
    assert wallet.balance == 1000


@pytest.mark.asyncio
async def test_cannot_pick_same_cartela_twice(service):
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 5)
    with pytest.raises(CartelaTakenError):
        await service.select_cartela(room.id, "player-2", 5)


@pytest.mark.asyncio
async def test_draw_number_does_not_refetch_cards_every_tick(service, wallet_service):
    """draw_number() decides the next ball purely from the pre-shuffled
    draw pool - it never inspects who holds which card, so it should
    never trigger a full-room card fetch on any tick (not just an
    amortized/cached one). This is what keeps the draw immune to any
    accidental reintroduction of outcome-based reshuffling."""
    await service.settings_repo.update({"min_cartelas_to_start": 2})
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    await service.select_cartela(room.id, "player-2", 2)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    calls_after_start = service.cards.list_by_room_calls
    for _ in range(10):
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    # Ten more draws, zero additional card fetches - the draw never looks
    # at who would win before picking the next number.
    assert service.cards.list_by_room_calls == calls_after_start

    await service.end_round_without_winner(room.id, "test cleanup")


@pytest.mark.asyncio
async def test_release_cartela_frees_it_up(service):
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 5)
    room = await service.release_cartela(room.id, "player-1")
    assert room.taken_cartelas == []
    # Someone else can now take it.
    room, _ = await service.select_cartela(room.id, "player-2", 5)
    assert room.taken_cartelas == [5]


@pytest.mark.asyncio
async def test_select_cartela_locked_in_final_seconds_of_countdown(service):
    """Once the lobby countdown has LOBBY_LOCK_SECONDS or fewer remaining,
    no new cartela may be picked - the round is about to charge everyone
    and start, and a pick landing in that instant could otherwise race
    charge_and_start()."""
    room = await service.get_or_create_waiting_session()
    room, _ = await service.select_cartela(room.id, "player-1", 1)

    # Simulate the countdown being almost over.
    stored = await service.rooms.get(room.id)
    stored.countdown_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=stored.countdown_seconds - service.LOBBY_LOCK_SECONDS + 1
    )

    with pytest.raises(InvalidStateError):
        await service.select_cartela(room.id, "player-2", 2)


@pytest.mark.asyncio
async def test_release_cartela_locked_in_final_seconds_of_countdown(service):
    room = await service.get_or_create_waiting_session()
    room, _ = await service.select_cartela(room.id, "player-1", 1)

    stored = await service.rooms.get(room.id)
    stored.countdown_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=stored.countdown_seconds - service.LOBBY_LOCK_SECONDS + 1
    )

    with pytest.raises(InvalidStateError):
        await service.release_cartela(room.id, "player-1")


@pytest.mark.asyncio
async def test_select_cartela_allowed_just_before_lock_window(service):
    """Sanity check the boundary isn't off-by-one in the overly strict
    direction - picking should still work with more than LOBBY_LOCK_SECONDS
    left on the clock."""
    room = await service.get_or_create_waiting_session()
    room, _ = await service.select_cartela(room.id, "player-1", 1)

    stored = await service.rooms.get(room.id)
    stored.countdown_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=stored.countdown_seconds - service.LOBBY_LOCK_SECONDS - 3
    )

    room, _ = await service.select_cartela(room.id, "player-2", 2)
    assert 2 in room.taken_cartelas


@pytest.mark.asyncio
async def test_insufficient_balance_blocks_join():
    poor_wallets = WalletService(
        wallet_repo=FakeWalletRepository(starting_balance=5),
        transaction_repo=FakeTransactionRepository(),
        settings_repo=FakeSettingsRepository(),
    )
    service = RoomService(
        room_repo=FakeRoomRepository(),
        card_repo=FakeCardRepository(),
        claim_repo=FakeClaimRepository(),
        draw_log_repo=FakeDrawLogRepository(),
        wallet_service=poor_wallets,
        settings_repo=FakeSettingsRepository(),
    )
    room = await service.get_or_create_waiting_session()
    with pytest.raises(InsufficientBalanceError):
        await service.select_cartela(room.id, "player-1", 1)


@pytest.mark.asyncio
async def test_cannot_draw_before_start(service):
    room = await service.get_or_create_waiting_session()
    with pytest.raises(InvalidStateError):
        await service.draw_number(room.id)


@pytest.mark.asyncio
async def test_single_player_can_start_when_test_setting_allows_it(service, wallet_service):
    """The service honours the configured threshold; admin validation prevents
    production settings from being changed below two."""
    # Override settings for this test
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)

    started_room, dropped = await service.charge_and_start(room.id)

    assert dropped == []
    assert started_room.state == "running"
    assert started_room.taken_cartelas == [1]
    assert started_room.prize_pool == started_room.stake_amount

    wallet = await wallet_service.get_or_create_wallet("player-1")
    assert wallet.balance == 1000 - started_room.stake_amount


@pytest.mark.asyncio
async def test_charge_and_start_drops_players_who_cant_afford_it(service, wallet_service):
    await service.settings_repo.update({"min_cartelas_to_start": 2})
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    await service.select_cartela(room.id, "player-2", 2)

    # player-2's balance disappears before the round starts (e.g. spent
    # elsewhere). The round must not silently become a one-player game.
    await wallet_service.wallet_repo.update("player-2", {"balance": 0})

    started_room, dropped = await service.charge_and_start(room.id)

    assert dropped == ["player-2"]
    assert started_room is None
    player_one = await wallet_service.get_or_create_wallet("player-1")
    assert player_one.balance == 1000  # charged stake was refunded


@pytest.mark.asyncio
async def test_charge_and_start_cancels_if_nobody_can_pay(service, wallet_service):
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    await wallet_service.wallet_repo.update("player-1", {"balance": 0})

    started_room, dropped = await service.charge_and_start(room.id)

    assert started_room is None
    assert dropped == ["player-1"]
    room = await service.get_room(room.id)
    assert room.state == "waiting"
    assert room.taken_cartelas == []


@pytest.mark.asyncio
async def test_full_round_to_claim_pays_out_winner(service, wallet_service):
    await service.settings_repo.update({"min_cartelas_to_start": 2})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, _ = await service.select_cartela(room.id, "player-2", 2)

    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    # Draw every number so any pattern is guaranteed complete.
    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    # Mark every non-free cell on player-1's card as if they tracked all draws.
    for row in card.grid:
        for cell in row:
            if not cell.get("free"):
                cell["marked"] = True

    is_valid, message, payout = await claim_and_finalize(service, room.id, "player-1", card.id)
    assert is_valid is True
    assert payout == round(room.prize_pool * 0.8) // 2

    wallet = await wallet_service.get_or_create_wallet("player-1")
    # Started with 1000, paid a 10 stake, then received the payout - all
    # already true the instant claim_win returns, atomically.
    assert wallet.balance == 1000 - room.stake_amount + payout

    ended_room = await service.get_room(room.id)
    assert ended_room.state == "ended"
    assert ended_room.end_reason == "winner"
    assert len(ended_room.winners) == 2
    assert ended_room.advanced is False  # still "the" session until someone clicks past it

    # get_or_create_waiting_session must keep returning THIS exact room -
    # nobody's results screen gets silently swapped for an empty lobby.
    still_same_room = await service.get_or_create_waiting_session()
    assert still_same_room.id == room.id

    # Only once a player explicitly advances does a fresh round appear.
    next_room = await service.advance_round(room.id)
    assert next_room.id != room.id
    assert next_room.state == "waiting"


@pytest.mark.asyncio
async def test_claim_bingo_works_without_any_client_side_marking(service, wallet_service):
    """Reproduces the exact reported production bug: the real Mini App
    never sends marked-cell state back to the server - it only ever calls
    /claim with the card id. So claim_bingo must determine a win purely
    from room.called_numbers vs. the card's fixed layout, NOT from
    whatever 'marked' flags happen to be sitting in storage (which, in
    production, are never touched again after the card is created)."""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    # Draw until the whole pool is exhausted - guarantees at least one
    # full pattern is complete. Deliberately do NOT touch card.grid at
    # all - this is exactly what the real API does (client-side marking
    # is purely cosmetic and is never persisted back to the server).
    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    is_valid, message, payout = await claim_and_finalize(service, room.id, "player-1", card.id)
    assert is_valid is True, message
    assert payout > 0



@pytest.mark.asyncio
async def test_player_can_hold_up_to_four_cartelas(service, wallet_service):
    room = await service.get_or_create_waiting_session()
    assert room.cards_per_player == 4

    room, card1 = await service.select_cartela(room.id, "player-1", 1)
    room, card2 = await service.select_cartela(room.id, "player-1", 2)
    room, card3 = await service.select_cartela(room.id, "player-1", 3)
    room, card4 = await service.select_cartela(room.id, "player-1", 4)
    assert card1.owner_id == card2.owner_id == card3.owner_id == card4.owner_id == "player-1"
    assert room.taken_cartelas == [1, 2, 3, 4]

    with pytest.raises(TooManyCartelasError):
        await service.select_cartela(room.id, "player-1", 5)


@pytest.mark.asyncio
async def test_second_cartela_requires_a_second_stake_worth_of_balance():
    poor_wallets = WalletService(
        wallet_repo=FakeWalletRepository(starting_balance=15),  # covers 1 stake (10) but not 2
        transaction_repo=FakeTransactionRepository(),
        settings_repo=FakeSettingsRepository(),
    )
    service = RoomService(
        room_repo=FakeRoomRepository(),
        card_repo=FakeCardRepository(),
        claim_repo=FakeClaimRepository(),
        draw_log_repo=FakeDrawLogRepository(),
        wallet_service=poor_wallets,
        settings_repo=FakeSettingsRepository(),
    )
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    with pytest.raises(InsufficientBalanceError):
        await service.select_cartela(room.id, "player-1", 2)


@pytest.mark.asyncio
async def test_release_one_of_two_cartelas_keeps_the_other(service):
    room = await service.get_or_create_waiting_session()
    room, card1 = await service.select_cartela(room.id, "player-1", 1)
    room, card2 = await service.select_cartela(room.id, "player-1", 2)

    room = await service.release_cartela(room.id, "player-1", card_id=card1.id)
    assert room.taken_cartelas == [2]
    remaining = await service.cards.list_by_room_and_owner(room.id, "player-1")
    assert len(remaining) == 1
    assert remaining[0].id == card2.id


@pytest.mark.asyncio
async def test_both_cartelas_get_charged_at_round_start(service, wallet_service):
    # Override for testing
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    await service.select_cartela(room.id, "player-1", 2)

    started_room, dropped = await service.charge_and_start(room.id)
    assert dropped == []
    assert started_room.prize_pool == started_room.stake_amount * 2

    wallet = await wallet_service.get_or_create_wallet("player-1")
    assert wallet.balance == 1000 - started_room.stake_amount * 2


@pytest.mark.asyncio
async def test_second_winning_claim_does_not_win_the_lock(service, wallet_service):
    """Two players complete a pattern on the same number: the first claim settles BOTH (equal split) and the
    second claim is told the win is already confirmed - nobody is ever paid twice."""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    room, card1 = await service.select_cartela(room.id, "player-1", 1)
    room, card2 = await service.select_cartela(room.id, "player-2", 2)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    outcome1 = await service.claim_win(room.id, "player-1", card1.id)
    assert outcome1.is_valid is True
    assert outcome1.won is True

    outcome2 = await service.claim_win(room.id, "player-2", card2.id)
    assert outcome2.is_valid is True
    assert outcome2.won is False
    assert "already confirmed" in outcome2.message.lower()

    updated_room = await service.get_room(room.id)
    assert updated_room.state == "ended"
    assert updated_room.end_reason == "winner"
    assert sorted(w["player_id"] for w in updated_room.winners) == ["player-1", "player-2"]
    ratio = await service.get_payout_ratio()
    assert {w["payout"] for w in updated_room.winners} == {round(room.prize_pool * ratio) // 2}


@pytest.mark.asyncio
async def test_duplicate_claim_from_same_winner_gets_friendly_message(service):
    # Override for testing
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    """A double-tap / duplicate auto-bingo call from the SAME player+card
    that already won should not be treated as "someone else" winning."""
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    outcome1 = await service.claim_win(room.id, "player-1", card.id)
    assert outcome1.won is True

    outcome2 = await service.claim_win(room.id, "player-1", card.id)
    assert outcome2.won is False
    assert "already confirmed" in outcome2.message.lower()


@pytest.mark.asyncio
async def test_claim_win_pays_out_and_ends_the_round_atomically(service, wallet_service):
    """The whole point of the rewrite: there is no in-between state at
    all. The instant `claim_win` returns with won=True, the wallet is
    already credited AND the room is already "ended" - nothing for a
    poller to ever catch halfway."""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    wallet_before = await wallet_service.get_or_create_wallet("player-1")
    total_before = wallet_before.balance + wallet_before.bonus_balance

    outcome = await service.claim_win(room.id, "player-1", card.id)
    assert outcome.won is True

    # Payout lands in `balance` (real, withdrawable) or `bonus_balance`
    # depending on which pool funded the winning card's stake - either way
    # the player's total playable funds must grow by exactly the payout,
    # and it must already be true by the time claim_win returns.
    wallet_after = await wallet_service.get_or_create_wallet("player-1")
    total_after = wallet_after.balance + wallet_after.bonus_balance
    assert total_after == total_before + outcome.payout

    ended_room = await service.get_room(room.id)
    assert ended_room.state == "ended"
    assert ended_room.winners[0]["payout"] == outcome.payout


@pytest.mark.asyncio
async def test_ended_round_stays_visible_until_explicitly_advanced(service):
    # Override for testing
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    """A poll/reload landing right after a round ends (or the app being
    backgrounded and resumed in the middle of it, which happens
    constantly in Telegram's WebView) must NOT spawn a brand new room and
    orphan the results screen for anyone - only an explicit advance_round
    call (the "Go to Lobby" button) may replace it."""
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    await service.claim_win(room.id, "player-1", card.id)
    ended_room = await service.get_room(room.id)
    assert ended_room.state == "ended"

    # Simulate several different clients polling/reloading before anyone
    # clicks "Go to Lobby" - every single one must see the SAME ended room.
    for _ in range(5):
        same_room = await service.get_or_create_waiting_session()
        assert same_room.id == room.id
        assert same_room.state == "ended"

    next_room = await service.advance_round(room.id)
    assert next_room.id != room.id
    assert next_room.state == "waiting"

    # And now everyone else's subsequent lookups land on the new round.
    for _ in range(3):
        current = await service.get_or_create_waiting_session()
        assert current.id == next_room.id


@pytest.mark.asyncio
async def test_advance_round_is_idempotent_across_multiple_players(service):
    """Every player closes their own results screen independently - the
    first advance_round call must create the next round, and every later
    call (from other players closing theirs) must just hand back that
    same new round instead of creating yet another one."""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []
    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break
    await service.claim_win(room.id, "player-1", card.id)

    first = await service.advance_round(room.id)
    second = await service.advance_round(room.id)
    third = await service.advance_round(room.id)
    assert first.id == second.id == third.id
    assert first.state == "waiting"


@pytest.mark.asyncio
async def test_claim_missing_card_raises(service):
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    await service.charge_and_start(room.id)
    with pytest.raises(RoomNotFoundError):
        await service.claim_win(room.id, "player-1", "does-not-exist")


@pytest.mark.asyncio
async def test_countdown_started_at_tracks_first_join(service):
    # Override for testing
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    """Needed so the server can accurately resume a countdown after a
    restart instead of always giving a fresh 30s (or leaving it stuck)."""
    room = await service.get_or_create_waiting_session()
    assert room.countdown_started_at is None

    room, _ = await service.select_cartela(room.id, "player-1", 1)
    assert room.countdown_started_at is not None
    first_stamp = room.countdown_started_at

    # A second player joining the same round doesn't reset the clock.
    room, _ = await service.select_cartela(room.id, "player-2", 2)
    assert room.countdown_started_at == first_stamp


@pytest.mark.asyncio
async def test_countdown_started_at_clears_when_everyone_leaves(service):
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    room = await service.release_cartela(room.id, "player-1")
    assert room.taken_cartelas == []

    room = await service.get_room(room.id)
    assert room.countdown_started_at is None


@pytest.mark.asyncio
async def test_countdown_started_at_clears_on_cancel(service, wallet_service):
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    await service.select_cartela(room.id, "player-1", 1)
    await wallet_service.wallet_repo.update("player-1", {"balance": 0})
    await service.charge_and_start(room.id)  # nobody can pay -> cancel_round

    room = await service.get_room(room.id)
    assert room.state == "waiting"
    assert room.countdown_started_at is None


@pytest.mark.asyncio
async def test_bonus_balance_spent_before_real_balance(service, wallet_service):
    # Override for testing
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    """Bonus credit takes priority so deposited funds remain withdrawable."""
    await wallet_service.get_or_create_wallet("player-1")
    await wallet_service.wallet_repo.update("player-1", {"balance": 1000, "bonus_balance": 50})

    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    wallet = await wallet_service.get_or_create_wallet("player-1")
    assert wallet.bonus_balance == 50 - room.stake_amount
    assert wallet.balance == 1000

    stored_card = await service.cards.get(card.id)
    assert stored_card.funded_by == "bonus"

    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    is_valid, _, payout = await claim_and_finalize(service, room.id, "player-1", card.id)
    assert is_valid is True

    wallet = await wallet_service.get_or_create_wallet("player-1")
    assert wallet.balance == 1000 - room.stake_amount + payout
    assert wallet.won_balance == payout
    assert wallet.bonus_balance == 50


@pytest.mark.asyncio
async def test_end_round_without_winner_moves_no_money(service, wallet_service):
    """Pool-exhausted / inactivity endings must end the round with an
    empty winners list, refund every charged stake back to whichever pool
    it came from (a round nobody won must cost nobody anything), and
    still require an explicit advance like any other ended round."""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    wallet_pre_stake = await wallet_service.get_or_create_wallet("player-1")
    total_pre_stake = wallet_pre_stake.balance + wallet_pre_stake.bonus_balance

    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    wallet_after_charge = await wallet_service.get_or_create_wallet("player-1")
    assert wallet_after_charge.balance + wallet_after_charge.bonus_balance == total_pre_stake - room.stake_amount

    ended_room = await service.end_round_without_winner(room.id, "no_winner_pool_exhausted")
    assert ended_room.state == "ended"
    assert ended_room.end_reason == "no_winner_pool_exhausted"
    assert ended_room.winners == []

    # Fully refunded - back to exactly the pre-stake total, in the same
    # pool (bonus vs real) it was originally charged from.
    wallet_after = await wallet_service.get_or_create_wallet("player-1")
    assert wallet_after.balance + wallet_after.bonus_balance == total_pre_stake
    assert wallet_after.balance == wallet_pre_stake.balance
    assert wallet_after.bonus_balance == wallet_pre_stake.bonus_balance

    # Refunding twice must never happen - end_round_without_winner is a
    # no-op once the round is already "ended".
    await service.end_round_without_winner(room.id, "no_winner_pool_exhausted")
    wallet_final = await wallet_service.get_or_create_wallet("player-1")
    assert wallet_final.balance == wallet_after.balance
    assert wallet_final.bonus_balance == wallet_after.bonus_balance

    # Still requires an explicit advance, exactly like a round with a winner.
    same_room = await service.get_or_create_waiting_session()
    assert same_room.id == room.id
    next_room = await service.advance_round(room.id)
    assert next_room.id != room.id
    assert next_room.state == "waiting"


@pytest.mark.asyncio
async def test_cancelled_round_auto_rotates_without_advance(service, wallet_service):
    """A round cancelled before it even started (not enough players ever
    joined) has nothing to show anyone - it should never require an
    explicit advance, unlike a real ended round."""
    await service.settings_repo.update({"min_cartelas_to_start": 2})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)

    started_room, dropped = await service.charge_and_start(room.id)
    assert started_room is None

    cancelled = await service.get_room(room.id)
    assert cancelled.state == "waiting"

    # No advance needed - the very next lookup should already be fresh.
    next_room = await service.get_or_create_waiting_session()
    assert next_room.id == room.id
    assert next_room.state == "waiting"


@pytest.mark.asyncio
async def test_winner_grid_is_stored_as_normal_nested_grid(service, wallet_service):
    """Post-Supabase-migration: the winning card's grid is stored as a
    normal nested 5x5 array directly on the winner record. (Historically,
    on Firestore, this had to be flattened to a single 25-entry array
    since Firestore disallows an array directly containing another array
    - Postgres JSONB has no such restriction, so that workaround is gone.)"""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []
    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    outcome = await service.claim_win(room.id, "player-1", card.id)
    assert outcome.won is True

    ended_room = await service.get_room(room.id)
    winner = ended_room.winners[0]
    assert "grid_flat" not in winner
    grid = winner["grid"]
    assert isinstance(grid, list)
    assert len(grid) == 5
    for row in grid:
        assert isinstance(row, list)
        assert len(row) == 5
        for cell in row:
            assert isinstance(cell, dict)


@pytest.mark.asyncio
async def test_pool_exhausted_never_ends_the_round(service, wallet_service):
    """Base rule: a round only ever ends because a player won it. Once
    every number has been drawn with nobody having claimed yet, the round
    must stay 'running' (not silently pop a "round over, no winner"
    screen) - and since every cell on every card is now guaranteed to
    have been called, the very next claim must succeed."""
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    room, card = await service.select_cartela(room.id, "player-1", 1)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []

    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break

    # Pool is exhausted - the round must still be "running", exactly as
    # if nothing happened. No auto-ending, no "no winner" state anywhere.
    still_running = await service.get_room(room.id)
    assert still_running.state == "running"
    assert still_running.winners == []
    assert still_running.end_reason is None

    # A late claim now must succeed - every cell has necessarily been
    # called by this point (75-ball bingo, same 1-75 range as the cards).
    outcome = await service.claim_win(room.id, "player-1", card.id)
    assert outcome.is_valid is True
    assert outcome.won is True

    ended_room = await service.get_room(room.id)
    assert ended_room.state == "ended"
    assert ended_room.end_reason == "winner"


# ---------------------------------------------------------------------------
# Multi-winner rounds (more than settings.multi_winner_min_players players)
# ---------------------------------------------------------------------------

async def _start_round_with_players(service, n_players):
    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    cards = []
    for i in range(1, n_players + 1):
        room, card = await service.select_cartela(room.id, f"player-{i}", i)
        cards.append(card)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []
    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break
    return room, cards


@pytest.mark.asyncio
async def test_ten_players_or_fewer_also_split_when_two_complete_on_the_same_number(service, wallet_service):
    """Small rounds follow the same rule as big ones: simultaneous winners share the prize equally."""
    room, cards = await _start_round(service, 10, winnable=[0, 1])
    ended = await _draw_until_settled(service, room.id)
    assert sorted(w["player_id"] for w in ended.winners) == ["player-1", "player-2"]
    ratio = await service.get_payout_ratio()
    assert {w["payout"] for w in ended.winners} == {round(room.prize_pool * ratio) // 2}


@pytest.mark.asyncio
async def test_small_round_single_winner_gets_everything(service, wallet_service):
    room, cards = await _start_round(service, 3, winnable=[0])
    ended = await _draw_until_settled(service, room.id)
    ratio = await service.get_payout_ratio()
    assert [w["player_id"] for w in ended.winners] == ["player-1"]
    assert ended.winners[0]["payout"] == round(room.prize_pool * ratio)


def _unwinnable(grid):
    """Same shape, but every cell holds a number that is never called and nothing is free."""
    return [[{**cell, "value": 1000 + r * 5 + c, "free": False, "marked": False}
             for c, cell in enumerate(row)] for r, row in enumerate(grid)]


async def _start_round(service, n_players, winnable):
    """Start a round (nothing drawn yet). Cards at indexes in `winnable` get IDENTICAL real grids
    (so they complete on the very same number); every other card can never complete."""
    import copy

    await service.settings_repo.update({"min_cartelas_to_start": 1})
    room = await service.get_or_create_waiting_session()
    cards = []
    for i in range(1, n_players + 1):
        room, card = await service.select_cartela(room.id, f"player-{i}", i)
        cards.append(card)
    room, dropped = await service.charge_and_start(room.id)
    assert dropped == []
    template = copy.deepcopy(service.cards.cards[cards[winnable[0]].id].grid)
    for i, card in enumerate(cards):
        stored = service.cards.cards[card.id]
        stored.grid = copy.deepcopy(template) if i in winnable else _unwinnable(template)
    return room, cards


async def _draw_until_settled(service, room_id):
    """What the server draw loop does: draw a number, then settle; stop once the round ends."""
    for _ in range(75):
        number, _seq = await service.draw_number(room_id)
        assert number is not None
        ended = await service.settle_round_winners(room_id)
        if ended is not None:
            return ended
    raise AssertionError("round never ended")


@pytest.mark.asyncio
async def test_more_than_ten_players_all_winners_on_the_same_number_share_equally(service, wallet_service):
    room, cards = await _start_round(service, 11, winnable=[0, 1, 2])
    ended = await _draw_until_settled(service, room.id)

    ratio = await service.get_payout_ratio()
    total = round(room.prize_pool * ratio)
    share = total // 3
    assert ended.state == "ended" and ended.end_reason == "winner"
    assert sorted(w["player_id"] for w in ended.winners) == ["player-1", "player-2", "player-3"]
    assert {w["payout"] for w in ended.winners} == {share}
    assert {w["winning_number"] for w in ended.winners} == {ended.called_numbers[-1]}
    assert all(w["paid"] for w in ended.winners)
    # nobody pressed claim: winners are paid anyway, losers are untouched
    for pid in ("player-1", "player-2", "player-3"):
        w = await wallet_service.get_or_create_wallet(pid)
        assert w.balance + w.bonus_balance == 1000 - room.stake_amount + share
    loser = await wallet_service.get_or_create_wallet("player-4")
    assert loser.balance == 1000 - room.stake_amount
    assert share * 3 <= total  # remainder from rounding down stays with the house


@pytest.mark.asyncio
async def test_a_single_card_completing_alone_wins_the_whole_prize(service, wallet_service):
    room, cards = await _start_round(service, 11, winnable=[4])
    ended = await _draw_until_settled(service, room.id)
    ratio = await service.get_payout_ratio()
    assert [w["player_id"] for w in ended.winners] == ["player-5"]
    assert ended.winners[0]["payout"] == round(room.prize_pool * ratio)
    assert ended.winners[0]["shared_with"] == 1


@pytest.mark.asyncio
async def test_two_winning_cartelas_of_one_player_still_get_one_share(service, wallet_service):
    room, cards = await _start_round(service, 11, winnable=[0, 1, 2])
    service.cards.cards[cards[2].id].owner_id = "player-1"  # player-1 now holds two winning cartelas
    ended = await _draw_until_settled(service, room.id)
    assert sorted(w["player_id"] for w in ended.winners) == ["player-1", "player-2"]
    ratio = await service.get_payout_ratio()
    assert {w["payout"] for w in ended.winners} == {round(room.prize_pool * ratio) // 2}


@pytest.mark.asyncio
async def test_manual_claim_settles_everyone_and_never_pays_twice(service, wallet_service):
    room, cards = await _start_round(service, 11, winnable=[0, 1, 2])
    while True:  # draw WITHOUT settling, like a player who claims before the server loop ticks
        number, _ = await service.draw_number(room.id)
        r = await service.get_room(room.id)
        from src.services.game_service import PatternVerifier

        grid = PatternVerifier.compute_effective_grid(service.cards.cards[cards[1].id].grid, r.called_numbers)
        if PatternVerifier.verify_pattern(grid, r.pattern.get("id", "any_line"), r.variant):
            break
    outcome = await service.claim_win(room.id, "player-2", cards[1].id)
    assert outcome.won is True and outcome.payout > 0

    ended = await service.get_room(room.id)
    assert ended.state == "ended" and len(ended.winners) == 3  # player-1 and player-3 never claimed
    share = outcome.payout
    for pid in ("player-1", "player-2", "player-3"):
        w = await wallet_service.get_or_create_wallet(pid)
        assert w.balance + w.bonus_balance == 1000 - room.stake_amount + share

    # a duplicate claim, a second settle, or a recovery sweep never pays anyone again
    duplicate = await service.claim_win(room.id, "player-2", cards[1].id)
    assert duplicate.is_valid is True and duplicate.won is False
    assert await service.settle_round_winners(room.id) is None
    await service.pay_unpaid_winners(room.id)
    w2 = await wallet_service.get_or_create_wallet("player-2")
    assert w2.balance + w2.bonus_balance == 1000 - room.stake_amount + share


@pytest.mark.asyncio
async def test_pay_unpaid_winners_recovers_a_crashed_payout_exactly_once(service, wallet_service):
    room, cards = await _start_round(service, 11, winnable=[0, 1])
    ended = await _draw_until_settled(service, room.id)
    share = ended.winners[0]["payout"]
    # simulate a crash before player-2 was paid
    stored = await service.get_room(room.id)
    stored.winners[1]["paid"] = False
    w2 = await wallet_service.get_or_create_wallet("player-2")
    w2.balance -= share
    await service.rooms.update(room.id, {"winners": stored.winners})
    await service.pay_unpaid_winners(room.id)
    await service.pay_unpaid_winners(room.id)
    w2 = await wallet_service.get_or_create_wallet("player-2")
    assert w2.balance + w2.bonus_balance == 1000 - room.stake_amount + share


@pytest.mark.asyncio
async def test_settle_does_nothing_until_a_card_completes(service):
    room, cards = await _start_round(service, 11, winnable=[0])
    assert await service.settle_round_winners(room.id) is None
    assert (await service.get_room(room.id)).state == "running"


@pytest.mark.asyncio
async def test_round_start_reports_the_charged_players_to_the_engagement_hook(service):
    seen = []

    async def hook(player_ids):
        seen.append(set(player_ids))

    service.on_round_started = hook
    await _start_round(service, 3, winnable=[0])
    await asyncio.sleep(0.01)
    assert seen == [{"player-1", "player-2", "player-3"}]


@pytest.mark.asyncio
async def test_a_failing_engagement_hook_never_affects_the_round(service):
    async def hook(player_ids):
        raise RuntimeError("engagement is down")

    service.on_round_started = hook
    room, _cards = await _start_round(service, 3, winnable=[0])
    await asyncio.sleep(0.01)
    assert (await service.get_room(room.id)).state == "running"


@pytest.mark.asyncio
async def test_countdown_starts_at_min_cartelas_even_for_one_player(service):
    await service.settings_repo.update({"min_cartelas_to_start": 3})
    room = await service.get_or_create_waiting_session()
    assert room.min_cartelas_to_start == 3
    room, _ = await service.select_cartela(room.id, "player-1", 1)
    room, _ = await service.select_cartela(room.id, "player-1", 2)
    assert room.countdown_started_at is None  # 2 cartelas: not enough yet

    room, _ = await service.select_cartela(room.id, "player-1", 3)
    assert room.countdown_started_at is not None  # 3 cartelas, 1 player: clock starts

    started, dropped = await service.charge_and_start(room.id)
    assert dropped == []
    assert started is not None and started.state == "running"
    assert len(started.taken_cartelas) == 3


@pytest.mark.asyncio
async def test_releasing_below_minimum_stops_the_countdown(service):
    await service.settings_repo.update({"min_cartelas_to_start": 3})
    room = await service.get_or_create_waiting_session()
    for n in (1, 2):
        room, _ = await service.select_cartela(room.id, "player-1", n)
    room, card3 = await service.select_cartela(room.id, "player-1", 3)
    assert room.countdown_started_at is not None

    room = await service.release_cartela(room.id, "player-1", card_id=card3.id)
    assert room.countdown_started_at is None


@pytest.mark.asyncio
async def test_player_can_hold_up_to_four_cartelas(service):
    room = await service.get_or_create_waiting_session()
    for n in (1, 2, 3, 4):
        room, _ = await service.select_cartela(room.id, "player-1", n)
    with pytest.raises(TooManyCartelasError):
        await service.select_cartela(room.id, "player-1", 5)


@pytest.mark.asyncio
async def test_cartela_limit_is_capped_at_four_even_if_room_allows_more(service):
    """Admin/DB values above 4 must never let a player hold more than 4 cartelas."""
    room = await service.get_or_create_waiting_session()
    await service.rooms.update(room.id, {"cards_per_player": 10})
    for n in (1, 2, 3, 4):
        room, _ = await service.select_cartela(room.id, "player-1", n)
    with pytest.raises(TooManyCartelasError):
        await service.select_cartela(room.id, "player-1", 5)


@pytest.mark.asyncio
async def test_claim_with_someone_elses_card_or_another_rounds_card_is_rejected(service, wallet_service):
    """A win can only be claimed with a card the claimer holds in THIS round (no stealing the pool)."""
    await service.settings_repo.update({"min_cartelas_to_start": 2})
    room = await service.get_or_create_waiting_session()
    room, card1 = await service.select_cartela(room.id, "player-1", 1)
    room, card2 = await service.select_cartela(room.id, "player-2", 2)
    room, _ = await service.charge_and_start(room.id)
    while True:
        number, _ = await service.draw_number(room.id)
        if number is None:
            break
    # every number is called, so every card is complete - but player-3 holds no card here,
    # and player-2 must not be able to claim with player-1's card.
    outcome = await service.claim_win(room.id, "player-3", card1.id)
    assert outcome.is_valid is False and outcome.won is False
    outcome = await service.claim_win(room.id, "player-2", card1.id)
    assert outcome.is_valid is False and outcome.won is False
    assert (await service.get_room(room.id)).state == "running"
    ok = await service.claim_win(room.id, "player-1", card1.id)
    assert ok.won is True
