"""Postgres repositories for the admin Control Centre: draft/publish config, audit log, player controls."""
from datetime import timedelta
from typing import Any, List, Optional

from src.models.schemas import new_id, utcnow
from src.repositories.base import BaseRepository

_ROW = "global"


class ControlRepository(BaseRepository):
    """Singleton `control_config` row holding the published config and an optional unpublished draft."""

    table_name = "control_config"

    async def get(self) -> dict:
        row = await self._fetchrow("SELECT * FROM control_config WHERE id = $1", _ROW)
        if row is None:
            await self._execute("INSERT INTO control_config (id) VALUES ($1) ON CONFLICT (id) DO NOTHING", _ROW)
            row = await self._fetchrow("SELECT * FROM control_config WHERE id = $1", _ROW)
        return row

    async def save_draft(self, draft: dict, admin_id: str) -> dict:
        await self.get()
        await self._execute(
            "UPDATE control_config SET draft = $2, draft_by = $3, draft_updated_at = $4 WHERE id = $1",
            _ROW, draft, admin_id, utcnow(),
        )
        return await self.get()

    async def discard_draft(self) -> dict:
        await self.get()
        await self._execute(
            "UPDATE control_config SET draft = NULL, draft_by = NULL, draft_updated_at = NULL WHERE id = $1", _ROW
        )
        return await self.get()

    async def publish(self, new_published: dict, admin_id: str, previous: dict) -> dict:
        """Atomically: previous <- `previous`, published <- new, draft cleared, version + 1."""
        await self.get()
        await self._execute(
            "UPDATE control_config SET previous = $2, published = $3, draft = NULL, draft_by = NULL, "
            "draft_updated_at = NULL, version = version + 1, published_by = $4, published_at = $5 WHERE id = $1",
            _ROW, previous, new_published, admin_id, utcnow(),
        )
        return await self.get()

    async def set_published_direct(self, new_published: dict, admin_id: str) -> dict:
        """Emergency path (maintenance / kill switch): change the published config only, keep any draft."""
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute("INSERT INTO control_config (id) VALUES ($1) ON CONFLICT (id) DO NOTHING", _ROW)
                cur = await c.fetchrow("SELECT published FROM control_config WHERE id = $1 FOR UPDATE", _ROW)
                await c.execute(
                    "UPDATE control_config SET previous = $2, published = $3, version = version + 1, "
                    "published_by = $4, published_at = $5 WHERE id = $1",
                    _ROW, cur["published"], new_published, admin_id, utcnow(),
                )
        return await self.get()


class AuditRepository(BaseRepository):
    table_name = "admin_audit_log"

    async def add(
        self,
        actor_id: str,
        action: str,
        *,
        actor_role: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        summary: Optional[str] = None,
        before: Any = None,
        after: Any = None,
        ip: Optional[str] = None,
    ) -> None:
        await self._execute(
            "INSERT INTO admin_audit_log (actor_id, actor_role, action, target_type, target_id, summary, "
            "before_json, after_json, ip) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            str(actor_id), actor_role, action, target_type, target_id, (summary or "")[:500], before, after, ip,
        )

    async def list(
        self,
        limit: int = 100,
        before_id: Optional[int] = None,
        actor_id: Optional[str] = None,
        action_prefix: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[dict]:
        where, args = [], []

        def add(clause: str, value: Any) -> None:
            args.append(value)
            where.append(clause.replace("?", f"${len(args)}"))

        if before_id:
            add("id < ?", int(before_id))
        if actor_id:
            add("actor_id = ?", str(actor_id))
        if action_prefix:
            add("action LIKE ?", action_prefix.replace("%", "").replace("_", r"\_") + "%")
        if target_type:
            add("target_type = ?", target_type)
        if target_id:
            add("target_id = ?", str(target_id))
        args.append(max(1, min(int(limit), 500)))
        sql = (
            "SELECT id, created_at, actor_id, actor_role, action, target_type, target_id, summary, "
            "before_json, after_json, ip FROM admin_audit_log"
            + (" WHERE " + " AND ".join(where) if where else "")
            + f" ORDER BY id DESC LIMIT ${len(args)}"
        )
        return await self._fetch(sql, *args)


class PlayerControlRepository(BaseRepository):
    """Freeze, personal limits, staff notes and rolling-window money totals for one player."""

    table_name = "players"

    async def set_frozen(self, player_id: str, frozen: bool, reason: Optional[str] = None) -> None:
        await self._execute(
            "UPDATE players SET is_frozen = $2, frozen_reason = $3, updated_at = $4 WHERE id = $1",
            player_id, frozen, reason if frozen else None, utcnow(),
        )

    async def set_limits(self, player_id: str, deposit: Optional[int], withdrawal: Optional[int]) -> None:
        await self._execute(
            "UPDATE players SET daily_deposit_limit = $2, daily_withdrawal_limit = $3, updated_at = $4 WHERE id = $1",
            player_id, deposit, withdrawal, utcnow(),
        )

    async def add_note(self, player_id: str, admin_id: str, note: str) -> dict:
        nid = new_id()
        await self._execute(
            "INSERT INTO player_notes (id, player_id, admin_id, note) VALUES ($1, $2, $3, $4)",
            nid, player_id, str(admin_id), note,
        )
        return {"id": nid, "player_id": player_id, "admin_id": str(admin_id), "note": note}

    async def list_notes(self, player_id: str, limit: int = 100) -> List[dict]:
        return await self._fetch(
            "SELECT id, player_id, admin_id, note, created_at FROM player_notes "
            "WHERE player_id = $1 ORDER BY created_at DESC LIMIT $2",
            player_id, limit,
        )

    async def delete_note(self, note_id: str) -> None:
        await self._execute("DELETE FROM player_notes WHERE id = $1", note_id)

    async def sum_recent(self, player_id: str, kind: str, hours: int = 24) -> int:
        """ETB requested (pending + approved) by this player in the last `hours` hours."""
        types = ("top_up_pending", "top_up_approved") if kind == "deposit" else ("withdrawal_pending", "withdrawal_approved")
        since = utcnow() - timedelta(hours=hours)
        total = await self._fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE player_id = $1 AND type = ANY($2::text[]) AND created_at >= $3",
            player_id, list(types), since,
        )
        return int(total or 0)

    async def count_approved_deposits(self, player_id: str) -> int:
        return int(await self._fetchval(
            "SELECT COUNT(*) FROM transactions WHERE player_id = $1 AND type = 'top_up_approved'", player_id
        ) or 0)
