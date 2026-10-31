"""Postgres repository for the RNG audit trail (DrawLog)."""
import hashlib
import json
from typing import Optional

from src.models.schemas import DrawLog
from src.repositories.base import BaseRepository


class DrawLogRepository(BaseRepository):
    table_name = "draw_logs"

    async def create_for_room(self, room_id: str, seed: str) -> DrawLog:
        log = DrawLog(room_id=room_id, seed=seed)
        data = log.model_dump()
        await self._execute(
            """
            INSERT INTO draw_logs (id, room_id, seed, sequence, final_hash, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            data["id"], data["room_id"], data["seed"], data["sequence"],
            data["final_hash"], data["created_at"],
        )
        return log

    async def get_by_room(self, room_id: str) -> Optional[DrawLog]:
        row = await self._fetchrow(
            "SELECT * FROM draw_logs WHERE room_id = $1 LIMIT 1", room_id
        )
        return DrawLog(**row) if row else None

    async def append_draw(self, log_id: str, draw_number: int, value: int) -> None:
        row = await self._fetchrow("SELECT * FROM draw_logs WHERE id = $1", log_id)
        if not row:
            return
        log = DrawLog(**row)
        log.sequence.append({
            "draw_number": draw_number,
            "value": value,
            "source": "server_csprng",
        })
        await self._execute(
            "UPDATE draw_logs SET sequence = $2 WHERE id = $1", log_id, log.sequence
        )

    async def finalize(self, log_id: str) -> None:
        """Compute and store a hash chaining the full draw sequence, so the
        fairness of a finished game can be independently verified."""
        row = await self._fetchrow("SELECT * FROM draw_logs WHERE id = $1", log_id)
        if not row:
            return
        log = DrawLog(**row)
        payload = json.dumps({"seed": log.seed, "sequence": log.sequence}, sort_keys=True, default=str)
        final_hash = hashlib.sha256(payload.encode()).hexdigest()
        await self._execute(
            "UPDATE draw_logs SET final_hash = $2 WHERE id = $1", log_id, final_hash
        )
