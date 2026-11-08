"""
Referral program - single source of truth for the bot, the Mini App, and
the admin panel.

How it works end-to-end:
1. Every registered player has an implicit referral link:
   `t.me/<bot>?start=ref_<their_telegram_id>`. No separate code needed.
2. When someone brand-new taps that link and hits /start, the bot passes
   the `ref_<telegram_id>` payload to `record_referral_click`. This links
   the new player's `referred_by` field AND creates a `Referral` audit
   record (referrer -> referred) - but doesn't pay anything yet.
3. The bonus is only paid once the REFERRED player finishes registering
   (shares their contact) - `award_if_eligible` is called from the same
   place `Player.registration_state` flips to "complete". Requiring a
   real registration (not just a link tap) is a meaningful anti-abuse bar.
4. A referrer is only ever PAID for their first
   `SystemSettings.max_referrals_per_user` (default 3) referred
   registrations - further referrals still create a Referral record (full
   audit trail for the admin panel), just with `awarded=False`.
5. The bonus always lands in the referrer's bonus_balance via
   `WalletService.credit_referral_bonus` - playable, never withdrawable,
   the same honest mechanism used everywhere else in this app. There is
   no hidden reduction of anyone's odds of winning; every draw is the
   same fair RNG for every player regardless of how their stake was
   funded.
"""
from typing import Dict, Optional

from src.models.schemas import Player, Referral, SystemSettings
from src.repositories.player_repository import PlayerRepository
from src.repositories.referral_repository import ReferralRepository
from src.repositories.settings_repository import SettingsRepository
from src.services.wallet_service import WalletService


