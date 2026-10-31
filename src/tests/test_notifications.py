"""
Tests for src.services.notifications.

The most important test here is `test_bot_is_initialized_before_use` -
it's a regression test for the actual production bug found in this
module: a raw `telegram.Bot` instance was being used to send messages
without ever calling `await bot.initialize()` first, which raises
`RuntimeError` on every single call in real python-telegram-bot (that
error was then silently swallowed by a broad except, so every admin
notification silently failed). The FakeBot below reproduces that same
"raises if used before initialize()" behavior so a regression would be
caught immediately by this test suite instead of silently in production.
"""
from types import SimpleNamespace

import pytest

from src.core.config import settings
from src.models.schemas import Transaction
import src.services.notifications as notifications


class FakeBot:
    """Mimics python-telegram-bot's real requirement: any network-hitting
    method raises unless `initialize()` was called first."""

    def __init__(self, token):
        self.token = token
        self.initialized = False
        self.sent_messages = []
        self.edits = []

    def _require_initialized(self):
        if not self.initialized:
            raise RuntimeError("This HTTPXRequest is not initialized!")

    async def initialize(self):
        self.initialized = True

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._require_initialized()
        self.sent_messages.append({"chat_id": chat_id, "text": text, "kind": "text"})
        return SimpleNamespace(message_id=1001)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        self._require_initialized()
        self.sent_messages.append({"chat_id": chat_id, "caption": caption, "kind": "photo"})
        return SimpleNamespace(message_id=1002)

    async def get_me(self):
        self._require_initialized()
        return SimpleNamespace(username="TestBingoBot")

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self._require_initialized()
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def edit_message_caption(self, chat_id, message_id, caption, parse_mode=None, reply_markup=None):
        self._require_initialized()
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": caption})


class FailingUninitializedBot(FakeBot):
    """A Bot stand-in that never gets initialized - used to prove that
    _get_bot() itself (not just the test double) performs the init."""

    async def initialize(self):
        raise AssertionError("initialize() should not be called by this fake")


@pytest.fixture(autouse=True)
def reset_bot_singleton(monkeypatch):
    """The real module caches a single Bot instance for the process
    lifetime - reset it between tests so each test gets a fresh fake."""
    monkeypatch.setattr(notifications, "_bot", None)
    monkeypatch.setattr(notifications, "_bot_username", None)
    monkeypatch.setattr(notifications, "Bot", FakeBot)
    monkeypatch.setattr(settings, "telegram_bot_token", "123:fake-token")
    monkeypatch.setattr(settings, "admin_chat_id", "-100999")
    monkeypatch.setattr(settings, "admin_telegram_ids", "")
    monkeypatch.setattr(settings, "public_base_url", "https://bingo.example.com")

    # notify_admins_of_* look up the player for a friendly display name -
    # not the concern of these tests, and would otherwise hit the real
    # database. Player-not-found is already a handled, valid case (falls
    # back to showing the raw player_id). Patching the repository method
    # directly is enough now - unlike the old Firestore-backed
    # BaseRepository, the Postgres one never eagerly connects in
    # __init__, so there's no client factory that needs patching too.

    async def _no_player(self, player_id):
        return None

    monkeypatch.setattr("src.repositories.player_repository.PlayerRepository.get_by_id", _no_player)

    # PaymentMethodRepository.get_by_key similarly isn't this module's
    # concern to test - falls back to showing the raw key when not found.
    async def _no_method(self, key):
        return None

    monkeypatch.setattr(
        "src.repositories.payment_method_repository.PaymentMethodRepository.get_by_key", _no_method
    )


@pytest.mark.asyncio
async def test_bot_is_initialized_before_use():
    """Regression test for the actual production bug: _get_bot() must
    call initialize() before the bot is usable, or every real send call
    would raise RuntimeError (silently swallowed by callers' try/except,
    which is exactly why this went unnoticed)."""
    bot = await notifications._get_bot()
    assert bot.initialized is True

    # And it must actually be usable afterwards - this would raise with
    # the old (buggy) implementation that skipped initialize().
    result = await bot.send_message(chat_id="123", text="hello")
    assert result.message_id == 1001


@pytest.mark.asyncio
async def test_get_bot_reuses_same_instance():
    bot1 = await notifications._get_bot()
    bot2 = await notifications._get_bot()
    assert bot1 is bot2  # only initialized once per process


@pytest.mark.asyncio
async def test_notify_admins_of_deposit_sends_and_returns_message_ref():
    txn = Transaction(player_id="tg_1", type="top_up_pending", amount=200, status="pending", payment_method="telebirr")
    ref = await notifications.notify_admins_of_deposit(txn)

    assert ref is not None
    chat_id, message_id, is_photo = ref
    assert chat_id == "-100999"
    assert message_id == 1001
    assert is_photo is False


@pytest.mark.asyncio
async def test_notify_admins_of_withdrawal_sends_to_configured_group():
    txn = Transaction(
        player_id="tg_1", type="withdrawal_pending", amount=-300, status="pending",
        payment_method="telebirr", legal_name="Test Player", withdrawal_address="0912345678",
    )
    ref = await notifications.notify_admins_of_withdrawal(txn)
    assert ref is not None
    assert ref[0] == "-100999"


@pytest.mark.asyncio
async def test_get_bot_username_fetches_and_caches():
    username = await notifications.get_bot_username()
    assert username == "TestBingoBot"

    # Second call must not need re-initialization/re-fetch to still work.
    username2 = await notifications.get_bot_username()
    assert username2 == "TestBingoBot"


@pytest.mark.asyncio
async def test_update_notification_message_edits_text_message():
    txn = Transaction(
        player_id="tg_1", type="top_up_approved", amount=200, status="completed",
        notification_chat_id="-100999", notification_message_id=1001, notification_is_photo=False,
    )
    await notifications.update_notification_message(txn, "✅ Approved by Admin (via web admin panel)")

    bot = await notifications._get_bot()
    assert len(bot.edits) == 1
    assert bot.edits[0]["message_id"] == 1001
    assert "Approved" in bot.edits[0]["text"]


@pytest.mark.asyncio
async def test_update_notification_message_edits_photo_caption():
    txn = Transaction(
        player_id="tg_1", type="top_up_approved", amount=200, status="completed",
        notification_chat_id="-100999", notification_message_id=1002, notification_is_photo=True,
    )
    await notifications.update_notification_message(txn, "✅ Approved")
    bot = await notifications._get_bot()
    assert len(bot.edits) == 1
    assert bot.edits[0]["message_id"] == 1002


@pytest.mark.asyncio
async def test_update_notification_message_is_noop_without_stored_ref():
    txn = Transaction(player_id="tg_1", type="top_up_approved", amount=200, status="completed")
    await notifications.update_notification_message(txn, "✅ Approved")
    bot = await notifications._get_bot()
    assert bot.edits == []


@pytest.mark.asyncio
async def test_no_admin_chat_or_ids_configured_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "admin_chat_id", None)
    monkeypatch.setattr(settings, "admin_telegram_ids", "")

    async def _no_admins():
        return []

    monkeypatch.setattr(notifications, "_admin_chat_targets", _no_admins)
    txn = Transaction(player_id="tg_1", type="top_up_pending", amount=200, status="pending", payment_method="telebirr")
    ref = await notifications.notify_admins_of_deposit(txn)
    assert ref is None


@pytest.mark.asyncio
async def test_no_bot_token_configured_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    txn = Transaction(player_id="tg_1", type="top_up_pending", amount=200, status="pending", payment_method="telebirr")
    ref = await notifications.notify_admins_of_deposit(txn)
    assert ref is None
