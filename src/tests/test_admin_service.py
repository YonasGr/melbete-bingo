"""Tests for admin-managed payment methods."""
import pytest

from src.models.schemas import PaymentMethod
from src.services.admin_service import AdminService


class FakePaymentMethodRepository:
    def __init__(self):
        self.methods = {}

    async def get(self, method_id):
        return self.methods.get(method_id)

    async def get_by_key(self, key):
        for m in self.methods.values():
            if m.key == key:
                return m
        return None

    async def create(self, method: PaymentMethod):
        self.methods[method.id] = method
        return method

    async def update(self, method_id, fields):
        m = self.methods[method_id]
        for k, v in fields.items():
            setattr(m, k, v)

    async def delete(self, method_id):
        self.methods.pop(method_id, None)

    async def list_all(self):
        return sorted(self.methods.values(), key=lambda m: m.display_order)

    async def list_active(self):
        return [m for m in await self.list_all() if m.active]


class FakeAdminRepository:
    def __init__(self):
        self.admins = {}

    async def get_by_telegram_id(self, telegram_id):
        return self.admins.get(str(telegram_id))

    async def create(self, admin):
        self.admins[admin.telegram_id] = admin
        return admin

    async def delete(self, telegram_id):
        self.admins.pop(str(telegram_id), None)

    async def list_all(self):
        return list(self.admins.values())


class FakeSettingsRepository:
    async def get_or_create(self):
        from src.models.schemas import SystemSettings

        return SystemSettings()

    async def update(self, fields):
        return await self.get_or_create()


@pytest.fixture
def admin_service():
    return AdminService(
        admin_repo=FakeAdminRepository(),
        payment_repo=FakePaymentMethodRepository(),
        settings_repo=FakeSettingsRepository(),
    )


@pytest.mark.asyncio
async def test_empty_payment_methods_are_not_seeded(admin_service):
    assert await admin_service.list_payment_methods() == []


@pytest.mark.asyncio
async def test_deleted_payment_method_stays_deleted(admin_service):
    method = await admin_service.upsert_payment_method(
        key="telebirr", name="Telebirr", account_info="0912345678"
    )
    await admin_service.delete_payment_method(method.id)
    assert await admin_service.list_payment_methods() == []


@pytest.mark.asyncio
async def test_list_payment_methods_returns_exact_database_rows(admin_service):
    await admin_service.upsert_payment_method(
        key="telebirr", name="Telebirr", account_info="0912345678"
    )
    methods = await admin_service.list_payment_methods()
    keys = {m.key for m in methods}
    assert keys == {"telebirr"}


@pytest.mark.asyncio
async def test_inactive_method_is_hidden_only_from_active_listing(admin_service):
    method = await admin_service.upsert_payment_method(
        key="cbe", name="CBE Birr", account_info="1000000000"
    )
    await admin_service.upsert_payment_method(
        method_id=method.id,
        key="cbe",
        name="CBE Birr",
        account_info=method.account_info,
        active=False,
    )

    active_methods = await admin_service.list_payment_methods(active_only=True)
    assert "cbe" not in {m.key for m in active_methods}

    all_methods = await admin_service.list_payment_methods(active_only=False)
    assert "cbe" in {m.key for m in all_methods}
    assert sum(1 for m in all_methods if m.key == "cbe") == 1


# --------------------------------------------------------------------------
# Staff management: promote, demote, remove
# --------------------------------------------------------------------------
from src.models.schemas import AdminUser  # noqa: E402
from src.services.admin_service import NotSuperAdminError  # noqa: E402


async def _staff(svc, owner="1", other_super="2", regular="3", role="finance"):
    """owner + another super admin + one regular staff member."""
    await svc.admins.create(AdminUser(telegram_id=owner, is_super_admin=True))
    await svc.admins.create(AdminUser(telegram_id=other_super, is_super_admin=True))
    await svc.admins.create(AdminUser(telegram_id=regular, role=role))


