# Security: admin access and incident response

## Admin access and two-factor login

Admin access needs two factors:

1. **Telegram identity.** The admin opens the panel from the official bot; the API verifies Telegram's signed, fresh Mini App `initData`.
2. **Authenticator code.** The admin enters their own six-digit TOTP code (Google Authenticator, Authy, 1Password, Bitwarden and similar all work).

There is no desktop-browser login, no reusable admin token and no admin secret in browser storage. On success the server issues a 15-minute `HttpOnly`, `Secure`, `SameSite=Strict` session cookie.

> Never commit real secrets, TOTP seeds, QR codes, database URLs, bot tokens or screenshots. A private repository is not a secret manager. Keep production secrets only in the host's encrypted secret store.

### Before enrolling anyone

1. List the administrators and their Telegram **numeric IDs** (use a trusted tool such as `@userinfobot`; usernames can change).
2. Keep at least two separately controlled super admins. Do not share phones, Telegram accounts or seeds.
3. Enable Telegram two-step verification (with a unique password and protected recovery email) on every admin account.
4. Decide how each seed is delivered privately (in person or a password manager's one-time share), never in a group chat or ordinary email.

### Enroll one administrator

Repeat for every person; never reuse a seed.

1. Generate a seed on a trusted machine, then clear it from the terminal afterwards:

   ```bash
   python -c "import base64,secrets; print(base64.b32encode(secrets.token_bytes(20)).decode())"
   ```

2. In the admin's authenticator app: add an account, choose **Enter a setup key**, name it (e.g. `Melbete Bingo - Finance`), paste the seed, select **Time based**, save, and confirm a six-digit code appears.
3. Add the entry to `ADMIN_TOTP_SECRETS` in the secret manager (format only; never put real values in docs or `.env.example`):

   ```text
   ADMIN_TOTP_SECRETS=123456789:BASE32_SEED_FOR_FINANCE,987654321:BASE32_SEED_FOR_SECOND_ADMIN
   ```

   Each entry is `TELEGRAM_NUMERIC_ID:BASE32_SEED`, comma-separated. No wildcard, shared or default seed is accepted. An admin missing from the mapping cannot log in even if the database says they are an admin; an empty value fails every login closed.
4. Set `SUPER_ADMIN_TELEGRAM_ID` to the first verified super admin (who must also have a TOTP entry) before first startup.
5. After deployment the admin opens the official bot, chooses **Open Admin Panel** and enters the current code. A code near expiry may fail; wait for the next one.

### Adding and removing admins

Add: verify identity and ID out of band, enroll the seed, deploy the configuration, **then** add the database role from an existing super admin, and watch the first login. Remove: delete the database role immediately, delete the TOTP entry, and rotate `SECRET_KEY` if a stolen session is possible. Staff changes appear in the audit log and alert the other admins.

### Lost phone or suspected compromise of one admin

- **Lost or replaced phone:** remove that admin's TOTP entry and role at once, verify them through another channel, enroll a new seed, deploy, then restore only the role they need.
- **Suspected stolen session or seed:** remove the admin, replace the seed, and rotate `SECRET_KEY` to invalidate every admin session.
- **No super admin left:** use the protected hosting/database recovery process with two authorized operators. Do not create a bypass token.

Keep an access-controlled recovery procedure with your operations team, and do not collect raw seeds as "backup codes".

## If secrets may have leaked

Rotate **before** redeploying: the Telegram bot token, the database password / connection URI, `SECRET_KEY` and `BOT_CONFIG_SECRET_KEY`, the storage connection string, Supabase keys, and hosting, registry and CI/CD credentials. Assume old browser, Git-history and CI-log copies are compromised. Then:

1. Apply the new secrets with the API and bot stopped, and redeploy.
2. Verify an admin login rejects a request without a fresh Telegram assertion, a wrong code and an expired code, and that a correct code works only through the Mini App.
3. Remove payment screenshots from any old public static upload directory.
4. Review database, hosting, bot and Git-provider logs for the exposure window, revoke unknown admin rows, and review the **audit log** and recent deposits/withdrawals for anything you did not do.

## Known limits

- The rate limiter and signed admin sessions are per process; move them to Redis before running several API instances ([deployment.md](deployment.md#6-scaling)).
- Admin sessions cannot be revoked individually (rotate `SECRET_KEY` to revoke all). For stronger protection put the admin hostname behind an identity provider with passkeys and network controls.
- Dependency scanning (`pip-audit`/Dependabot) is not set up in CI.
- The 2026-07 audit's findings and recommendations are kept in [archive/security-audit-2026-07.md](archive/security-audit-2026-07.md); it predates the move off Firestore and is not a sign-off.
