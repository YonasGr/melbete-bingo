"""Non-stop rounds: a finished round is followed by a fresh waiting round without anyone clicking."""
import asyncio
from datetime import datetime, timedelta, timezone


from src.services.room_service import RoomService


class _Rooms:
    def __init__(self, room):
        self.room, self.created = room, []

    async def get_latest(self, track):
        return self.room

    async def get(self, room_id):
        return self.room

    async def update(self, room_id, fields):
        for k, v in fields.items():
            setattr(self.room, k, v)


def _service(room, new_room):
    svc = RoomService.__new__(RoomService)
    svc.rooms = _Rooms(room)
    calls = {"waiting": 0}

    async def get_or_create(track="real_10"):
        calls["waiting"] += 1
        return new_room

    async def new_waiting(track="real_10"):
        svc.rooms.room = new_room
        return new_room

    svc.get_or_create_waiting_session = get_or_create
    svc._new_waiting_room = new_waiting
    svc._room_locks = {}
    svc._get_room_lock = lambda rid: asyncio.Lock()
    return svc, calls


def _room(**kw):
    from src.models.schemas import GameRoom

    return GameRoom(**kw)


def test_ended_round_advances_after_delay():
    old = _room(state="ended", winners=[{"paid": True}], advanced=False)
    old.updated_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    new = _room(state="waiting")
    svc, _ = _service(old, new)
    moved = asyncio.run(svc.auto_advance_track("real_10", 15))
    assert moved is not None and moved[0].id == old.id and moved[1].id == new.id
    assert old.advanced is True


def test_ended_round_waits_for_results_screen():
    old = _room(state="ended", winners=[{"paid": True}], advanced=False)
    old.updated_at = datetime.now(timezone.utc) - timedelta(seconds=3)
    svc, _ = _service(old, _room(state="waiting"))
    assert asyncio.run(svc.auto_advance_track("real_10", 15)) is None
    assert old.advanced is False


def test_no_round_creates_lobby():
    new = _room(state="waiting")
    svc, calls = _service(None, new)
    assert asyncio.run(svc.auto_advance_track("real_10", 15)) is None
    assert calls["waiting"] == 1