@pytest.fixture
def owner_is_1(monkeypatch):
    from src.services import admin_service as mod

    monkeypatch.setattr(mod.env_settings, "super_admin_telegram_id", "1", raising=False)


async def test_promote_makes_regular_admin_super(admin_service, owner_is_1):
    await _staff(admin_service)
    promoted = await admin_service.promote_to_super_admin("1", "3")
    assert promoted.is_super_admin is True
    assert await admin_service.is_super_admin("3")


async def test_promote_rejects_non_super_requester_bad_id_and_repeat(admin_service, owner_is_1):
    await _staff(admin_service)
    with pytest.raises(NotSuperAdminError):
        await admin_service.promote_to_super_admin("3", "3")
    with pytest.raises(ValueError):
        await admin_service.promote_to_super_admin("1", "abc")
    with pytest.raises(ValueError):
        await admin_service.promote_to_super_admin("1", "2")  # already super


async def test_demote_returns_super_to_chosen_role(admin_service, owner_is_1):
    await _staff(admin_service)
    demoted = await admin_service.demote_super_admin("1", "2", role="support")
    assert demoted.is_super_admin is False and demoted.role == "support"
    assert not await admin_service.is_super_admin("2")


async def test_demote_guards(admin_service, owner_is_1):
    await _staff(admin_service)
    with pytest.raises(ValueError):
        await admin_service.demote_super_admin("2", "2")            # yourself
    with pytest.raises(NotSuperAdminError):
        await admin_service.demote_super_admin("2", "1")            # the owner account
    with pytest.raises(ValueError):
        await admin_service.demote_super_admin("1", "3")            # not a super admin
    with pytest.raises(ValueError):
        await admin_service.demote_super_admin("1", "2", role="super")  # not an assignable role
    with pytest.raises(NotSuperAdminError):
        await admin_service.demote_super_admin("3", "2")            # requester is not super


async def test_remove_regular_and_other_super_admin(admin_service, owner_is_1):
    await _staff(admin_service)
    await admin_service.remove_admin("1", "3")
    assert await admin_service.get_admin("3") is None
    await admin_service.remove_admin("1", "2")                     # another super admin may be removed
    assert await admin_service.get_admin("2") is None
    assert await admin_service.is_super_admin("1")                 # the remover always remains


async def test_remove_guards(admin_service, owner_is_1):
    await _staff(admin_service)
    with pytest.raises(ValueError):
        await admin_service.remove_admin("2", "2")                  # yourself
    with pytest.raises(NotSuperAdminError):
        await admin_service.remove_admin("2", "1")                  # the owner account
    with pytest.raises(ValueError):
        await admin_service.remove_admin("1", "999")                # not on the list
    with pytest.raises(NotSuperAdminError):
        await admin_service.remove_admin("3", "2")                  # requester is not super
    assert await admin_service.get_admin("1") is not None


# --------------------------------------------------------------------------
# SUPER_ADMIN_TELEGRAM_ID may hold several IDs ("111,222")
# --------------------------------------------------------------------------
async def test_bootstrap_creates_every_listed_super_admin(admin_service, monkeypatch):
    from src.services import admin_service as mod

    monkeypatch.setattr(mod.env_settings, "super_admin_telegram_id", "898505692,394684531", raising=False)
    await admin_service.ensure_super_admin_bootstrapped()
    assert await admin_service.is_super_admin("898505692")
    assert await admin_service.is_super_admin("394684531")
    assert await admin_service.get_admin("898505692,394684531") is None   # no record under the joined string
    assert admin_service.is_owner("394684531") and admin_service.is_owner("898505692")
    assert not admin_service.is_owner("123")


async def test_bootstrap_ignores_non_numeric_entries(admin_service, monkeypatch):
    from src.services import admin_service as mod

    monkeypatch.setattr(mod.env_settings, "super_admin_telegram_id", "abc; 55 55,,x", raising=False)
    await admin_service.ensure_super_admin_bootstrapped()
    assert await admin_service.is_super_admin("55")
    assert await admin_service.get_admin("abc") is None and await admin_service.get_admin("x") is None
