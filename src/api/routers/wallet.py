"""
Wallet balance, Deposits, Withdrawals, Payment Methods, and Transactions endpoints.
"""
from fastapi import APIRouter, Body, Header, HTTPException, Request

from src.api.idempotency import run_idempotent
from src.core.rate_limit import client_ip, enforce as enforce_rate_limit
from src.services.abuse_signals import record_ip
from src.repositories import PaymentMethodRepository, PlayerRepository
from src.services import InsufficientBalanceError
from src.services.wallet_service import (
    DuplicateTransactionError,
    InvalidAmountError,
    WithdrawalAddressMissingError,
)
from src.services.sms_parser import extract_verification_url
from src.api.dependencies import (
    _reject_if_banned,
    _require_player_identity,
    _save_screenshot,
    get_admin_service,
    get_wallet_service,
)

router = APIRouter(tags=["wallet"])


@router.get("/api/wallet/{player_id}")
async def get_wallet(
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    wallet = await get_wallet_service().get_or_create_wallet(player_id)
    return {
        "player_id": player_id,
        "balance": wallet.balance,
        "bonus_balance": wallet.bonus_balance,
        "won_balance": wallet.won_balance,
        # Only game winnings can be withdrawn (deposits and bonus are play-only). Never more than the real
        # balance, never negative - this is the same cap the withdrawal hold enforces.
        "withdrawable_balance": max(0, min(int(wallet.won_balance or 0), int(wallet.balance or 0))),
        "lifetime_deposited": wallet.lifetime_deposited,
    }


@router.get("/api/payment-methods")
async def list_payment_methods():
    methods = await get_admin_service().list_payment_methods(active_only=True)
    return {
        "methods": [
            {
                "id": m.id,
                "key": m.key,
                "name": m.name,
                "account_info": m.account_info,
                "account_holder": m.account_holder,
                "instructions": m.instructions,
                "supports_deposit": m.supports_deposit,
                "supports_withdrawal": m.supports_withdrawal,
            }
            for m in methods
        ]
    }


@router.get("/api/wallet/{player_id}/limits")
async def get_wallet_limits(player_id: str):
    s = await get_admin_service().get_settings()
    return {
        "min_deposit": s.min_deposit,
        "max_deposit": s.max_deposit,
        "min_withdrawal": s.min_withdrawal,
        "max_withdrawal": s.max_withdrawal,
        "stake_amount": s.stake_amount,
        "support_contact": s.support_contact,
    }


@router.post("/api/wallet/{player_id}/deposit")
async def request_deposit(
    request: Request,
    player_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    enforce_rate_limit("deposit", player_id)
    await record_ip(player_id, client_ip(request))
    return await run_idempotent(player_id, "deposit", idempotency_key, payload, lambda: _submit_deposit(player_id, payload))


async def _submit_deposit(player_id: str, payload: dict) -> dict:
    amount = payload.get("amount")
    payment_method = str(payload.get("payment_method") or "").strip()
    sms_text = payload.get("sms_text")
    screenshot_base64 = payload.get("screenshot_base64")

    if amount is None or not payment_method:
        raise HTTPException(status_code=400, detail="amount and payment_method are required")
    if not sms_text or not sms_text.strip():
        raise HTTPException(status_code=400, detail="Please paste the full payment SMS message received from your payment app.")
    if not screenshot_base64:
        raise HTTPException(
            status_code=400,
            detail="A transaction screenshot is required to submit a deposit. "
                   "Please attach a screenshot of your payment confirmation.",
        )

    url = extract_verification_url(sms_text)

    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a number")

    try:
        screenshot_url = _save_screenshot(screenshot_base64)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        txn = await get_wallet_service().request_deposit(
            player_id, amount, payment_method, screenshot_url=screenshot_url,
            sms_text=sms_text.strip(), payment_verification_url=url
        )
    except InvalidAmountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicateTransactionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"message": "Deposit request submitted - awaiting admin approval", "transaction_id": txn.id, "amount": amount}


def _find_withdrawal_address(accounts: dict | None, *keys: str) -> str | None:
    """Look up a saved withdrawal address by exact key first, then trimmed case-insensitive key."""
    if not accounts:
        return None
    for k in keys:
        if k and accounts.get(k):
            return accounts[k]
    targets = {k.strip().lower() for k in keys if k and k.strip()}
    if not targets:
        return None
    for k, v in accounts.items():
        if v and str(k).strip().lower() in targets:
            return v
    return None


@router.get("/api/wallet/{player_id}/withdrawal-accounts")
async def get_withdrawal_accounts(
    player_id: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    player = await PlayerRepository().get_by_id(player_id)
    return {"accounts": player.withdrawal_accounts if player else {}}


@router.post("/api/wallet/{player_id}/withdrawal-accounts")
async def set_withdrawal_account(
    player_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    method_key = str(payload.get("method_key") or "").strip()
    address = str(payload.get("address") or "").strip()
    if not method_key or not address:
        raise HTTPException(status_code=400, detail="method_key and address are required")
    player_repo = PlayerRepository()
    player = await player_repo.get_by_id(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found - please open the game via Telegram /start first")
    method = await PaymentMethodRepository().get_by_key(method_key)
    if not method or not method.active or not method.supports_withdrawal:
        raise HTTPException(status_code=400, detail="Choose an active withdrawal payment method")
    canonical_key = method.key.strip()
    await player_repo.set_withdrawal_account(player_id, canonical_key, address)
    if method.key != canonical_key:
        await player_repo.set_withdrawal_account(player_id, method.key, address)
    updated = await player_repo.get_by_id(player_id)
    return {"message": "Withdrawal address saved", "method_key": canonical_key, "accounts": updated.withdrawal_accounts if updated else {}}


@router.post("/api/wallet/{player_id}/withdraw")
async def request_withdrawal(
    request: Request,
    player_id: str,
    payload: dict = Body(...),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    enforce_rate_limit("withdraw", player_id)
    await record_ip(player_id, client_ip(request))
    return await run_idempotent(player_id, "withdraw", idempotency_key, payload, lambda: _submit_withdrawal(player_id, payload))


async def _submit_withdrawal(player_id: str, payload: dict) -> dict:
    amount = payload.get("amount")
    payment_method = str(payload.get("payment_method") or "").strip()
    legal_name = str(payload.get("legal_name") or "").strip()
    if amount is None or not payment_method or not legal_name:
        raise HTTPException(status_code=400, detail="amount, payment_method and legal_name are required")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a number")

    player = await PlayerRepository().get_by_id(player_id)
    method = await PaymentMethodRepository().get_by_key(payment_method)
    if not method or not method.active or not method.supports_withdrawal:
        raise HTTPException(status_code=400, detail="Choose an active withdrawal payment method")
    canonical_key = method.key.strip()
    address = _find_withdrawal_address(player.withdrawal_accounts if player else None, payment_method, method.key, canonical_key)
    if not address:
        raise HTTPException(status_code=400, detail=f"Set your {(method.name or canonical_key).strip()} withdrawal address first")

    try:
        txn = await get_wallet_service().request_withdrawal(player_id, amount, canonical_key, legal_name, address)
    except WithdrawalAddressMissingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InvalidAmountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InsufficientBalanceError as e:
        raise HTTPException(status_code=402, detail=str(e))

    return {"message": "Withdrawal request submitted - awaiting admin approval", "transaction_id": txn.id, "amount": amount}


@router.get("/api/wallet/{player_id}/transactions")
async def get_transactions(
    player_id: str,
    limit: int = 20,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    await _require_player_identity(player_id, x_telegram_init_data)
    txns = await get_wallet_service().transaction_repo.list_by_player(player_id, limit)
    return {
        "transactions": [
            {
                "id": t.id,
                "type": t.type,
                "amount": t.amount,
                "status": t.status,
                "payment_method": t.payment_method,
                "note": t.note,
                "created_at": t.created_at.isoformat(),
            }
            for t in txns
        ]
    }
