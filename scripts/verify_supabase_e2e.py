"""
End-to-end smoke test: plays a full bingo round through the REAL service
layer (RoomService, WalletService) against a live Postgres database -
not fakes, not mocks. This is the closest thing to "does the actual app
work" without deploying it.

Run against your own Supabase project the same way as
verify_supabase_repositories.py:

    DATABASE_URL="postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres" \\
        python scripts/verify_supabase_e2e.py
"""
import asyncio
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@127.0.0.1:5432/bingo_test")

sys.path.insert(0, ".")

from src.core.db import close_db  # noqa: E402
from src.repositories import (  # noqa: E402
    CardRepository, ClaimRepository, PlayerRepository, RoomRepository,
    SettingsRepository, TransactionRepository, WalletRepository,
)
from src.repositories.draw_log_repository import DrawLogRepository  # noqa: E402
from src.services.room_service import RoomService  # noqa: E402
from src.services.wallet_service import WalletService  # noqa: E402


async def main():
    failures = []

    def check(label, cond):
        status = "OK" if cond else "FAIL"
        print(f"[{status}] {label}")
        if not cond:
            failures.append(label)

    wallet_service = WalletService(
        wallet_repo=WalletRepository(),
        transaction_repo=TransactionRepository(),
        settings_repo=SettingsRepository(),
    )
    room_service = RoomService(
        room_repo=RoomRepository(),
        card_repo=CardRepository(),
        claim_repo=ClaimRepository(),
        draw_log_repo=DrawLogRepository(),
        wallet_service=wallet_service,
        settings_repo=SettingsRepository(),
    )

    players = PlayerRepository()
    player = await players.create("777000111", "E2E Player")

    wallet = await wallet_service.get_or_create_wallet(player.id)
    await wallet_service.wallet_repo.update(player.id, {"balance": 1000})

    await room_service.settings_repo.update({"min_cartelas_to_start": 1})

    room = await room_service.get_or_create_waiting_session()
    check("get_or_create_waiting_session creates a real waiting room", room.state == "waiting")

    room, card = await room_service.select_cartela(room.id, player.id, 17)
    check("select_cartela reserves a real cartela via live SQL", card.cartela_number == 17)

    room, dropped = await room_service.charge_and_start(room.id)
    check("charge_and_start moves the round to running", room is not None and room.state == "running")
    check("charge_and_start actually debited the wallet", dropped == [])

    wallet_after_charge = await wallet_service.get_or_create_wallet(player.id)
    check("stake was really debited in Postgres", wallet_after_charge.balance == 1000 - room.stake_amount)

    while True:
        number, _ = await room_service.draw_number(room.id)
        if number is None:
            break

    outcome = await room_service.claim_win(room.id, player.id, card.id)
    check("claim_win succeeds against live DB", outcome.is_valid and outcome.won)

    ended_room = await room_service.get_room(room.id)
    check("room really ended in Postgres", ended_room.state == "ended")
    check("winner really recorded with nested grid JSONB", len(ended_room.winners) == 1 and isinstance(ended_room.winners[0]["grid"], list) and isinstance(ended_room.winners[0]["grid"][0], list))

    wallet_final = await wallet_service.get_or_create_wallet(player.id)
    expected = 1000 - room.stake_amount + outcome.payout
    check("payout really credited in Postgres", wallet_final.balance == expected)

    txns = await wallet_service.transaction_repo.list_by_player(player.id)
    check("transaction ledger has real rows (stake + payout)", len(txns) >= 2)

    next_room = await room_service.advance_round(room.id)
    check("advance_round creates a genuinely new row", next_room.id != room.id and next_room.state == "waiting")

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S): {failures} ===")
        sys.exit(1)
    print("=== FULL END-TO-END GAME ROUND PASSED AGAINST LIVE POSTGRES ===")
    await close_db()


asyncio.run(main())
