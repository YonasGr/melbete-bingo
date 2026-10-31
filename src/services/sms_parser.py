"""SMS parser service to extract payment verification URLs."""
import re
from typing import Optional


def extract_verification_url(sms_text: str) -> Optional[str]:
    """Extracts the payment verification URL (starting with https://) from SMS text.

    Returns the clean URL or None if no valid https:// URL was found.
    """
    if not sms_text:
        return None

    match = re.search(r"https://\S+", sms_text)
    if not match:
        return None

    url = match.group(0).strip()
    # Strip any trailing punctuation that might be attached at sentence end
    url = url.rstrip(".,;!)]}")
    return url if url.startswith("https://") else None
