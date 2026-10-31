"""Admin Control Centre service.

  * draft -> publish workflow for games, money rules, content, feature flags and settings
  * instant emergency switches (maintenance mode, kill switch), always audited
  * one-step rollback
  * audit log writer
  * read helpers the rest of the platform uses to enforce what an admin published

Enforcement helpers are deliberately FAIL-OPEN on infrastructure errors (a database hiccup must
not lock every player out); the decisions themselves are pure functions in control_logic.
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any, Optional

from src.services import control_logic as cl

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 5.0  # multi-container: another instance picks up a publish within this window
EMERGENCY_SECTIONS = ("maintenance", "kill_switch")


class ControlError(Exception):
    """Business-rule failure; the message is safe to show an admin."""


class ControlPermissionError(ControlError):
    pass


class ControlService:
    def __init__(self, repo=None, audit_repo=None, player_repo=None, admin_service=None, settings_repo=None, alerts=None):
        if repo is None or audit_repo is None or player_repo is None:
            from src.repositories.control_repository import AuditRepository, ControlRepository, PlayerControlRepository

            repo = repo or ControlRepository()
            audit_repo = audit_repo or AuditRepository()
            player_repo = player_repo or PlayerControlRepository()
        self.repo = repo
        self.audit_repo = audit_repo
        self.player_repo = player_repo
        self._admin_service = admin_service
        self._settings_repo = settings_repo
        self._alerts = alerts
        self._cache: Optional[dict] = None
        self._cache_at = 0.0
        self._last_version = 0

    # ------------------------------------------------------------------
    # Collaborators (lazy so unit tests can inject fakes)
    # ------------------------------------------------------------------

    @property
    def admin_service(self):
        if self._admin_service is None:
            from src.api.dependencies import get_admin_service

            self._admin_service = get_admin_service()
        return self._admin_service

    @property
    def settings_repo(self):
        if self._settings_repo is None:
            from src.repositories.settings_repository import SettingsRepository

            self._settings_repo = SettingsRepository()
        return self._settings_repo

    def invalidate(self) -> None:
        self._cache, self._cache_at = None, 0.0

    # ------------------------------------------------------------------
    # Reading the published config (cached, never raises)
    # ------------------------------------------------------------------

    async def get_published(self) -> dict:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < _CACHE_TTL_SECONDS:
            return self._cache
        try:
            row = await self.repo.get()
            cfg = cl.normalize_config(row.get("published") or {})
            self._last_version = int(row.get("version") or 0)
        except Exception:  # noqa: BLE001 - unreachable DB or a hand-edited row: keep serving
            logger.warning("Control config unavailable; using last known / default values", exc_info=True)
            return self._cache if self._cache is not None else cl.normalize_config({})
        self._cache, self._cache_at = cfg, now
        return cfg

    async def public(self) -> dict:
        cfg = await self.get_published()
        view = cl.public_view(cfg)
        view["version"] = self._last_version
        return view

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def permissions(self, admin_id: str) -> frozenset:
        return await self.admin_service.permissions_of(str(admin_id))

    async def require(self, admin_id: str, perm: str) -> None:
        if perm not in await self.permissions(admin_id):
            raise ControlPermissionError(f"Your role does not allow this ({cl.PERMISSIONS.get(perm, perm)}).")

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    async def audit(
        self,
        actor_id: str,
        action: str,
        *,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        summary: Optional[str] = None,
        before: Any = None,
        after: Any = None,
        ip: Optional[str] = None,
    ) -> None:
        """Record an admin action. Never raises: an audit hiccup must not block the action itself
        (it is logged loudly instead)."""
        try:
            role = None
            if actor_id not in ("system", "auto"):
                admin = await self.admin_service.get_admin(str(actor_id))
                role = ("super" if admin.is_super_admin else admin.role) if admin else "admin"
            await self.audit_repo.add(
                str(actor_id), action, actor_role=role, target_type=target_type, target_id=target_id,
                summary=summary, before=before, after=after, ip=ip,
            )
        except Exception:  # noqa: BLE001
            logger.exception("AUDIT WRITE FAILED action=%s actor=%s target=%s/%s", action, actor_id, target_type, target_id)

    async def _role_of(self, admin_id: str) -> Optional[str]:
        try:
            admin = await self.admin_service.get_admin(str(admin_id))
            return ("super" if admin.is_super_admin else admin.role) if admin else None
        except Exception:  # noqa: BLE001
            return None

    async def _alert(self, alert_type: str, data: dict) -> None:
        """Telegram alert to admins; fire-and-forget and never raises."""
        try:
            alerts = self._alerts
            if alerts is None:
                from src.services.admin_alerts import get_alert_service

                alerts = get_alert_service()
            alerts.schedule(alert_type, data)
        except Exception:  # noqa: BLE001
            logger.warning("Alert scheduling failed for %s", alert_type, exc_info=True)

    # ------------------------------------------------------------------
    # Snapshot for the admin UI
    # ------------------------------------------------------------------

    @staticmethod
    def _live_settings(settings_obj) -> dict:
        data = settings_obj.model_dump() if hasattr(settings_obj, "model_dump") else dict(settings_obj)
        return {k: data.get(k) for k in cl.SETTINGS_SPEC if k in data}

    async def snapshot(self, admin_id: str) -> dict:
        row = await self.repo.get()
        published = cl.normalize_config(row.get("published") or {})
        draft_raw = row.get("draft")
        draft = self._with_emergency_from(cl.normalize_config(draft_raw), published) if draft_raw else copy.deepcopy(published)
        self._last_version = int(row.get("version") or 0)
        live = self._live_settings(await self.admin_service.get_settings())
        perms = await self.permissions(admin_id)
        return {
            "version": int(row.get("version") or 0),
            "published": published,
            "draft": draft,
            "has_draft": bool(draft_raw),
            "changes": cl.diff_config(published, draft) if draft_raw else [],
            "published_by": row.get("published_by"),
            "published_at": row["published_at"].isoformat() if row.get("published_at") else None,
            "draft_by": row.get("draft_by"),
            "draft_updated_at": row["draft_updated_at"].isoformat() if row.get("draft_updated_at") else None,
            "can_rollback": bool(row.get("previous")),
            "live_settings": live,
            "settings_spec": {k: {"type": v[0].__name__, "min": v[1], "max": v[2]} for k, v in cl.SETTINGS_SPEC.items()},
            "permissions": sorted(perms),
            "flags": cl.FLAGS,
            "tracks": list(cl.TRACKS),
            "active_tracks": self._active_tracks(),
        }

    @staticmethod
    def _active_tracks() -> list:
        try:
            from src.services.room_service import RoomService

            return [t for t in RoomService.TRACKS if t in cl.TRACKS]
        except Exception:  # noqa: BLE001
            return ["real_10"]

    @staticmethod
    def _with_emergency_from(cfg: dict, published: dict) -> dict:
        """Maintenance and the kill switch are instant controls, never part of a draft."""
        for key in EMERGENCY_SECTIONS:
            cfg[key] = copy.deepcopy(published[key])
        return cfg

    # ------------------------------------------------------------------
    # Draft workflow
    # ------------------------------------------------------------------

    async def save_draft(self, admin_id: str, config: dict, ip: Optional[str] = None) -> dict:
        await self.require(admin_id, "control.edit")
        row = await self.repo.get()
        published = cl.normalize_config(row.get("published") or {})
        try:
            draft = self._with_emergency_from(cl.normalize_config(config), published)
        except ValueError as e:
            raise ControlError(str(e))
        # Settings overlay: keep only values that actually differ from what is live right now.
        live = self._live_settings(await self.admin_service.get_settings())
        draft["settings"] = {k: v for k, v in draft["settings"].items() if live.get(k) != v}
        try:
            await self.admin_service.check_settings_patch(draft["settings"])
        except ValueError as e:
            raise ControlError(str(e))
        await self.repo.save_draft(draft, str(admin_id))
        await self.audit(
            admin_id, "control.draft_save", target_type="config", target_id="global", ip=ip,
            summary=f"{len(cl.diff_config(published, draft))} unpublished change(s)",
        )
        return await self.snapshot(admin_id)

    async def discard_draft(self, admin_id: str, ip: Optional[str] = None) -> dict:
        await self.require(admin_id, "control.edit")
        await self.repo.discard_draft()
        await self.audit(admin_id, "control.draft_discard", target_type="config", target_id="global", ip=ip)
        return await self.snapshot(admin_id)

    async def publish(self, admin_id: str, ip: Optional[str] = None) -> dict:
        await self.require(admin_id, "control.publish")
        row = await self.repo.get()
        if not row.get("draft"):
            raise ControlError("There are no unpublished changes.")
        published = cl.normalize_config(row.get("published") or {})
        try:
            new = self._with_emergency_from(cl.normalize_config(row["draft"]), published)
        except ValueError as e:
            raise ControlError(str(e))
        overlay = new["settings"]
        changes = cl.diff_config(published, new)

        # Validate settings against the live values BEFORE anything is written.
        try:
            await self.admin_service.check_settings_patch(overlay)
        except ValueError as e:
            raise ControlError(str(e))

        live_before = self._live_settings(await self.admin_service.get_settings())
        settings_before = {k: live_before.get(k) for k in overlay}

        previous = copy.deepcopy(published)
        previous["settings"] = settings_before      # lets a rollback restore the old values
        new["settings"] = {}                        # the overlay is applied, not stored as state
        await self.repo.publish(new, str(admin_id), previous)
        if overlay:
            await self.admin_service.update_settings(str(admin_id), overlay)
        self.invalidate()
        await self.audit(
            admin_id, "control.publish", target_type="config", target_id="global", ip=ip,
            summary=f"Published {len(changes)} change(s)", before={"settings": settings_before}, after={"changes": changes},
        )
        await self._alert("control_publish", {"summary": f"Published {len(changes)} Control Center change(s)", "actor": admin_id})
        return await self.snapshot(admin_id)

    async def rollback(self, admin_id: str, ip: Optional[str] = None) -> dict:
        await self.require(admin_id, "control.publish")
        row = await self.repo.get()
        prev_raw = row.get("previous")
        if not prev_raw:
            raise ControlError("There is nothing to roll back to.")
        current = cl.normalize_config(row.get("published") or {})
        try:
            target = self._with_emergency_from(cl.normalize_config(prev_raw), current)
        except ValueError as e:
            raise ControlError(str(e))
        overlay = target["settings"]
        try:
            await self.admin_service.check_settings_patch(overlay)
        except ValueError as e:
            raise ControlError(f"Cannot roll back: {e}")
        live_before = self._live_settings(await self.admin_service.get_settings())
        previous = copy.deepcopy(current)
        previous["settings"] = {k: live_before.get(k) for k in overlay}   # so the rollback can itself be undone
        target["settings"] = {}
        await self.repo.publish(target, str(admin_id), previous)
        if overlay:
            await self.admin_service.update_settings(str(admin_id), overlay)
        self.invalidate()
        await self.audit(
            admin_id, "control.rollback", target_type="config", target_id="global", ip=ip,
            summary="Rolled back to the previous published version",
            after={"changes": cl.diff_config(current, target)},
        )
        await self._alert("control_publish", {"summary": "Rolled back to the previous published version", "actor": admin_id})
        return await self.snapshot(admin_id)

    # ------------------------------------------------------------------
    # Emergency controls: apply immediately (no draft), always audited
    # ------------------------------------------------------------------

    async def set_emergency(
        self,
        admin_id: str,
        *,
        maintenance: Optional[dict] = None,
        kill_switch: Optional[dict] = None,
        ip: Optional[str] = None,
    ) -> dict:
        await self.require(admin_id, "control.emergency")
        row = await self.repo.get()
        published = cl.normalize_config(row.get("published") or {})
        new = copy.deepcopy(published)
        if maintenance is not None:
            new["maintenance"] = {**new["maintenance"], **maintenance}
        if kill_switch is not None:
            new["kill_switch"] = {**new["kill_switch"], **kill_switch}
        try:
            new = cl.normalize_config(new)
        except ValueError as e:
            raise ControlError(str(e))
        previous = copy.deepcopy(published)
        previous["settings"] = {}
        await self.repo.set_published_direct(new, str(admin_id))
        self.invalidate()
        parts = []
        if maintenance is not None:
            parts.append(f"maintenance {'ON' if new['maintenance']['enabled'] else 'off'}")
        if kill_switch is not None:
            parts.append(f"kill switch {'ON' if new['kill_switch']['enabled'] else 'off'}")
        await self.audit(
            admin_id, "control.emergency", target_type="config", target_id="global", ip=ip,
            summary=", ".join(parts),
            before={k: published[k] for k in EMERGENCY_SECTIONS}, after={k: new[k] for k in EMERGENCY_SECTIONS},
        )
        summary = ", ".join(parts)
        await self._alert("emergency", {"summary": summary[:1].upper() + summary[1:], "actor": admin_id, "role": await self._role_of(admin_id)})
        return await self.snapshot(admin_id)

    # ------------------------------------------------------------------
    # Enforcement helpers used by the wallet, room, bonus and agent code paths
    # ------------------------------------------------------------------

    async def flag_block(self, flag: str) -> str:
        """'' when allowed, otherwise the message to show the player."""
        ok, msg = cl.flag_state(await self.get_published(), flag)
        return "" if ok else msg

    async def track_block(self, track: str) -> str:
        ok, msg = cl.track_state(await self.get_published(), track)
        return "" if ok else msg

    async def any_play_restriction(self) -> bool:
        """Cheap pre-check so the join path only looks up a room's track when something is restricted."""
        cfg = await self.get_published()
        if not cl.flag_state(cfg, "play")[0]:
            return True
        return any(not t.get("open", True) for t in cfg["games"]["tracks"].values())

    async def player_money_block(self, player_id: str, kind: str, amount: int) -> str:
        """Frozen flag + per-24h limits for a deposit/withdrawal request. '' = allowed.
        Fails open if the player or totals cannot be read."""
        try:
            from src.repositories.player_repository import PlayerRepository

            player = await PlayerRepository().get_by_id(player_id)
            if player is None:
                return ""
            if getattr(player, "is_frozen", False):
                return "Your account is under review. Please contact support to continue."
            personal = player.daily_deposit_limit if kind == "deposit" else player.daily_withdrawal_limit
            limit = cl.daily_limit(await self.get_published(), kind, personal)
            if limit > 0:
                used = await self.player_repo.sum_recent(player_id, kind, 24)
                if used + abs(int(amount)) > limit:
                    left = max(limit - used, 0)
                    verb = "deposit" if kind == "deposit" else "withdraw"
                    return f"Daily {verb} limit reached. You can {verb} up to {left} ETB more in the next 24 hours."
        except Exception:  # noqa: BLE001
            logger.warning("Player money check skipped for %s", player_id, exc_info=True)
        return ""

    async def should_auto_approve(self, player_id: str, amount: int, has_url: bool) -> tuple[bool, str]:
        cfg = await self.get_published()
        if not cfg["money"]["auto_approve"]["enabled"]:
            return False, "auto-approve is off"
        try:
            from src.repositories.player_repository import PlayerRepository
            from src.services.abuse_signals import build_report

            player = await PlayerRepository().get_by_id(player_id)
            if player is None:
                return False, "player not found"
            report = await build_report()
            flagged = any(
                p.get("id") == player_id
                for group in ("shared_ip", "duplicate_phone", "shared_payout_account")
                for cluster in report.get(group, [])
                for p in cluster.get("players", [])
            )
            prior = await self.player_repo.count_approved_deposits(player_id)
            return cl.decide_auto_approve(
                cfg, amount, prior, has_url,
                is_banned=player.is_banned, is_frozen=getattr(player, "is_frozen", False), abuse_flagged=flagged,
            )
        except Exception:  # noqa: BLE001 - when in doubt, a human decides
            logger.warning("Auto-approve check failed for %s; leaving for manual review", player_id, exc_info=True)
            return False, "check failed"

    async def withdrawal_needs_senior(self, amount: int) -> bool:
        return cl.withdrawal_needs_senior(await self.get_published(), amount)


_instance: Optional[ControlService] = None


def get_control_service() -> ControlService:
    """Process-wide ControlService (the config cache lives on it)."""
    global _instance
    if _instance is None:
        _instance = ControlService()
    return _instance
