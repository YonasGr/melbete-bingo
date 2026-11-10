"""
Admin service - everything a super admin needs to run the product without
a redeveloy: manage who else is an admin, edit payment method details
(account numbers, active/inactive), and tune the game/deposit/withdrawal
knobs at runtime.

Identity model: admins are identified by their Telegram numeric user id.
The Mini App sends this id (from Telegram WebApp `initDataUnsafe.user.id`)
on every admin API call; `AdminService.is_admin` / `is_super_admin` gate
access. NOTE: this trusts the client-supplied id - for production, verify
Telegram's `initData` HMAC signature server-side before trusting it. That
verification step is not implemented here and should be added before
going live with real money.
"""
from typing import List, Optional

from src.core.config import settings as env_settings
from src.services import control_logic
from src.models.schemas import AdminUser, PaymentMethod, SystemSettings
from src.repositories import AdminRepository, PaymentMethodRepository, SettingsRepository


from src.services.control_logic import SETTINGS_SPEC

# Human labels for the live game controls (used in validation errors).
_GAME_RANGE_LABELS = {
    "stake_amount": "Stake",
    "stake_amount_real_10": "Real-money stake",
    "stake_amount_real_50": "Real-money (50) stake",
    "stake_amount_bonus": "Bonus stake",
    "lobby_countdown_seconds": "Lobby countdown (seconds)",
    "draw_interval": "Draw interval (seconds)",
    "cards_per_player": "Cartelas per player",
    "min_cartelas_to_start": "Minimum cartelas to start",
}

class NotSuperAdminError(Exception):
    pass


