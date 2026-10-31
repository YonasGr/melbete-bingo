"""Queries behind the multi-account signals. Read-mostly; the only write is the IP log upsert."""
from src.repositories.base import BaseRepository


class AbuseRepository(BaseRepository):
    table_name = "player_ip_log"

    async def record_ip(self, player_id: str, ip_hash: str) -> None:
        await self._execute(
            "INSERT INTO player_ip_log (player_id, ip_hash) VALUES ($1, $2) "
            "ON CONFLICT (player_id, ip_hash) DO UPDATE SET last_seen = now(), hits = player_ip_log.hits + 1",
            player_id, ip_hash,
        )

    async def ip_clusters(self, min_players: int, limit: int = 50) -> list[dict]:
        rows = await self._fetch(
            "SELECT l.ip_hash, array_agg(DISTINCT l.player_id) AS players "
            "FROM player_ip_log l JOIN players p ON p.id = l.player_id AND NOT p.is_bot "
            "GROUP BY l.ip_hash HAVING count(DISTINCT l.player_id) >= $1 "
            "ORDER BY count(DISTINCT l.player_id) DESC LIMIT $2", min_players, limit,
        )
        return [{"key": r["ip_hash"][:8], "players": list(r["players"])} for r in rows]

    async def phone_duplicates(self, limit: int = 50) -> list[dict]:
        rows = await self._fetch(
            "SELECT phone_normalized, array_agg(id) AS players FROM players "
            "WHERE phone_normalized IS NOT NULL AND NOT is_bot "
            "GROUP BY phone_normalized HAVING count(*) > 1 LIMIT $1", limit,
        )
        return [{"key": "…" + r["phone_normalized"][-4:], "players": list(r["players"])} for r in rows]

    async def shared_payout_accounts(self, limit: int = 50) -> list[dict]:
        rows = await self._fetch(
            "SELECT withdrawal_address, array_agg(DISTINCT player_id) AS players FROM transactions "
            "WHERE type LIKE 'withdrawal%' AND COALESCE(withdrawal_address, '') <> '' "
            "GROUP BY withdrawal_address HAVING count(DISTINCT player_id) > 1 LIMIT $1", limit,
        )
        return [{"key": "…" + str(r["withdrawal_address"])[-4:], "players": list(r["players"])} for r in rows]

    async def players_info(self, ids: list[str]) -> dict:
        rows = await self._fetch(
            "SELECT id, telegram_id, display_name, is_banned FROM players WHERE id = ANY($1::text[])", ids,
        )
        return {r["id"]: {"id": r["id"], "telegram_id": r["telegram_id"], "display_name": r["display_name"],
                          "is_banned": bool(r["is_banned"])} for r in rows}
