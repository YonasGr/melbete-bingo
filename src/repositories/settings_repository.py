"""Postgres repository for the singleton system_settings row."""
import time
from typing import Optional

from src.models.schemas import SystemSettings, utcnow
from src.repositories.base import BaseRepository

_DOC_ID = "global"

_COLUMNS = (
    "id", "stake_amount", "free_credit_amount", "lobby_countdown_seconds",
    "draw_interval", "min_deposit", "max_deposit", "min_withdrawal",
    "max_withdrawal", "payout_ratio", "referral_bonus", "max_referrals_per_user",
    "cards_per_player", "min_players_to_start", "min_cartelas_to_start", "cartela_min", "cartela_max",
    "game_inactivity_timeout_minutes", "support_contact", "bot_system_enabled",
    "deposits_enabled", "max_bots_to_join",
    "stake_amount_real_10", "stake_amount_real_50", "stake_amount_bonus",
    "bot_enabled_real_10", "bot_enabled_real_50", "bot_enabled_bonus",
    "max_bots_real_10", "max_bots_real_50", "max_bots_bonus",
    "payout_custom_real_10", "payout_custom_real_50", "payout_custom_bonus",
    "payout_ratio_real_10", "payout_ratio_real_50", "payout_ratio_bonus",
    "updated_at",
)

# These settings change only when an admin edits them in the panel, yet
# get_or_create() used to be called on EVERY /api/session request - i.e.
# every player's poll tick (every ~15s) and every game action, all hitting
# the database just to read values that are almost always unchanged. Under
# many concurrent players this alone was enough to help exhaust the DB
# connection pool. A short process-wide cache, invalidated immediately on
# `update()`, cuts that to near-zero extra DB load while still picking up
# admin changes within a few seconds - and instantly for the admin who
# made the change.
_CACHE_TTL_SECONDS = 5.0
_cached_settings: Optional[SystemSettings] = None
_cached_at: float = 0.0


class SettingsRepository(BaseRepository):
    table_name = "system_settings"

    async def get(self) -> Optional[SystemSettings]:
        row = await self._fetchrow("SELECT * FROM system_settings WHERE id = $1", _DOC_ID)
        return SystemSettings(**row) if row else None

    async def get_or_create(self) -> SystemSettings:
        global _cached_settings, _cached_at
        now = time.monotonic()
        if _cached_settings is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
            return _cached_settings

        existing = await self.get()
        if existing:
            _cached_settings, _cached_at = existing, now
            return existing

        defaults = SystemSettings(id=_DOC_ID)
        data = defaults.model_dump()
        placeholders = ", ".join(f"${i + 1}" for i in range(len(_COLUMNS)))
        columns = ", ".join(_COLUMNS)
        await self._execute(
            f"""
            INSERT INTO system_settings ({columns}) VALUES ({placeholders})
            ON CONFLICT (id) DO NOTHING
            """,
            *(data[c] for c in _COLUMNS),
        )
        # Someone else may have created it in a race between the check
        # above and this insert - fetch back the authoritative row either
        # way, exactly like Firestore's set(merge=True) round trip did.
        result = await self.get()
        _cached_settings, _cached_at = result, now
        return result

    async def update(self, fields: dict) -> SystemSettings:
        global _cached_settings, _cached_at
        await self.get_or_create()  # ensure the row exists first
        fields = {**fields, "updated_at": utcnow()}
        await self._update_fields("system_settings", "id", _DOC_ID, fields)
        result = await self.get()
        # Invalidate immediately so the admin who just changed a setting -
        # and everyone else within the next request - sees the new value
        # right away instead of waiting out the TTL.
        _cached_settings, _cached_at = result, time.monotonic()
        return result
