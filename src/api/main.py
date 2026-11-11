"""
FastAPI application for Melbete Bingo.

Serves the REST API, WebSocket real-time updates, and the Telegram Mini
App frontend. Routes are modularized under `src/api/routers/`.
"""
import asyncio
import logging
import os
import re
import secrets
from typing import Optional
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.core.config import settings
from src.core import loop_lock
from src.core.db import close_db, get_pool, init_db
from src.services import InvalidStateError, RoomNotFoundError, RoomService
from src.api.dependencies import (
    RECENT_WINNER_DEPRIORITIZE_SECONDS,
    _active_draw_loops,
    _active_lobby_timers,
    _require_player_identity,
    _reshape_winners,
    _resolve_telegram_id,
    get_admin_service,
    get_control_service,
    get_room_service,
    manager,
)
from src.api.routers import admin, agent, auth, bingo, control, notifications, wallet, bonus

logger = logging.getLogger(__name__)

# Re-exports for test backward-compatibility
__all__ = [
    "_require_player_identity",
    "_resolve_telegram_id",
    "_reshape_winners",
    "app",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("MOCK_DB") != "1":
        await init_db()
        try:
            await get_admin_service().ensure_super_admin_bootstrapped()
        except Exception:  # noqa: BLE001
            logger.exception("Startup admin bootstrap failed (database not reachable yet?)")
        try:
            await resume_active_room_if_needed()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to resume the active room after restart")
    logger.info("Melbete Bingo API starting up")
    background: list = []
    if os.environ.get("MOCK_DB") != "1":
        from src.services.engagement_service import engagement_loop
        from src.services.reconciliation import reconciliation_loop
        from src.services.agent_rewards import agent_rewards_loop
        from src.core.bus import bus

        await bus.start(manager.broadcast_local)  # no-op unless REDIS_URL is set

        background = [
            asyncio.create_task(engagement_loop()),        # win-back + streak reminders (one instance at a time)
            asyncio.create_task(loop_supervisor()),        # keeps round loops alive on whichever instance owns them
            asyncio.create_task(round_cycler()),           # non-stop rounds: a finished round is followed by a new one
            asyncio.create_task(reconciliation_loop()),    # daily wallet reconciliation report
            asyncio.create_task(agent_rewards_loop()),     # agent tiers + held reward payouts (one instance at a time)
        ]
    yield
    if os.environ.get("MOCK_DB") != "1":
        # Hand every lease over first, so the new revision takes the rounds at once and our own
        # still-running loops cannot take them back (try_acquire refuses once shutdown() has run).
        await loop_lock.shutdown()
    for task in background:
        task.cancel()
    if os.environ.get("MOCK_DB") != "1":
        from src.core.bus import bus

        await bus.stop()
        await close_db()
    logger.info("Melbete Bingo API shutting down")


async def resume_active_room_if_needed():
    room_service = get_room_service()
    for track in RoomService.TRACKS:
        room = await room_service.rooms.get_latest(track)
        if not room:
            continue

        if room.state == "running":
            if room.id not in _active_draw_loops:
                logger.info("Starting auto-draw loop for room %s (%s)", room.id, track)
                _active_draw_loops.add(room.id)
                asyncio.create_task(auto_draw_task(room.id))
        elif room.state == "waiting" and room.taken_cartelas and room.countdown_started_at:
            started_at = room.countdown_started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            remaining = max(room.countdown_seconds - elapsed, 0)
            logger.debug(
                "Resuming lobby countdown for room %s (%s, %.0fs remaining) after restart",
                room.id, track, remaining,
            )
            if room.id not in _active_lobby_timers:
                _active_lobby_timers.add(room.id)
                asyncio.create_task(lobby_countdown_task(room.id, initial_wait=remaining))


app = FastAPI(title="Melbete Bingo API", version="4.0.0", lifespan=lifespan)

cors_origins = [settings.public_base_url.rstrip("/")] if settings.public_base_url else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


# Paths that keep working during maintenance: the admin panel itself and the public status endpoint
# the player app polls to know when to show the maintenance screen (and when it is over).
_MAINTENANCE_EXEMPT_PREFIXES = ("/api/admin", "/api/control/public")
# A player who already won must still be able to claim during maintenance (new joins stay blocked).
_MAINTENANCE_EXEMPT_CLAIM = re.compile(r"^/api/session/[^/]+/claim$")


@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(_MAINTENANCE_EXEMPT_PREFIXES) and not _MAINTENANCE_EXEMPT_CLAIM.match(path):
        from fastapi.responses import JSONResponse
        from src.api.dependencies import get_control_service
        from src.core.admin_auth import verify_admin_session

        cfg = await get_control_service().get_published()
        mt = cfg["maintenance"]
        if mt["enabled"] and not (mt["allow_admins"] and verify_admin_session(request.cookies.get("admin_session"))):
            return JSONResponse(
                status_code=503, headers={"Retry-After": "60"},
                content={"detail": mt["message"], "maintenance": True, "eta": mt["eta"]},
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self' wss: https:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    })
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    from fastapi.responses import JSONResponse

    detail = str(exc) if settings.debug else "Internal server error - please try again."
    return JSONResponse(status_code=500, content={"detail": detail})


APP_VERSION = "24"


class CacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.startswith("uploads/"):
            resp.headers.update({"Cache-Control": "no-store, must-revalidate"})
        else:
            resp.headers.update({"Cache-Control": "public, max-age=31536000, immutable"})
        return resp


app.mount("/static", CacheStaticFiles(directory="src/static"), name="static")
templates = Jinja2Templates(directory="src/templates")

UPLOADS_DIR = Path("uploads/deposits")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# HTML Frontend Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "Melbete Bingo API", "version": "4.0.0"}


@app.get("/ready")
async def readiness_check():
    """Readiness: can this instance reach the database? 503 until it can, so the platform
    does not route traffic to (or complete a rollout onto) an instance that cannot serve."""
    if os.environ.get("MOCK_DB") != "1":
        try:
            pool = await get_pool()
            await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=3)
        except Exception:  # noqa: BLE001
            logger.warning("Readiness check failed: database unreachable", exc_info=True)
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/app", response_class=HTMLResponse)
async def mini_app(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "APP_VERSION": APP_VERSION})


