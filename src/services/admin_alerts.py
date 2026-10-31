"""Telegram alerts to admins: who gets what, delivery, cooldowns and failed-login tracking.

Alerts are fire-and-forget: sending never blocks or breaks the request that caused it, and a
Telegram failure is logged, not raised. Types, defaults and message text live in control_logic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from src.services import control_logic as cl

logger = logging.getLogger(__name__)

_FAILED_LOGIN_WINDOW = 600.0      # seconds
_FAILED_LOGIN_COOLDOWN = 900.0    # one alert per source per 15 minutes
_FLAG_CACHE_TTL = 300.0


class FailedLoginTracker:
    """Counts failed admin sign-ins per source within a sliding window."""

    def __init__(self, window: float = _FAILED_LOGIN_WINDOW):
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._alerted: dict[str, float] = {}

    def record(self, key: str, threshold: int, now: Optional[float] = None) -> Optional[int]:
        """Returns the attempt count when an alert should fire now, else None."""
        now = time.monotonic() if now is None else now
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 5000:  # bound memory under a distributed scan
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
        if len(hits) >= max(1, threshold) and now - self._alerted.get(key, -1e9) >= _FAILED_LOGIN_COOLDOWN:
            self._alerted[key] = now
            return len(hits)
        return None


class AdminAlertService:
    def __init__(
        self,
        admin_repo=None,
        bot_getter: Optional[Callable[[], Awaitable[Any]]] = None,
        config_getter: Optional[Callable[[], Awaitable[dict]]] = None,
        flagged_getter: Optional[Callable[[], Awaitable[set]]] = None,
        group_chat_id: Optional[str] = None,
        env_admin_ids: Optional[list] = None,
    ):
        self._admin_repo = admin_repo
        self._bot_getter = bot_getter
        self._config_getter = config_getter
        self._flagged_getter = flagged_getter
        self._group = group_chat_id
        self._env_admins = env_admin_ids
        self._cooldowns: dict[str, float] = {}
        self._tasks: set = set()
        self._flag_cache: tuple[float, set] = (-1e9, set())   # never loaded yet
        self.failed_logins = FailedLoginTracker()

    # ---- collaborators (lazy: keeps this module importable without the app stack) ----
    @property
    def admin_repo(self):
        if self._admin_repo is None:
            from src.repositories.admin_repository import AdminRepository

            self._admin_repo = AdminRepository()
        return self._admin_repo

    async def _bot(self):
        if self._bot_getter:
            return await self._bot_getter()
        from src.services.notifications import _get_bot

        return await _get_bot()

    async def _config(self) -> dict:
        if self._config_getter:
            return await self._config_getter()
        from src.services.control_service import get_control_service

        return await get_control_service().get_published()

    def _group_chat(self) -> Optional[str]:
        if self._group is not None:
            return self._group or None
        from src.core.config import settings

        return settings.admin_chat_id or None

    def _env_ids(self) -> list:
        if self._env_admins is not None:
            return self._env_admins
        from src.core.config import settings

        return list(settings.admin_ids)

    # ---- who gets what ----
    async def recipients(self, alert_type: str) -> list[str]:
        targets: list[str] = []
        known: set[str] = set()
        try:
            for a in await self.admin_repo.list_all():
                known.add(a.telegram_id)
                types = cl.effective_alert_types(a.role, a.alert_types, a.is_super_admin)
                if alert_type in types:
                    targets.append(a.telegram_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not load staff for alert routing")
        for env_id in self._env_ids():  # legacy env-only admins have no row: role defaults
            sid = str(env_id)
            if sid not in known and alert_type in cl.effective_alert_types("admin"):
                targets.append(sid)
        group = self._group_chat()
        if group and alert_type in cl.ALERT_GROUP_TYPES:
            targets.append(str(group))
        return list(dict.fromkeys(targets))

    # ---- delivery ----
    async def send(self, alert_type: str, text: str, only_to: Optional[list] = None) -> dict:
        """Deliver now. Returns {sent, failed, errors}; never raises."""
        result = {"sent": 0, "failed": 0, "errors": []}
        try:
            bot = await self._bot()
            if bot is None:
                result["errors"].append("Telegram bot is not configured")
                return result
            targets = only_to if only_to is not None else await self.recipients(alert_type)
            for chat_id in targets:
                try:
                    await bot.send_message(chat_id=chat_id, text=text[:4000])
                    result["sent"] += 1
                except Exception as e:  # noqa: BLE001 - blocked bot, never pressed Start, bad group id...
                    result["failed"] += 1
                    result["errors"].append(f"{chat_id}: {type(e).__name__}")
                    logger.warning("Alert to %s failed: %s", chat_id, e)
        except Exception:  # noqa: BLE001
            logger.exception("Alert delivery crashed (type=%s)", alert_type)
        return result

    def _cooldown_ok(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        if now - self._cooldowns.get(key, -1e9) < seconds:
            return False
        self._cooldowns[key] = now
        if len(self._cooldowns) > 2000:
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if now - v < 3600}
        return True

    def schedule(self, alert_type: str, data: dict, dedupe_key: Optional[str] = None, cooldown: float = 60.0) -> None:
        """Fire and forget from inside a request. Safe to call from anywhere."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._dispatch(alert_type, data, dedupe_key, cooldown))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, alert_type: str, data: dict, dedupe_key: Optional[str], cooldown: float) -> None:
        try:
            cfg = await self._config()
            if not cfg.get("alerts", {}).get("enabled", True):
                return
            if dedupe_key and not self._cooldown_ok(f"{alert_type}:{dedupe_key}", cooldown):
                return
            await self.send(alert_type, cl.format_alert(alert_type, data))
        except Exception:  # noqa: BLE001
            logger.exception("Alert dispatch failed (type=%s)", alert_type)

    # ---- event helpers used by the rest of the app ----
    async def on_withdrawal_requested(self, player_id: str, player_name: str, amount: int, method: Optional[str]) -> None:
        cfg = await self._config()
        al = cfg.get("alerts", {})
        if not al.get("enabled", True):
            return
        amount = abs(int(amount))
        threshold = int(al.get("big_withdrawal_amount", 0) or 0)
        if threshold and amount >= threshold:
            self.schedule("big_withdrawal", {"amount": amount, "player_name": player_name, "method": method, "threshold": threshold},
                          dedupe_key=f"{player_id}:{amount}", cooldown=300)
        if await self._is_flagged(player_id):
            self.schedule("fraud_flag", {"amount": amount, "player_name": player_name}, dedupe_key=player_id, cooldown=86400)

    async def _is_flagged(self, player_id: str) -> bool:
        now = time.monotonic()
        stamp, cached = self._flag_cache
        if now - stamp > _FLAG_CACHE_TTL:
            try:
                cached = await self._flagged() if self._flagged_getter is None else await self._flagged_getter()
            except Exception:  # noqa: BLE001
                logger.warning("Abuse lookup failed; fraud alerts skipped this round", exc_info=True)
                cached = set()
            self._flag_cache = (now, cached)
        return player_id in cached

    @staticmethod
    async def _flagged() -> set:
        from src.services.abuse_signals import build_report

        report = await build_report()
        return {
            p.get("id")
            for group in ("shared_ip", "duplicate_phone", "shared_payout_account")
            for cluster in report.get(group, [])
            for p in cluster.get("players", [])
            if p.get("id")
        }

    async def on_failed_login(self, ip: str, telegram_id: Optional[str], known_admin: bool) -> None:
        cfg = await self._config()
        al = cfg.get("alerts", {})
        if not al.get("enabled", True):
            return
        count = self.failed_logins.record(ip or "unknown", int(al.get("failed_logins_before_alert", 3)))
        if count:
            detail = ("A real admin account entered a wrong code." if known_admin else "The account is not an admin.") \
                if telegram_id else "No Telegram identity was supplied."
            self.schedule("failed_login", {"ip": ip, "count": count, "minutes": int(_FAILED_LOGIN_WINDOW // 60), "detail": detail})


_instance: Optional[AdminAlertService] = None


def get_alert_service() -> AdminAlertService:
    global _instance
    if _instance is None:
        _instance = AdminAlertService()
    return _instance
