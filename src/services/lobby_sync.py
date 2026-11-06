"""Push admin game-setting changes into the lobby that is already open.

Settings are read when a lobby is *created*, so without this an admin who changes the stake, countdown,
draw interval or minimum cartelas would see nothing happen until the next round. This applies them to
every waiting lobby right away, with one safety rule: values players have already committed to - the
stake, cartelas per player and the cartela range - only change while nobody holds a cartela yet.
Otherwise they are reported as `deferred` and apply from the next round.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# room field -> how to read it from (settings, track config)
_LIVE_FIELDS = ("draw_interval", "countdown_seconds", "min_cartelas_to_start")
_LOCKED_FIELDS = ("stake_amount", "cards_per_player", "cartela_min", "cartela_max")


def desired_room_values(settings, track_cfg: dict) -> dict:
    """What a waiting lobby should look like under the current settings."""
    return {
        "draw_interval": settings.draw_interval,
        "countdown_seconds": settings.lobby_countdown_seconds,
        "min_cartelas_to_start": settings.min_cartelas_to_start,
        "stake_amount": track_cfg["stake_amount"],
        "cards_per_player": settings.cards_per_player,
        "cartela_min": settings.cartela_min,
        "cartela_max": settings.cartela_max,
    }


def plan_room_update(room, desired: dict) -> tuple[dict, list[str]]:
    """Pure helper: (fields to write now, names deferred to the next round)."""
    fields: dict = {}
    deferred: list[str] = []
    empty = not (room.taken_cartelas or [])
    for name in _LIVE_FIELDS:
        if getattr(room, name, None) != desired[name]:
            fields[name] = desired[name]
    for name in _LOCKED_FIELDS:
        if getattr(room, name, None) != desired[name]:
            if empty:
                fields[name] = desired[name]
            else:
                deferred.append(name)
    return fields, deferred


async def sync_waiting_lobbies() -> dict:
    """Apply the current settings to every waiting lobby. Never raises - callers are saving settings."""
    result = {"rooms": 0, "applied": [], "deferred": []}
    try:
        from src.api.dependencies import _active_lobby_timers, get_room_service

        svc = get_room_service()
        settings = await svc.settings_repo.get_or_create()
        for stub in await svc.rooms.list_recent(limit=20, state_filter="waiting"):
            async with svc._exclusive_room(stub.id):
                room = await svc.get_room(stub.id)
                if room.state != "waiting":
                    continue
                desired = desired_room_values(settings, svc._track_settings(settings, room.track))
                fields, deferred = plan_room_update(room, desired)
                taken = len(room.taken_cartelas or [])
                minimum = desired["min_cartelas_to_start"]
                start_clock = False
                if taken >= minimum and not room.countdown_started_at:
                    fields["countdown_started_at"] = datetime.now(timezone.utc)   # enough cartelas now: clock starts
                    start_clock = True
                elif taken < minimum and room.countdown_started_at:
                    fields["countdown_started_at"] = None                          # raised above what is held: stop it
                if fields:
                    await svc.rooms.update(room.id, fields)
                    result["rooms"] += 1
                    result["applied"] += [k for k in fields if k != "countdown_started_at"]
                result["deferred"] += deferred
                if start_clock and room.id not in _active_lobby_timers:
                    from src.api.main import lobby_countdown_task

                    _active_lobby_timers.add(room.id)
                    asyncio.create_task(lobby_countdown_task(room.id))
        result["applied"] = sorted(set(result["applied"]))
        result["deferred"] = sorted(set(result["deferred"]))
    except Exception:  # noqa: BLE001 - the settings are already saved; this is best-effort
        logger.exception("Could not apply new settings to the open lobby")
    return result
