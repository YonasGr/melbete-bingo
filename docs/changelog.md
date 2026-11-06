# Round A - first impressions and the money flow

- Cosmic splash (starfield, orbiting rings, tagline) behind the existing loading view.
- First-run onboarding (language choice, how a round works, how money works); shown once per device (`melbete_onboarded`).
- English / Amharic toggle in the top bar (`melbete-i18n.js`, `data-i18n` attributes, `mbT()` for JS strings). Covers the hub, deposit and withdraw modals, toasts, receipt, tracker and history status. Choice stored in `melbete_lang`; default is Amharic.
- Deposit and withdraw modals get a step progress bar driven by the form state.
- Receipt screen after every deposit or withdrawal request (short reference, method, time, copy button).
- The home status tracker now follows withdrawals as well as deposits: Requested > Reviewing > Paid (or Refunded).
- Ethiopic font-face uses `local()` first and `unicode-range`; Amharic gets looser line height. Run `scripts/fetch_fonts.sh` so the self-hosted Noto Sans Ethiopic files exist.
- No backend, API or wallet logic changed.

# Changes in this build

## Round 1: live dashboard + Telegram alerts

Same caveat as below: Python compiles, the JS passes `node --check`, and the new pure/service tests (`test_live_logic.py`, `test_admin_alerts.py` + additions; 39 tests in total with the Control Center ones) were run with in-memory fakes. Nothing has run against Postgres or the Telegram API yet. Apply `supabase/migrations/202610310002_admin_alerts.sql`, run the test files named in the section below plus `test_live_logic.py` and `test_admin_alerts.py`, then check Admin > Dashboard and Control Center > Alerts on a phone.

- **Live dashboard** (Admin > Dashboard, refreshes every few seconds): an alerts strip (kill switch / maintenance on, deposits or withdrawals waiting too long, rounds paying out more than they take, unpublished changes), then *Right now* (playing now, live connections, deposits and withdrawals waiting with amount and age) and *Today* (revenue, deposits approved, withdrawals paid, players who played, new players, rounds) each compared with yesterday, and a 7-day deposits/revenue chart. "Today" starts at local midnight (`DASHBOARD_UTC_OFFSET_HOURS`, default 3 = Ethiopia). Revenue = stakes kept minus payouts and refunds. "Live connections" counts WebSockets open on the server answering the request; with several replicas it is per replica.
- **Telegram alerts to admins** (private bot messages, plus the admin group for the critical ones): large withdrawal requests, a flagged account (shares a connection, phone or payout account) asking to withdraw, kill switch / maintenance changes, repeated failed admin sign-ins, large wallet adjustments, Control Center publishes/rollbacks, and staff changes. Thresholds and the master switch are part of the Control Center draft (Alerts section). Each admin chooses their own alert types (role defaults otherwise) and can send themselves a test. Alerts never block or fail the action that caused them; a blocked bot or missing group is logged. Failed sign-ins are also written to the audit log.
- Players must have pressed Start on the bot for DMs to arrive; the test button explains this when delivery fails.

## Admin Control Center (draft -> publish), staff roles, audit log, player controls

**Not executed against a database yet** (no Postgres/pytest in the build sandbox). Python compiles, the three JS files pass `node --check`, and the pure logic + service tests (`test_control_logic.py`, `test_control_service.py`, 22 tests) were run directly with in-memory fakes. Apply `supabase/migrations/202610310001_admin_control.sql` (idempotent; also appended to `schema.sql`), then run `pytest src/tests/test_control_logic.py src/tests/test_control_service.py src/tests/test_admin_service.py src/tests/test_wallet_service.py` and click through Admin > Control Center on a phone before relying on it.

