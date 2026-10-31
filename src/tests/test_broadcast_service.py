import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram.error import Forbidden, NetworkError, BadRequest

from src.services.broadcast_service import BroadcastService, BroadcastValidationError, validate_buttons


def test_broadcast_buttons_accept_https_urls_and_ignore_empty_rows():
    assert validate_buttons([
        {"text": "Open app", "url": "https://example.com/app"},
        {"text": "", "url": ""},
    ]) == [{"text": "Open app", "url": "https://example.com/app"}]


@pytest.mark.parametrize("buttons", [
    [{"text": "Missing URL", "url": ""}],
    [{"text": "Bad URL", "url": "javascript:alert(1)"}],
    [{"text": "1", "url": "https://a.example"}] * 4,
])
def test_broadcast_buttons_reject_invalid_or_excessive_actions(buttons):
    with pytest.raises(BroadcastValidationError):
        validate_buttons(buttons)


@pytest.mark.asyncio
async def test_broadcast_service_throttles_db_updates():
    mock_repo = MagicMock()
    mock_repo._fetchval = AsyncMock(return_value=300)
    mock_repo._execute = AsyncMock()

    # 12 batches of recipients (limit=25)
    batches = [["101", "102"] for _ in range(12)] + [[]]
    mock_repo.list_telegram_ids = AsyncMock(side_effect=batches)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    service = BroadcastService(player_repo=mock_repo)
    result = await service.send("Hello World", bot=mock_bot)

    assert result.attempted == 24
    assert result.delivered == 24
    assert result.failed == 0

    # Collect UPDATE query calls to broadcast_progress
    update_calls = [
        call for call in mock_repo._execute.call_args_list
        if "UPDATE broadcast_progress" in call.args[0]
    ]

    # Batch 5 (sent=10), Batch 10 (sent=20), and final completion (status='completed') -> total 3 update calls
    assert len(update_calls) == 3
    assert "status = 'completed'" not in update_calls[0].args[0]
    assert update_calls[0].args[1] == 10  # attempted at batch 5
    assert update_calls[1].args[1] == 20  # attempted at batch 10
    assert "status = 'completed'" in update_calls[2].args[0]
    assert update_calls[2].args[1] == 24  # final attempted count


@pytest.mark.asyncio
async def test_broadcast_service_logs_specific_delivery_exceptions(caplog):
    mock_repo = MagicMock()
    mock_repo._fetchval = AsyncMock(return_value=3)
    mock_repo._execute = AsyncMock()
    mock_repo.list_telegram_ids = AsyncMock(side_effect=[["user1", "user2", "user3"], []])

    mock_bot = MagicMock()
    async def mock_send(chat_id, text, **kwargs):
        if chat_id == "user1":
            raise Forbidden("bot was blocked by the user")
        elif chat_id == "user2":
            raise NetworkError("Connection reset")
        elif chat_id == "user3":
            return True

    mock_bot.send_message = AsyncMock(side_effect=mock_send)

    service = BroadcastService(player_repo=mock_repo)

    with caplog.at_level("INFO"):
        result = await service.send("Test failure logging", bot=mock_bot)

    assert result.attempted == 3
    assert result.delivered == 1
    assert result.failed == 2

    assert "User blocked bot or forbidden" in caplog.text
    assert "Broadcast network error" in caplog.text


@pytest.mark.asyncio
async def test_broadcast_service_cancellation():
    mock_repo = MagicMock()
    # 1st call total count, 2nd call loop 1 status, 3rd call loop 2 status ('cancelling')
    mock_repo._fetchval = AsyncMock(side_effect=[100, "running", "cancelling"])
    mock_repo._execute = AsyncMock()

    mock_repo.list_telegram_ids = AsyncMock(return_value=["user1", "user2"])

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=True)

    service = BroadcastService(player_repo=mock_repo)
    result = await service.send("Test cancel", bot=mock_bot)

    # 1st batch sent 2 users, 2nd iteration detected cancelling and exited early
    assert result.attempted == 2
    assert result.delivered == 2

    cancel_updates = [
        call for call in mock_repo._execute.call_args_list
        if "status = 'cancelled'" in call.args[0]
    ]
    assert len(cancel_updates) == 1


