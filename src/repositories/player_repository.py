"""Postgres repository for players."""
from typing import Iterable, List, Optional

from src.models.schemas import Player, utcnow
from src.repositories.base import BaseRepository


class PhoneAlreadyRegistered(Exception):
    """Another real account already uses this phone number."""


class PlayerRepository(BaseRepository):
    table_name = "players"

    async def get_by_id(self, player_id: str) -> Optional[Player]:
        row = await self._fetchrow("SELECT * FROM players WHERE id = $1", player_id)
        return Player(**row) if row else None

    async def get_by_telegram_id(self, telegram_id: str) -> Optional[Player]:
        row = await self._fetchrow("SELECT * FROM players WHERE telegram_id = $1", telegram_id)
        return Player(**row) if row else None

    async def list_all(self, limit: int = 100) -> List[Player]:
        """Most-recently-registered players first - powers the admin
        panel's Users tab."""
        rows = await self._fetch(
            "SELECT * FROM players ORDER BY created_at DESC LIMIT $1", limit
        )
        return [Player(**r) for r in rows]

    async def list_telegram_ids(self, limit: int = 100, offset: int = 0) -> List[str]:
        """Return bot recipients in small pages for rate-limited fan-out."""
        rows = await self._fetch(
            "SELECT telegram_id FROM players WHERE telegram_id IS NOT NULL "
            "ORDER BY telegram_id ASC LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
        return [str(row["telegram_id"]) for row in rows if row.get("telegram_id")]

    async def count(self) -> int:
        return await self._fetchval("SELECT COUNT(*) FROM players")

    async def list_by_ids(self, player_ids: Iterable[str]) -> List[Player]:
        """Batch lookup for admin/history views; avoids one query per row."""
        ids = list(dict.fromkeys(player_ids))
        if not ids:
            return []
        rows = await self._fetch("SELECT * FROM players WHERE id = ANY($1::text[])", ids)
        return [Player(**r) for r in rows]

    async def list_all_with_wallets(self, limit: int = 100) -> list[dict]:
        """Admin directory in one query instead of an N+1 wallet lookup."""
        return await self._fetch(
            """
            SELECT p.*, COALESCE(w.balance, 0) AS balance,
                   COALESCE(w.won_balance, 0) AS won_balance,
                   COALESCE(w.bonus_balance, 0) AS bonus_balance,
                   COALESCE(w.lifetime_deposited, 0) AS lifetime_deposited
            FROM players p
            LEFT JOIN wallets w ON w.player_id = p.id
            ORDER BY (COALESCE(w.balance, 0) + COALESCE(w.bonus_balance, 0)) DESC, p.created_at DESC
            LIMIT $1
            """,
            limit,
        )

    async def search_with_wallets(self, query: str, limit: int = 50) -> list[dict]:
        """Server-side search by telegram_id, display_name, or player id.
        Used by the admin user directory search so users are found regardless
        of how many total players exist (no client-side limit gap)."""
        q = f"%{query}%"
        return await self._fetch(
            """
            SELECT p.*, COALESCE(w.balance, 0) AS balance,
                   COALESCE(w.won_balance, 0) AS won_balance,
                   COALESCE(w.bonus_balance, 0) AS bonus_balance,
                   COALESCE(w.lifetime_deposited, 0) AS lifetime_deposited
            FROM players p
            LEFT JOIN wallets w ON w.player_id = p.id
            WHERE p.telegram_id ILIKE $1
               OR p.display_name ILIKE $1
               OR p.id ILIKE $1
            ORDER BY (COALESCE(w.balance, 0) + COALESCE(w.bonus_balance, 0)) DESC, p.created_at DESC
            LIMIT $2
            """,
            q,
            limit,
        )

    async def get_wallet_totals(self) -> dict:
        """Aggregate real balance and bonus balance across all wallets.
        Used in the admin Economy tab to show platform-wide balance totals."""
        row = await self._fetchrow("""
            SELECT
                COALESCE(SUM(balance), 0)          AS total_real_balance,
                COALESCE(SUM(bonus_balance), 0)    AS total_bonus_balance,
                COALESCE(SUM(won_balance), 0)      AS total_won_balance,
                COALESCE(SUM(lifetime_deposited), 0) AS total_lifetime_deposited
            FROM wallets
        """)
        return dict(row) if row else {}

    async def create(
        self,
        telegram_id: str,
        display_name: str,
        avatar_url: Optional[str] = None,
        referred_by: Optional[str] = None,
    ) -> Player:
        # IMPORTANT: id is deterministic (`tg_<telegram_id>`), NOT a random
        # uuid. The Mini App computes this exact same id client-side
        # (`tg_${user.id}`) from Telegram's WebApp initData, so the bot's
        # registration/top-up flow and the Mini App's wallet/game calls
        # always resolve to the same player + wallet row.
        player = Player(
            id=f"tg_{telegram_id}",
            telegram_id=telegram_id,
            display_name=display_name,
            avatar_url=avatar_url,
            referred_by=referred_by,
        )
        data = player.model_dump()
        await self._execute(
            """
            INSERT INTO players
                (id, telegram_id, display_name, avatar_url, phone_number,
                 payment_name, registration_state, withdrawal_accounts,
                 auto_bingo_enabled, referred_by, referral_count,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            data["id"], data["telegram_id"], data["display_name"], data["avatar_url"],
            data["phone_number"], data["payment_name"], data["registration_state"],
            data["withdrawal_accounts"], data["auto_bingo_enabled"], data["referred_by"],
            data["referral_count"], data["created_at"], data["updated_at"],
        )
        return player

    async def get_or_create(
        self,
        telegram_id: str,
        display_name: str,
        avatar_url: Optional[str] = None,
        referred_by: Optional[str] = None,
    ) -> Player:
        existing = await self.get_by_telegram_id(telegram_id)
        if existing:
            return existing
        return await self.create(telegram_id, display_name, avatar_url, referred_by)

    async def touch(self, player_id: str) -> None:
        await self._update_fields("players", "id", player_id, {"updated_at": utcnow()})

    async def update(self, player_id: str, fields: dict) -> None:
        fields = {**fields, "updated_at": utcnow()}
        await self._update_fields("players", "id", player_id, fields)

    async def set_contact(self, player_id: str, phone_number: str) -> None:
        """Raises PhoneAlreadyRegistered if another real account already uses this number."""
        from asyncpg import UniqueViolationError

        from src.services.abuse_signals import normalize_phone

        norm = normalize_phone(phone_number)
        fields = {"phone_number": phone_number, "registration_state": "complete"}
        if norm:
            taken = await self._fetchval(
                "SELECT id FROM players WHERE phone_normalized = $1 AND id <> $2 AND NOT is_bot LIMIT 1", norm, player_id,
            )
            if taken:
                raise PhoneAlreadyRegistered(norm)
            fields["phone_normalized"] = norm
        try:
            await self.update(player_id, fields)
        except UniqueViolationError:  # two accounts raced past the check above; the DB index decides
            raise PhoneAlreadyRegistered(norm)

    async def set_payment_name(self, player_id: str, payment_name: str) -> None:
        """Called from the deposit/withdraw flows (not registration) once
        we need the player's real/payment name to reconcile funds."""
        await self.update(player_id, {"payment_name": payment_name})

    async def set_withdrawal_account(self, player_id: str, method_key: str, address: str) -> None:
        player = await self.get_by_id(player_id)
        accounts = dict(player.withdrawal_accounts) if player else {}
        accounts[method_key] = address
        await self.update(player_id, {"withdrawal_accounts": accounts})

    async def set_auto_bingo(self, player_id: str, enabled: bool) -> None:
        await self.update(player_id, {"auto_bingo_enabled": enabled})

    async def set_banned(self, player_id: str, banned: bool, reason: Optional[str] = None) -> None:
        """Ban/unban a player - e.g. for repeated fake deposit screenshots
        or claimed amounts that don't match what was actually sent. A
        banned player is blocked from both the bot and the Mini App (see
        `require_admin_dep`'s sibling check in the API and the bot's
        per-message ban check)."""
        await self.update(player_id, {"is_banned": banned, "ban_reason": reason if banned else None})