@app.get("/download", response_class=HTMLResponse)
async def download_page(request: Request):
    """Public page for the Android app. The APK itself is a static file you drop in
    src/static/downloads/ (see android/README.md); until it exists the button shows 'coming soon'."""
    apk = Path("src/static/downloads/melbete-bingo.apk")
    sha_file = Path("src/static/downloads/melbete-bingo.apk.sha256")
    sha = ""
    if sha_file.is_file():
        parts = sha_file.read_text(errors="ignore").split()
        sha = parts[0] if parts and len(parts[0]) == 64 else ""
    return templates.TemplateResponse("download.html", {
        "request": request, "APP_VERSION": APP_VERSION,
        "apk_available": apk.is_file(),
        "apk_size_mb": round(apk.stat().st_size / 1_048_576, 1) if apk.is_file() else None,
        "apk_sha256": sha,
    })


@app.get("/bingo", response_class=HTMLResponse)
async def bingo_app(request: Request):
    return templates.TemplateResponse("bingo.html", {"request": request, "APP_VERSION": APP_VERSION})




@app.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    return templates.TemplateResponse("agent.html", {"request": request, "APP_VERSION": APP_VERSION})


@app.get("/admin", response_class=HTMLResponse)
async def admin_app(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request, "APP_VERSION": APP_VERSION})



# ---------------------------------------------------------------------------
# Include Modular APIRouters
# ---------------------------------------------------------------------------

from src.api.routers import progress as progress_router  # noqa: E402
app.include_router(auth.router)
app.include_router(progress_router.router)
app.include_router(admin.router)
app.include_router(control.router)
app.include_router(wallet.router)
app.include_router(bingo.router)
app.include_router(bonus.router)
app.include_router(agent.router)
from src.api.routers import leaderboard as leaderboard_router  # noqa: E402
app.include_router(leaderboard_router.router)
from src.api.routers import missions as missions_router  # noqa: E402
app.include_router(missions_router.router)
app.include_router(notifications.router)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

