import pytest

@pytest.fixture(autouse=True)
def mock_global_notifications(monkeypatch, request):
    """Globally mock notifications for all tests except test_notifications.py.
    (No database client to mock/patch here anymore - Postgres repositories
    are lazy by design and never connect just from being instantiated; see
    src/repositories/base.py.)"""
    if "test_notifications" in request.node.nodeid:
        return

    async def fake_notify_deposit(txn):
        return ("-100999", 1001, False)

    async def fake_notify_withdrawal(txn):
        return ("-100999", 1002, False)

    async def fake_update_notification_message(txn, line):
        return None

    monkeypatch.setattr("src.services.notifications.notify_admins_of_deposit", fake_notify_deposit)
    monkeypatch.setattr("src.services.notifications.notify_admins_of_withdrawal", fake_notify_withdrawal)
    monkeypatch.setattr("src.services.notifications.update_notification_message", fake_update_notification_message)


@pytest.fixture(autouse=True)
def silence_player_notifications(monkeypatch, request):
    """Event hooks call schedule_notify after money moves; unit tests have no database, so make it a
    no-op everywhere except the notification tests themselves."""
    if "test_player_notifications" in request.node.nodeid:
        return
    monkeypatch.setattr("src.services.player_notifications.schedule_notify", lambda *a, **k: None)
