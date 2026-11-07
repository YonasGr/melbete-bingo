"""Transactional promotional bonus rewards (amounts are whole ETB, non-withdrawable)."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import asyncio
import logging
from src.core.db import get_pool
from src.services.promotions import multiplier_at, weighted_deposits

logger = logging.getLogger(__name__)

ADDIS = ZoneInfo("Africa/Addis_Ababa")
AMOUNTS = {"welcome": 20, "referral": 5, "daily": 1}

_SETTLING: set = set()
CASHBACK_LOOKBACK_DAYS = 7   # unsettled days are caught up when the player next opens the app


def cashback_amount(deposited_etb: int, percent: int, cap: int = 0) -> int:
    """Whole ETB cashback for one day's approved deposits (rounded down). cap > 0 limits the credit per day."""
    if deposited_etb <= 0 or percent <= 0:
        return 0
    amount = (int(deposited_etb) * int(percent)) // 100
    return min(amount, int(cap)) if cap and int(cap) > 0 else amount


def budget_alert_level(used: int, budget: int) -> int:
    """0, 80 or 100: how much of a budget is used (alert thresholds). A zero budget never alerts."""
    if budget <= 0:
        return 0
    pct = used * 100 // budget
    return 100 if pct >= 100 else (80 if pct >= 80 else 0)


def cashback_days_due(deposits_by_day: dict, withdrawal_days: set, claimed_days: set, today) -> dict:
    """{day: deposited ETB} for finished days that earn cashback: they had approved deposits, no withdrawal
    request that day, are not today (the day is still open) and were not already paid."""
    return {d: int(t) for d, t in deposits_by_day.items()
            if d < today and int(t) > 0 and d not in withdrawal_days and d not in claimed_days}


async def _cashback_promotions() -> list:
    """Timed cashback boosts from the published Control Center config ([] when none or unreadable)."""
    try:
        from src.api.dependencies import get_control_service
        return list((await get_control_service().get_published()).get("promotions") or [])
    except Exception:  # noqa: BLE001
        logger.warning("could not read promotions; no boost applied")
        return []


async def _cashback_player_limits(c, player_id: str) -> tuple:
    """(excluded, daily cap in ETB or 0). Missing table/column (migration not applied yet) means no limits."""
    try:
        rule = await c.fetchrow("SELECT excluded, daily_cap_etb FROM cashback_player_rules WHERE player_id=$1", player_id)
        default_cap = await c.fetchval("SELECT per_player_daily_max_etb FROM bonus_campaigns WHERE reward_type='cashback'")
    except Exception:  # noqa: BLE001
        return False, 0
    if rule and rule["excluded"]:
        return True, 0
    return False, int(rule["daily_cap_etb"]) if rule and rule["daily_cap_etb"] else int(default_cap or 0)


async def _cashback_paused_by_control() -> bool:
    """True when the Control Center's "Daily cashback" switch (or the kill switch) is off. Fails closed."""
    try:
        from src.api.dependencies import get_control_service
        return bool(await get_control_service().flag_block("cashback"))
    except Exception:  # noqa: BLE001
        logger.exception("could not read the cashback switch; skipping cashback this time")
        return True


class BonusError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)

