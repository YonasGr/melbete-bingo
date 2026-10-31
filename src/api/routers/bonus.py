"""Authenticated player bonus center and admin campaign controls."""
from fastapi import APIRouter, Body, Header, HTTPException, Request
from datetime import datetime, date
from src.api.dependencies import _require_player_identity, _reject_if_banned, get_control_service, require_super_admin_dep
from src.repositories import PlayerRepository
from src.services.bonus_service import BonusService, BonusError
from src.core.db import get_pool
from src.core.rate_limit import enforce as enforce_rate_limit

router = APIRouter(tags=["bonus"])
def _json_safe(row):
    return {k: (v.isoformat() if isinstance(v, (datetime, date)) else v) for k, v in dict(row).items()}

service = BonusService()

@router.get('/api/bonus/summary/{player_id}')
async def bonus_summary(player_id: str, x_telegram_init_data: str | None = Header(default=None, alias='X-Telegram-Init-Data')):
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    blocked = await get_control_service().flag_block("bonus_center")
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    try:
        result = await service.summary(player_id)
        # Public referral link follows the existing Telegram deep-link convention.
        player = await PlayerRepository().get_by_id(player_id)
        if player:
            from src.services.notifications import get_bot_username
            username = await get_bot_username()
            result['referral_link'] = f"https://t.me/{username}?start=ref_{player.telegram_id}" if username else None
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail='Bonus center is temporarily unavailable') from e

@router.post('/api/bonus/{reward_type}/claim/{player_id}')
async def claim_bonus(reward_type: str, player_id: str, payload: dict = Body(default={}), x_telegram_init_data: str | None = Header(default=None, alias='X-Telegram-Init-Data')):
    await _require_player_identity(player_id, x_telegram_init_data)
    await _reject_if_banned(player_id)
    enforce_rate_limit('bonus_claim', player_id)
    blocked = await get_control_service().flag_block("bonus_center")
    if blocked:
        raise HTTPException(status_code=503, detail={'code': 'disabled', 'message': blocked})
    try:
        return await service.claim(player_id, reward_type, str(payload.get('idempotency_key') or ''))
    except BonusError as e:
        raise HTTPException(status_code=e.status_code, detail={'code':e.code,'message':e.message}) from e

@router.get('/api/bonus/history/{player_id}')
async def bonus_history(player_id: str, limit: int = 50, offset: int = 0, x_telegram_init_data: str | None = Header(default=None, alias='X-Telegram-Init-Data')):
    await _require_player_identity(player_id, x_telegram_init_data)
    pool = await get_pool()
    limit = max(1, min(limit, 100)); offset = max(0, offset)
    rows = await pool.fetch('SELECT amount_etb,entry_type,reward_type,note,created_at FROM bonus_ledger WHERE player_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3', player_id, limit, offset)
    return {'items':[dict(r) for r in rows], 'limit':limit, 'offset':offset}

@router.get('/api/admin/bonus/campaigns')
async def admin_bonus_campaigns(request: Request):
    admin_id = await require_super_admin_dep(request)
    pool = await get_pool()
    state = await pool.fetchrow('SELECT globally_enabled FROM bonus_campaign_state WHERE id=1')
    rows = await pool.fetch('SELECT * FROM bonus_campaigns ORDER BY reward_type')
    return {'globally_enabled': bool(state['globally_enabled']) if state else False, 'campaigns':[dict(r) for r in rows]}

