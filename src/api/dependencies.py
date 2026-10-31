"""
Shared API dependencies, singletons, auth helpers, and connection manager.
"""
import asyncio
import base64
import logging
import re
import uuid
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import HTTPException, Request, WebSocket

from src.core.admin_auth import verify_admin_session
from src.core.telegram_auth import extract_telegram_id
from src.repositories import LinkCodeRepository, PlayerRepository
from src.services import AgentService, RoomService, WalletService
from src.services.admin_service import AdminService
from src.services.bot_service import BotService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str):
        await websocket.accept()
        self.active_connections.setdefault(room_id, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, room_id: str):
        if room_id in self.active_connections:
            self.active_connections[room_id].discard(websocket)
            if not self.active_connections[room_id]:
                del self.active_connections[room_id]

    async def broadcast(self, room_id: str, message: dict):
        """Send to every player in the room, on every container (see src/core/bus.py)."""
        from src.core.bus import bus

        if await bus.publish(room_id, message):
            return  # each container, including this one, delivers it from its subscription
        await self.broadcast_local(room_id, message)

    async def broadcast_local(self, room_id: str, message: dict):
        connections = tuple(self.active_connections.get(room_id, set()))

        async def send(connection: WebSocket) -> WebSocket | None:
            try:
                await connection.send_json(message)
            except Exception:
                return connection
            return None

        dead = [socket for socket in await asyncio.gather(*(send(c) for c in connections)) if socket]
        for connection in dead:
            self.disconnect(connection, room_id)


manager = ConnectionManager()

# ---------------------------------------------------------------------------
# Lazily-created singletons & shared state
# ---------------------------------------------------------------------------
_room_service: RoomService | None = None
_wallet_service: WalletService | None = None
_admin_service: AdminService | None = None
_agent_service: AgentService | None = None
_link_codes: LinkCodeRepository | None = None
_bot_service: BotService | None = None

_active_lobby_timers: Set[str] = set()
_active_draw_loops: Set[str] = set()
_active_bot_fills: Set[str] = set()

RECENT_WINNER_DEPRIORITIZE_SECONDS = 15 * 60


async def _resolve_player_names(player_ids) -> dict:
    """{player_id: display_name} so shared-win results show every winner's name."""
    from src.repositories.player_repository import PlayerRepository

    players = await PlayerRepository().list_by_ids(player_ids)
    return {p.id: p.display_name for p in players}


def get_room_service() -> RoomService:
    global _room_service
    if _room_service is None:
        _room_service = RoomService()
        _room_service.name_resolver = _resolve_player_names
        from src.services.engagement_service import get_engagement_service

        _room_service.on_round_started = get_engagement_service().record_round_played
    return _room_service


def get_wallet_service() -> WalletService:
    global _wallet_service
    if _wallet_service is None:
        _wallet_service = WalletService()
    return _wallet_service


def get_agent_service() -> AgentService:
    global _agent_service
    if _agent_service is None:
        _agent_service = AgentService()
    return _agent_service


def get_admin_service() -> AdminService:
    global _admin_service
    if _admin_service is None:
        _admin_service = AdminService()
    return _admin_service


def get_link_codes() -> LinkCodeRepository:
    global _link_codes
    if _link_codes is None:
        _link_codes = LinkCodeRepository()
    return _link_codes


def get_bot_service() -> BotService:
    global _bot_service
    if _bot_service is None:
        _bot_service = BotService()
    return _bot_service


# ---------------------------------------------------------------------------
# Auth / Identity dependencies
# ---------------------------------------------------------------------------
async def _resolve_telegram_id(
    x_telegram_init_data: str | None,
) -> str | None:
    """Only Telegram's signed, fresh initData may establish an admin identity."""
    if x_telegram_init_data:
        return extract_telegram_id(x_telegram_init_data)
    return None


def get_control_service():
    """Admin Control Centre (draft/publish config, audit log, enforcement helpers)."""
    from src.services.control_service import get_control_service as _get

    return _get()


