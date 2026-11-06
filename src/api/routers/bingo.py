"""
Bingo session API endpoints and WebSocket connection.
"""
import asyncio
import base64
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header, HTTPException, WebSocket, WebSocketDisconnect

from src.api.idempotency import run_idempotent
from src.core.rate_limit import enforce as enforce_rate_limit
from src.core.telegram_auth import extract_telegram_id
from src.repositories import PlayerRepository
from src.services import (
    CartelaTakenError,
    InsufficientBalanceError,
    InvalidStateError,
    RoomNotFoundError,
    RoomService,
    TooManyCartelasError,
)
from src.api.dependencies import (
    _active_bot_fills,
    _active_lobby_timers,
    _reject_if_banned,
    get_control_service,
    _require_player_identity,
    _reshape_winners,
    get_bot_service,
    get_room_service,
    manager,
)

from src.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bingo"])


async def _room_summary(room, player_count: int = 0) -> dict:
    payout_ratio = await get_room_service().get_payout_ratio()
    real_prize = round(room.prize_pool * payout_ratio)
    winners = _reshape_winners(room.winners)
    missing_name_ids = [w.get("player_id") for w in winners if w.get("player_id") and not w.get("player_name")]
    if missing_name_ids:
        players = await PlayerRepository().list_by_ids(missing_name_ids)
        names = {player.id: player.display_name for player in players}
        for winner in winners:
            if not winner.get("player_name"):
                winner["player_name"] = names.get(winner.get("player_id"), winner.get("player_id", "Player"))
    return {
        "id": room.id,
        "state": room.state,
        "variant": room.variant,
        "pattern": room.pattern.get("id"),
        "called_numbers": room.called_numbers,
        "winners": winners,
        "end_reason": getattr(room, "end_reason", None),
        "advanced": getattr(room, "advanced", False),
        "auto_next_round_seconds": settings.auto_next_round_seconds,
        "ended_at": room.updated_at.isoformat() if room.state == "ended" and getattr(room, "updated_at", None) else None,
        "stake_amount": room.stake_amount,
        "prize_pool": real_prize,
        # While the lobby is open nothing has been charged yet, so show what the winners' pot would be right now.
        "estimated_prize": real_prize if room.state != "waiting" else round(room.stake_amount * len(room.taken_cartelas or []) * payout_ratio),
        "taken_cartelas": room.taken_cartelas,
        "min_players_to_start": room.min_players_to_start,
        "min_cartelas_to_start": getattr(room, "min_cartelas_to_start", 2),
        "countdown_seconds": room.countdown_seconds,
        "lobby_lock_seconds": RoomService.LOBBY_LOCK_SECONDS,
        "countdown_started_at": room.countdown_started_at.isoformat() if getattr(room, "countdown_started_at", None) else None,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "draw_interval": room.draw_interval,
        "cards_per_player": room.cards_per_player,
        "cartela_min": getattr(room, "cartela_min", 1),
        "cartela_max": getattr(room, "cartela_max", 500),
        "players": [],
        "player_count": player_count,
    }


async def _reject_if_play_blocked(room_id: str) -> None:
    """Control Centre: maintenance/kill switch/flag/paused game stop NEW joins (running rounds finish)."""
    control = get_control_service()
    if not await control.any_play_restriction():
        return
    try:
        track = (await get_room_service().get_room(room_id)).track
    except RoomNotFoundError:
        return  # the normal handler reports the missing room
    message = await control.track_block(track)
    if message:
        raise HTTPException(status_code=503, detail=message)


async def _player_count_in_room(room_id: str) -> int:
    return await get_room_service().cards.count_players_by_room(room_id)


def start_bot_fill(room_id: str) -> None:
    """Start filling a waiting lobby with bots (once per room per process; one instance cluster-wide)."""
    if room_id in _active_bot_fills:
        return
    _active_bot_fills.add(room_id)

    async def _run_bot_fill() -> None:
        from src.core import loop_lock

        lock = f"botfill:{room_id}"
        held = False
        try:
            # With several containers, only one may fill a given room with bots.
            held = await loop_lock.try_acquire(lock, 300)
            if held:
                await get_bot_service().fill_room_with_bots(get_room_service(), room_id)
        finally:
            _active_bot_fills.discard(room_id)
            if held:
                await loop_lock.release(lock)

    asyncio.create_task(_run_bot_fill())