class AdminService:
    def __init__(
        self,
        admin_repo: Optional[AdminRepository] = None,
        payment_repo: Optional[PaymentMethodRepository] = None,
        settings_repo: Optional[SettingsRepository] = None,
    ):
        self.admins = admin_repo or AdminRepository()
        self.payments = payment_repo or PaymentMethodRepository()
        self.settings_repo = settings_repo or SettingsRepository()

    # ------------------------------------------------------------------
    # Bootstrap / lookup
    # ------------------------------------------------------------------

    async def ensure_super_admin_bootstrapped(self) -> None:
        """Called once at startup. Every ID in SUPER_ADMIN_TELEGRAM_ID (one, or several separated by commas)
        becomes a super admin: created if missing, upgraded if it exists as a lower role. Safe to call at
        every startup."""
        for telegram_id in env_settings.super_admin_ids:
            existing = await self.admins.get_by_telegram_id(telegram_id)
            if existing:
                if not existing.is_super_admin:
                    existing.is_super_admin = True
                    await self.admins.create(existing)
                continue
            await self.admins.create(AdminUser(telegram_id=telegram_id, is_super_admin=True))

    async def get_admin(self, telegram_id: str) -> Optional[AdminUser]:
        return await self.admins.get_by_telegram_id(str(telegram_id))

    async def is_admin(self, telegram_id: str) -> bool:
        if telegram_id and int(telegram_id) in env_settings.admin_ids:
            return True  # legacy env-based admins still honored
        return (await self.get_admin(telegram_id)) is not None

    async def is_super_admin(self, telegram_id: str) -> bool:
        admin = await self.get_admin(telegram_id)
        return bool(admin and admin.is_super_admin)

    # ------------------------------------------------------------------
    # Admin management (super admin only)
    # ------------------------------------------------------------------

    async def list_admins(self) -> List[AdminUser]:
        return await self.admins.list_all()

    async def permissions_of(self, telegram_id: str) -> frozenset:
        """Effective permissions of a staff member (empty set = not staff)."""
        admin = await self.get_admin(telegram_id)
        if admin:
            return control_logic.permissions_for(admin.role, admin.extra_permissions, admin.is_super_admin)
        try:
            if telegram_id and int(telegram_id) in env_settings.admin_ids:
                return control_logic.permissions_for("admin")  # legacy env admins keep the pre-roles rights
        except (TypeError, ValueError):
            pass
        return frozenset()

    async def add_admin(
        self,
        requester_telegram_id: str,
        new_admin_telegram_id: str,
        role: str = "admin",
        extra_permissions=None,
    ) -> AdminUser:
        if not await self.is_super_admin(requester_telegram_id):
            raise NotSuperAdminError("Only the super admin can add other admins")
        if role not in control_logic.ASSIGNABLE_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(control_logic.ASSIGNABLE_ROLES)}")
        extra = control_logic.validate_extra_permissions(extra_permissions)
        return await self.admins.create(
            AdminUser(
                telegram_id=str(new_admin_telegram_id), added_by=str(requester_telegram_id),
                role=role, extra_permissions=extra,
            )
        )

    async def set_role(
        self, requester_telegram_id: str, target_telegram_id: str, role: str, extra_permissions=None
    ) -> AdminUser:
        """Change an existing staff member's role preset and extra permissions (super admin only)."""
        if not await self.is_super_admin(requester_telegram_id):
            raise NotSuperAdminError("Only the super admin can change staff roles")
        target = await self.get_admin(target_telegram_id)
        if target is None:
            raise ValueError("That person is not on the staff list")
        if target.is_super_admin:
            raise NotSuperAdminError("A super admin's role cannot be changed here")
        if role not in control_logic.ASSIGNABLE_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(control_logic.ASSIGNABLE_ROLES)}")
        target.role = role
        target.extra_permissions = control_logic.validate_extra_permissions(extra_permissions)
        return await self.admins.create(target)

    @staticmethod
    def is_owner(telegram_id: str) -> bool:
        """The account named in SUPER_ADMIN_TELEGRAM_ID. It is re-created as super admin at every
        startup, so removing or demoting it here would silently undo itself."""
        return str(telegram_id) in env_settings.super_admin_ids

    async def remove_admin(self, requester_telegram_id: str, target_telegram_id: str) -> None:
        """Remove someone's staff access (super admin only). A super admin can be removed by another
        super admin; nobody can remove themselves or the owner account."""
        if not await self.is_super_admin(requester_telegram_id):
            raise NotSuperAdminError("Only a super admin can remove admins")
        target_id = str(target_telegram_id)
        if target_id == str(requester_telegram_id):
            raise ValueError("You cannot remove yourself. Ask another super admin to do it.")
        if self.is_owner(target_id):
            raise NotSuperAdminError("This is the owner account (SUPER_ADMIN_TELEGRAM_ID) and cannot be removed here")
        target = await self.get_admin(target_id)
        if target is None:
            raise ValueError("That person is not on the staff list")
        await self.admins.delete(target_id)

    async def promote_to_super_admin(self, requester_telegram_id: str, target_telegram_id: str) -> AdminUser:
        """Promote an existing admin to super admin. Only a super admin can do this."""
        if not await self.is_super_admin(requester_telegram_id):
            raise NotSuperAdminError("Only a super admin can promote other admins")
        target_id = str(target_telegram_id or "").strip()
        if not target_id.isdigit():
            raise ValueError("Enter the person's numeric Telegram ID")
        admin = await self.get_admin(target_id)
        if admin and admin.is_super_admin:
            raise ValueError("That person is already a super admin")
        if not admin:
            # Auto-create admin record if they don't have one yet
            admin = AdminUser(telegram_id=target_id, added_by=str(requester_telegram_id))
        admin.is_super_admin = True
        return await self.admins.create(admin)

    async def demote_super_admin(
        self, requester_telegram_id: str, target_telegram_id: str, role: str = "admin", extra_permissions=None
    ) -> AdminUser:
        """Turn a super admin back into a regular staff member with the given role (super admin only).
        You cannot demote yourself or the owner account, so at least one super admin always remains."""
        if not await self.is_super_admin(requester_telegram_id):
            raise NotSuperAdminError("Only a super admin can demote a super admin")
        target_id = str(target_telegram_id)
        if target_id == str(requester_telegram_id):
            raise ValueError("You cannot demote yourself. Ask another super admin to do it.")
        if self.is_owner(target_id):
            raise NotSuperAdminError("This is the owner account (SUPER_ADMIN_TELEGRAM_ID) and cannot be demoted here")
        if role not in control_logic.ASSIGNABLE_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(control_logic.ASSIGNABLE_ROLES)}")
        target = await self.get_admin(target_id)
        if target is None:
            raise ValueError("That person is not on the staff list")
        if not target.is_super_admin:
            raise ValueError("That person is not a super admin")
        target.is_super_admin = False
        target.role = role
        target.extra_permissions = control_logic.validate_extra_permissions(extra_permissions)
        return await self.admins.create(target)

    # ------------------------------------------------------------------
    # Payment methods
    # ------------------------------------------------------------------

    async def list_payment_methods(self, active_only: bool = False) -> List[PaymentMethod]:
        # Payment methods are entirely database-managed. A redeploy must
        # never recreate deleted accounts or revive old personal details.
        all_methods = await self.payments.list_all()
        return [m for m in all_methods if m.active] if active_only else all_methods

    async def upsert_payment_method(
        self,
        key: str,
        name: str,
        account_info: str,
        account_holder: Optional[str] = None,
        instructions: Optional[str] = None,
        active: bool = True,
        supports_deposit: bool = True,
        supports_withdrawal: bool = True,
        display_order: int = 0,
        method_id: Optional[str] = None,
    ) -> PaymentMethod:
        if method_id:
            existing = await self.payments.get(method_id)
        else:
            existing = await self.payments.get_by_key(key)
        fields = dict(
            key=key,
            name=name,
            account_info=account_info,
            account_holder=account_holder,
            instructions=instructions,
            active=active,
            supports_deposit=supports_deposit,
            supports_withdrawal=supports_withdrawal,
            display_order=display_order,
        )
        if existing:
            await self.payments.update(existing.id, fields)
            return await self.payments.get(existing.id)
        method = PaymentMethod(**fields)
        return await self.payments.create(method)

    async def delete_payment_method(self, method_id: str) -> None:
        await self.payments.delete(method_id)

    # ------------------------------------------------------------------
    # Runtime settings
    # ------------------------------------------------------------------

    async def get_settings(self) -> SystemSettings:
        return await self.settings_repo.get_or_create()

    async def update_settings(self, requester_telegram_id: str, fields: dict) -> SystemSettings:
        if not await self.is_admin(requester_telegram_id):
            raise NotSuperAdminError("Admin access required to change system settings")
        allowed = {
            "stake_amount",
            "free_credit_amount",
            "lobby_countdown_seconds",
            "draw_interval",
            "min_deposit",
            "max_deposit",
            "min_withdrawal",
            "max_withdrawal",
            "payout_ratio",
            "referral_bonus",
            "max_referrals_per_user",
            "cards_per_player",
            "min_players_to_start",
            "min_cartelas_to_start",
            "cartela_min",
            "cartela_max",
            "game_inactivity_timeout_minutes",
            "support_contact",
            "bot_system_enabled",
            "deposits_enabled",
            "max_bots_to_join",
            "stake_amount_real_10",
            "stake_amount_real_50",
            "stake_amount_bonus",
            "bot_enabled_real_10",
            "bot_enabled_real_50",
            "bot_enabled_bonus",
            "max_bots_real_10",
            "max_bots_real_50",
            "max_bots_bonus",
            "payout_custom_real_10",
            "payout_custom_real_50",
            "payout_custom_bonus",
            "payout_ratio_real_10",
            "payout_ratio_real_50",
            "payout_ratio_bonus",
        }
        clean = {k: v for k, v in fields.items() if k in allowed and v is not None}
        await self._validate_settings_patch(clean)
        # `stake_amount` is the legacy name of the real_10 stake (wallet limits and bot messages still
        # read it). Keep the two in lock-step whichever one the caller sent - the Control Centre only
        # ever sends stake_amount_real_10.
        if "stake_amount_real_10" in clean:
            clean["stake_amount"] = clean["stake_amount_real_10"]
        elif "stake_amount" in clean:
            clean["stake_amount_real_10"] = clean["stake_amount"]
        return await self.settings_repo.update(clean)

    async def _validate_settings_patch(self, clean: dict) -> None:
        """Cross-field rules shared by live updates and the Control Centre's publish step."""
        # Range-check the game controls so a typo (0 seconds, a blank field sent as 0, a 5-digit stake)
        # can never be saved and then break lobbies, the draw loop or the stake maths.
        for key, label in _GAME_RANGE_LABELS.items():
            if key not in clean:
                continue
            _kind, lo, hi = SETTINGS_SPEC.get(key) or SETTINGS_SPEC["stake_amount_real_10"]
            raw = clean[key]
            try:
                if isinstance(raw, bool) or int(float(raw)) != float(raw):
                    raise ValueError
                n = int(float(raw))
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a whole number")
            if not lo <= n <= hi:
                raise ValueError(f"{label} must be between {lo} and {hi}")
            clean[key] = n
        for key in ("payout_ratio_real_10", "payout_ratio_real_50", "payout_ratio_bonus"):
            if key in clean:
                try:
                    ratio = float(clean[key])
                except (TypeError, ValueError):
                    raise ValueError("Payout ratio must be a number")
                if not 0.5 <= ratio <= 1.0:
                    raise ValueError("Payout ratio must be between 0.5 and 1.0")
                clean[key] = ratio
        if "min_players_to_start" in clean and int(clean["min_players_to_start"]) < 1:
            raise ValueError("min_players_to_start must be at least 1")
        curr = await self.get_settings()
        eff_cmin = clean.get("cartela_min", curr.cartela_min)
        eff_cmax = clean.get("cartela_max", curr.cartela_max)
        if eff_cmin > eff_cmax:
            raise ValueError(f"Cartela min ({eff_cmin}) cannot be greater than Cartela max ({eff_cmax})")
        eff_dmin = clean.get("min_deposit", curr.min_deposit)
        eff_dmax = clean.get("max_deposit", curr.max_deposit)
        if eff_dmin > eff_dmax:
            raise ValueError(f"Min deposit ({eff_dmin}) cannot be greater than Max deposit ({eff_dmax})")
        eff_wmin = clean.get("min_withdrawal", curr.min_withdrawal)
        eff_wmax = clean.get("max_withdrawal", curr.max_withdrawal)
        if eff_wmin > eff_wmax:
            raise ValueError(f"Min withdrawal ({eff_wmin}) cannot be greater than Max withdrawal ({eff_wmax})")

    async def check_settings_patch(self, fields: dict) -> None:
        """Dry-run validation (raises ValueError) used before a draft is published."""
        await self._validate_settings_patch({k: v for k, v in fields.items() if v is not None})

    _TRACK_BOT_FIELD = {
        "real_10": "bot_enabled_real_10",
        "real_50": "bot_enabled_real_50",
        "bonus": "bot_enabled_bonus",
    }

    async def toggle_bot_system(self, requester_telegram_id: str, enabled: bool, track: str = "real_10") -> SystemSettings:
        field = self._TRACK_BOT_FIELD.get(track, "bot_enabled_real_10")
        # Keep the legacy global flag in sync with the real_10 track only,
        # so any old code/UI still reading `bot_system_enabled` sees a
        # sensible value rather than a stale one.
        extra = {"bot_system_enabled": enabled} if track == "real_10" else {}
        return await self.update_settings(requester_telegram_id, {field: enabled, **extra})

    async def toggle_deposits(self, requester_telegram_id: str, enabled: bool) -> SystemSettings:
        return await self.update_settings(requester_telegram_id, {"deposits_enabled": enabled})
