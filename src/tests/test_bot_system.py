"""Unit tests for the 50 Ethiopian Automated Bot System and Deposit Toggle."""
import pytest
from src.models.schemas import SystemSettings
from src.repositories.bot_repository import ETHIOPIAN_NAMES, BotRepository
from src.services.bot_service import BotService
from src.services.wallet_service import InvalidAmountError, WalletService


def test_ethiopian_names_count():
    assert len(ETHIOPIAN_NAMES) == 88
    assert "Abatu_G" in ETHIOPIAN_NAMES
    assert "Ziyad_S" in ETHIOPIAN_NAMES


@pytest.mark.asyncio
async def test_bot_repository_seeding(monkeypatch):
    class DummyPool:
        async def fetch(self, query, *args):
            return []

        async def fetchrow(self, query, *args):
            return None

        async def execute(self, query, *args):
            return "OK"

    async def dummy_get_pool():
        return DummyPool()

    monkeypatch.setattr("src.repositories.base.get_pool", dummy_get_pool)

    # Verify BotRepository instantiation
    repo = BotRepository()
    assert repo is not None


@pytest.mark.asyncio
async def test_deposit_toggle_blocking(monkeypatch):
    # Test that when deposits_enabled is False, WalletService.request_deposit raises InvalidAmountError
    ws = WalletService()

    async def mock_limits():
        return SystemSettings(id="global", deposits_enabled=False)

    monkeypatch.setattr(ws, "_limits", mock_limits)

    with pytest.raises(InvalidAmountError, match="Deposits are currently disabled"):
        await ws.request_deposit("player_123", 100, "telebirr")
