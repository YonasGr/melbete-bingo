"""Transactional promotional bonus rewards (amounts are whole ETB, non-withdrawable)."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from src.core.db import get_pool

ADDIS = ZoneInfo("Africa/Addis_Ababa")
AMOUNTS = {"welcome": 20, "referral": 5, "daily": 1}

class BonusError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)

class BonusService:
    async def summary(self, player_id: str) -> dict:
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
                status = "claimed" if ((kind == "welcome" and claimed_welcome) or (kind == "daily" and claimed_daily) or (kind == "first_deposit" and claimed_first_deposit)) else ("automatic" if kind == "first_deposit" and enabled else ("available" if enabled else "paused"))
                result[kind] = {"amount_etb": int(camp["amount_etb"]), "enabled": enabled,
                    "status": status, "reward_kind": "percentage" if kind == "first_deposit" else "fixed"}
            return {"bonus_balance": balance, "currency": "ETB", "restriction": "Bonus credit — non-withdrawable; Bingo cartelas only.",
                    "campaign_enabled": bool(global_on), "rewards": result,
                    "referrals": {"successful": int(referrals["successful"] or 0), "pending": int(referrals["pending"] or 0)},
                    "next_daily_claim_at": datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=ADDIS).isoformat()}

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
                return await self.award_first_deposit_in_tx(c, player_id, deposit_id, deposit_amount_etb)

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
