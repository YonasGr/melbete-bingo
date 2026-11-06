# Admin operations: money, audit and reconciliation

For the people who run the admin panel (`<url>/admin`). Login and 2FA are in [security.md](security.md).

## Roles

| Role | Can |
|---|---|
| Super admin | Everything, including staff, agents and configuration. |
| Finance | Approve/reject deposits and withdrawals (including large ones), view players, view the audit log, reconciliation and exports. |
| Manager | Edit and publish Control Center settings, emergency switches, view players and the audit log. |
| Support | View players, ban/freeze, add notes. No money actions. |
| Admin (legacy) | Money approvals, view players, draft control changes, view the audit log. |

Withdrawals above the review threshold (Control Center, Money) need a role with "approve large withdrawals".

## Audit log

Every state-changing admin action writes a row to `admin_audit_log`: who (Telegram ID and role), what, which target, a short summary, before/after values where relevant, the caller's IP and the time. View it in **Control Center → Audit** (needs the "view the audit log" permission); filter by actor, action prefix or target.

Recorded, among others:

- Deposits and withdrawals: `deposit.approve`, `deposit.reject`, `deposit.bulk_approve`, `deposit.auto_approve`, `withdrawal.approve`, `withdrawal.reject`. This includes decisions made with the buttons in the Telegram admin group (summary ends "via Telegram bot", `after.source` is `bot`).
- Player money and access: `wallet.reset`, `player.ban`, `player.unban`, `player.freeze`, notes and limits.
- Agents: create, suspend, activate, commission, rewards, **float adjustments**, reward settings, tiers, cancellations, applications.
- Configuration: settings, payment methods (save/delete), Control Center publish/rollback, emergency switches, bot toggles, deposit toggle.
- Staff: add, remove, role change, promote. Failed admin logins.
- Games and broadcasts: terminate/delete a game, start/cancel a broadcast.
- **Exports:** `export.deposits` and `export.withdrawals` record who pulled player and payout data, the date range, status filter and row count.

Properties and limits you should know:

- The log is **append-only in the database** (a trigger rejects UPDATE, DELETE and TRUNCATE; migration `202611010001`). That stops application bugs and a stolen app credential from rewriting history. A database owner can still drop the trigger, which is a visible schema change.
- An audit write never blocks the action: if the insert fails, the approval still goes through and the failure is written to the application log (`AUDIT WRITE FAILED`). The transaction itself independently keeps `admin_id` and `resolved_at`, so the decision is never untraceable. Watch the logs for that line.
- A test (`test_admin_audit_coverage.py`) fails the build if a new state-changing admin endpoint is added without an audit call.

## Daily reconciliation screen

**Reconciliation** in the left menu (or *More* on a phone). Read-only; it never changes a balance. Needs the "view the audit log" permission (Finance, Manager, Admin, Super).

- **Banner:** green when no wallet has an impossible balance; red with a count when a balance is negative or winnings exceed the balance.
- **Totals:** real player money, withdrawable (won), bonus (not withdrawable), wallet count, deposits and withdrawals waiting, and how many have waited over 24 hours.
- **Findings** (only shown when present): negative balances; won balance above balance; wallets whose lifetime deposits differ from the sum of approved top-ups (a "review" item, not an alarm).
- **By day** (7/14/30/90 days, Ethiopia time): deposits approved and rejected, withdrawals paid and rejected, still pending, and **net cash** = deposits approved minus withdrawals paid. A request counts on the day it was resolved.

This is a live read. The 03:00 job posts the same checks to the admin chat but re-reads after 60 seconds and drops anything that was only a transaction in flight; the screen does not, so confirm a finding by refreshing before acting on it. `RECONCILIATION_HOUR_EAT=-1` disables the 03:00 job only.

Suggested daily routine: open the screen, check the banner, clear anything pending over 24 hours, then compare the day's "deposits approved" and "withdrawals paid" ETB with your Telebirr/bank statements. The *Match a payment statement* tool on the Deposits page does that matching automatically for deposits.

## CSV export

On **Deposits** and **Withdrawals**, use *Export CSV*: choose from/to dates (Ethiopia time, inclusive, default last 30 days, at most 366 days), a status (all, pending, approved, rejected) and download. Needs the "view the audit log" permission.

- One row per request, oldest first, UTF-8 with a BOM so Excel shows Amharic names correctly. Times are Ethiopia time.
- Deposits: ID, requested/resolved time, status, player ID and name, amount, method, receipt link, reviewed by, note. Withdrawals: the same with account holder and payout account instead of receipt link.
- A range with more than 50,000 rows is refused: choose a shorter range.
- Cells that start with `=`, `+`, `-` or `@` get a leading apostrophe so a spreadsheet cannot run player-supplied text as a formula. Phone numbers beginning with `+` therefore show an apostrophe; strip it when importing.
- The files contain players' names and payout accounts. Store and share them like financial records, and delete copies you no longer need. Every export is audited.

## Notifications

Players get each message in two places: a Telegram message from the bot and an entry in the in-app inbox (bell on the hub). While the Mini App is open, a fresh message also appears as a banner at the top; tapping it opens the inbox. The banner only shows when a new message arrived since the last check, so opening the app never replays old ones, and a "round starting" message expires after one minute.

| Message | When | Muted by `/mute`? |
|---|---|---|
| Deposit received / Withdrawal requested | Right after the player submits (not sent for an auto-approved deposit, which goes straight to "approved") | No |
| Deposit approved or rejected, Withdrawal paid or rejected | When an admin decides, from the web panel or the Telegram buttons | No |
| You won (with the new balance) | When a round pays out | No |
| Balance running low | After a round, if real + bonus balance is below `ENGAGEMENT_LOW_BALANCE_THRESHOLD_ETB` (default 20, `0` disables); at most once per Ethiopia day, never 22:00-07:00 | Yes |
| A round is starting | A new lobby opens and the player recently played but is not in it (existing nudge, rate-limited) | Yes |

Admins: a withdrawal at or above **Control Center → Alerts → large withdrawal amount** (default 5,000 ETB, `0` = off) sends a Telegram alert to the admin group and roles that subscribe to it, and is marked **Large** in the Withdrawals tab with a banner counting the large ones waiting.