DRAW_LOCK_TTL = 30      # seconds; renewed every draw
LOBBY_LOCK_TTL = 180    # seconds; the lobby countdown is ~30s, released as soon as the task ends


async def loop_supervisor():
    """Every few seconds, make sure a running round / lobby countdown has a live loop in THIS process
    if (and only if) this process can take the Postgres lease. This is what makes rolling deploys safe:
    the old instance keeps its rounds until it hands them over, the new one picks them up within seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            await resume_active_room_if_needed()
        except Exception:  # noqa: BLE001
            logger.exception("Loop supervisor tick failed")


CYCLER_LOCK_TTL = 15    # seconds; renewed every tick


async def round_cycler():
    """Non-stop rounds. On every track, as soon as a round has been showing its results for
    `auto_next_round_seconds` the next waiting round is created (and filled with bots if enabled), and the
    players still on the old results screen are told to move on. One instance at a time owns this."""
    from src.api.routers.bingo import start_bot_fill

    while True:
        await asyncio.sleep(2)
        from src.api.dependencies import get_control_service as _gcs
        await _gcs().get_published()      # keeps the cache warm so timing overrides apply
        seconds = _gcs().peek_timing("next_round_seconds", settings.auto_next_round_seconds)
        if seconds <= 0:
            continue
        try:
            if not await loop_lock.try_acquire("round_cycler", CYCLER_LOCK_TTL):
                continue
            service = get_room_service()
            control = get_control_service()
            for track in RoomService.TRACKS:
                try:
                    moved = await service.auto_advance_track(track, seconds)
                    if moved:
                        old_room, new_room = moved
                        logger.info("Round cycler: %s (%s) -> new round %s", old_room.id, track, new_room.id)
                        await manager.broadcast(old_room.id, {
                            "type": "new_round", "track": track,
                            "previous_room_id": old_room.id, "room_id": new_room.id,
                        })
                    if not await control.track_block(track):
                        room = await service.rooms.get_latest(track)
                        if room and room.state == "waiting":
                            start_bot_fill(room.id)
                except Exception:  # noqa: BLE001
                    logger.exception("Round cycler failed for track %s", track)
        except Exception:  # noqa: BLE001
            logger.exception("Round cycler tick failed")


async def lobby_countdown_task(room_id: str, initial_wait: float | None = None):
    lock = f"lobby:{room_id}"
    if not await loop_lock.try_acquire(lock, LOBBY_LOCK_TTL):
        logger.info("Lobby countdown for room %s is owned by another instance", room_id)
        _active_lobby_timers.discard(room_id)
        return
    try:
        await _lobby_countdown_inner(room_id, initial_wait)
    finally:
        # Always free the in-process flag, even if the countdown crashed, otherwise no new
        # countdown could ever start for this room (select_cartela and the supervisor both skip it).
        _active_lobby_timers.discard(room_id)
        await loop_lock.release(lock)


async def _lobby_countdown_inner(room_id: str, initial_wait: float | None = None):
    try:
        room = await get_room_service().get_room(room_id)
    except RoomNotFoundError:
        _active_lobby_timers.discard(room_id)
        return

    if initial_wait is None and room.countdown_started_at:
        started_at = room.countdown_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        wait_seconds = max(room.countdown_seconds - elapsed, 0)
    else:
        wait_seconds = max(initial_wait or 0, 0)
    await manager.broadcast(room_id, {"type": "countdown_started", "seconds": wait_seconds})
    await asyncio.sleep(wait_seconds)

    # The countdown can be reset while we sleep (a player removed cartelas and
    # the lobby fell below the minimum, then refilled). Always trust the stored
    # timestamp: sleep again until it truly elapses, or stop if it was cleared.
    while True:
        try:
            room = await get_room_service().get_room(room_id)
        except RoomNotFoundError:
            _active_lobby_timers.discard(room_id)
            return
        if room.state != "waiting" or not room.countdown_started_at:
            _active_lobby_timers.discard(room_id)
            return
        started_at = room.countdown_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        remaining = room.countdown_seconds - (datetime.now(timezone.utc) - started_at).total_seconds()
        if remaining <= 0.2:
            break
        await asyncio.sleep(remaining)

    _active_lobby_timers.discard(room_id)
    await start_round_and_announce(room_id)


async def start_round_and_announce(room_id: str, force: bool = False):
    """Charge the lobby and start the round, tell the room what happened, and start the draw loop.

    Shared by the lobby countdown and the admin "start now" action so both behave identically.
    Returns (started_room, dropped_player_ids); started_room is None when nothing started."""
    started_room, dropped = await get_room_service().charge_and_start(room_id, force=force)
    if started_room is None and not dropped:
        return None, []  # lobby didn't have enough cartelas - stays open, no countdown running

    if dropped:
        for player_id in dropped:
            await manager.broadcast(room_id, {
                "type": "player_dropped",
                "player_id": player_id,
                "message": "Removed from the round - insufficient balance when the round started.",
            })

    if started_room is None:
        await manager.broadcast(room_id, {
            "type": "round_cancelled",
            "message": "Nobody could be charged when the round started. Pick a cartela to start a new round.",
        })
        return None, dropped

    await manager.broadcast(room_id, {
        "type": "game_started",
        "room_id": room_id,
        "taken_cartelas": started_room.taken_cartelas,
        "prize_pool": started_room.prize_pool,
    })
    if room_id not in _active_draw_loops:
        _active_draw_loops.add(room_id)
        asyncio.create_task(auto_draw_task(room_id))
    return started_room, dropped


async def auto_draw_task(room_id: str):
    from src.repositories.player_repository import PlayerRepository as _PR
    multi_round: Optional[bool] = None  # decided once: cards are fixed after the round starts
    lock = f"draw:{room_id}"
    # Fixed cadence: schedule each draw against a monotonic deadline instead of sleeping a full interval
    # AFTER the work, otherwise every database round-trip stretches the gap (interval + ~0.2-0.5s of drift).
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    try:
        while True:
            # Single-writer guard: renew the Postgres lease before every draw; if another instance owns
            # this round (or the lease cannot be confirmed) stop - the supervisor restarts us if we own it.
            if not await loop_lock.try_acquire(lock, DRAW_LOCK_TTL):
                logger.info("Draw loop for room %s stopping: lease not held by this instance", room_id)
                break
            try:
                room = await get_room_service().get_room(room_id)
            except RoomNotFoundError:
                break

            if room.state != "running":
                break

            try:
                number, sequence = await get_room_service().draw_number(room_id)
            except InvalidStateError:
                break

            if number is None:
                break

            await manager.broadcast(room_id, {"type": "number_drawn", "number": number, "sequence": sequence})

            try:
                room_after = await get_room_service().get_room(room_id)
                if room_after.state == "running":
                    # After every number, check if any player or bot has completed a winning
                    # pattern: split equally among simultaneous winners, pay, and end the round.
                    ended = await get_room_service().settle_round_winners(room_id)
                    if ended is not None:
                        await manager.broadcast(room_id, {
                            "type": "round_ended",
                            "winner_count": len(ended.winners),
                            "winning_number": ended.winners[0].get("winning_number") if ended.winners else None,
                        })
                        logger.info(
                            "Room %s ended on number %s with %d winner(s)",
                            room_id, ended.winners[0].get("winning_number"), len(ended.winners),
                        )
            except Exception:
                logger.exception("Server-side auto-claim check failed for room %s", room_id)

            try:
                post_room = await get_room_service().get_room(room_id)
            except RoomNotFoundError:
                break
            if post_room.state != "running":
                break

            next_at += max(float(room.draw_interval), 1.0)
            delay = next_at - loop.time()
            if delay < 0:
                # Fell more than a whole interval behind (slow database, paused container): resync rather
                # than calling several numbers back-to-back.
                next_at = loop.time()
                delay = 0.0
            await asyncio.sleep(delay)
    finally:
        _active_draw_loops.discard(room_id)
        await loop_lock.release(lock)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
