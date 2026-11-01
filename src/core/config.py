"""
Application configuration.

All configuration is provided via environment variables (or a local .env
file when developing). See `.env.example` for the full list of variables
and README.md for a guided setup (Telegram bot token + Supabase project).
"""
from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.secret_check import weak_secret_reason


class Settings(BaseSettings):
    """Application settings, loaded from environment variables / .env"""

    # --- Telegram Bot -------------------------------------------------
    telegram_bot_token: str
    # Bot's @username (no @), e.g. "melbetebingo_bot" - used to build the
    # t.me deep link for the web account-linking flow. Falls back to
    # asking Telegram's API for it at startup if left blank (see
    # telegram_bot.py's on_startup), but setting it explicitly avoids
    # that extra call.
    telegram_bot_username: str = ""
    # Public HTTPS URL where this app is reachable (Render/Railway/Fly/etc).
    # Used to build the Telegram Mini App link and (optionally) the webhook.
    public_base_url: Optional[str] = None
    # If set, the bot runs with a webhook instead of long-polling.
    telegram_webhook_secret: Optional[str] = None
    telegram_game_short_name: Optional[str] = "bingo"
    # How long a Telegram Mini App's signed `initData` stays accepted after
    # it was issued. Telegram generates this string once when the Mini App
    # launches and does NOT refresh it while the app stays open in the
    # background - a bingo lobby wait + a full round easily runs well past
    # any short window, so this must comfortably outlast a normal play
    # session or every identity-checked call (profile, wallet, claim, the
    # WebSocket handshake) starts failing partway through a game with no
    # user-visible cause other than "reopen the app". Default: 6 hours.
    telegram_init_data_max_age_seconds: int = 6 * 60 * 60

    # --- Database (any standard Postgres) -------------------------------
    # Any standard Postgres connection string works here - Supabase,
    # Azure Database for PostgreSQL, self-hosted, etc. See
    # MIGRATING_TO_SUPABASE.md for the Supabase-specific walkthrough.
    # If connecting through a transaction-mode PgBouncer pooler (e.g.
    # Supabase's pooler, host contains "pooler.supabase.com", port 6543),
    # leave db_use_prepared_statements=False (the default) - see the note
    # on that setting below.
    # Example: postgresql://user:PASSWORD@your-db-host:5432/dbname
    database_url: str
    # Connection pool sizing - tune these up as concurrent players grow.
    # 10 max connections is fine for local dev/testing but saturates fast
    # once many players are polling /api/session + taking actions at once,
    # which shows up as slow/failed requests across the whole app. Check
    # your Postgres provider's own max_connections limit (varies by plan/
    # tier) before raising this much further, especially if anything else
    # (the bot process, an admin session, migrations) also connects.
    db_pool_min_size: int = 2
    db_pool_max_size: int = 30
    # Seconds a query may run before asyncpg gives up and raises - without
    # this, one hung query can tie up a pool connection indefinitely.
    db_command_timeout_seconds: float = 15.0
    # asyncpg's server-side prepared statement cache. Left off by default
    # because Supabase's recommended connection pooler runs PgBouncer in
    # transaction mode, which doesn't support server-side prepared
    # statements at all - if left on, every query would fail against that
    # pooler. If you're connecting directly to Postgres (no PgBouncer in
    # front) - which is the normal setup for Azure Database for
    # PostgreSQL, most self-hosted setups, and Supabase's non-pooled
    # "direct connection" string - turning this on is safe and saves a
    # small amount of repeat-query overhead. When in doubt, leave it off;
    # it costs very little either way.
    db_use_prepared_statements: bool = False

    # --- Application -----------------------------------------------------
    secret_key: str
    app_env: str = "development"
    debug: bool = False
    reconciliation_hour_eat: int = 3  # hour (Ethiopia time) of the daily wallet reconciliation report; -1 disables

    @model_validator(mode="after")
    def _reject_weak_secret_key(self):
        """SECRET_KEY signs admin sessions. In production a weak or placeholder value stops the app at startup."""
        reason = weak_secret_reason(self.secret_key)
        if reason:
            msg = (f"SECRET_KEY is not safe: {reason}. Generate one with: "
                   "python -c \"import secrets; print(secrets.token_hex(32))\"")
            if self.app_env.strip().lower() == "production":
                raise ValueError(msg)
            import logging

            logging.getLogger(__name__).warning("%s (allowed outside production)", msg)
        return self

    # Rounds with MORE than this many distinct players can have several winners: after every drawn
    # number, every player whose BINGO is completed by that same last number wins and the prize is
    # split equally (see RoomService.settle_round_winners). 0 disables the feature.
    multi_winner_min_players: int = 10
    lobby_countdown_seconds: int = 30  # time players get to pick cartelas before auto-start
    # Non-stop rounds: this many seconds after a round ends (winner screen shown) the server starts the
    # next round by itself, without waiting for anyone to tap "Go to Lobby". 0 = old manual behaviour.
    auto_next_round_seconds: int = 15

    # --- Player engagement (streaks, round nudges, win-back) ---------------
    # All house-funded rewards are bonus balance (playable, never withdrawable). Promotional
    # messages respect each player's /mute opt-out and quiet hours (Ethiopia time, EAT = UTC+3).
    # "days:ETB" pairs: reward when a player has played this many days IN A ROW. Empty = off.
    engagement_streak_rewards: str = "3:10,7:30"
    engagement_winback_days: int = 7  # a player who has not played for this many days gets a free round
    engagement_winback_bonus_etb: int = 10  # 0 disables win-back
    engagement_winback_cooldown_days: int = 30  # at most one win-back bonus per player per this period
    engagement_winback_daily_budget_etb: int = 500  # hard cap on win-back bonus paid per day (house protection)
    engagement_nudge_cooldown_hours: int = 6  # at most one "round starting" nudge per player per period
    engagement_nudge_max_recipients: int = 100  # per nudge batch
    engagement_nudge_batch_gap_minutes: int = 10  # at most one nudge batch per track per period
    engagement_loop_minutes: int = 30  # how often the win-back / streak-reminder job runs; 0 disables it
    engagement_quiet_start_hour: int = 22  # no promotional messages from this hour (EAT) ...
    engagement_quiet_end_hour: int = 7  # ... until this hour

    # --- Deposits / withdrawals --------------------------------------------
    min_deposit_amount: int = 50
    max_deposit_amount: int = 70000
    min_withdrawal_amount: int = 200
    max_withdrawal_amount: int = 10000

    # --- Admin / payments -----------------------------------------------
    # Telegram numeric user ID of the ONE super admin, bootstrapped from the
    # environment on first startup. The super admin can promote/demote other
    # admins from the admin panel; nothing else needs an env var again.
    super_admin_telegram_id: Optional[str] = None
    # One Base32 TOTP seed per administrator: "telegram_id:BASE32SEED,...".
    # This is mandatory for admin login; there is deliberately no desktop
    # shared-secret fallback. Keep it in a secrets manager, never in git.
    admin_totp_secrets: str = ""
    # Legacy fallback list (comma-separated Telegram numeric IDs). Still
    # honored for backwards compatibility, but new admins should be managed
    # via AdminService/the admin panel instead of editing env vars.
    admin_telegram_ids: str = ""
    # Chat ID (group/channel the admins are in) where top-up screenshots and
    # end-of-round reports are forwarded. If unset, falls back to DMing each
    # id in admin_telegram_ids individually.
    admin_chat_id: Optional[str] = None
    # Hours ahead of UTC used for "today" on the admin dashboard (Ethiopia = +3, no daylight saving).
    dashboard_utc_offset_hours: int = 3
    # Legacy comma-separated payment method names. Payment methods are now
    # managed dynamically (see PaymentMethodRepository / AdminService) so
    # admins can edit account numbers without a redeploy; this only seeds
    # the very first run.
    payment_methods: str = "Telebirr,CBE Birr"

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_telegram_ids.split(",") if x.strip().isdigit()]

    @property
    def payment_method_list(self) -> list[str]:
        return [m.strip() for m in self.payment_methods.split(",") if m.strip()]

    # --- Azure Storage -------------------------------------------------
    azure_storage_connection_string: Optional[str] = None
    azure_storage_container_name: str = "screenshots"

    # --- Server --------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


@lru_cache
def get_settings() -> "Settings":
    """Cached settings accessor - use this instead of instantiating Settings()
    directly so tests / scripts can import config without requiring every
    environment variable to already be set."""
    return Settings()


# Convenience module-level instance used throughout the codebase.
settings = get_settings()