- **Draft -> review -> publish**: Games, Money, Content and feature Switches are edited as a draft (`control_config`). Publish shows every change, validates the settings first, applies them, and records who/when. "Undo last publish" restores the previous config *and* the settings it changed (and can itself be undone). Existing Configuration form still works but now needs the publish permission and is audited.
- **Emergency controls apply instantly (not drafts)**: maintenance mode and the kill switch (stops deposits, withdrawals and joining rounds). They are always audited and a stale draft can never switch them back off. Rounds already running finish; a winner can still claim during maintenance.
- **Games**: per-game open/paused (with a message), stake, platform commission (payout ratio, max 50%), lobby countdown, ball interval, cartelas per player / to start / number range, bots. Only `real_10` is offered by `RoomService.TRACKS` today; the other tracks are shown dimmed.
- **Money**: per-request limits, rolling-24h daily limits (platform default + personal override), senior approval above an amount, and an optional **auto-approve** rule for small deposits from players with prior approved deposits, a payment link and no abuse flag. Off by default; withdrawals are never auto-approved.
- **Players**: ban/unban, freeze (blocks deposits/withdrawals only), personal daily limits, staff notes, wallet adjustment (atomic, reason required, ledger + audit), activity and staff-action history.
- **Staff roles**: super, manager, finance, support, and legacy admin (= exactly what admins could do before, plus drafting) with optional extra permissions per person. Deposit/withdrawal approval, ban, wallet adjust and publish now check permissions. Telegram-bot approval buttons are unchanged (they still use the old admin check).
- **Audit log** (`admin_audit_log`): deposits, withdrawals, bans, freezes, limits, notes, wallet changes, staff changes, settings and every control action, with before/after.
- **Content**: announcement bar (optional end time), promo banner, deposit/withdraw intro text and a home note, via the public `GET /api/control/public` and `melbete-control.js`. Admin-written text is rendered as text, never HTML.
- Not done: signups and referral payouts are not feature switches yet (only deposits, withdrawals, playing, Bonus Center, Agent portal are enforced). If the database is unreachable the player-side checks fail open by design.
- `APP_VERSION` bumped to 12 so browsers fetch the new CSS/JS.

## Android app (launcher) + /download page

**Not built yet** (no Android SDK in the build sandbox; XML and Python were checked, the Java and Gradle files were reviewed by hand). Build it once and install it on a real phone before sharing the link. Steps are in `android/README.md`.

- `android/`: a dependency-free native app (no permissions, no network) that opens the Melbete bot in Telegram, offers to install Telegram if missing, and auto-opens the game on a fresh launch. It is a launcher, not a standalone client: the game is a Telegram Mini App and browser login is deliberately disabled, so a web view in the app could not authenticate.
- `.github/workflows/android-apk.yml`: builds `melbete-bingo.apk` (+ SHA-256) with GitHub Actions. Needs the repository variable `TELEGRAM_BOT_USERNAME`; release signing uses your keystore secrets (without them it signs with a debug key).
- `GET /download`: public page with install steps. Shows the Download button once `src/static/downloads/melbete-bingo.apk` exists (and the SHA-256 if `melbete-bingo.apk.sha256` is next to it).

## Scale-out readiness, multi-account detection, payment statement matching

**Not executed yet** (no DB/pytest/Redis in the build sandbox; Python compiles, JS passes `node --check`, and the pure matching/bus logic was run directly). Apply `supabase/migrations/202610240003_abuse_signals.sql`, then run `pytest src/tests/test_statement_match.py src/tests/test_abuse_signals.py src/tests/test_ws_bus.py src/tests/test_agent_rewards.py`.

- **Several API containers (optional, off by default)**: `REDIS_URL` turns on a Redis channel so live-game messages reach players on every container (`src/core/bus.py`); without it, or if Redis is down, delivery is local exactly as before. Bot filling now takes a database lease. Bicep gets `apiMaxReplicas` (default 1) and `redisUrl`. Still per-container: rate limits. See DEPLOY_AZURE.md "More than one API container" for the checklist; load test before raising replicas.
- **One account per phone number**: `players.phone_normalized` (last 9 digits, so +251... and 09... match). Registration is refused with a friendly message if the number belongs to another real account; a unique index enforces it once existing duplicates are cleaned up (the migration only creates the index when there are none).
- **IP signal**: a keyed hash of the connection used for deposits and withdrawals (`player_ip_log`; raw IPs are never stored). Agent rewards are refused when the referred player shares the agent's phone or connection.
- **Admin > Players > Abuse review** (super admin): accounts sharing a connection, a phone number (legacy duplicates) or a payout account, each with a Ban button. Signals only: nothing is banned automatically, and the IP signal can be spoofed by a determined user.
- **Admin > Deposits > Match a payment statement**: paste a Telebirr/bank CSV; pending deposits are checked by reference AND amount into matched / amount differs / same payment claimed twice / no match, plus money received that nobody has claimed. "Approve all matched" re-runs the match on the server and approves only matched deposits (max 50 per click) through the normal atomic approval. Withdrawals are still paid and marked by hand.

