"""SECRET_KEY strength check (no heavy imports so it can be unit-tested on its own)."""
from typing import Optional

_PLACEHOLDERS = ("change-this", "change_this", "changeme", "change-me", "your-secret", "your_secret", "replace-me",
                 "placeholder", "example", "secret-key", "default", "password", "dev-secret", "test")


def weak_secret_reason(secret: Optional[str]) -> Optional[str]:
    """None if the key looks fine, otherwise a short human reason."""
    s = (secret or "").strip()
    if len(s) < 32:
        return "it is shorter than 32 characters"
    low = s.lower()
    if any(p in low for p in _PLACEHOLDERS):
        return "it looks like a placeholder value"
    if len(set(s)) < 10:
        return "it has too little variety"
    return None