@router.get("/api/session")
async def get_session(track: str = "real_10"):
    if track not in RoomService.TRACKS:
        raise HTTPException(status_code=400, detail=f"Unknown track '{track}'")
    room = await get_room_service().get_or_create_waiting_session(track)
    if room.state == "waiting":
        start_bot_fill(room.id)
    player_count = await _player_count_in_room(room.id)
    return await _room_summary(room, player_count)


@router.get("/api/lobby")
async def get_lobby():
    room_service = get_room_service()

    async def _track_summary(track: str):
        room = await room_service.get_or_create_waiting_session(track)
        player_count = await _player_count_in_room(room.id)
        return await _room_summary(room, player_count)

    summaries = await asyncio.gather(*(_track_summary(t) for t in RoomService.TRACKS))
    return {track: summary for track, summary in zip(RoomService.TRACKS, summaries)}


@router.get("/api/session/{room_id}")
async def get_session_by_id(room_id: str):
    try:
        room = await get_room_service().get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    player_count = await _player_count_in_room(room_id)
    return await _room_summary(room, player_count)


@router.get("/api/session/{room_id}/my-card")
async def get_my_card(
    room_id: str,
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    cards = await get_room_service().cards.list_by_room_and_owner(room_id, player_id)
    if not cards:
        return {"card": None}
    card = cards[0]
    return {"card": {"id": card.id, "grid": card.grid, "variant": card.variant, "cartela_number": card.cartela_number}}


@router.get("/api/session/{room_id}/cartela-preview/{cartela_number}")
async def cartela_preview(
    room_id: str,
    cartela_number: int,
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    """The exact card a cartela number will give the player - shown BEFORE
    they reserve it. Read-only: nothing is reserved or charged here."""
    await _require_player_identity(player_id, x_telegram_init_data)
    try:
        room = await get_room_service().get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    if not (room.cartela_min <= cartela_number <= room.cartela_max):
        raise HTTPException(status_code=400, detail="Cartela number out of range")
    from src.services.game_service import CardGenerator
    grid = CardGenerator.generate_card(
        room.variant, cartela_number=cartela_number,
        cartela_min=room.cartela_min, cartela_max=room.cartela_max,
    )
    return {
        "card": {"id": None, "grid": grid, "variant": room.variant, "cartela_number": cartela_number},
        "taken": cartela_number in room.taken_cartelas,
    }


@router.get("/api/session/{room_id}/my-cards")
async def get_my_cards(
    room_id: str,
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    cards = await get_room_service().cards.list_by_room_and_owner(room_id, player_id)
    return {
        "cards": [
            {"id": c.id, "grid": c.grid, "variant": c.variant, "cartela_number": c.cartela_number}
            for c in cards
        ]
    }


@router.get("/api/session/{room_id}/card/{card_id}")
async def get_room_card(room_id: str, card_id: str):
    card = await get_room_service().cards.get(card_id)
    if not card or card.room_id != room_id:
        raise HTTPException(status_code=404, detail="Card not found")
    return {
        "id": card.id,
        "grid": card.grid,
        "variant": card.variant,
        "cartela_number": card.cartela_number,
    }


@router.get("/api/my-round/{player_id}")
async def my_running_round(player_id: str, x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    """The round this player is in right now (they hold cartelas in a running game), so the hub can offer 'Return'."""
    await _require_player_identity(player_id, x_telegram_init_data)
    room = await get_room_service().rooms.running_for_player(player_id)
    if room is None:
        return {"round": None}
    mine = await get_room_service().cards.list_by_room_and_owner(room.id, player_id)
    return {"round": {"room_id": room.id, "cartelas": len(mine), "called": len(room.called_numbers or []),
                      "stake_amount": room.stake_amount}}


@router.get("/api/session/recent")
async def recent_games(limit: int = 10):
    rooms = await get_room_service().rooms.list_recent(limit=limit)
    out = []
    for r in rooms:
        out.append({
            "id": r.id,
            "state": r.state,
            "started_at": r.created_at.isoformat() if r.created_at else None,
            "ended_at": r.updated_at.isoformat() if getattr(r, "updated_at", None) else None,
            "winners": _reshape_winners(r.winners or []),
            "prize_pool": r.prize_pool or 0,
            "stake_amount": r.stake_amount or 0,
        })
    return {"games": out}


@router.post("/api/session/{room_id}/select-cartela")
async def select_cartela(
    room_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    player_id = payload.get("player_id")
    cartela_number = payload.get("cartela_number")
    if not player_id or cartela_number is None:
        raise HTTPException(status_code=400, detail="player_id and cartela_number are required")
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    await _reject_if_play_blocked(room_id)
    enforce_rate_limit("select_cartela", player_id)

    try:
        cartela_number = int(cartela_number)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="cartela_number must be an integer")

    try:
        room, card = await get_room_service().select_cartela(room_id, player_id, cartela_number)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except InvalidStateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except CartelaTakenError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TooManyCartelasError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InsufficientBalanceError:
        try:
            failed_room = await get_room_service().get_room(room_id)
            track = failed_room.track
        except RoomNotFoundError:
            track = "real_10"
        if track == "bonus":
            raise HTTPException(
                status_code=402,
                detail="Not enough bonus balance to join. Earn bonus credit through referrals or promotions, then come back.",
            )
        raise HTTPException(status_code=402, detail="Insufficient balance - please deposit to join")

    asyncio.create_task(_broadcast_cartela_picked(room_id, player_id, cartela_number, room.taken_cartelas))

    # The 30s countdown only exists once the lobby holds the minimum number
    # of cartelas (room.countdown_started_at is set by select_cartela).
    if room.countdown_started_at and room_id not in _active_lobby_timers:
        _active_lobby_timers.add(room_id)
        from src.api.main import lobby_countdown_task
        asyncio.create_task(lobby_countdown_task(room_id))
        asyncio.create_task(_nudge_new_lobby(room_id))

    return {
        "message": "Cartela reserved",
        "card": {"id": card.id, "grid": card.grid, "variant": card.variant, "cartela_number": card.cartela_number},
        "room": await _room_summary(room),
    }


async def _nudge_new_lobby(room_id: str) -> None:
    """A lobby just opened: invite recent players who are not in it (bounded, opt-out aware)."""
    try:
        from src.core.config import settings
        from src.services.engagement_service import get_engagement_service

        room = await get_room_service().get_room(room_id)
        owners = sorted({c.owner_id for c in await get_room_service().cards.list_by_room(room_id)})
        await get_engagement_service().nudge_round_starting(
            room.track, room.stake_amount, owners, settings.lobby_countdown_seconds
        )
    except Exception:  # noqa: BLE001 - a nudge problem must never affect joining a round
        logger.exception("Lobby nudge failed for room %s", room_id)


async def _broadcast_cartela_picked(room_id: str, player_id: str, cartela_number: int, taken_cartelas: list):
    try:
        player = await PlayerRepository().get_by_id(player_id)
        await manager.broadcast(room_id, {
            "type": "player_joined",
            "player": {"id": player_id, "name": player.display_name if player else "Player", "cartela_number": cartela_number},
            "taken_cartelas": taken_cartelas,
        })
    except Exception:
        logger.exception("Failed to broadcast cartela pick for room %s", room_id)


@router.post("/api/session/{room_id}/rematch")
async def rematch(
    room_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    """One-tap "Play again": move on from this finished round and reserve the SAME cartela numbers
    the player just had in the next round, as far as they are still free and affordable. Uses the
    normal select-cartela path for each number, so every existing rule (taken, limits, balance)
    still applies. Anything that could not be reserved is reported, never silently dropped."""
    player_id = payload.get("player_id")
    if not player_id:
        raise HTTPException(status_code=400, detail="player_id is required")
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    await _reject_if_play_blocked(room_id)
    enforce_rate_limit("rematch", player_id)

    service = get_room_service()
    try:
        old = await service.get_room(room_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    if old.state not in ("ended", "cancelled"):
        raise HTTPException(status_code=400, detail="This round is still in progress")
    previous = sorted({c.cartela_number for c in await service.cards.list_by_room(room_id) if c.owner_id == player_id})

    next_room = await service.advance_round(room_id)
    reserved: list[int] = []
    unavailable: list[dict] = []
    for number in previous:
        try:
            next_room, _card = await service.select_cartela(next_room.id, player_id, number)
            reserved.append(number)
        except CartelaTakenError:
            unavailable.append({"cartela_number": number, "reason": "taken"})
        except TooManyCartelasError:
            unavailable.append({"cartela_number": number, "reason": "limit"})
        except InsufficientBalanceError:
            unavailable.append({"cartela_number": number, "reason": "balance"})
            break  # no point trying the rest
        except InvalidStateError:
            unavailable.append({"cartela_number": number, "reason": "round_started"})
            break
    if reserved and next_room.countdown_started_at and next_room.id not in _active_lobby_timers:
        _active_lobby_timers.add(next_room.id)
        from src.api.main import lobby_countdown_task
        asyncio.create_task(lobby_countdown_task(next_room.id))
        asyncio.create_task(_nudge_new_lobby(next_room.id))
    return {"room": await _room_summary(next_room), "reserved": reserved, "unavailable": unavailable}


@router.post("/api/session/{room_id}/release-cartela")
async def release_cartela(
    room_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    player_id = payload.get("player_id")
    card_id = payload.get("card_id")
    if not player_id:
        raise HTTPException(status_code=400, detail="player_id is required")
    await _require_player_identity(player_id, x_telegram_init_data)
    try:
        room = await get_room_service().release_cartela(room_id, player_id, card_id=card_id)
    except RoomNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except InvalidStateError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await manager.broadcast(room_id, {"type": "player_left", "player_id": player_id, "taken_cartelas": room.taken_cartelas})
    return {"room": await _room_summary(room)}


@router.post("/api/session/{room_id}/claim")
async def claim_bingo(
    room_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    player_id = payload.get("player_id")
    card_id = payload.get("card_id")
    if not player_id or not card_id:
        raise HTTPException(status_code=400, detail="player_id and card_id are required")
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)

    enforce_rate_limit("claim", player_id)

    async def _process_claim() -> dict:
        player = await PlayerRepository().get_by_id(player_id)
        player_name = player.display_name if player else player_id

        try:
            outcome = await get_room_service().claim_win(room_id, player_id, card_id, player_name=player_name)
        except RoomNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        if not outcome.is_valid or not outcome.won:
            return {"valid": False, "message": outcome.message, "payout": 0}

        pattern_name = outcome.winner.get("pattern", "a winning pattern") if outcome.winner else "a winning pattern"
        try:
            pattern_map = {
                "any_line": "a Line, Diagonal, or 4 Corners",
                "any_line_or_corners": "a Line, Diagonal, or 4 Corners",
                "horizontal_line": "a Horizontal Line",
                "vertical_line": "a Vertical Line",
                "diagonal": "a Diagonal Line",
                "four_corners": "4 Corners",
                "full_house": "a Full House",
            }
            pattern_id = outcome.winner["pattern"]
            pattern_name = pattern_map.get(pattern_id, pattern_id.replace("_", " ").title())

            await manager.broadcast(room_id, {
                "type": "winner_announced",
                "player_id": player_id,
                "player_name": player_name,
                "card": {"cartela_number": outcome.card.cartela_number, "grid": outcome.card.grid},
                "pattern_name": pattern_name,
                "payout": outcome.payout,
                "prize_pool": outcome.winner["prize_pool"],
            })
        except Exception:
            logger.exception(
                "Post-win broadcast/formatting failed for room %s (payout already committed, not affected)",
                room_id,
            )

        return {
            "valid": True,
            "message": "Bingo! Valid winning pattern.",
            "status": "accepted",
            "payout": outcome.payout,
            "prize_pool": outcome.winner.get("prize_pool", 0) if outcome.winner else 0,
        }

    # Only a VALID (accepted) claim is pinned to the key; an invalid one may succeed on retry.
    return await run_idempotent(
        player_id, f"claim:{room_id}", idempotency_key, payload, _process_claim,
        cacheable=lambda r: bool(r.get("valid")),
    )


@router.post("/api/session/{room_id}/advance")
async def advance_round(
    room_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    player_id = payload.get("player_id")
    if not player_id:
        raise HTTPException(status_code=400, detail="player_id is required")
    await _require_player_identity(player_id, x_telegram_init_data)
    try:
        await get_room_service().get_room(room_id)
    except RoomNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    next_room = await get_room_service().advance_round(room_id)
    return {"room": await _room_summary(next_room)}


@router.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    protocol = websocket.headers.get("sec-websocket-protocol", "")
    encoded_init_data = next((item.strip()[3:] for item in protocol.split(",") if item.strip().startswith("tg.")), "")
    try:
        init_data = base64.urlsafe_b64decode(encoded_init_data + "=" * (-len(encoded_init_data) % 4)).decode()
    except (UnicodeDecodeError, ValueError):
        init_data = ""
    if not extract_telegram_id(init_data):
        await websocket.close(code=4003)
        return
    try:
        await get_room_service().get_room(room_id)
    except RoomNotFoundError:
        await websocket.close(code=4004)
        return
    await manager.connect(websocket, room_id)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id)