## Agent rewards: tiers, held new-player / retention rewards, anti-abuse, agent + admin screens

**Not executed yet** (no DB/pytest in the build sandbox; Python files compile and both JS files pass `node --check`). Apply `supabase/migrations/202610240002_agent_rewards.sql`, then run `pytest src/tests/test_agent_rewards.py src/tests/test_agent_service.py src/tests/test_player_notifications.py`.

- **Tiers**: an agent's volume this month (own top-ups + approved deposits of their players) picks a tier (seeded Bronze 3% / Silver 4% at 50,000 / Gold 5% at 200,000 ETB). The rate paid is `max(commission_bps, tier_bps)`, so tiers never lower an agent's own rate. Applies to agent top-ups and to referral commission on player deposits.
- **New-player reward** (default 20 ETB): created on hold when a linked player makes their first deposit (min 50 ETB). Paid by the `agent_rewards_loop` (hourly, `AGENT_REWARDS_LOOP_MINUTES`, one instance at a time) only after the hold period (3 days) AND once the player has played 3 rounds. Unqualified rewards are cancelled after 30 more days.
- **Retention reward** (default 10 ETB): created when a rewarded player is still playing after 30 days.
- **Anti-abuse**: one reward per player (unique `dedupe_key`); no reward for the agent's own account, bots, banned players, or a player with the same phone number as the agent; banning a player cancels their held rewards; daily payout cap per agent (default 2,000 ETB) enforced inside the paying transaction; admin flags for one agent topping up the same player 5+ times in 24 h. A reward hook can never fail a deposit or top-up.
- **Agent page**: tier badge, progress to the next tier, rewards on hold / paid, reward list, anonymised weekly leaderboard.
- **Admin panel (Agents tab)**: edit reward rules and tiers, list rewards with a cancel button while on hold, abuse flags.
- **Notifications**: `agent_tier_up` (real promotions only) and `agent_reward_paid`.
- Not included yet: milestone rewards and leaderboard prizes (the `milestone` reward type exists in the table but nothing creates it), and a manual clawback of already-paid rewards.

## Player return features: rematch, streaks, round nudges, win-back

**Not executed yet** (no DB/pytest in the build sandbox). Apply `supabase/migrations/202610190003_engagement.sql`, then run `pytest src/tests/test_engagement_service.py src/tests/test_room_service.py src/tests/test_player_notifications.py`.

- **One-tap rematch**: `POST /api/session/{room_id}/rematch` + a "Play again (cartela #N)" button on the results screen. Moves on from the finished round and reserves the SAME cartela numbers through the normal select-cartela path (taken / limit / balance rules still apply); anything not reserved is reported with a reason.
- **Play streaks**: a round counts when the player is charged and the round starts (`RoomService.on_round_started`). Days follow Ethiopia time (EAT). Default rewards `3:10,7:30` (ETB bonus balance) via `ENGAGEMENT_STREAK_REWARDS`, once per milestone per streak run (guarded UPDATE); a missed day resets the run. One gentle evening reminder (16:00-21:00 EAT) for players who played yesterday but not yet today.
- **"Round starting" nudge**: when a lobby opens, recent players (last 14 days) who are not in it get a message with an Open button. Limits: 6 h per-player cooldown, one batch per track per 10 min, 100 recipients, quiet hours 22:00-07:00 EAT. (Rooms have no player cap, so this is "a round is starting", not "almost full".)
- **Win-back free round**: players inactive 7-90 days get a 10 ETB bonus (playable, not withdrawable), at most once per 30 days, with a hard `ENGAGEMENT_WINBACK_DAILY_BUDGET_ETB` (default 500) and a released claim if payment fails.
- **Opt-out**: `players.promo_opt_out`; `/mute` and `/unmute` in the bot and a toggle in the inbox sheet. Nudges, reminders and win-back messages are skipped for opted-out players (money messages always send). Promotional texts end with "Send /mute to stop...".
- Background job `engagement_loop` (every `ENGAGEMENT_LOOP_MINUTES`, 0 = off) runs win-back and reminders; every action is claim-guarded so overlapping runs or workers cannot double-send or double-pay. Nothing here touches the draw or any result.
- Streak/win-back bonuses are credited with a guarded claim first, then paid: if the payment step crashes after a streak claim the reward is logged and skipped rather than risking a double payment.
- Tuning knobs are environment settings (see `env.example`), not admin-panel fields yet.

