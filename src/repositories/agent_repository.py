"""Postgres repository for reseller agents and their float/commission ledger."""
import secrets
from datetime import datetime, timedelta
from typing import Optional

from src.models.schemas import Agent, AgentLedgerEntry, new_id, utcnow
from src.repositories.base import BaseRepository

# No 0/O/1/I so codes are easy to read out or type.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class AgentNotFound(Exception):
    pass


class AgentAlreadyExists(Exception):
    pass


class FloatWouldGoNegative(Exception):
    """The adjustment would take the agent's float below zero; nothing was changed."""


class AgentSuspended(Exception):
    pass


class CommissionWouldGoNegative(Exception):
    pass


def new_agent_code() -> str:
    return "AG" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


class AgentRepository(BaseRepository):
    table_name = "agents"

    async def get(self, agent_id: str) -> Optional[Agent]:
        row = await self._fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
        return Agent(**row) if row else None

    async def get_by_player(self, player_id: str) -> Optional[Agent]:
        row = await self._fetchrow("SELECT * FROM agents WHERE player_id = $1", player_id)
        return Agent(**row) if row else None

    async def get_by_code(self, agent_code: str) -> Optional[Agent]:
        row = await self._fetchrow("SELECT * FROM agents WHERE agent_code = $1", agent_code)
        return Agent(**row) if row else None

    async def list_all(self, limit: int = 200) -> list[Agent]:
        rows = await self._fetch("SELECT * FROM agents ORDER BY created_at DESC LIMIT $1", limit)
        return [Agent(**r) for r in rows]

    async def create(self, player_id: str, commission_bps: int, created_by: str) -> Agent:
        """Insert a new active agent with an empty float. Raises AgentAlreadyExists if this
        player is already an agent. A (very unlikely) agent_code collision is retried."""
        for _ in range(5):
            agent = Agent(
                player_id=player_id, agent_code=new_agent_code(),
                commission_bps=commission_bps, created_by=created_by,
            )
            row = await self._fetchrow(
                """
                INSERT INTO agents (id, player_id, agent_code, status, commission_bps, created_by, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                agent.id, agent.player_id, agent.agent_code, agent.status, agent.commission_bps,
                agent.created_by, agent.created_at, agent.updated_at,
            )
            if row:
                return Agent(**row)
            if await self.get_by_player(player_id):
                raise AgentAlreadyExists(player_id)
            # otherwise the generated code collided: try another one
        raise RuntimeError("Could not generate a unique agent code")

    async def set_status(self, agent_id: str, status: str) -> Optional[Agent]:
        row = await self._fetchrow(
            "UPDATE agents SET status = $2, updated_at = $3 WHERE id = $1 RETURNING *",
            agent_id, status, utcnow(),
        )
        return Agent(**row) if row else None

    async def adjust_float_atomic(self, agent_id: str, delta: int, admin_id: str, note: str = "") -> Agent:
        """Add (delta > 0) or remove (delta < 0) float in ONE database transaction together with its
        ledger row. The `float_balance + delta >= 0` guard is in the UPDATE's WHERE clause, so
        concurrent adjustments can never overdraw the float. Raises AgentNotFound /
        FloatWouldGoNegative without changing anything."""
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(
                    "UPDATE agents SET float_balance = float_balance + $2, updated_at = $3 "
                    "WHERE id = $1 AND float_balance + $2 >= 0 RETURNING *",
                    agent_id, delta, utcnow(),
                )
                if row is None:
                    if await c.fetchval("SELECT 1 FROM agents WHERE id = $1", agent_id) is None:
                        raise AgentNotFound(agent_id)
                    raise FloatWouldGoNegative(agent_id)
                agent = Agent(**dict(row))
                entry = AgentLedgerEntry(
                    agent_id=agent_id, type="float_adjust", float_delta=delta, float_after=agent.float_balance,
                    commission_after=agent.commission_balance, admin_id=admin_id, note=note or None,
                )
                await c.execute(
                    """
                    INSERT INTO agent_ledger
                        (id, agent_id, type, float_delta, float_after, commission_delta, commission_after,
                         player_id, admin_id, note, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    entry.id, entry.agent_id, entry.type, entry.float_delta, entry.float_after,
                    entry.commission_delta, entry.commission_after, entry.player_id, entry.admin_id,
                    entry.note, entry.created_at,
                )
                return agent

    async def list_ledger(self, agent_id: str, limit: int = 100) -> list[AgentLedgerEntry]:
        rows = await self._fetch(
            "SELECT * FROM agent_ledger WHERE agent_id = $1 ORDER BY created_at DESC LIMIT $2", agent_id, limit
        )
        return [AgentLedgerEntry(**r) for r in rows]

    @staticmethod
    async def _insert_ledger(c, entry: AgentLedgerEntry) -> None:
        await c.execute(
            """
            INSERT INTO agent_ledger
                (id, agent_id, type, float_delta, float_after, commission_delta, commission_after,
                 player_id, admin_id, note, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            entry.id, entry.agent_id, entry.type, entry.float_delta, entry.float_after,
            entry.commission_delta, entry.commission_after, entry.player_id, entry.admin_id,
            entry.note, entry.created_at,
        )

    async def topup_atomic(self, agent_id: str, target_player_id: str, amount: int, commission: int) -> dict:
        """An agent tops up a player. ONE database transaction:
        debit the agent's float (only while the agent is active and the float covers it), credit the
        agent's commission, credit the player's wallet as a deposit, write the wallet transaction and
        the agent ledger row, and link the player to this agent if they have no agent yet.
        Raises AgentNotFound / AgentSuspended / FloatWouldGoNegative with nothing changed."""
        pool = await self._pool()
        now = utcnow()
        async with pool.acquire() as c:
            async with c.transaction():
                # Lock order is wallet -> agent everywhere (deposit approval does the same), so a
                # top-up and a deposit approval for the same player can never deadlock.
                await c.execute(
                    "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
                    new_id(), target_player_id,
                )
                await c.fetchval("SELECT 1 FROM wallets WHERE player_id = $1 FOR UPDATE", target_player_id)
                row = await c.fetchrow(
                    "UPDATE agents SET float_balance = float_balance - $2, commission_balance = commission_balance + $3, "
                    "updated_at = $4 WHERE id = $1 AND status = 'active' AND float_balance >= $2 RETURNING *",
                    agent_id, amount, commission, now,
                )
                if row is None:
                    cur = await c.fetchrow("SELECT status FROM agents WHERE id = $1", agent_id)
                    if cur is None:
                        raise AgentNotFound(agent_id)
                    if cur["status"] != "active":
                        raise AgentSuspended(agent_id)
                    raise FloatWouldGoNegative(agent_id)
                agent = Agent(**dict(row))

                wallet = await c.fetchrow(
                    "UPDATE wallets SET balance = balance + $2, lifetime_deposited = lifetime_deposited + $2, "
                    "updated_at = $3 WHERE player_id = $1 RETURNING *",
                    target_player_id, amount, now,
                )
                txn_id = new_id()
                await c.execute(
                    "INSERT INTO transactions (id, player_id, type, amount, balance_after, status, note, created_at) "
                    "VALUES ($1, $2, 'agent_topup', $3, $4, 'completed', $5, $6)",
                    txn_id, target_player_id, amount, wallet["balance"],
                    f"agent={agent.id} code={agent.agent_code}", now,
                )
                await c.execute(
                    "UPDATE players SET agent_id = $2, updated_at = $3 WHERE id = $1 AND agent_id IS NULL",
                    target_player_id, agent.id, now,
                )
                await self._insert_ledger(c, AgentLedgerEntry(
                    agent_id=agent.id, type="topup", float_delta=-amount, float_after=agent.float_balance,
                    commission_delta=commission, commission_after=agent.commission_balance,
                    player_id=target_player_id, note=f"txn={txn_id}",
                ))
        return {"agent": agent, "wallet_balance": int(wallet["balance"]), "transaction_id": txn_id}

    async def cashout_commission_atomic(self, agent_id: str, amount: int) -> dict:
        """Move earned commission into the agent's own player wallet as WITHDRAWABLE balance (balance
        and won_balance both rise), so it leaves through the normal withdrawal approval flow.
        ONE transaction; the `commission_balance >= amount` guard is in the UPDATE."""
        pool = await self._pool()
        now = utcnow()
        async with pool.acquire() as c:
            async with c.transaction():
                owner = await c.fetchrow("SELECT player_id FROM agents WHERE id = $1", agent_id)
                if owner is not None:  # wallet -> agent lock order (see topup_atomic)
                    await c.execute(
                        "INSERT INTO wallets (id, player_id) VALUES ($1, $2) ON CONFLICT (player_id) DO NOTHING",
                        new_id(), owner["player_id"],
                    )
                    await c.fetchval("SELECT 1 FROM wallets WHERE player_id = $1 FOR UPDATE", owner["player_id"])
                row = await c.fetchrow(
                    "UPDATE agents SET commission_balance = commission_balance - $2, updated_at = $3 "
                    "WHERE id = $1 AND status = 'active' AND commission_balance >= $2 RETURNING *",
                    agent_id, amount, now,
                )
                if row is None:
                    cur = await c.fetchrow("SELECT status FROM agents WHERE id = $1", agent_id)
                    if cur is None:
                        raise AgentNotFound(agent_id)
                    if cur["status"] != "active":
                        raise AgentSuspended(agent_id)
                    raise CommissionWouldGoNegative(agent_id)
                agent = Agent(**dict(row))
                wallet = await c.fetchrow(
                    "UPDATE wallets SET balance = balance + $2, won_balance = won_balance + $2, updated_at = $3 "
                    "WHERE player_id = $1 RETURNING *",
                    agent.player_id, amount, now,
                )
                txn_id = new_id()
                await c.execute(
                    "INSERT INTO transactions (id, player_id, type, amount, balance_after, status, note, created_at) "
                    "VALUES ($1, $2, 'agent_commission', $3, $4, 'completed', $5, $6)",
                    txn_id, agent.player_id, amount, wallet["balance"], f"agent={agent.id}", now,
                )
                await self._insert_ledger(c, AgentLedgerEntry(
                    agent_id=agent.id, type="commission_cashout", float_after=agent.float_balance,
                    commission_delta=-amount, commission_after=agent.commission_balance,
                    player_id=agent.player_id, note=f"txn={txn_id}",
                ))
        return {"agent": agent, "wallet_balance": int(wallet["balance"]), "transaction_id": txn_id}

    async def link_player(self, player_id: str, agent_id: str) -> bool:
        """Attach a player to an agent only if they have none yet. True if linked now."""
        row = await self._fetchrow(
            "UPDATE players SET agent_id = $2, updated_at = $3 WHERE id = $1 AND agent_id IS NULL RETURNING id",
            player_id, agent_id, utcnow(),
        )
        return row is not None

    async def count_players(self, agent_id: str) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM players WHERE agent_id = $1", agent_id) or 0)

    async def list_players(self, agent_id: str, limit: int = 100) -> list[dict]:
        return await self._fetch(
            "SELECT id, telegram_id, display_name, created_at FROM players WHERE agent_id = $1 "
            "ORDER BY created_at DESC LIMIT $2", agent_id, limit,
        )

    async def credit_referral_commission_in_tx(self, conn, player_id: str, txn_id: str, amount: int) -> Optional[dict]:
        """Referral reward: when a player linked to an ACTIVE agent has a deposit approved, credit the
        agent floor(amount * commission_bps / 10000) ETB of commission. Runs on the deposit-approval
        connection, so it commits or rolls back with the player's cash credit, and it runs once per
        deposit (approval is applied once). Returns the ETB credited, or None if no reward applies."""
        agent_row = await conn.fetchrow(
            "SELECT a.* FROM agents a JOIN players p ON p.agent_id = a.id "
            "WHERE p.id = $1 AND a.status = 'active' FOR UPDATE OF a",
            player_id,
        )
        if agent_row is None:
            return None
        rate = max(int(agent_row["commission_bps"]), int(agent_row.get("tier_bps") or 0))
        earned = int(amount) * rate // 10000
        if earned <= 0:
            return None
        row = await conn.fetchrow(
            "UPDATE agents SET commission_balance = commission_balance + $2, updated_at = $3 WHERE id = $1 RETURNING *",
            agent_row["id"], earned, utcnow(),
        )
        agent = Agent(**dict(row))
        await self._insert_ledger(conn, AgentLedgerEntry(
            agent_id=agent.id, type="referral_commission", float_after=agent.float_balance,
            commission_delta=earned, commission_after=agent.commission_balance,
            player_id=player_id, note=f"deposit={txn_id} amount={amount}",
        ))
        return {"agent_player_id": agent.player_id, "earned": earned}

    # ------------------------------------------------------------------ rewards & tiers

    _SETTING_COLUMNS = (
        "enabled", "new_player_bounty", "min_first_deposit", "min_rounds_played",
        "retention_bonus", "retention_days", "hold_days", "daily_payout_cap",
    )

    async def get_reward_settings(self) -> dict:
        await self._execute("INSERT INTO agent_reward_settings (id) VALUES ('global') ON CONFLICT (id) DO NOTHING")
        row = await self._fetchrow("SELECT * FROM agent_reward_settings WHERE id = 'global'")
        return {k: row[k] for k in self._SETTING_COLUMNS}

    async def update_reward_settings(self, values: dict) -> dict:
        cols = [c for c in self._SETTING_COLUMNS if c in values]
        if cols:
            sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
            await self._execute(
                f"UPDATE agent_reward_settings SET {sets}, updated_at = now() WHERE id = 'global'",
                *[values[c] for c in cols],
            )
        return await self.get_reward_settings()

    async def list_tiers(self) -> list[dict]:
        rows = await self._fetch(
            "SELECT label, min_monthly_volume, commission_bps FROM agent_tiers ORDER BY min_monthly_volume"
        )
        return [dict(r) for r in rows]

    async def replace_tiers(self, tiers: list[dict]) -> list[dict]:
        pool = await self._pool()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute("DELETE FROM agent_tiers")
                for t in tiers:
                    await c.execute(
                        "INSERT INTO agent_tiers (id, label, min_monthly_volume, commission_bps) VALUES ($1, $2, $3, $4)",
                        new_id(), t["label"], t["min_monthly_volume"], t["commission_bps"],
                    )
        return await self.list_tiers()

    async def volume_since(self, agent_id: str, since: datetime) -> int:
        """ETB moved through this agent since `since`: their own top-ups plus approved deposits of
        the players linked to them."""
        return int(await self._fetchval(
            "SELECT (SELECT COALESCE(SUM(-float_delta), 0) FROM agent_ledger "
            "        WHERE agent_id = $1 AND type = 'topup' AND created_at >= $2) "
            "     + (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t JOIN players p ON p.id = t.player_id "
            "        WHERE p.agent_id = $1 AND t.type = 'top_up_approved' AND t.created_at >= $2)",
            agent_id, since,
        ) or 0)

    async def set_tier(self, agent_id: str, tier_bps: int, tier_label: str) -> Optional[Agent]:
        row = await self._fetchrow(
            "UPDATE agents SET tier_bps = $2, tier_label = $3, updated_at = $4 WHERE id = $1 RETURNING *",
            agent_id, tier_bps, tier_label, utcnow(),
        )
        return Agent(**row) if row else None

    async def create_reward(self, agent_id: str, player_id: Optional[str], type_: str, amount: int,
                            release_at: datetime, dedupe_key: str, note: str = "") -> bool:
        """Insert a pending reward. False if this event already has one (dedupe_key is unique)."""
        row = await self._fetchrow(
            "INSERT INTO agent_rewards (id, agent_id, player_id, type, amount, release_at, dedupe_key, note) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            new_id(), agent_id, player_id, type_, amount, release_at, dedupe_key, note[:200],
        )
        return row is not None

    async def list_rewards(self, agent_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        rows = await self._fetch(
            "SELECT r.*, a.agent_code FROM agent_rewards r JOIN agents a ON a.id = r.agent_id "
            "WHERE ($1::text IS NULL OR r.agent_id = $1) AND ($2::text IS NULL OR r.status = $2) "
            "ORDER BY r.created_at DESC LIMIT $3",
            agent_id, status, limit,
        )
        return [dict(r) for r in rows]

    async def reward_totals(self, agent_id: str) -> dict:
        rows = await self._fetch(
            "SELECT status, COALESCE(SUM(amount), 0) AS total FROM agent_rewards WHERE agent_id = $1 GROUP BY status",
            agent_id,
        )
        out = {"pending": 0, "paid": 0}
        for r in rows:
            out[r["status"]] = int(r["total"])
        return out

    async def cancel_reward(self, reward_id: str, note: str = "") -> Optional[dict]:
        """Cancel a reward that has not been paid yet. None if it was already paid/cancelled/unknown."""
        row = await self._fetchrow(
            "UPDATE agent_rewards SET status = 'cancelled', note = COALESCE(NULLIF($2, ''), note) "
            "WHERE id = $1 AND status = 'pending' RETURNING *", reward_id, note[:200],
        )
        return dict(row) if row else None

    async def cancel_pending_for_player(self, player_id: str, note: str) -> int:
        """Deposit reversed / player banned: cancel every unpaid reward that came from that player."""
        rows = await self._fetch(
            "UPDATE agent_rewards SET status = 'cancelled', note = $2 "
            "WHERE player_id = $1 AND status = 'pending' RETURNING id", player_id, note[:200],
        )
        return len(rows)

    async def due_rewards(self, limit: int = 200) -> list[dict]:
        rows = await self._fetch(
            "SELECT * FROM agent_rewards WHERE status = 'pending' AND release_at <= now() "
            "ORDER BY release_at LIMIT $1", limit,
        )
        return [dict(r) for r in rows]

    async def rounds_played(self, player_id: str) -> int:
        return int(await self._fetchval("SELECT COUNT(DISTINCT room_id) FROM cards WHERE owner_id = $1", player_id) or 0)

    async def player_deposit_state(self, player_id: str) -> Optional[dict]:
        row = await self._fetchrow(
            "SELECT p.agent_id, p.is_banned, p.is_bot, COALESCE(w.lifetime_deposited, 0) AS lifetime_deposited "
            "FROM players p LEFT JOIN wallets w ON w.player_id = p.id WHERE p.id = $1", player_id,
        )
        return dict(row) if row else None

    async def pay_reward_atomic(self, reward_id: str, daily_cap: int) -> dict:
        """Pay one due reward into the agent's commission balance. ONE transaction: the reward is
        locked, the agent is locked, the daily cap is checked, then balance + ledger + status change
        together. Returns {"result": paid|cap|inactive|gone, ...}."""
        pool = await self._pool()
        now = utcnow()
        async with pool.acquire() as c:
            async with c.transaction():
                rw = await c.fetchrow(
                    "SELECT * FROM agent_rewards WHERE id = $1 AND status = 'pending' AND release_at <= $2 FOR UPDATE",
                    reward_id, now,
                )
                if rw is None:
                    return {"result": "gone"}
                ag = await c.fetchrow("SELECT * FROM agents WHERE id = $1 FOR UPDATE", rw["agent_id"])
                if ag is None or ag["status"] != "active":
                    return {"result": "inactive"}
                if daily_cap > 0:
                    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    paid_today = int(await c.fetchval(
                        "SELECT COALESCE(SUM(amount), 0) FROM agent_rewards "
                        "WHERE agent_id = $1 AND status = 'paid' AND paid_at >= $2", rw["agent_id"], day_start,
                    ) or 0)
                    if paid_today + int(rw["amount"]) > daily_cap:
                        return {"result": "cap"}
                row = await c.fetchrow(
                    "UPDATE agents SET commission_balance = commission_balance + $2, updated_at = $3 WHERE id = $1 RETURNING *",
                    rw["agent_id"], rw["amount"], now,
                )
                agent = Agent(**dict(row))
                await c.execute("UPDATE agent_rewards SET status = 'paid', paid_at = $2 WHERE id = $1", reward_id, now)
                await self._insert_ledger(c, AgentLedgerEntry(
                    agent_id=agent.id, type=f"reward_{rw['type']}", float_after=agent.float_balance,
                    commission_delta=int(rw["amount"]), commission_after=agent.commission_balance,
                    player_id=rw["player_id"], note=f"reward={reward_id}",
                ))
        return {"result": "paid", "agent_player_id": agent.player_id, "amount": int(rw["amount"]), "type": rw["type"]}

    async def retention_candidates(self, older_than: datetime, active_since: datetime, limit: int = 200) -> list[dict]:
        rows = await self._fetch(
            "SELECT DISTINCT ON (r.player_id) r.agent_id, r.player_id FROM agent_rewards r "
            "WHERE r.type = 'new_player' AND r.status <> 'cancelled' AND r.created_at <= $1 "
            "AND NOT EXISTS (SELECT 1 FROM agent_rewards x WHERE x.dedupe_key = 'retention:' || r.player_id) "
            "AND EXISTS (SELECT 1 FROM cards c WHERE c.owner_id = r.player_id AND c.created_at >= $2) "
            "LIMIT $3", older_than, active_since, limit,
        )
        return [dict(r) for r in rows]

    async def active_agents(self, limit: int = 1000) -> list[Agent]:
        rows = await self._fetch("SELECT * FROM agents WHERE status = 'active' LIMIT $1", limit)
        return [Agent(**r) for r in rows]

    async def leaderboard(self, since: datetime, limit: int = 10) -> list[dict]:
        rows = await self._fetch(
            "SELECT a.id, a.agent_code, "
            " (SELECT COALESCE(SUM(-l.float_delta), 0) FROM agent_ledger l WHERE l.agent_id = a.id AND l.type = 'topup' AND l.created_at >= $1) "
            " + (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t JOIN players p ON p.id = t.player_id "
            "    WHERE p.agent_id = a.id AND t.type = 'top_up_approved' AND t.created_at >= $1) AS volume "
            "FROM agents a WHERE a.status = 'active' ORDER BY volume DESC, a.created_at LIMIT $2", since, limit,
        )
        return [{"id": r["id"], "agent_code": r["agent_code"], "volume": int(r["volume"])} for r in rows if int(r["volume"]) > 0]

    async def suspicious_topups(self, since: datetime, min_topups: int) -> list[dict]:
        """Agents who topped up the same player many times in a short window - a common sign of
        cycling money to farm commission."""
        rows = await self._fetch(
            "SELECT l.agent_id, a.agent_code, l.player_id, COUNT(*) AS topups, SUM(-l.float_delta) AS total "
            "FROM agent_ledger l JOIN agents a ON a.id = l.agent_id "
            "WHERE l.type = 'topup' AND l.created_at >= $1 GROUP BY l.agent_id, a.agent_code, l.player_id "
            "HAVING COUNT(*) >= $2 ORDER BY topups DESC LIMIT 50", since, min_topups,
        )
        return [{"agent_id": r["agent_id"], "agent_code": r["agent_code"], "player_id": r["player_id"],
                 "topups": int(r["topups"]), "total": int(r["total"])} for r in rows]

    async def shares_ip_with_agent(self, player_id: str, agent_player_id: str) -> bool:
        """True when the referred player and the agent have used the same connection for money actions."""
        return bool(await self._fetchval(
            "SELECT 1 FROM player_ip_log a JOIN player_ip_log b ON a.ip_hash = b.ip_hash "
            "WHERE a.player_id = $1 AND b.player_id = $2 LIMIT 1", player_id, agent_player_id,
        ))

    async def shares_phone_with_agent(self, player_id: str, agent_player_id: str) -> bool:
        """True when the referred player and the agent registered the same phone number - the agent
        is then rewarding themselves through a second account."""
        return bool(await self._fetchval(
            "SELECT 1 FROM players a JOIN players b ON a.phone_number = b.phone_number "
            "WHERE a.id = $1 AND b.id = $2 AND a.phone_number IS NOT NULL AND a.phone_number <> ''",
            player_id, agent_player_id,
        ))
