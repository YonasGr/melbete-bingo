from src.core.secret_check import weak_secret_reason


def test_rejects_weak_keys():
    assert weak_secret_reason("short")
    assert weak_secret_reason("change-this-to-a-long-random-string-please-0123456789")
    assert weak_secret_reason("a" * 40)
    assert weak_secret_reason(None)


def test_accepts_strong_key():
    assert weak_secret_reason("f3b9c1d47a8e2650b1c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7") is None