## Player notifications + in-app inbox

**Not executed yet** (no DB/pytest in the build sandbox). Apply `supabase/migrations/202610190002_notifications.sql`, then run `pytest src/tests/test_player_notifications.py src/tests/test_agent_service.py`.

- New `notifications` table = the in-app inbox AND the record of Telegram delivery (`telegram_status`: sent / blocked / failed / skipped). `dedupe_key` guarantees one notification per event.
- `src/services/player_notifications.py`: `schedule_notify(player_id, type, data, dedupe_key)` runs in the background after the transaction commits and never raises or delays a payout. Templates for: deposit approved/rejected, withdrawal paid/rejected, win (single or shared), bonus granted, agent top-up received, referral reward, agent commission earned.
- Hooks: `WalletService` (deposit/withdrawal approve/reject, agent commission), `RoomService` (single and shared wins), `AgentService.topup_player`, `ReferralService.award_if_eligible`.
- API: `GET /api/notifications`, `GET /api/notifications/unread-count`, `POST /api/notifications/read` (a player only sees/changes their own).
- UI: bell with unread badge in the hub header and a bottom-sheet inbox (`static/js/inbox.js`, `static/css/inbox.css`, polls every 45 s). Opening the inbox marks everything read.
- Delivery is best-effort (no retry worker): if Telegram is down the message is still in the inbox. Notifications are never deleted yet - add a retention job before the table grows large.
- Note: `WalletService` still contains two definitions of `request_withdrawal/approve_withdrawal/reject_withdrawal`; the later ones are the active ones and carry the notification hooks.

## Agents (resellers) - complete system

**Not executed yet** (no DB/pytest in the build sandbox). Apply `supabase/migrations/202610190001_agents.sql`, then run `pytest src/tests/test_agent_service.py`.

