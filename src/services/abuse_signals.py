"""Multi-account signals: normalised phone numbers and hashed IPs.

Nothing here bans anyone. It makes duplicates impossible for phones (one account per number) and
visible for IPs and payout accounts (several people legitimately share Wi-Fi or a family phone, so
those only go on the admin "Abuse review" list for a human to judge).
"""
import hashlib
import hmac
import logging
import re
from typing import Optional

from src.core.config import settings

logger = logging.getLogger(__name__)

_UNKNOWN_IPS = {"", "unknown", "testclient"}


def normalize_phone(raw) -> Optional[str]:
    """Last 9 digits, so +251911234567, 251911234567 and 0911234567 are the same number.
    None when there are fewer than 9 digits (not a usable phone number)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-9:] if len(digits) >= 9 else None


def hash_ip(ip: str) -> Optional[str]:
    """Keyed hash: the raw address is never stored, and the hash is useless without SECRET_KEY."""
    ip = (ip or "").strip()
    if ip.lower() in _UNKNOWN_IPS:
        return None
    return hmac.new(settings.secret_key.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]


async def record_ip(player_id: str, ip: str) -> None:
    """Best-effort: remember that this player used this connection for a money action. Never raises."""
    try:
        from src.repositories.abuse_repository import AbuseRepository

        h = hash_ip(ip)
        if h:
            await AbuseRepository().record_ip(player_id, h)
    except Exception:  # noqa: BLE001 - a logging problem must not block a deposit or withdrawal
        logger.exception("Could not record IP signal for %s", player_id)


async def build_report(min_ip_players: int = 3) -> dict:
    from src.repositories.abuse_repository import AbuseRepository

    repo = AbuseRepository()
    ip_clusters = await repo.ip_clusters(min_ip_players)
    phones = await repo.phone_duplicates()
    payouts = await repo.shared_payout_accounts()
    ids = {p for c in (*ip_clusters, *phones, *payouts) for p in c["players"]}
    info = await repo.players_info(sorted(ids)) if ids else {}

    def hydrate(cluster: dict) -> dict:
        return {**cluster, "players": [info.get(p, {"id": p, "display_name": p, "is_banned": False}) for p in cluster["players"]]}

    return {
        "shared_ip": [hydrate(c) for c in ip_clusters],
        "duplicate_phone": [hydrate(c) for c in phones],
        "shared_payout_account": [hydrate(c) for c in payouts],
        "min_ip_players": min_ip_players,
    }
