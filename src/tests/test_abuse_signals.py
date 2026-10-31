"""Multi-account signals: phone normalisation and hashed IPs. Pure functions, no DB."""
from src.services.abuse_signals import hash_ip, normalize_phone


def test_same_number_in_every_format_normalises_the_same():
    forms = ["+251911234567", "251911234567", "0911234567", "+251 91 123 4567", "(0911) 234-567"]
    assert {normalize_phone(f) for f in forms} == {"911234567"}


def test_unusable_numbers_return_none():
    for bad in (None, "", "12345", "abc", "+25191"):
        assert normalize_phone(bad) is None


def test_different_numbers_stay_different():
    assert normalize_phone("0911234567") != normalize_phone("0911234568")


def test_ip_hash_is_stable_keyed_and_never_the_raw_address():
    a = hash_ip("203.0.113.7")
    assert a and a == hash_ip("203.0.113.7") and "203" not in a
    assert a != hash_ip("203.0.113.8")
    assert hash_ip("") is None and hash_ip("unknown") is None
