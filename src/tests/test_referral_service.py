"""
Tests for ReferralService - the referral program's single source of
truth. Covers the business-critical rules: a referral only pays once
(idempotent), a referrer can only be PAID for their first N referrals
(admin-configurable cap) even though further referrals still get
tracked, and self-referrals/bogus codes are silently ignored rather than
causing errors.
"""
import pytest

from src.tests.fake_wallet_ops import AtomicWalletOps
from src.models.schemas import Player, Referral, SystemSettings, Transaction, Wallet
from src.services.referral_service import ReferralService
from src.services.wallet_service import WalletService


class FakePlayerRepository:
    def __init__(self):
        self.players = {}

    async def get_by_id(self, player_id):
        return self.players.get(player_id)

    async def get_by_telegram_id(self, telegram_id):
        for p in self.players.values():
            if p.telegram_id == telegram_id:
                return p
        return None

    async def update(self, player_id, fields):
        p = self.players[player_id]
        for k, v in fields.items():
            setattr(p, k, v)

    def add(self, player: Player):
        self.players[player.id] = player
        return player


class FakeReferralRepository:
    def __init__(self):
        self.referrals = {}

    async def create(self, referral: Referral):
        self.referrals[referral.id] = referral
        return referral

    async def update(self, referral_id, fields):
        r = self.referrals[referral_id]
        for k, v in fields.items():
            setattr(r, k, v)

    async def get_by_referred_id(self, referred_id):
        for r in self.referrals.values():
            if r.referred_id == referred_id:
                return r
        return None

    async def list_by_referrer(self, referrer_id):
        return [r for r in self.referrals.values() if r.referrer_id == referrer_id]

    async def list_all(self, limit=500):
        return sorted(self.referrals.values(), key=lambda r: r.created_at, reverse=True)[:limit]


class FakeWalletRepository(AtomicWalletOps):
    def __init__(self):
        self.wallets = {}

    async def get_by_player(self, player_id):
        return self.wallets.get(player_id)

    async def create(self, wallet: Wallet):
        self.wallets[wallet.player_id] = wallet
        return wallet

    async def update(self, player_id, fields):
        wallet = self.wallets[player_id]
        for key, value in fields.items():
            setattr(wallet, key, value)

    async def atomic_debit_real(self, player_id, amount):
        wallet = self.wallets.get(player_id)
        if not wallet or wallet.balance < amount:
            return None
        wallet.balance -= amount
        wallet.won_balance = min(wallet.won_balance, wallet.balance)
        return wallet


class FakeTransactionRepository:
    def __init__(self):
        self.transactions = {}

    async def create(self, transaction: Transaction):
        self.transactions[transaction.id] = transaction
        return transaction

    async def get(self, transaction_id):
        return self.transactions.get(transaction_id)

    async def update(self, transaction_id, fields):
        for k, v in fields.items():
            setattr(self.transactions[transaction_id], k, v)

    async def list_pending_topups(self):
        return []

    async def list_pending_withdrawals(self):
        return []


class FakeSettingsRepository:
    def __init__(self, referral_bonus=20, max_referrals_per_user=3):
        self._settings = SystemSettings(referral_bonus=referral_bonus, max_referrals_per_user=max_referrals_per_user)

    async def get(self):
        return self._settings

    async def get_or_create(self):
        return self._settings

    async def update(self, fields):
        for k, v in fields.items():
            setattr(self._settings, k, v)
        return self._settings


def make_player(telegram_id: str, referred_by=None) -> Player:
    return Player(
        id=f"tg_{telegram_id}",
        telegram_id=telegram_id,
        display_name=f"Player{telegram_id}",
        registration_state="complete",
        referred_by=referred_by,
    )


@pytest.fixture
def players():
    return FakePlayerRepository()


@pytest.fixture
def wallet_service():
    return WalletService(
        wallet_repo=FakeWalletRepository(),
        transaction_repo=FakeTransactionRepository(),
        settings_repo=FakeSettingsRepository(),
    )


@pytest.fixture
def service(players, wallet_service):
    return ReferralService(
        referral_repo=FakeReferralRepository(),
        player_repo=players,
        wallet_service=wallet_service,
        settings_repo=FakeSettingsRepository(),
    )


@pytest.mark.asyncio
async def test_record_referral_click_creates_relationship(service, players):
    referrer = players.add(make_player("111"))
    new_player = players.add(make_player("222"))

    referral = await service.record_referral_click("111", new_player)

    assert referral is not None
    assert referral.referrer_id == referrer.id
    assert referral.referred_id == new_player.id
    assert referral.awarded is False
    assert players.players[new_player.id].referred_by == "111"


@pytest.mark.asyncio
async def test_self_referral_is_ignored(service, players):
    player = players.add(make_player("111"))
    referral = await service.record_referral_click("111", player)
    assert referral is None
    assert players.players[player.id].referred_by is None


