"""Pure (no database, no framework) logic for the admin Control Centre.

Everything here works on plain dicts so it can be unit-tested without Postgres and
reused by the API layer, the wallet/room services and the public config endpoint:

  * DEFAULT_CONFIG / normalize_config  - the shape of the draft + published config
  * PERMISSIONS / ROLE_PRESETS         - who may do what
  * diff_config                        - "what changes if I publish this draft?"
  * flag_state / track_state           - feature flags, kill switch, per-game pause
  * decide_auto_approve / daily_limit  - money rules
  * public_view                        - the safe subset players are allowed to see
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Config shape
# --------------------------------------------------------------------------

TRACKS = ("real_10", "real_50", "bonus")

# Every feature flag the platform actually enforces (server-side). `label` is shown in admin.
FLAGS = {
    "deposits": "Deposits",
    "withdrawals": "Withdrawals",
    "play": "Joining / playing rounds",
    "bonus_center": "Bonus Center & claims",
    "agent_portal": "Agent portal",
    "cashback": "Daily cashback",
}

_LANG_TEXTS = ("cashback_note", "agent_intro", "agent_link_hint", "agent_note_hint")
TEXT_KEYS = ("deposit_intro", "withdraw_intro", "home_note") + tuple(f"{k}_{lang}" for k in _LANG_TEXTS for lang in ("en", "am"))

DEFAULT_MAINTENANCE_MESSAGE = "Melbete Bingo is under maintenance. We'll be back shortly."
DEFAULT_KILL_MESSAGE = "Money movement and play are temporarily paused. Please check back soon."

DEFAULT_CONFIG: dict = {
    "maintenance": {"enabled": False, "message": DEFAULT_MAINTENANCE_MESSAGE, "eta": "", "allow_admins": True},
    "kill_switch": {"enabled": False, "reason": ""},
    "flags": {name: True for name in FLAGS},
    "games": {"tracks": {t: {"open": True, "pause_message": ""} for t in TRACKS}},
    "money": {
        "daily_deposit_limit": 0,        # ETB per player per day, 0 = no platform limit
        "daily_withdrawal_limit": 0,
        "withdrawal_review_above": 0,    # withdrawals above this need `money.approve_large`; 0 = off
        "auto_approve": {
            "enabled": False,
            "deposit_max": 0,            # auto-approve deposits up to this many ETB
            "min_prior_approved": 3,     # player must already have this many approved deposits
            "require_verification_url": True,
        },
    },
    "content": {
        "announcement": {"enabled": False, "level": "info", "title": "", "body": "", "ends_at": ""},
        "banner": {"enabled": False, "tag": "", "title": "", "detail": "", "cta_label": "", "cta_target": "deposit"},
        "texts": {k: "" for k in TEXT_KEYS},
    },
    "promotions": [],   # timed cashback boosts, see normalize_promotions()
    "alerts": {
        "enabled": True,
        "big_withdrawal_amount": 5000,      # ETB; a withdrawal request at or above this alerts admins (0 = never)
        "big_wallet_adjust_amount": 2000,   # ETB; a wallet adjustment at or above this alerts admins (0 = never)
        "failed_logins_before_alert": 3,    # failed admin sign-ins from one place within 10 minutes
    },
    "settings": {},  # overlay of SystemSettings fields, applied to system_settings on publish
}

ANNOUNCEMENT_LEVELS = ("info", "success", "warning", "critical")
CTA_TARGETS = ("deposit", "withdraw", "promos", "profile", "none")

# SystemSettings fields the draft may change: name -> (type, min, max). Cross-field rules
# (min<=max etc.) are enforced again by AdminService.update_settings when the draft is published.
SETTINGS_SPEC: dict[str, tuple] = {
    "stake_amount_real_10": (int, 1, 100_000),
    "stake_amount_real_50": (int, 1, 100_000),
    "stake_amount_bonus": (int, 1, 100_000),
    "payout_ratio": (float, 0.5, 1.0),         # 0.8 = 20% platform commission
    "lobby_countdown_seconds": (int, 5, 300),
    "draw_interval": (int, 1, 30),
    "cards_per_player": (int, 1, 4),
    "min_cartelas_to_start": (int, 1, 500),
    "cartela_min": (int, 1, 10_000),
    "cartela_max": (int, 1, 10_000),
    "min_deposit": (int, 1, 10_000_000),
    "max_deposit": (int, 1, 10_000_000),
    "min_withdrawal": (int, 1, 10_000_000),
    "max_withdrawal": (int, 1, 10_000_000),
    "free_credit_amount": (int, 0, 100_000),
    "referral_bonus": (int, 0, 10_000),
    "max_referrals_per_user": (int, 0, 1_000),
    "payout_custom_real_10": (bool, None, None),
    "payout_custom_real_50": (bool, None, None),
    "payout_custom_bonus": (bool, None, None),
    "payout_ratio_real_10": (float, 0.5, 1.0),
    "payout_ratio_real_50": (float, 0.5, 1.0),
    "payout_ratio_bonus": (float, 0.5, 1.0),
    "max_bots_real_10": (int, 0, 200),
    "max_bots_real_50": (int, 0, 200),
    "max_bots_bonus": (int, 0, 200),
    "bot_enabled_real_10": (bool, None, None),
    "bot_enabled_real_50": (bool, None, None),
    "bot_enabled_bonus": (bool, None, None),
    "support_contact": (str, 0, 64),
}

# --------------------------------------------------------------------------
# Small coercion helpers
# --------------------------------------------------------------------------


def _bool(v: Any, name: str) -> bool:
    if isinstance(v, bool):
        return v
    if v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.lower() in ("true", "false", "1", "0"):
        return v.lower() in ("true", "1")
    raise ValueError(f"{name} must be true or false")


def _int(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{name} must be a whole number")
    try:
        n = int(float(v)) if not isinstance(v, int) else v
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if n < lo or n > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return n


def _text(v: Any, name: str, max_len: int) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(f"{name} must be text")
    v = v.strip()
    if len(v) > max_len:
        raise ValueError(f"{name} is too long (max {max_len} characters)")
    return v


def _iso(v: Any, name: str) -> str:
    v = _text(v, name, 40)
    if not v:
        return ""
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be a valid date/time")
    return v


def _section(raw: dict, key: str) -> dict:
    v = raw.get(key, {})
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"'{key}' must be an object")
    return v


# --------------------------------------------------------------------------
# normalize_config
# --------------------------------------------------------------------------


def normalize_settings_overlay(raw: Optional[dict]) -> dict:
    """Validate a partial SystemSettings overlay. Unknown keys are rejected (a typo must not
    silently do nothing)."""
    out: dict = {}
    for key, value in (raw or {}).items():
        if key not in SETTINGS_SPEC:
            raise ValueError(f"Unknown setting '{key}'")
        typ, lo, hi = SETTINGS_SPEC[key]
        if value is None:
            continue
        if typ is bool:
            out[key] = _bool(value, key)
        elif typ is int:
            out[key] = _int(value, key, lo, hi)
        elif typ is float:
            try:
                f = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            if f < lo or f > hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            out[key] = round(f, 4)
        else:
            out[key] = _text(value, key, hi)
    if "cartela_min" in out and "cartela_max" in out and out["cartela_min"] > out["cartela_max"]:
        raise ValueError("cartela_min cannot be greater than cartela_max")
    if "min_deposit" in out and "max_deposit" in out and out["min_deposit"] > out["max_deposit"]:
        raise ValueError("min_deposit cannot be greater than max_deposit")
    if "min_withdrawal" in out and "max_withdrawal" in out and out["min_withdrawal"] > out["max_withdrawal"]:
        raise ValueError("min_withdrawal cannot be greater than max_withdrawal")
    return out


MAX_PROMOTIONS = 10


def _date_str(v: Any, name: str) -> str:
    v = str(v or "").strip()
    if not v:
        return ""
    try:
        return datetime.strptime(v[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-12-31")


def _time_str(v: Any, name: str) -> str:
    v = str(v or "").strip() or "00:00"
    try:
        h, m = v.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        raise ValueError(f"{name} must be a time like 18:30")


def normalize_promotions(raw: Any) -> list:
    """Timed cashback boosts. Each: name, enabled, multiplier (1.1 to 5), days (Mon..Sun), optional dates and window."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("promotions must be a list")
    if len(raw) > MAX_PROMOTIONS:
        raise ValueError(f"At most {MAX_PROMOTIONS} promotions")
    out = []
    for i, p in enumerate(raw, start=1):
        if not isinstance(p, dict):
            raise ValueError(f"Promotion {i} is not valid")
        label = f"Promotion {i}"
        name = _text(p.get("name", ""), f"{label} name", 40)
        if not name:
            raise ValueError(f"{label} needs a name")
        try:
            mult = round(float(p.get("multiplier", 2)), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{label} multiplier must be a number")
        if not 1.1 <= mult <= 5:
            raise ValueError(f"{label} multiplier must be between 1.1 and 5")
        days = p.get("days", [True] * 7)
        if not isinstance(days, list) or len(days) != 7:
            raise ValueError(f"{label} days must have 7 entries (Monday to Sunday)")
        days = [_bool(d, f"{label} day") for d in days]
        if not any(days):
            raise ValueError(f"{label} needs at least one day ticked")
        start_date, end_date = _date_str(p.get("start_date"), f"{label} start date"), _date_str(p.get("end_date"), f"{label} end date")
        if start_date and end_date and start_date > end_date:
            raise ValueError(f"{label} ends before it starts")
        out.append({
            "name": name, "enabled": _bool(p.get("enabled", True), f"{label} switch"), "applies": ["cashback"],
            "multiplier": mult, "days": days, "start_date": start_date, "end_date": end_date,
            "start_time": _time_str(p.get("start_time"), f"{label} start time"), "end_time": _time_str(p.get("end_time"), f"{label} end time"),
        })
    return out


def normalize_config(raw: Optional[dict]) -> dict:
    """Merge `raw` onto the defaults, coerce every value and drop unknown keys.

    Raises ValueError (with a message safe to show an admin) on bad input."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("Config must be an object")
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    m = _section(raw, "maintenance")
    cfg["maintenance"] = {
        "enabled": _bool(m.get("enabled", False), "maintenance.enabled"),
        "message": _text(m.get("message", DEFAULT_MAINTENANCE_MESSAGE), "maintenance.message", 400) or DEFAULT_MAINTENANCE_MESSAGE,
        "eta": _text(m.get("eta", ""), "maintenance.eta", 80),
        "allow_admins": _bool(m.get("allow_admins", True), "maintenance.allow_admins"),
    }

    k = _section(raw, "kill_switch")
    cfg["kill_switch"] = {
        "enabled": _bool(k.get("enabled", False), "kill_switch.enabled"),
        "reason": _text(k.get("reason", ""), "kill_switch.reason", 300),
    }

    f = _section(raw, "flags")
    for name in f:
        if name not in FLAGS:
            raise ValueError(f"Unknown feature flag '{name}'")
    cfg["flags"] = {name: _bool(f.get(name, True), f"flags.{name}") for name in FLAGS}

    g = _section(raw, "games")
    tracks_raw = g.get("tracks", {}) or {}
    if not isinstance(tracks_raw, dict):
        raise ValueError("games.tracks must be an object")
    for t in tracks_raw:
        if t not in TRACKS:
            raise ValueError(f"Unknown game track '{t}'")
    cfg["games"] = {"tracks": {}}
    for t in TRACKS:
        tr = tracks_raw.get(t, {}) or {}
        if not isinstance(tr, dict):
            raise ValueError(f"games.tracks.{t} must be an object")
        cfg["games"]["tracks"][t] = {
            "open": _bool(tr.get("open", True), f"games.tracks.{t}.open"),
            "pause_message": _text(tr.get("pause_message", ""), f"games.tracks.{t}.pause_message", 200),
        }

    mo = _section(raw, "money")
    aa = mo.get("auto_approve", {}) or {}
    if not isinstance(aa, dict):
        raise ValueError("money.auto_approve must be an object")
    big = 100_000_000
    cfg["money"] = {
        "daily_deposit_limit": _int(mo.get("daily_deposit_limit", 0), "money.daily_deposit_limit", 0, big),
        "daily_withdrawal_limit": _int(mo.get("daily_withdrawal_limit", 0), "money.daily_withdrawal_limit", 0, big),
        "withdrawal_review_above": _int(mo.get("withdrawal_review_above", 0), "money.withdrawal_review_above", 0, big),
        "auto_approve": {
            "enabled": _bool(aa.get("enabled", False), "money.auto_approve.enabled"),
            "deposit_max": _int(aa.get("deposit_max", 0), "money.auto_approve.deposit_max", 0, big),
            "min_prior_approved": _int(aa.get("min_prior_approved", 3), "money.auto_approve.min_prior_approved", 1, 1000),
            "require_verification_url": _bool(aa.get("require_verification_url", True), "money.auto_approve.require_verification_url"),
        },
    }
    if cfg["money"]["auto_approve"]["enabled"] and cfg["money"]["auto_approve"]["deposit_max"] <= 0:
        raise ValueError("Set a maximum deposit amount before turning auto-approve on")

    c = _section(raw, "content")
    an = c.get("announcement", {}) or {}
    bn = c.get("banner", {}) or {}
    tx = c.get("texts", {}) or {}
    if not all(isinstance(x, dict) for x in (an, bn, tx)):
        raise ValueError("content sections must be objects")
    level = an.get("level", "info")
    if level not in ANNOUNCEMENT_LEVELS:
        raise ValueError(f"announcement level must be one of {', '.join(ANNOUNCEMENT_LEVELS)}")
    target = bn.get("cta_target", "deposit")
    if target not in CTA_TARGETS:
        raise ValueError(f"banner button target must be one of {', '.join(CTA_TARGETS)}")
    for key in tx:
        if key not in TEXT_KEYS:
            raise ValueError(f"Unknown text '{key}'")
    cfg["content"] = {
        "announcement": {
            "enabled": _bool(an.get("enabled", False), "announcement.enabled"),
            "level": level,
            "title": _text(an.get("title", ""), "announcement.title", 80),
            "body": _text(an.get("body", ""), "announcement.body", 500),
            "ends_at": _iso(an.get("ends_at", ""), "announcement.ends_at"),
        },
        "banner": {
            "enabled": _bool(bn.get("enabled", False), "banner.enabled"),
            "tag": _text(bn.get("tag", ""), "banner.tag", 24),
            "title": _text(bn.get("title", ""), "banner.title", 80),
            "detail": _text(bn.get("detail", ""), "banner.detail", 160),
            "cta_label": _text(bn.get("cta_label", ""), "banner.cta_label", 24),
            "cta_target": target,
        },
        "texts": {k_: _text(tx.get(k_, ""), f"texts.{k_}", 600) for k_ in TEXT_KEYS},
    }
    if cfg["content"]["announcement"]["enabled"] and not cfg["content"]["announcement"]["body"]:
        raise ValueError("An announcement needs some text")
    if cfg["content"]["banner"]["enabled"] and not cfg["content"]["banner"]["title"]:
        raise ValueError("A promo banner needs a title")

    al = _section(raw, "alerts")
    cfg["alerts"] = {
        "enabled": _bool(al.get("enabled", True), "alerts.enabled"),
        "big_withdrawal_amount": _int(al.get("big_withdrawal_amount", 5000), "alerts.big_withdrawal_amount", 0, big),
        "big_wallet_adjust_amount": _int(al.get("big_wallet_adjust_amount", 2000), "alerts.big_wallet_adjust_amount", 0, big),
        "failed_logins_before_alert": _int(al.get("failed_logins_before_alert", 3), "alerts.failed_logins_before_alert", 1, 20),
    }

    cfg["promotions"] = normalize_promotions(raw.get("promotions"))
    cfg["settings"] = normalize_settings_overlay(raw.get("settings"))
    return cfg


# --------------------------------------------------------------------------
# Diff (for the "unpublished changes" review before publishing)
# --------------------------------------------------------------------------


def _flatten(obj: Any, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = {}
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list) and obj and all(isinstance(v, dict) for v in obj):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}.{i + 1}"))
        out[f"{prefix}.count"] = len(obj)
    else:
        out[prefix] = obj
    return out


def diff_config(old: Optional[dict], new: Optional[dict]) -> list[dict]:
    """[{path, from, to}] for every leaf value that differs. `settings` is compared per key."""
    a, b = _flatten(old or {}), _flatten(new or {})
    changes = []
    for path in sorted(set(a) | set(b)):
        if a.get(path) != b.get(path) and not (path in ("settings",) and not a.get(path) and not b.get(path)):
            changes.append({"path": path, "from": a.get(path), "to": b.get(path)})
    return changes


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------

PERMISSIONS = {
    "money.approve": "Approve / reject deposits and withdrawals",
    "money.approve_large": "Approve withdrawals above the review threshold",
    "players.view": "View players, wallets and activity",
    "players.ban": "Ban, unban and freeze players",
    "players.notes": "Add staff notes and set personal limits",
    "players.wallet_adjust": "Adjust player wallets",
    "control.edit": "Edit the control draft (games, money, content, flags)",
    "control.publish": "Publish, discard and roll back control changes",
    "control.emergency": "Use maintenance mode and the kill switch instantly",
    "staff.manage": "Add or remove staff and change roles",
    "audit.view": "View the audit log",
}

ROLE_LABELS = {
    "super": "Super admin",
    "manager": "Manager",
    "finance": "Finance",
    "support": "Support",
    "admin": "Admin (legacy)",
}

ROLE_PRESETS: dict[str, frozenset] = {
    "super": frozenset(PERMISSIONS),
    "manager": frozenset({"control.edit", "control.publish", "control.emergency", "players.view", "audit.view"}),
    "finance": frozenset({"money.approve", "money.approve_large", "players.view", "audit.view"}),
    "support": frozenset({"players.view", "players.ban", "players.notes"}),
    # Existing admins keep what they could do before: money approvals + read access, plus drafting.
    "admin": frozenset({"money.approve", "players.view", "control.edit", "audit.view"}),
}
ASSIGNABLE_ROLES = ("manager", "finance", "support", "admin")


def permissions_for(role: Optional[str], extra: Optional[Iterable[str]] = None, is_super: bool = False) -> frozenset:
    """Effective permission set. Super admins always have everything."""
    if is_super or role == "super":
        return ROLE_PRESETS["super"]
    base = set(ROLE_PRESETS.get(role or "admin", ROLE_PRESETS["admin"]))
    base.update(p for p in (extra or []) if p in PERMISSIONS)
    return frozenset(base)


def validate_extra_permissions(extra: Any) -> list[str]:
    if extra is None:
        return []
    if not isinstance(extra, (list, tuple)):
        raise ValueError("extra_permissions must be a list")
    bad = [p for p in extra if p not in PERMISSIONS]
    if bad:
        raise ValueError(f"Unknown permission '{bad[0]}'")
    return sorted(set(extra))


# --------------------------------------------------------------------------
# Runtime decisions
# --------------------------------------------------------------------------

# Flags that the kill switch also turns off.
_KILLED_BY_KILL_SWITCH = ("deposits", "withdrawals", "play")


def flag_state(config: dict, flag: str) -> tuple[bool, str]:
    """(allowed, message). Honours the kill switch and the individual flag."""
    cfg = config or DEFAULT_CONFIG
    ks = cfg.get("kill_switch", {})
    if ks.get("enabled") and flag in _KILLED_BY_KILL_SWITCH:
        return False, ks.get("reason") or DEFAULT_KILL_MESSAGE
    if not cfg.get("flags", {}).get(flag, True):
        return False, f"{FLAGS.get(flag, flag)} {'are' if flag in ('deposits', 'withdrawals') else 'is'} temporarily unavailable."
    return True, ""


def track_state(config: dict, track: str) -> tuple[bool, str]:
    """(open, message) for one game track; the play flag and kill switch apply to every track."""
    ok, msg = flag_state(config, "play")
    if not ok:
        return False, msg
    tr = (config or DEFAULT_CONFIG).get("games", {}).get("tracks", {}).get(track, {})
    if not tr.get("open", True):
        return False, tr.get("pause_message") or "This game is paused for a moment. Please try again soon."
    return True, ""


def daily_limit(config: dict, kind: str, player_limit: Optional[int]) -> int:
    """Effective per-day limit in ETB (0 = unlimited). A player's personal limit wins over the platform one."""
    if player_limit is not None and player_limit >= 0:
        return int(player_limit)
    return int((config or DEFAULT_CONFIG).get("money", {}).get(f"daily_{kind}_limit", 0) or 0)


def decide_auto_approve(
    config: dict,
    amount: int,
    prior_approved: int,
    has_verification_url: bool,
    is_banned: bool = False,
    is_frozen: bool = False,
    abuse_flagged: bool = False,
) -> tuple[bool, str]:
    """Should this pending deposit be approved automatically? Conservative: every condition must hold."""
    rule = (config or DEFAULT_CONFIG).get("money", {}).get("auto_approve", {})
    if not rule.get("enabled"):
        return False, "auto-approve is off"
    if is_banned or is_frozen:
        return False, "player is banned or frozen"
    if abuse_flagged:
        return False, "player is flagged for review"
    if amount > int(rule.get("deposit_max", 0)):
        return False, "amount above the auto-approve limit"
    if prior_approved < int(rule.get("min_prior_approved", 3)):
        return False, "not enough previously approved deposits"
    if rule.get("require_verification_url", True) and not has_verification_url:
        return False, "no payment verification link"
    return True, "matched auto-approve rule"


def withdrawal_needs_senior(config: dict, amount: int) -> bool:
    above = int((config or DEFAULT_CONFIG).get("money", {}).get("withdrawal_review_above", 0) or 0)
    return above > 0 and abs(int(amount)) > above


def _announcement_live(an: dict, now: datetime) -> bool:
    if not an.get("enabled") or not an.get("body"):
        return False
    ends = an.get("ends_at")
    if ends:
        try:
            end = datetime.fromisoformat(ends.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if now >= end:
                return False
        except ValueError:
            return True
    return True


def _active_promotions_public(cfg: dict, now: datetime) -> list:
    """Promotions running right now (Ethiopia time), for the player app's cashback label."""
    from zoneinfo import ZoneInfo
    from src.services.promotions import active_now
    local = now.astimezone(ZoneInfo("Africa/Addis_Ababa")).replace(tzinfo=None)
    return [{"name": p["name"], "multiplier": p["multiplier"], "applies": "cashback"} for p in active_now(cfg.get("promotions", []), local)]


def public_view(config: dict, now: Optional[datetime] = None) -> dict:
    """The part of the published config players' apps may see. Never includes limits or rules."""
    cfg = config or DEFAULT_CONFIG
    now = now or datetime.now(timezone.utc)
    flags = {name: flag_state(cfg, name)[0] for name in FLAGS}
    messages = {name: flag_state(cfg, name)[1] for name in FLAGS if not flags[name]}
    content = cfg.get("content", {})
    banner = content.get("banner", {})
    an = content.get("announcement", {})
    mt = cfg.get("maintenance", {})
    return {
        "maintenance": {
            "enabled": bool(mt.get("enabled")),
            "message": mt.get("message") or DEFAULT_MAINTENANCE_MESSAGE,
            "eta": mt.get("eta", ""),
        },
        "flags": flags,
        "flag_messages": messages,
        "tracks": {t: track_state(cfg, t)[0] for t in TRACKS},
        "announcement": (
            {"level": an.get("level", "info"), "title": an.get("title", ""), "body": an.get("body", "")}
            if _announcement_live(an, now) else None
        ),
        "banner": (
            {k: banner.get(k, "") for k in ("tag", "title", "detail", "cta_label", "cta_target")}
            if banner.get("enabled") and banner.get("title") else None
        ),
        "texts": {k: v for k, v in content.get("texts", {}).items() if v},
        "promotions": _active_promotions_public(cfg, now),
    }


# --------------------------------------------------------------------------
# Telegram alerts to admins
# --------------------------------------------------------------------------

ALERT_TYPES = {
    "big_withdrawal": ("Large withdrawal requests", "A player asks to withdraw at least the amount set in Alerts."),
    "fraud_flag": ("Flagged player moving money", "A player who shares a connection, phone or payout account asks to withdraw."),
    "emergency": ("Kill switch and maintenance", "Maintenance mode or the kill switch is turned on or off."),
    "failed_login": ("Failed admin sign-ins", "Repeated wrong codes or unknown accounts trying the admin panel."),
    "wallet_adjust": ("Large wallet adjustments", "Staff add or remove at least the amount set in Alerts."),
    "control_publish": ("Control Center published", "Changes are published or rolled back."),
    "staff_change": ("Staff changes", "Someone is added, removed, promoted or given a new role."),
    "bonus_budget": ("Bonus budget nearly used", "A bonus campaign has used 80% or 100% of its daily or total budget."),
}
# The shared admin group (ADMIN_CHAT_ID), if configured, also gets these.
ALERT_GROUP_TYPES = frozenset({"big_withdrawal", "fraud_flag", "emergency", "failed_login"})
ALERT_ROLE_DEFAULTS: dict[str, frozenset] = {
    "super": frozenset(ALERT_TYPES),
    "manager": frozenset({"big_withdrawal", "fraud_flag", "emergency", "failed_login", "control_publish", "bonus_budget"}),
    "finance": frozenset({"big_withdrawal", "fraud_flag", "wallet_adjust", "bonus_budget"}),
    "support": frozenset({"fraud_flag"}),
    "admin": frozenset({"big_withdrawal", "fraud_flag", "emergency"}),
}


def effective_alert_types(role: Optional[str], custom: Optional[Iterable[str]] = None, is_super: bool = False) -> frozenset:
    """What one admin gets: their own choice if they made one, otherwise the role's defaults."""
    if custom is not None:
        return frozenset(t for t in custom if t in ALERT_TYPES)
    return ALERT_ROLE_DEFAULTS["super" if is_super else (role if role in ALERT_ROLE_DEFAULTS else "admin")]


def validate_alert_types(types: Any) -> Optional[list[str]]:
    if types is None:
        return None  # back to the role defaults
    if not isinstance(types, (list, tuple)):
        raise ValueError("types must be a list")
    bad = [t for t in types if t not in ALERT_TYPES]
    if bad:
        raise ValueError(f"Unknown alert type '{bad[0]}'")
    return sorted(set(types))


def _etb(n: Any) -> str:
    try:
        return f"{int(n):,} ETB"
    except (TypeError, ValueError):
        return str(n)


def format_alert(alert_type: str, d: dict) -> str:
    """Plain-text Telegram message (no Markdown, so player-written names can never break it)."""
    if alert_type == "big_withdrawal":
        return (f"\U0001F4B8 Large withdrawal request\n{_etb(d.get('amount'))} by {d.get('player_name', '?')}\n"
                f"Method: {d.get('method') or 'n/a'}\nAlert threshold: {_etb(d.get('threshold'))}\n"
                "Open Admin > Withdrawals to review.")
    if alert_type == "fraud_flag":
        return (f"\U0001F6A9 Flagged player asked to withdraw\n{d.get('player_name', '?')}: {_etb(d.get('amount'))}\n"
                "This account shares a connection, phone number or payout account with other accounts.\n"
                "Check Players > Abuse review before approving.")
    if alert_type == "emergency":
        return (f"\U0001F6A8 {d.get('summary', 'Emergency control changed')}\n"
                f"By {d.get('actor', '?')}{' (' + d['role'] + ')' if d.get('role') else ''}\n"
                "Applies to all players immediately.")
    if alert_type == "failed_login":
        return (f"\U0001F510 Failed admin sign-ins\n{d.get('count', '?')} attempts in {d.get('minutes', 10)} minutes from {d.get('ip', 'unknown')}\n"
                f"{d.get('detail', '')}".rstrip())
    if alert_type == "wallet_adjust":
        return (f"\U0001F4B0 Large wallet adjustment\n{d.get('delta', 0):+,} ETB ({d.get('kind', '?')}) for {d.get('player_name', '?')}\n"
                f"By {d.get('actor', '?')}\nReason: {d.get('reason', '')}")
    if alert_type == "control_publish":
        return f"\U0001F4E2 {d.get('summary', 'Control Center changed')}\nBy {d.get('actor', '?')}"
    if alert_type == "staff_change":
        return f"\U0001F465 Staff change\n{d.get('summary', '')}\nBy {d.get('actor', '?')}"
    if alert_type == "bonus_budget":
        icon = "\U0001F6D1" if d.get("level") == 100 else "\u26A0\uFE0F"
        what = "fully used" if d.get("level") == 100 else "80% used"
        return (f"{icon} Bonus budget {what}\n{d.get('reward_type', '?')} {d.get('scope', '')} budget: "
                f"{d.get('used', 0):,} of {d.get('budget', 0):,} ETB")
    if alert_type == "test":
        return "\u2705 Test alert from Melbete Admin. You will receive alerts here."
    return str(d.get("summary", alert_type))