@router.put('/api/admin/bonus/campaigns/{reward_type}')
async def update_bonus_campaign(reward_type: str, request: Request, payload: dict = Body(...)):
    admin_id = await require_super_admin_dep(request)
    if reward_type not in {'welcome','referral','daily','first_deposit'}:
        raise HTTPException(404, 'Unknown reward type')
    reason = str(payload.get('reason') or '').strip()
    if len(reason) < 8:
        raise HTTPException(400, 'A change reason of at least 8 characters is required')
    allowed = {'amount_etb','enabled','starts_at','ends_at','daily_budget_etb','total_budget_etb','per_player_cap'}
    changes = {k:payload[k] for k in allowed if k in payload}
    for k in ('amount_etb','daily_budget_etb','total_budget_etb','per_player_cap'):
        if k in changes and (not isinstance(changes[k],int) or changes[k] < (1 if k in ('amount_etb','per_player_cap') else 0)):
            raise HTTPException(400, f'Invalid {k}')
    if reward_type == 'first_deposit' and 'amount_etb' in changes and changes['amount_etb'] > 100:
        raise HTTPException(400, 'First-deposit reward percentage must be between 1 and 100')
    if not changes: raise HTTPException(400, 'No campaign changes supplied')
    pool = await get_pool()
    async with pool.acquire() as c:
        async with c.transaction():
            before = await c.fetchrow('SELECT * FROM bonus_campaigns WHERE reward_type=$1 FOR UPDATE', reward_type)
            if not before: raise HTTPException(404, 'Campaign not found')
            cols = list(changes)
            vals = [changes[k] for k in cols]
            assignments = ','.join(f'{k}=${i+2}' for i,k in enumerate(cols))
            await c.execute(f'UPDATE bonus_campaigns SET {assignments},updated_at=now() WHERE reward_type=$1', reward_type, *vals)
            after = await c.fetchrow('SELECT * FROM bonus_campaigns WHERE reward_type=$1', reward_type)
            await c.execute('INSERT INTO bonus_admin_audit(admin_telegram_id,action,target_type,target_id,before_json,after_json,reason) VALUES($1,$2,$3,$4,$5,$6,$7)', admin_id,'update_campaign','bonus_campaign',reward_type,_json_safe(before),_json_safe(after),reason)
    return {'campaign':dict(after)}

@router.put('/api/admin/bonus/global')
async def set_bonus_global(request: Request, payload: dict = Body(...)):
    admin_id = await require_super_admin_dep(request)
    reason = str(payload.get('reason') or '').strip()
    if len(reason) < 8 or not isinstance(payload.get('enabled'), bool):
        raise HTTPException(400, 'enabled and a reason of at least 8 characters are required')
    pool = await get_pool()
    async with pool.acquire() as c:
        async with c.transaction():
            before = await c.fetchrow('SELECT globally_enabled FROM bonus_campaign_state WHERE id=1 FOR UPDATE')
            await c.execute('UPDATE bonus_campaign_state SET globally_enabled=$1,updated_at=now() WHERE id=1',payload['enabled'])
            await c.execute('INSERT INTO bonus_admin_audit(admin_telegram_id,action,target_type,target_id,before_json,after_json,reason) VALUES($1,$2,$3,$4,$5,$6,$7)',admin_id,'global_pause_toggle','bonus_campaign_state','1',_json_safe(before or {}),{'globally_enabled':payload['enabled']},reason)
    return {'globally_enabled':payload['enabled']}


@router.get('/api/admin/bonus/report')
async def admin_bonus_report(request: Request):
    admin_id = await require_super_admin_dep(request)
    pool = await get_pool()
    totals = await pool.fetch("SELECT reward_type, COUNT(*) FILTER (WHERE entry_type='grant') AS grants, COALESCE(SUM(amount_etb) FILTER (WHERE entry_type='grant'),0) AS granted_etb, COALESCE(SUM(-amount_etb) FILTER (WHERE entry_type='spend'),0) AS spent_etb FROM bonus_ledger GROUP BY reward_type ORDER BY reward_type")
    daily = await pool.fetch("SELECT (created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS local_date, reward_type, COUNT(*) AS claims, SUM(amount_etb) AS granted_etb FROM bonus_ledger WHERE entry_type='grant' AND reward_type IS NOT NULL GROUP BY local_date,reward_type ORDER BY local_date DESC LIMIT 90")
    claims = await pool.fetch("SELECT status, reward_type, COUNT(*) AS count FROM bonus_claims GROUP BY status,reward_type ORDER BY reward_type,status")
    return {'totals':[dict(r) for r in totals], 'daily':[dict(r) for r in daily], 'claims':[dict(r) for r in claims]}