class ReferralService:
    def __init__(
        self,
        referral_repo: Optional[ReferralRepository] = None,
        player_repo: Optional[PlayerRepository] = None,
        wallet_service: Optional[WalletService] = None,
        settings_repo: Optional[SettingsRepository] = None,
    ):
        self.referrals = referral_repo or ReferralRepository()
        self.players = player_repo or PlayerRepository()
        self.wallets = wallet_service or WalletService()
        self.settings_repo = settings_repo or SettingsRepository()

    async def _limits(self) -> Optional[SystemSettings]:
        try:
            return await self.settings_repo.get_or_create()
        except Exception:  # noqa: BLE001
            return None

    async def _cap_and_bonus(self) -> tuple:
        s = await self._limits()
        cap = s.max_referrals_per_user if s else 3
        bonus = s.referral_bonus if s else 20
        return cap, bonus

    def referral_link(self, bot_username: str, referrer_telegram_id: str) -> str:
        if not bot_username:
            return ""
        return f"https://t.me/{bot_username}?start=ref_{referrer_telegram_id}"

    # ------------------------------------------------------------------
    # Click tracking
    # ------------------------------------------------------------------

    async def record_referral_click(self, referrer_telegram_id: Optional[str], new_player: Player) -> Optional[Referral]:
        """Called from the bot's /start handler with a `ref_<telegram_id>`
        payload, ONLY for a genuinely brand-new player (never for a
        returning user - re-triggering /start with a referral link must
        not create duplicate relationships or let someone refer
        themselves)."""
        if not referrer_telegram_id or referrer_telegram_id == new_player.telegram_id:
            return None
        referrer = await self.players.get_by_telegram_id(referrer_telegram_id)
        if not referrer:
            return None  # bogus/stale referral code - ignore quietly

        existing = await self.referrals.get_by_referred_id(new_player.id)
        if existing:
            return existing  # already linked - never overwrite/duplicate

        await self.players.update(new_player.id, {"referred_by": referrer_telegram_id})
        referral = Referral(referrer_id=referrer.id, referred_id=new_player.id)
        return await self.referrals.create(referral)

    # ------------------------------------------------------------------
    # Awarding
    # ------------------------------------------------------------------

    async def award_if_eligible(self, referred_player: Player) -> Optional[Dict]:
        """Call this the moment a player's registration completes. Pays
        the referrer their bonus IF this referral hasn't already been
        awarded AND the referrer hasn't hit their cap yet. Returns details
        for the caller to build a notification message, or None if
        nothing was paid (no referrer, already awarded, or over cap)."""
        if not referred_player.referred_by:
            return None
        from src.services.control_service import reward_blocked
        if await reward_blocked("referral"):
            return None   # referral rewards are paused in the Control Center
        referral = await self.referrals.get_by_referred_id(referred_player.id)
        if not referral or referral.awarded:
            return None  # not referred, or this exact referral was already paid - never pay twice

        referrer = await self.players.get_by_id(referral.referrer_id)
        if not referrer:
            return None

        cap, bonus_amount = await self._cap_and_bonus()
        if referrer.referral_count >= cap:
            return None  # tracked, but this referrer is already at their cap - no payout

        await self.wallets.credit_referral_bonus(referrer.id, bonus_amount, referred_player.id)
        new_count = referrer.referral_count + 1
        await self.players.update(referrer.id, {"referral_count": new_count})
        await self.referrals.update(referral.id, {"awarded": True, "bonus_amount": bonus_amount})

        from src.services import player_notifications as pn

        pn.schedule_notify(referrer.id, "referral_reward", {"amount": bonus_amount}, dedupe_key=f"referral:{referral.id}")
        return {
            "referrer": referrer,
            "bonus_amount": bonus_amount,
            "new_count": new_count,
            "cap": cap,
        }

    # ------------------------------------------------------------------
    # Stats / listing
    # ------------------------------------------------------------------

    async def get_stats(self, player: Player, bot_username: str) -> dict:
        """Everything the profile screen / bot /referral command needs."""
        s = await self._limits()
        cap = s.max_referrals_per_user if s else 3
        bonus_amount = s.referral_bonus if s else 20
        free_credit = s.free_credit_amount if s else 50
        slots_remaining = max(cap - player.referral_count, 0)

        # Count total referrals from this referrer in the database
        all_refs = await self.referrals.list_by_referrer(player.id)
        total_invited = len(all_refs)

        return {
            "referral_link": self.referral_link(bot_username, player.telegram_id),
            "referral_count": player.referral_count,
            "max_referrals": cap,
            "slots_remaining": slots_remaining,
            "bonus_per_referral": bonus_amount,
            "free_credit_amount": free_credit,
            "total_invited": total_invited,
        }

    async def list_all_for_admin(self, limit: int = 500) -> dict:
        """Full "who invited who" list, grouped by referrer, for the admin
        panel's Referrals tab."""
        referrals = await self.referrals.list_all(limit=limit)
        cap, bonus_amount = await self._cap_and_bonus()

        grouped: Dict[str, dict] = {}
        for r in referrals:
            if r.referrer_id not in grouped:
                referrer = await self.players.get_by_id(r.referrer_id)
                grouped[r.referrer_id] = {
                    "referrer_id": r.referrer_id,
                    "referrer_name": referrer.display_name if referrer else r.referrer_id,
                    "referrer_telegram_id": referrer.telegram_id if referrer else None,
                    "total_referrals": referrer.referral_count if referrer else 0,
                    "referred_users": [],
                }
            referred = await self.players.get_by_id(r.referred_id)
            grouped[r.referrer_id]["referred_users"].append({
                "player_id": r.referred_id,
                "display_name": referred.display_name if referred else r.referred_id,
                "telegram_id": referred.telegram_id if referred else None,
                "joined_at": r.created_at.isoformat(),
                "awarded": r.awarded,
                "bonus_amount": r.bonus_amount,
            })

        return {
            "referrals": list(grouped.values()),
            "total_referrers": len(grouped),
            "total_referred_users": len(referrals),
            "bonus_per_referral": bonus_amount,
            "max_referrals_per_user": cap,
        }