- Tables `agents`, `agent_ledger`, column `players.agent_id`. Integer ETB; float and commission can never go negative (CHECK + guarded UPDATEs); every change writes an `agent_ledger` row in the same transaction.
- **Float**: house-funded by a super admin (`POST /api/admin/agents/{id}/float`, Idempotency-Key supported; negative takes float back). Suspend/activate/list/ledger endpoints; commission capped at 30%.
- **Top-up**: `POST /api/agent/topup` - one transaction debits the agent's float, credits the player's wallet as a deposit (playable, not withdrawable), credits the agent's commission (floor(amount x rate)), writes the wallet transaction (`agent_topup`) and links the player to the agent. 10-50,000 ETB each; not self/banned/bot; only active agents; float is the exposure limit.
- **Commission cash-out**: `POST /api/agent/commission/cashout` moves commission into the agent's wallet as withdrawable balance (transaction `agent_commission`), then it leaves through the normal withdrawal approval flow.
- **Linking**: Telegram deep link `/start ag_<CODE>` attaches a new player to the agent (once; never self; only active agents); the first top-up also links.
- **UI**: agent dashboard at `/agent` (float, commission, players, invite link, top-up, cash-out, activity); admin panel "Agents" tab (register, adjust float, ledger, suspend/activate).
- **Referral rewards from deposits**: when a player linked to an ACTIVE agent has a normal deposit approved (web panel or bot), the agent earns floor(amount x rate) commission, credited inside the same database transaction as the player's cash credit (`on_approved` hook in `approve_topup_atomic`; ledger type `referral_commission`). A failure is logged and never blocks the deposit. Agent-made top-ups earn the same rate. Lock order is wallet -> agent in every path to avoid deadlocks.
- Commission model implemented: % of each top-up (agent-made or a referred player's own deposit); rate set per agent. Agent top-ups do not trigger the first-deposit reward. An agent with no deposit of their own may be refused by the existing withdrawal eligibility rule.

## Multi-winner rounds: winners = everyone completed by the same last called number

**Written without pytest/pydantic available - not executed yet. Run `pytest src/tests/test_room_service.py` first.**

- Rounds with more than `multi_winner_min_players` (10) players no longer use a claim window.
  After every drawn number the server checks every card (`RoomService.settle_round_winners`); all players whose BINGO
  is completed by that same number win together, one share per player (even with two winning cartelas).
- The prize (after the payout ratio) is split equally and paid at once; ETB left over from rounding down stays with the house.
  Winners do not need to press claim (auto-BINGO or not); a manual claim just triggers the same settlement.
- Usually there is 1 winner; several only when several cards complete on the same number.
- Crash safety: the round ends first with the split frozen in the winner records (`paid: false`), then each winner is
  paid and flagged `paid: true`; `RoomService.pay_unpaid_winners(room_id)` finishes an interrupted payout exactly once.
- Removed: `winner_window_seconds`, `winner_window_remaining`, `finalize_winners`, `schedule_winner_finalization`,
  the pending-winner state and the "more can still claim" banner.
- 10 players or fewer: unchanged single-winner behaviour.

Wallet-safety fixes on top of the bonus-rewards release. **Not yet run against a
database or the test suite** - run `pip install -r requirements.txt && pytest src/tests`
and a two-request concurrency test on a real Postgres before enabling real-money play.

- Deposit approval is one locked transaction (no double credit from concurrent approvals).
- First-deposit reward commits with the cash credit; a reward failure never blocks the
  deposit and is recorded in `bonus_admin_audit` (`first_deposit_reward_failed`).
- Card purchases, payouts, refunds, referral credits and admin adjustments are atomic
  (`WalletRepository.debit_stake_atomic` / `apply_delta`; split logic in `src/services/stake_math.py`).
- Removed a dead duplicate `refund_stake` definition.
- Not included in this zip: `azurpob.txt` (raw Azure CLI dump), `.pytest_cache`, `__pycache__`.

## Money-safety hardening (this pass)

Written without a database or test runner available, so **nothing below has been executed yet**.
Run `pytest src/tests` (unit) and the real-Postgres suite before real-money play:

    docker run -d --rm -p 5432:5432 -e POSTGRES_PASSWORD=test postgres:16
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres pytest src/tests/integration

- **Withdrawal double-spend fixed**: `WalletRepository.hold_withdrawal_atomic` re-checks eligibility on the
  locked wallet row and debits + records the pending transaction in one DB transaction.
- **Approve/reject withdrawal** is one locked transaction (`resolve_withdrawal_atomic`): no double refund,
  no pay-and-refund.
- **Free credit** is one guarded UPDATE (`grant_free_credit_atomic`): granted exactly once.
- **Duplicate deposit URL** is now a DB guarantee (unique partial index); the app maps the violation to 409.
- **Idempotency keys** (`Idempotency-Key` header, `src/api/idempotency.py`, table `idempotency_keys`) on
  deposit, withdraw and claim; sent by app.js / gameselect.js. Key is optional server-side for old clients.
- **Rate limiting** (`src/core/rate_limit.py`, limits in `LIMITS`): claim, deposit, withdraw, bonus claim,
  cartela select (per player) and admin login (per IP). In-process; move to Redis if you run >1 API instance.
- CI now starts Postgres and runs `src/tests/integration`.
- **Apply `supabase/migrations/202610160001_wallet_hardening.sql`** to any existing database
  (fresh installs get it via schema.sql).

Still open: bonus-ledger coverage of spends/referrals, conflicting welcome/referral reward systems,
`merge_wallets` (still read-modify-write), idempotency on cartela select, purging old `idempotency_keys` rows.

## Ties split in every round (any number of players)

- Every round now settles like the former ">10 players" rule: all players whose BINGO is completed by the same last called
  number split the prize equally (one share per player). Removed the single-winner tie-break (random order with recent
  winners deprioritised) from the draw loop. `multi_winner_min_players` is deprecated and unused.
- Claims now require the card to belong to the claimer in that round.
- **Not run here (no pytest/pydantic). Run `pytest src/tests` before deploying.** Older tests that draw every number with
  several players and then claim for one of them will now see the prize split; adjust those to the new rule.