class BonusService:
    async def summary(self, player_id: str) -> dict:
        # Cashback is settled in the background: the bonus screen (and everything polling it) never waits on it,
        # and a player can only have one settlement running at a time.
        if player_id not in _SETTLING:
            _SETTLING.add(player_id)
            asyncio.create_task(self._settle_cashback_safely(player_id))
        pool = await get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow("SELECT COALESCE(bonus_balance,0) AS balance FROM wallets WHERE player_id=$1", player_id)
            if not row:
                balance = 0
            else:
                balance = int(row["balance"])
            global_on = await c.fetchval("SELECT globally_enabled FROM bonus_campaign_state WHERE id=1")
            campaigns = await c.fetch("SELECT * FROM bonus_campaigns ORDER BY reward_type")
            today = datetime.now(ADDIS).date()
            claims = await c.fetch("SELECT reward_type, local_reward_date FROM bonus_claims WHERE player_id=$1 AND status='granted'", player_id)
            claimed_welcome = any(x["reward_type"] == "welcome" for x in claims)
            claimed_daily = any(x["reward_type"] == "daily" and x["local_reward_date"] == today for x in claims)
            claimed_first_deposit = any(x["reward_type"] == "first_deposit" for x in claims)
            referrals = await c.fetchrow("SELECT COUNT(*) FILTER (WHERE awarded) AS successful, COUNT(*) FILTER (WHERE NOT awarded) AS pending FROM referrals WHERE referrer_id=$1", player_id)
            result = {}
            now = datetime.now(timezone.utc)
            for camp in campaigns:
                kind = camp["reward_type"]
                in_window = (camp["starts_at"] is None or camp["starts_at"] <= now) and (camp["ends_at"] is None or camp["ends_at"] > now)
                enabled = bool(global_on and camp["enabled"] and in_window)
                status = "claimed" if ((kind == "welcome" and claimed_welcome) or (kind == "daily" and claimed_daily) or (kind == "first_deposit" and claimed_first_deposit)) else ("automatic" if kind in ("first_deposit", "cashback") and enabled else ("available" if enabled else "paused"))
                result[kind] = {"amount_etb": int(camp["amount_etb"]), "enabled": enabled,
                    "status": status, "reward_kind": "percentage" if kind in ("first_deposit", "cashback") else "fixed"}
            if "cashback" in result:
                excluded, cap = await _cashback_player_limits(c, player_id)
                promos = await _cashback_promotions()
                result["cashback"].update(await self._cashback_today(c, player_id, int(result["cashback"]["amount_etb"]), promos, cap))
                if excluded or await _cashback_paused_by_control():
                    result["cashback"]["enabled"] = False
                    result["cashback"]["status"] = "paused"
            return {"bonus_balance": balance, "currency": "ETB", "restriction": "Bonus credit — non-withdrawable; Bingo cartelas only.",
                    "campaign_enabled": bool(global_on), "rewards": result,
                    "referrals": {"successful": int(referrals["successful"] or 0), "pending": int(referrals["pending"] or 0)},
                    "next_daily_claim_at": datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=ADDIS).isoformat()}

    # ------------------------------------------------------------------
    # Daily cashback
    # ------------------------------------------------------------------
    async def _cashback_today(self, c, player_id: str, percent: int, promos: list = (), cap: int = 0) -> dict:
        """What the player sees on the cashback card: today's deposits, whether they withdrew, and what
        tomorrow's credit would be if they leave the money in (promotions and the daily cap included)."""
        now_local = datetime.now(ADDIS)
        today = now_local.date()
        rows = await c.fetch(
            "SELECT amount, (COALESCE(resolved_at, created_at) AT TIME ZONE 'Africa/Addis_Ababa') AS local_ts FROM transactions "
            "WHERE player_id=$1 AND type='top_up_approved' "
            "AND (COALESCE(resolved_at, created_at) AT TIME ZONE 'Africa/Addis_Ababa')::date = $2", player_id, today)
        withdrew = await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM transactions WHERE player_id=$1 AND type IN ('withdrawal_pending','withdrawal_approved') "
            "AND (created_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $2)", player_id, today)
        last = await c.fetchrow(
            "SELECT amount_etb, local_reward_date FROM bonus_claims WHERE player_id=$1 AND reward_type='cashback' "
            "AND status='granted' ORDER BY local_reward_date DESC LIMIT 1", player_id)
        deposited = int(sum(int(r["amount"]) for r in rows))
        boosted = weighted_deposits([(r["local_ts"], r["amount"]) for r in rows], list(promos)).get(today, 0)
        return {
            "percent": percent,
            "today_deposited_etb": deposited,
            "withdrew_today": bool(withdrew),
            "pending_etb": 0 if withdrew else cashback_amount(boosted, percent, cap),
            "boost_now": multiplier_at(list(promos), now_local.replace(tzinfo=None)),
            "last_amount_etb": int(last["amount_etb"]) if last else 0,
            "last_date": last["local_reward_date"].isoformat() if last else None,
        }

    async def _settle_cashback_safely(self, player_id: str) -> None:
        try:
            await asyncio.wait_for(self.settle_cashback(player_id), timeout=20)
        except Exception:  # noqa: BLE001 - cashback trouble must never affect the player's screen
            logger.exception("cashback settlement failed for %s", player_id)
        finally:
            _SETTLING.discard(player_id)

    async def settle_cashback(self, player_id: str) -> list[dict]:
        """Pay cashback for every finished day (last CASHBACK_LOOKBACK_DAYS) on which the player deposited and did
        not withdraw. Safe to call as often as you like: each day is paid once (unique claim + ledger key)."""
        pool = await get_pool()
        today = datetime.now(ADDIS).date()
        first_day = today - timedelta(days=CASHBACK_LOOKBACK_DAYS)
        since = datetime.combine(first_day, datetime.min.time(), tzinfo=ADDIS)
        async with pool.acquire() as c:
            camp = await c.fetchrow("SELECT amount_etb, enabled FROM bonus_campaigns WHERE reward_type='cashback'")
            if not camp or not camp["enabled"]:
                return []
            if await _cashback_paused_by_control():
                return []
            excluded, cap = await _cashback_player_limits(c, player_id)
            if excluded:
                return []
            promos = await _cashback_promotions()
            dep_rows = await c.fetch(
                "SELECT amount, (COALESCE(resolved_at, created_at) AT TIME ZONE 'Africa/Addis_Ababa') AS local_ts "
                "FROM transactions WHERE player_id=$1 AND type='top_up_approved' AND COALESCE(resolved_at, created_at) >= $2",
                player_id, since)
            if not dep_rows:
                return []
            wd_rows = await c.fetch(
                "SELECT DISTINCT (created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS d FROM transactions "
                "WHERE player_id=$1 AND type IN ('withdrawal_pending','withdrawal_approved') AND created_at >= $2",
                player_id, since)
            done_rows = await c.fetch(
                "SELECT local_reward_date FROM bonus_claims WHERE player_id=$1 AND reward_type='cashback' AND local_reward_date >= $2",
                player_id, first_day)
        pairs = [(r["local_ts"], r["amount"]) for r in dep_rows]
        actual: dict = {}
        for ts, amount in pairs:
            actual[ts.date()] = actual.get(ts.date(), 0) + int(amount)
        due = cashback_days_due(weighted_deposits(pairs, promos), {r["d"] for r in wd_rows},
                                {r["local_reward_date"] for r in done_rows}, today)
        granted = []
        for day in sorted(due):
            async with pool.acquire() as c:
                async with c.transaction():
                    res = await self._grant_cashback_in_tx(c, player_id, day, due[day], cap, actual.get(day, due[day]))
            if res.get("status") == "granted":
                granted.append(res)
                self.schedule_budget_check("cashback")
                try:
                    from src.services import player_notifications as pn
                    pn.schedule_notify(player_id, "cashback_granted",
                                       {"amount": res["amount_etb"], "percent": res["percent"], "day": day.isoformat()},
                                       dedupe_key=f"cashback:{player_id}:{day.isoformat()}")
                except Exception:  # noqa: BLE001
                    logger.warning("cashback notification not scheduled for %s", player_id)
        return granted

    async def _grant_cashback_in_tx(self, c, player_id: str, day, deposited_etb: int, cap: int = 0, actual_etb: int = 0) -> dict:
        key = f"cashback:{player_id}:{day.isoformat()}"
        now = datetime.now(timezone.utc)
        await c.execute("SET LOCAL lock_timeout = '3s'")   # never queue behind other bonus/wallet work for long
        await c.execute("SELECT pg_advisory_xact_lock(hashtext('melbete_bonus_campaign'))")
        if await c.fetchval("SELECT EXISTS(SELECT 1 FROM bonus_ledger WHERE idempotency_key=$1)", key):
            return {"status": "already_processed"}
        camp = await c.fetchrow("SELECT * FROM bonus_campaigns WHERE reward_type='cashback' FOR UPDATE")
        globally_on = await c.fetchval("SELECT globally_enabled FROM bonus_campaign_state WHERE id=1 FOR UPDATE")
        if (not globally_on or not camp or not camp["enabled"]
                or (camp["starts_at"] and camp["starts_at"] > now) or (camp["ends_at"] and camp["ends_at"] <= now)):
            return {"status": "campaign_paused"}
        player = await c.fetchrow("SELECT registration_state, is_banned FROM players WHERE id=$1 FOR UPDATE", player_id)
        if not player or player["is_banned"] or player["registration_state"] != "complete":
            return {"status": "not_eligible"}
        percent = int(camp["amount_etb"])
        amount = cashback_amount(deposited_etb, percent, cap)
        if amount <= 0:
            return {"status": "below_minimum"}
        daily = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type='cashback' AND amount_etb > 0 AND created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'")
        total = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type='cashback' AND amount_etb > 0")
        if int(daily or 0) + amount > int(camp["daily_budget_etb"]) or int(total or 0) + amount > int(camp["total_budget_etb"]):
            return {"status": "budget_exhausted"}
        await c.execute("INSERT INTO wallets(id, player_id, bonus_balance) VALUES ($1,$2,0) ON CONFLICT(player_id) DO NOTHING", f"wallet-{player_id}", player_id)
        await c.execute("UPDATE wallets SET bonus_balance=bonus_balance+$2, updated_at=now() WHERE player_id=$1", player_id, amount)
        capped = bool(cap and cap > 0 and cashback_amount(deposited_etb, percent) > cap)
        shown = actual_etb or deposited_etb
        note = (f"Cashback: {percent}% of {shown} ETB deposited on {day.isoformat()} (no withdrawal that day)"
                + (f", boosted by promotions to {deposited_etb} ETB" if deposited_etb != shown else "")
                + (f", capped at {cap} ETB" if capped else "") + "; cartelas only")
        ledger = await c.fetchrow(
            "INSERT INTO bonus_ledger(player_id,amount_etb,entry_type,reward_type,source_id,idempotency_key,note) "
            "VALUES($1,$2,'grant','cashback',$3,$4,$5) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id",
            player_id, amount, day.isoformat(), key, note)
        if not ledger:
            return {"status": "already_processed"}
        await c.execute(
            "INSERT INTO bonus_claims(player_id,reward_type,local_reward_date,status,amount_etb,ledger_id,idempotency_key) "
            "VALUES($1,'cashback',$2,'granted',$3,$4,$5)", player_id, day, amount, ledger["id"], key)
        return {"status": "granted", "amount_etb": amount, "percent": percent, "day": day.isoformat(), "deposited_etb": int(actual_etb or deposited_etb)}

    # ------------------------------------------------------------------
    # Budget usage + alerts
    # ------------------------------------------------------------------
    async def budget_usage(self) -> dict:
        """{reward_type: {today_etb, total_etb, today_count}} of granted credit (Ethiopia-time day)."""
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT reward_type, "
            "COALESCE(SUM(amount_etb) FILTER (WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'),0) AS today_etb, "
            "COALESCE(SUM(amount_etb),0) AS total_etb, "
            "COUNT(*) FILTER (WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa') AS today_count "
            "FROM bonus_ledger WHERE entry_type='grant' AND amount_etb > 0 GROUP BY reward_type")
        return {r["reward_type"]: {"today_etb": int(r["today_etb"]), "total_etb": int(r["total_etb"]), "today_count": int(r["today_count"])} for r in rows}

    def schedule_budget_check(self, reward_type: str) -> None:
        try:
            asyncio.get_running_loop().create_task(self.check_budget_alert(reward_type))
        except RuntimeError:
            pass

    async def check_budget_alert(self, reward_type: str) -> None:
        """Tell admins (Telegram) when a campaign reaches 80% and 100% of its daily or total budget."""
        try:
            pool = await get_pool()
            camp = await pool.fetchrow("SELECT daily_budget_etb, total_budget_etb FROM bonus_campaigns WHERE reward_type=$1", reward_type)
            if not camp:
                return
            use = (await self.budget_usage()).get(reward_type, {"today_etb": 0, "total_etb": 0})
            today = datetime.now(ADDIS).date().isoformat()
            from src.services.admin_alerts import get_alert_service
            for scope, used, budget, key in (("daily", use["today_etb"], int(camp["daily_budget_etb"]), today), ("total", use["total_etb"], int(camp["total_budget_etb"]), "all")):
                level = budget_alert_level(used, budget)
                if level:
                    get_alert_service().schedule(
                        "bonus_budget", {"reward_type": reward_type, "scope": scope, "level": level, "used": used, "budget": budget},
                        dedupe_key=f"{reward_type}:{scope}:{key}:{level}", cooldown=86400.0)
        except Exception:  # noqa: BLE001
            logger.exception("bonus budget alert check failed for %s", reward_type)

    async def claim(self, player_id: str, reward_type: str, idempotency_key: str) -> dict:
        if reward_type not in {"welcome", "daily"}:
            raise BonusError("INVALID_REWARD", "This reward cannot be claimed directly.")
        key = (idempotency_key or "").strip()
        if not 8 <= len(key) <= 128:
            raise BonusError("INVALID_IDEMPOTENCY_KEY", "A valid idempotency key is required.")
        pool = await get_pool()
        now = datetime.now(timezone.utc)
        local_date = datetime.now(ADDIS).date() if reward_type == "daily" else None
        async with pool.acquire() as c:
            async with c.transaction():
                # Serialize campaign claims so concurrent requests cannot overspend budget.
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('melbete_bonus_campaign'))")
                existing = await c.fetchrow("SELECT * FROM bonus_claims WHERE idempotency_key=$1", key)
                if existing:
                    if existing["player_id"] != player_id or existing["reward_type"] != reward_type:
                        raise BonusError("IDEMPOTENCY_CONFLICT", "This request key was already used.", 409)
                    if existing["status"] == "granted":
                        bal = await c.fetchval("SELECT bonus_balance FROM wallets WHERE player_id=$1", player_id)
                        return {"status":"already_claimed", "amount_etb":int(existing["amount_etb"]), "bonus_balance":int(bal or 0)}
                    raise BonusError("ALREADY_PROCESSED", "This claim was already processed.", 409)
                player = await c.fetchrow("SELECT id, registration_state, is_banned FROM players WHERE id=$1 FOR UPDATE", player_id)
                if not player:
                    raise BonusError("PLAYER_NOT_FOUND", "Player account was not found.", 404)
                if player["is_banned"]:
                    raise BonusError("ACCOUNT_SUSPENDED", "This account cannot claim bonuses.", 403)
                if player["registration_state"] != "complete":
                    raise BonusError("VERIFICATION_REQUIRED", "Complete Telegram contact verification before claiming.", 403)
                camp = await c.fetchrow("SELECT * FROM bonus_campaigns WHERE reward_type=$1 FOR UPDATE", reward_type)
                globally_on = await c.fetchval("SELECT globally_enabled FROM bonus_campaign_state WHERE id=1 FOR UPDATE")
                if not globally_on or not camp or not camp["enabled"] or (camp["starts_at"] and camp["starts_at"] > now) or (camp["ends_at"] and camp["ends_at"] <= now):
                    raise BonusError("CAMPAIGN_PAUSED", "This bonus campaign is not currently available.", 403)
                if reward_type == "welcome":
                    was_claimed = await c.fetchval("SELECT EXISTS(SELECT 1 FROM bonus_claims WHERE player_id=$1 AND reward_type='welcome' AND status='granted')", player_id)
                else:
                    was_claimed = await c.fetchval("SELECT EXISTS(SELECT 1 FROM bonus_claims WHERE player_id=$1 AND reward_type='daily' AND local_reward_date=$2 AND status='granted')", player_id, local_date)
                if was_claimed:
                    raise BonusError("ALREADY_CLAIMED", "You have already claimed this reward.", 409)
                spent_daily = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type=$1 AND amount_etb > 0 AND created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'", reward_type)
                spent_total = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type=$1 AND amount_etb > 0", reward_type)
                amount = int(camp["amount_etb"])
                if int(spent_daily or 0) + amount > int(camp["daily_budget_etb"]) or int(spent_total or 0) + amount > int(camp["total_budget_etb"]):
                    raise BonusError("BUDGET_EXHAUSTED", "The bonus budget has been reached. Please try again later.", 409)
                await c.execute("INSERT INTO wallets(id, player_id, bonus_balance) VALUES ($1,$2,0) ON CONFLICT(player_id) DO NOTHING", f"wallet-{player_id}", player_id)
                await c.execute("UPDATE wallets SET bonus_balance=bonus_balance+$2, updated_at=now() WHERE player_id=$1", player_id, amount)
                ledger = await c.fetchrow("INSERT INTO bonus_ledger(player_id,amount_etb,entry_type,reward_type,source_id,idempotency_key,note) VALUES($1,$2,'grant',$3,$4,$5,$6) RETURNING id", player_id, amount, reward_type, key, key, f"{reward_type.title()} bonus; non-withdrawable, cartelas only")
                await c.execute("INSERT INTO bonus_claims(player_id,reward_type,local_reward_date,status,amount_etb,ledger_id,idempotency_key) VALUES($1,$2,$3,'granted',$4,$5,$6)", player_id, reward_type, local_date, amount, ledger["id"], key)
                balance = await c.fetchval("SELECT bonus_balance FROM wallets WHERE player_id=$1", player_id)
                self.schedule_budget_check(reward_type)
                return {"status":"granted", "reward_type":reward_type, "amount_etb":amount, "bonus_balance":int(balance)}

    async def award_first_deposit(self, player_id: str, deposit_id: str, deposit_amount_etb: int) -> dict:
        """Standalone entry point: grants the first-deposit reward in its own transaction.

        Idempotent per deposit ID, so it is also the safe way to re-run a reward
        that failed earlier (see `record_award_failure`). Deposit approval itself
        uses `award_first_deposit_in_tx` so the reward commits atomically with the
        cash credit.
        """
        pool = await get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                result = await self.award_first_deposit_in_tx(c, player_id, deposit_id, deposit_amount_etb)
        if isinstance(result, dict) and result.get("status") == "granted":
            self.schedule_budget_check("first_deposit")
        return result

    async def award_first_deposit_in_tx(self, c, player_id: str, deposit_id: str, deposit_amount_etb: int) -> dict:
        """Grant the configured percentage of a player's first approved deposit.

        The caller must already be inside a transaction on connection `c`.
        The approved deposit transaction ID is the idempotency source. The bonus is
        credited to the separate non-withdrawable bonus wallet, never the cash balance.
        Campaign amount_etb stores the percentage (50 means 50%).

        Lock order (shared with deposit approval and `claim`): the campaign
        advisory lock is always taken before any transaction/wallet row lock.
        """
        if deposit_amount_etb <= 0:
            return {"status": "not_eligible"}
        key = f"first-deposit:{deposit_id}"
        now = datetime.now(timezone.utc)
        await c.execute("SELECT pg_advisory_xact_lock(hashtext('melbete_bonus_campaign'))")
        existing = await c.fetchrow("SELECT amount_etb FROM bonus_ledger WHERE idempotency_key=$1", key)
        if existing:
            balance = await c.fetchval("SELECT bonus_balance FROM wallets WHERE player_id=$1", player_id)
            return {"status": "already_processed", "amount_etb": int(existing["amount_etb"]), "bonus_balance": int(balance or 0)}
        campaign = await c.fetchrow("SELECT * FROM bonus_campaigns WHERE reward_type='first_deposit' FOR UPDATE")
        globally_on = await c.fetchval("SELECT globally_enabled FROM bonus_campaign_state WHERE id=1 FOR UPDATE")
        if not globally_on or not campaign or not campaign["enabled"] or (campaign["starts_at"] and campaign["starts_at"] > now) or (campaign["ends_at"] and campaign["ends_at"] <= now):
            return {"status": "campaign_paused"}
        # Only award when no earlier first-deposit reward has been recorded.
        already = await c.fetchval("SELECT EXISTS(SELECT 1 FROM bonus_claims WHERE player_id=$1 AND reward_type='first_deposit' AND status='granted')", player_id)
        if already:
            return {"status": "already_processed"}
        percent = int(campaign["amount_etb"])
        if percent < 1 or percent > 100:
            raise BonusError("INVALID_CAMPAIGN", "First-deposit percentage must be between 1 and 100.", 400)
        amount = (int(deposit_amount_etb) * percent) // 100
        if amount <= 0:
            return {"status": "below_minimum"}
        daily = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type='first_deposit' AND amount_etb > 0 AND created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'")
        total = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type='first_deposit' AND amount_etb > 0")
        if int(daily or 0) + amount > int(campaign["daily_budget_etb"]) or int(total or 0) + amount > int(campaign["total_budget_etb"]):
            return {"status": "budget_exhausted"}
        await c.execute("INSERT INTO wallets(id,player_id,bonus_balance) VALUES($1,$2,0) ON CONFLICT(player_id) DO NOTHING", f"wallet-{player_id}", player_id)
        await c.execute("UPDATE wallets SET bonus_balance=bonus_balance+$2,updated_at=now() WHERE player_id=$1", player_id, amount)
        ledger = await c.fetchrow("INSERT INTO bonus_ledger(player_id,amount_etb,entry_type,reward_type,source_id,idempotency_key,note) VALUES($1,$2,'grant','first_deposit',$3,$4,$5) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id", player_id, amount, deposit_id, key, f"First deposit reward: {percent}% of {deposit_amount_etb} ETB; non-withdrawable, cartelas only")
        if not ledger:
            raise BonusError("ALREADY_PROCESSED", "This first-deposit reward was already processed.", 409)
        await c.execute("INSERT INTO bonus_claims(player_id,reward_type,local_reward_date,status,amount_etb,ledger_id,idempotency_key) VALUES($1,'first_deposit',NULL,'granted',$2,$3,$4)", player_id, amount, ledger["id"], key)
        balance = await c.fetchval("SELECT bonus_balance FROM wallets WHERE player_id=$1", player_id)
        return {"status": "granted", "reward_type": "first_deposit", "percentage": percent, "deposit_amount_etb": int(deposit_amount_etb), "amount_etb": amount, "bonus_balance": int(balance or 0)}

    async def record_award_failure(self, c, player_id: str, deposit_id: str, error: str) -> None:
        """Leave a durable audit row when an automatic first-deposit reward fails,
        so the miss is discoverable and can be re-run with `award_first_deposit`."""
        await c.execute(
            "INSERT INTO bonus_admin_audit(admin_telegram_id,action,target_type,target_id,before_json,after_json,reason) "
            "VALUES('system','first_deposit_reward_failed','deposit',$1,$2,$3,$4)",
            deposit_id, {"player_id": player_id}, {"error": error[:300]},
            "Automatic first-deposit reward failed; the cash deposit was credited. "
            "Re-run award_first_deposit for this deposit (idempotent).",
        )


    async def qualify_referral(self, referred_player_id: str, idempotency_key: str) -> dict:
        """Pay the referrer once when the referred player completes onboarding."""
        pool = await get_pool()
        key = (idempotency_key or f"referral:{referred_player_id}").strip()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('melbete_bonus_campaign'))")
                referred = await c.fetchrow("SELECT id, referred_by, registration_state, is_banned FROM players WHERE id=$1 FOR UPDATE", referred_player_id)
                if not referred or referred["registration_state"] != "complete" or referred["is_banned"] or not referred["referred_by"]:
                    return {"status":"not_qualified"}
                # Existing referrals table maps referrer IDs and uniquely prevents duplicate referred accounts.
                ref = await c.fetchrow("SELECT * FROM referrals WHERE referred_id=$1 FOR UPDATE", referred_player_id)
                if not ref or ref["referrer_id"] == referred_player_id or ref["awarded"]:
                    return {"status":"already_processed" if ref and ref["awarded"] else "not_qualified"}
                campaign = await c.fetchrow("SELECT * FROM bonus_campaigns WHERE reward_type='referral' FOR UPDATE")
                global_on = await c.fetchval("SELECT globally_enabled FROM bonus_campaign_state WHERE id=1 FOR UPDATE")
                now = datetime.now(timezone.utc)
                if not global_on or not campaign or not campaign["enabled"] or (campaign["starts_at"] and campaign["starts_at"] > now) or (campaign["ends_at"] and campaign["ends_at"] <= now):
                    return {"status":"campaign_paused"}
                count = await c.fetchval("SELECT COUNT(*) FROM referrals WHERE referrer_id=$1 AND awarded=TRUE", ref["referrer_id"])
                if int(count or 0) >= int(campaign["per_player_cap"]):
                    return {"status":"referral_cap_reached"}
                amount = int(campaign["amount_etb"])
                daily = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type='referral' AND amount_etb > 0 AND created_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa'")
                total = await c.fetchval("SELECT COALESCE(SUM(amount_etb),0) FROM bonus_ledger WHERE reward_type='referral' AND amount_etb > 0")
                if int(daily or 0)+amount > campaign["daily_budget_etb"] or int(total or 0)+amount > campaign["total_budget_etb"]:
                    return {"status":"budget_exhausted"}
                await c.execute("INSERT INTO wallets(id,player_id,bonus_balance) VALUES($1,$2,0) ON CONFLICT(player_id) DO NOTHING", f"wallet-{ref['referrer_id']}", ref["referrer_id"])
                await c.execute("UPDATE wallets SET bonus_balance=bonus_balance+$2,updated_at=now() WHERE player_id=$1", ref["referrer_id"], amount)
                ledger = await c.fetchrow("INSERT INTO bonus_ledger(player_id,amount_etb,entry_type,reward_type,source_id,idempotency_key,note) VALUES($1,$2,'grant','referral',$3,$4,$5) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id", ref["referrer_id"], amount, referred_player_id, key, "Referral reward; non-withdrawable, cartelas only")
                if not ledger:
                    # Raising forces the transaction to roll back the wallet update.
                    raise BonusError("ALREADY_PROCESSED", "This referral reward was already processed.", 409)
                await c.execute("UPDATE referrals SET awarded=TRUE,bonus_amount=$2,awarded_at=now() WHERE id=$1", ref["id"], amount)
                await c.execute("UPDATE players SET referral_count=referral_count+1 WHERE id=$1", ref["referrer_id"])
                return {"status":"granted", "amount_etb":amount, "referrer_player_id":ref["referrer_id"]}
