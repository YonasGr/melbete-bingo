import pytest

from src.services.agent_service import InvalidAgentInputError, normalize_telegram_address


@pytest.mark.parametrize("raw,expected", [
    ("@melbete_news", "https://t.me/melbete_news"),
    ("t.me/melbete_news", "https://t.me/melbete_news"),
    ("https://t.me/melbete_news/", "https://t.me/melbete_news"),
    ("HTTP://telegram.me/melbete_news?start=1", "https://t.me/melbete_news"),
    ("melbete_news", "https://t.me/melbete_news"),
    ("https://t.me/+AbCdEf12345", "https://t.me/+AbCdEf12345"),
    ("t.me/joinchat/AbCdEf12345", "https://t.me/joinchat/AbCdEf12345"),
])
def test_telegram_address_is_normalised(raw, expected):
    assert normalize_telegram_address(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "https://example.com/melbete", "t.me/ab", "t.me/a/b/c", "@bad name", "x" * 300])
def test_bad_telegram_address_is_rejected(raw):
    with pytest.raises(InvalidAgentInputError):
        normalize_telegram_address(raw)