def require_perm(*perms: str):
    """FastAPI dependency factory: the caller must be signed in AND hold every listed permission."""

    async def _dep(request: Request) -> str:
        telegram_id = verify_admin_session(request.cookies.get("admin_session"))
        if not telegram_id:
            raise HTTPException(status_code=403, detail="Admin access required")
        held = await get_admin_service().permissions_of(telegram_id)
        if not held:
            raise HTTPException(status_code=403, detail="Admin access required")
        missing = [p for p in perms if p not in held]
        if missing:
            raise HTTPException(status_code=403, detail="Your role does not allow this action")
        return telegram_id

    return _dep


async def require_admin_dep(request: Request) -> str:
    telegram_id = verify_admin_session(request.cookies.get("admin_session"))
    if not telegram_id or not await get_admin_service().is_admin(telegram_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return telegram_id


async def require_super_admin_dep(request: Request) -> str:
    telegram_id = verify_admin_session(request.cookies.get("admin_session"))
    if not telegram_id or not await get_admin_service().is_super_admin(telegram_id):
        raise HTTPException(status_code=403, detail="Super admin access required")
    return telegram_id


async def _require_player_identity(
    player_id: str,
    x_telegram_init_data: str | None,
) -> None:
    """Every money or account action needs a verified Telegram identity."""
    if not player_id.startswith("tg_"):
        raise HTTPException(status_code=403, detail="Open the app from Telegram to verify your identity")
    verified_id = extract_telegram_id(x_telegram_init_data)
    if not verified_id or f"tg_{verified_id}" != player_id:
        raise HTTPException(
            status_code=403,
            detail="Identity verification failed - please reopen the app from Telegram.",
        )


async def _reject_if_banned(player_id: str) -> None:
    """Blocks a banned player from money-moving and game-join actions."""
    player = await PlayerRepository().get_by_id(player_id)
    if player and player.is_banned:
        raise HTTPException(
            status_code=403,
            detail="Your account has been suspended. Contact support if you believe this is a mistake.",
        )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
IMAGE_SIGNATURES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n", "image/webp": b"RIFF"}


def _decode_image_data_url(data_url: str, *, max_bytes: int, field_name: str) -> tuple[str, bytes, str]:
    """Decode a small, validated JPEG/PNG/WebP data URL without trusting its MIME label."""
    match = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", data_url or "")
    if not match:
        raise ValueError(f"{field_name} must be a JPEG, PNG, or WebP image")
    mime, encoded = match.groups()
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 4:
        raise ValueError(f"{field_name} is too large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError(f"{field_name} contains invalid image data") from exc
    if len(raw) > max_bytes:
        raise ValueError(f"{field_name} is too large")
    if not raw.startswith(IMAGE_SIGNATURES[mime]) or (mime == "image/webp" and raw[8:12] != b"WEBP"):
        raise ValueError(f"{field_name} content does not match its declared type")
    return mime, raw, mime.split("/")[-1].replace("jpeg", "jpg")


def _save_screenshot(data_url: str) -> str:
    """Decodes a data URL uploaded from the Mini App's deposit form and saves it to storage."""
    _, raw, ext = _decode_image_data_url(data_url, max_bytes=6_000_000, field_name="Screenshot")
    filename = f"{uuid.uuid4()}.{ext}"
    from src.services.screenshot_storage import save_screenshot
    save_screenshot(filename, raw)
    return f"/api/admin/uploads/{filename}"


def _reshape_winners(winners: list) -> list:
    """Backward-compat only: reshapes 1D flat grids to 5x5 for legacy rows."""
    reshaped = []
    for w in winners:
        w = dict(w)
        flat = w.pop("grid_flat", None)
        if flat and "grid" not in w:
            w["grid"] = [flat[i:i + 5] for i in range(0, 25, 5)]
        reshaped.append(w)
    return reshaped
