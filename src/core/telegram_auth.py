"""
Telegram WebApp `initData` verification.

Telegram signs the data it hands to a Mini App with an HMAC-SHA256 derived
from the bot token (see
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
Verifying this signature server-side is the ONLY way to trust a claimed
Telegram identity - a client-supplied `telegram_id` or `player_id` on its
own is trivially spoofable (anyone can send any ID in a header/body).

Every endpoint that reads or moves a specific player's money, or that
grants admin access, must resolve identity through `extract_telegram_id`
here rather than trusting a raw id from the request.

During incident response this module accepts only fresh Telegram-signed
``initData``. The former long-lived browser bearer-token flow is disabled:
it could not provide the same proof of possession as Telegram and had no
server-side revocation list.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from src.core.config import settings

logger = logging.getLogger(__name__)

# initData older than this is rejected, even if the signature is valid -
# limits how long a captured/replayed initData string stays useful.
# NOTE: this used to be hardcoded to 5 minutes, which is shorter than a
# normal bingo lobby-wait + round. Telegram issues initData once when the
# Mini App launches and does not refresh it while the app stays open, so a
# too-short window here silently breaks profile/wallet/claim/WebSocket
# calls partway through a game with no clear error for the player - see
# `telegram_init_data_max_age_seconds` in src/core/config.py.
MAX_INIT_DATA_AGE_SECONDS = settings.telegram_init_data_max_age_seconds

def verify_init_data(init_data: str, max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS) -> Optional[dict]:
    """Validates a Telegram WebApp `initData` string against the bot
    token. Returns the parsed fields (with `user` decoded from JSON) if
    the signature is valid and fresh, otherwise None.
    """
    if not init_data or not settings.telegram_bot_token:
        return None

    try:
        parsed_pairs = parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return None
    # Duplicate keys otherwise let parsing differences create signature bugs.
    if len({key for key, _ in parsed_pairs}) != len(parsed_pairs):
        return None
    pairs = dict(parsed_pairs)

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            age = time.time() - int(auth_date)
            if age < -30 or age > max_age_seconds:
                return None
        except ValueError:
            return None

    result = dict(pairs)
    if "user" in result:
        try:
            result["user"] = json.loads(result["user"])
        except (ValueError, TypeError):
            pass
    return result


def extract_telegram_id(init_data: Optional[str]) -> Optional[str]:
    """Verify fresh Telegram initData and return its numeric user id."""
    if not init_data:
        return None
    data = verify_init_data(init_data)
    if not data:
        return None
    user = data.get("user")
    if isinstance(user, dict) and "id" in user:
        return str(user["id"])
    return None
