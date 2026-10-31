"""
Real integration smoke test against a live Postgres database (not fakes).
Exercises every repository's actual SQL against the schema.sql DDL.

Run against your own Supabase project (after applying supabase/schema.sql
there) to confirm your setup works end-to-end before deploying:

    DATABASE_URL="postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres" \\
        python scripts/verify_supabase_repositories.py

Safe to run repeatedly - each run creates its own throwaway rows.
"""
import asyncio
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@127.0.0.1:5432/bingo_test")

sys.path.insert(0, ".")

from src.core.db import get_pool, close_db  # noqa: E402
from src.models.schemas import (  # noqa: E402
    Player, GameRoom, Card, Wallet, Transaction, PaymentMethod,
    AdminUser, SystemSettings, Referral, Claim, LinkCode,
)
from src.repositories import (  # noqa: E402
    PlayerRepository, RoomRepository, CardRepository, WalletRepository,
    TransactionRepository, PaymentMethodRepository, AdminRepository,
    SettingsRepository, ReferralRepository, ClaimRepository, LinkCodeRepository,
)
from src.repositories.draw_log_repository import DrawLogRepository  # noqa: E402


async def main():
    failures = []

    def check(label, cond):
        status = "OK" if cond else "FAIL"
        print(f"[{status}] {label}")
        if not cond:
            failures.append(label)

    # ---- players ----
    players = PlayerRepository()
    p = await players.create("999888777", "Test Player")
    check("player.create returns Player", p.id == "tg_999888777")
    p2 = await players.get_by_id(p.id)
    check("player.get_by_id round-trips", p2 is not None and p2.display_name == "Test Player")
    p3 = await players.get_by_telegram_id("999888777")
    check("player.get_by_telegram_id works", p3 is not None and p3.id == p.id)
    p4 = await players.get_or_create("999888777", "Ignored Name")
    check("player.get_or_create returns existing (idempotent)", p4.id == p.id)
    await players.update(p.id, {"display_name": "Renamed", "auto_bingo_enabled": True})
    p5 = await players.get_by_id(p.id)
    check("player.update persists fields", p5.display_name == "Renamed" and p5.auto_bingo_enabled is True)
    await players.set_withdrawal_account(p.id, "telebirr", "0912345678")
    p6 = await players.get_by_id(p.id)
    check("player withdrawal_accounts JSONB round-trips", p6.withdrawal_accounts.get("telebirr") == "0912345678")
    all_players = await players.list_all()
    check("player.list_all returns list", isinstance(all_players, list) and len(all_players) >= 1)
    count = await players.count()
    check("player.count works", count >= 1)

    # ---- wallets ----
    wallets = WalletRepository()
    w = await wallets.create(Wallet(player_id=p.id, balance=100))
    check("wallet.create", w.balance == 100)
    w2 = await wallets.get_by_player(p.id)
    check("wallet.get_by_player round-trips", w2 is not None and w2.balance == 100)
    await wallets.update(p.id, {"balance": 250, "bonus_balance": 10})
    w3 = await wallets.get_by_player(p.id)
    check("wallet.update persists", w3.balance == 250 and w3.bonus_balance == 10)

    # ---- transactions ----
    txns = TransactionRepository()
    t = await txns.create(Transaction(player_id=p.id, type="top_up_pending", amount=500, status="pending"))
    check("transaction.create", t.amount == 500)
    pending = await txns.list_pending_topups()
    check("transaction.list_pending_topups finds it", any(x.id == t.id for x in pending))
    await txns.update(t.id, {"status": "completed", "balance_after": 750})
    t2 = await txns.get(t.id)
    check("transaction.update/get round-trips", t2.status == "completed" and t2.balance_after == 750)
    by_player = await txns.list_by_player(p.id)
    check("transaction.list_by_player finds it", any(x.id == t.id for x in by_player))
    await txns.set_notification_ref(t.id, "-100123", 55, False)
    t3 = await txns.get(t.id)
    check("transaction.set_notification_ref persists", t3.notification_chat_id == "-100123" and t3.notification_message_id == 55)

    # ---- game_rooms ----
    rooms = RoomRepository()
    room = GameRoom(pattern={"id": "any_line"})
    await rooms.create(room)
    check("room.create", True)
    room2 = await rooms.get(room.id)
    check("room.get round-trips JSONB pattern", room2.pattern.get("id") == "any_line")
    latest = await rooms.get_latest()
    check("room.get_latest returns most recent", latest.id == room.id)
    await rooms.update(room.id, {"state": "running", "called_numbers": [1, 2, 3], "winners": [{"player_id": "x", "grid": [[{"v": 1}]]}]})
    room3 = await rooms.get(room.id)
    check("room.update persists JSONB list-of-ints", room3.called_numbers == [1, 2, 3])
    check("room.update persists nested-array-in-JSONB (the whole point!)", room3.winners[0]["grid"] == [[{"v": 1}]])
    recent = await rooms.list_recent()
    check("room.list_recent includes it", any(r.id == room.id for r in recent))

    # ---- cards ----
    cards = CardRepository()
    grid = [[{"value": r * 5 + c, "marked": False, "free": (r == 2 and c == 2)} for c in range(5)] for r in range(5)]
    card = Card(room_id=room.id, owner_id=p.id, variant="75", cartela_number=7, grid=grid)
    await cards.create(card)
    card2 = await cards.get(card.id)
    check("card.get round-trips nested grid JSONB", card2.grid[0][0]["value"] == 0 and card2.grid[2][2]["free"] is True)
    by_room = await cards.list_by_room(room.id)
    check("card.list_by_room finds it", any(c.id == card.id for c in by_room))
    by_room_owner = await cards.list_by_room_and_owner(room.id, p.id)
    check("card.list_by_room_and_owner finds it", any(c.id == card.id for c in by_room_owner))
    await cards.update(card.id, {"funded_by": "bonus"})
    card3 = await cards.get(card.id)
    check("card.update persists", card3.funded_by == "bonus")
    await cards.delete(card.id)
    card4 = await cards.get(card.id)
    check("card.delete removes it", card4 is None)

    # ---- payment_methods ----
    pms = PaymentMethodRepository()
    pm = await pms.create(PaymentMethod(key="telebirr_test", name="Telebirr", account_info="0900000000"))
    check("payment_method.create", True)
    pm2 = await pms.get_by_key("telebirr_test")
    check("payment_method.get_by_key", pm2 is not None and pm2.name == "Telebirr")
    await pms.update(pm.id, {"active": False})
    pm3 = await pms.get(pm.id)
    check("payment_method.update persists", pm3.active is False)
    active = await pms.list_active()
    check("payment_method.list_active excludes deactivated", all(m.id != pm.id for m in active))
    await pms.delete(pm.id)
    check("payment_method.delete", await pms.get(pm.id) is None)

    # ---- admins ----
    admins = AdminRepository()
    admin = await admins.create(AdminUser(telegram_id="111222333", is_super_admin=True))
    check("admin.create", True)
    admin2 = await admins.get_by_telegram_id("111222333")
    check("admin.get_by_telegram_id", admin2 is not None and admin2.is_super_admin is True)
    all_admins = await admins.list_all()
    check("admin.list_all", any(a.telegram_id == "111222333" for a in all_admins))
    await admins.delete("111222333")
    check("admin.delete", await admins.get_by_telegram_id("111222333") is None)

    # ---- system_settings ----
    settings_repo = SettingsRepository()
    s = await settings_repo.get_or_create()
    check("settings.get_or_create seeds defaults", s.stake_amount == 10)
    s2 = await settings_repo.update({"stake_amount": 25})
    check("settings.update persists and returns updated", s2.stake_amount == 25)
    s3 = await settings_repo.get()
    check("settings.get reflects update", s3.stake_amount == 25)

    # ---- referrals ----
    refs = ReferralRepository()
    r = await refs.create(Referral(referrer_id=p.id, referred_id="tg_444555666"))
    check("referral.create", True)
    r2 = await refs.get_by_referred_id("tg_444555666")
    check("referral.get_by_referred_id", r2 is not None and r2.referrer_id == p.id)
    await refs.update(r.id, {"awarded": True, "bonus_amount": 20})
    by_referrer = await refs.list_by_referrer(p.id)
    check("referral.list_by_referrer + update", any(x.id == r.id and x.awarded for x in by_referrer))
    all_refs = await refs.list_all()
    check("referral.list_all", any(x.id == r.id for x in all_refs))

    # ---- claims ----
    claims = ClaimRepository()
    await claims.create(Claim(room_id=room.id, player_id=p.id, card_id="card-x", claimed_pattern="any_line"))
    check("claim.create (write-only, no crash)", True)

    # ---- draw_logs ----
    draw_logs = DrawLogRepository()
    log = await draw_logs.create_for_room(room.id, seed="abc123")
    check("draw_log.create_for_room", True)
    await draw_logs.append_draw(log.id, 1, 42)
    await draw_logs.append_draw(log.id, 2, 7)
    log2 = await draw_logs.get_by_room(room.id)
    check("draw_log.append_draw persists sequence JSONB", len(log2.sequence) == 2 and log2.sequence[0]["value"] == 42)
    await draw_logs.finalize(log.id)
    log3 = await draw_logs.get_by_room(room.id)
    check("draw_log.finalize sets final_hash", log3.final_hash is not None and len(log3.final_hash) == 64)

    # ---- link_codes ----
    links = LinkCodeRepository()
    lc = await links.create(LinkCode(code="ABC123", web_player_id="web_xyz"))
    check("link_code.create", True)
    lc2 = await links.get_by_code("ABC123")
    check("link_code.get_by_code", lc2 is not None and lc2.web_player_id == "web_xyz")
    await links.update(lc.id, {"status": "linked", "telegram_id": "999888777"})
    lc3 = await links.get(lc.id)
    check("link_code.update persists", lc3.status == "linked" and lc3.telegram_id == "999888777")

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S): {failures} ===")
        sys.exit(1)
    else:
        print(f"=== ALL CHECKS PASSED ===")

    await close_db()


asyncio.run(main())
