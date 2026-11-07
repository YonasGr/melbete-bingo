"""Admin global search: turns what an admin typed into safe SQL patterns."""
import re

MIN_QUERY_LENGTH = 2


def like_pattern(query: str) -> str:
    """'%text%' with the user's own % _ \\ escaped, so they match literally (used with ESCAPE '\\')."""
    q = str(query or "").strip()[:100]
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def phone_pattern(query: str) -> str | None:
    """Pattern for players.phone_normalized (the last 9 digits of the number). None when the text is
    not number-like enough (fewer than 4 digits) to search phones with."""
    digits = re.sub(r"\D", "", str(query or ""))
    if len(digits) < 4:
        return None
    return f"%{digits[-9:]}%"


def clean_query(query: str) -> str:
    return str(query or "").strip()[:100]