@pytest.mark.asyncio
async def test_bogus_referrer_code_is_ignored(service, players):
    new_player = players.add(make_player("222"))
    referral = await service.record_referral_click("does-not-exist", new_player)
    assert referral is None


@pytest.mark.asyncio
async def test_clicking_twice_does_not_duplicate_relationship(service, players):
    players.add(make_player("111"))
    new_player = players.add(make_player("222"))

    first = await service.record_referral_click("111", new_player)
    second = await service.record_referral_click("111", new_player)
    assert first.id == second.id


@pytest.mark.asyncio
async def test_award_credits_referrer_bonus_balance(service, players, wallet_service):
    referrer = players.add(make_player("111"))
    referred = players.add(make_player("222", referred_by="111"))
    await service.record_referral_click("111", referred)

    award = await service.award_if_eligible(referred)

    assert award is not None
    assert award["bonus_amount"] == 20
    assert award["new_count"] == 1

    wallet = await wallet_service.get_or_create_wallet(referrer.id)
    assert wallet.bonus_balance == 20
    assert wallet.balance == 0  # never touches withdrawable/real balance

    assert players.players[referrer.id].referral_count == 1


@pytest.mark.asyncio
async def test_award_never_pays_twice_for_same_referral(service, players, wallet_service):
    referrer = players.add(make_player("111"))
    referred = players.add(make_player("222", referred_by="111"))
    await service.record_referral_click("111", referred)

    await service.award_if_eligible(referred)
    second_award = await service.award_if_eligible(referred)

    assert second_award is None
    wallet = await wallet_service.get_or_create_wallet(referrer.id)
    assert wallet.bonus_balance == 20  # only credited once


@pytest.mark.asyncio
async def test_award_respects_the_cap(players, wallet_service):
    service = ReferralService(
        referral_repo=FakeReferralRepository(),
        player_repo=players,
        wallet_service=wallet_service,
        settings_repo=FakeSettingsRepository(max_referrals_per_user=2),
    )
    referrer = players.add(make_player("111"))

    referred_ids = ["222", "333", "444"]
    for tg_id in referred_ids:
        p = players.add(make_player(tg_id, referred_by="111"))
        await service.record_referral_click("111", p)

    results = []
    for tg_id in referred_ids:
        p = players.players[f"tg_{tg_id}"]
        results.append(await service.award_if_eligible(p))

    # Only the first 2 (the configured cap) actually pay out.
    assert results[0] is not None
    assert results[1] is not None
    assert results[2] is None  # third is over the cap - tracked, not paid

    wallet = await wallet_service.get_or_create_wallet(referrer.id)
    assert wallet.bonus_balance == 40  # 2 x 20 ETB, not 3x
    assert players.players[referrer.id].referral_count == 2

    # But all three referrals are still recorded for the audit trail.
    all_for_referrer = await service.referrals.list_by_referrer(referrer.id)
    assert len(all_for_referrer) == 3
    assert sum(1 for r in all_for_referrer if r.awarded) == 2


@pytest.mark.asyncio
async def test_award_with_no_referrer_is_a_noop(service, players):
    player = players.add(make_player("999"))  # never referred
    award = await service.award_if_eligible(player)
    assert award is None


@pytest.mark.asyncio
async def test_get_stats_reflects_configured_cap_and_bonus(service, players):
    player = players.add(make_player("111"))
    player.referral_count = 1

    # Let's add a fake referral to test total_invited
    referred = players.add(make_player("222"))
    await service.record_referral_click("111", referred)

    stats = await service.get_stats(player, "MyBingoBot")

    assert stats["referral_link"] == "https://t.me/MyBingoBot?start=ref_111"
    assert stats["referral_count"] == 1
    assert stats["max_referrals"] == 3
    assert stats["slots_remaining"] == 2
    assert stats["bonus_per_referral"] == 20
    assert stats["free_credit_amount"] == 50
    assert stats["total_invited"] == 1


@pytest.mark.asyncio
async def test_get_stats_without_bot_username_returns_empty_link(service, players):
    player = players.add(make_player("111"))
    stats = await service.get_stats(player, "")
    assert stats["referral_link"] == ""


@pytest.mark.asyncio
async def test_list_all_for_admin_groups_by_referrer(service, players):
    referrer = players.add(make_player("111"))
    referred1 = players.add(make_player("222", referred_by="111"))
    referred2 = players.add(make_player("333", referred_by="111"))
    await service.record_referral_click("111", referred1)
    await service.record_referral_click("111", referred2)
    await service.award_if_eligible(referred1)

    result = await service.list_all_for_admin()

    assert result["total_referrers"] == 1
    assert result["total_referred_users"] == 2
    assert result["max_referrals_per_user"] == 3
    group = result["referrals"][0]
    assert group["referrer_telegram_id"] == "111"
    assert len(group["referred_users"]) == 2
    awarded_flags = {u["telegram_id"]: u["awarded"] for u in group["referred_users"]}
    assert awarded_flags["222"] is True
    assert awarded_flags["333"] is False
