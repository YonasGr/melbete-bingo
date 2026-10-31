"""Public service exports with lazy loading to keep package imports acyclic."""
from importlib import import_module


_EXPORTS = {
    "CardGenerator": ("game_service", "CardGenerator"),
    "DrawEngine": ("game_service", "DrawEngine"),
    "PatternVerifier": ("game_service", "PatternVerifier"),
    "CartelaTakenError": ("room_service", "CartelaTakenError"),
    "InsufficientBalanceError": ("room_service", "InsufficientBalanceError"),
    "InvalidStateError": ("room_service", "InvalidStateError"),
    "RoomNotFoundError": ("room_service", "RoomNotFoundError"),
    "RoomService": ("room_service", "RoomService"),
    "TooManyCartelasError": ("room_service", "TooManyCartelasError"),
    "WalletService": ("wallet_service", "WalletService"),
    "AgentService": ("agent_service", "AgentService"),
    "BroadcastService": ("broadcast_service", "BroadcastService"),
    "BroadcastValidationError": ("broadcast_service", "BroadcastValidationError"),
}


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    attribute = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = attribute
    return attribute

__all__ = [
    "CardGenerator",
    "DrawEngine",
    "PatternVerifier",
    "RoomService",
    "RoomNotFoundError",
    "InvalidStateError",
    "CartelaTakenError",
    "TooManyCartelasError",
    "InsufficientBalanceError",
    "WalletService",
    "AgentService",
    "BroadcastService",
    "BroadcastValidationError",
]
