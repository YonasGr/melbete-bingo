# Security incident response and Google Authenticator setup

This guide documents the emergency security changes and the exact process for
enrolling administrators with Google Authenticator-compatible two-factor
authentication (TOTP).

> Never commit real secrets, TOTP seeds, QR codes, database URLs, bot tokens,
> or screenshots to this repository. A private repository is not a secret
> manager: collaborators, CI logs, compromised accounts, backups, and future
> access changes can expose its contents. Store production secrets only in the
> hosting provider's encrypted secret manager.

## Do not restart yet

Rotate the Telegram bot token, database password/connection URI, `SECRET_KEY`,
all Firebase/Google service-account keys, Supabase keys, hosting secrets, and
CI/CD/registry credentials. Assume any old browser or Git history copy is
compromised. Rotation must occur before deploying this patch.

## What changed

Admin access now requires both factors:

1. **Telegram identity:** the administrator opens the panel from the official
   bot. The API verifies Telegram's signed, fresh Mini App `initData`.
2. **Google Authenticator code:** the administrator enters their own six-digit,
   time-based one-time password (TOTP). Codes change every 30 seconds.

There is no desktop-browser login, no reusable `X-Admin-Token`, and no admin
secret in browser local storage. On success, the server issues a 15-minute
`HttpOnly`, `Secure`, `SameSite=Strict` session cookie. TOTP is an open
standard, so Google Authenticator, Authy, 1Password, Bitwarden, and similar
authenticator apps work.

## Before enrolling anyone

1. Make a short, reviewed list of administrators and their Telegram numeric
   IDs. Get an ID from a trusted tool such as `@userinfobot`; do not use a
   username because usernames can change.
2. Keep at least two separately controlled super-admin accounts to avoid a
   single-person lockout. Do not share phones, Telegram accounts, or TOTP
   seeds.
3. Enable Telegram two-step verification on every admin's Telegram account,
   use a unique password, and protect its recovery email.
4. Choose a secure, private way to transmit each seed to its intended admin:
   ideally in person or through an enterprise password manager's one-time
   secure-share feature. Do not send seeds in group chats or ordinary email.

## Enroll one administrator with Google Authenticator

Repeat this process separately for every administrator.

1. Record the Telegram numeric ID. Example only:

   ```text
   Admin name: Finance operator
   Telegram ID: 123456789
   ```

2. On a trusted administrator machine, generate a new unique Base32 seed:

   ```bash
   python -c "import base64,secrets; print(base64.b32encode(secrets.token_bytes(20)).decode())"
   ```

   The command prints one secret. Copy it directly into the secure deployment
   secret manager and into the intended administrator's authenticator app;
   then clear it from the terminal history/screen. Generate a different seed
   for every person. Do not reuse a seed.

3. On that administrator's phone, open Google Authenticator:

   - Tap `+`.
   - Choose **Enter a setup key**.
   - Set the account name to, for example, `Melbete Bingo - Finance operator`.
   - Paste that person's Base32 seed as the key.
   - Select **Time based** (not counter based).
   - Save it and confirm a six-digit code appears.

4. Store the seed in the production secret manager, using the exact mapping
   format below. This is a format example only; never put real values in this
   document or `.env.example`:

   ```text
   ADMIN_TOTP_SECRETS=123456789:BASE32_SEED_FOR_FINANCE,987654321:BASE32_SEED_FOR_SECOND_ADMIN
   ```

   Rules:

   - Each entry is `TELEGRAM_NUMERIC_ID:BASE32_SEED`.
   - Separate entries with commas.
   - No wildcard, shared seed, or default seed is accepted.
   - An admin missing from this mapping cannot log in, even if their database
     role says they are an admin.
   - An empty `ADMIN_TOTP_SECRETS` value fails every admin login closed.

5. Set `SUPER_ADMIN_TELEGRAM_ID` to the first verified super admin's numeric
   ID before first startup. Ensure that same ID also has a TOTP entry. The
   application bootstraps this account on startup.

6. After deployment, have the administrator open the official Telegram bot,
   choose **Open Admin Panel**, and enter the current code from Google
   Authenticator. A code near expiry may fail; wait for the next one and retry.

## Add a future admin safely

1. Verify their identity and Telegram numeric ID out of band.
2. Generate and enroll a unique TOTP seed first.
3. Add their `ID:SEED` entry to the hosting secret manager and deploy the
   configuration.
4. Only then add their admin database role using an existing super admin.
5. Have them complete a successful Google Authenticator login while supervised.

Do the reverse when removing access: remove the database role immediately,
remove their TOTP mapping, and rotate `SECRET_KEY` if there is any concern an
active browser session was stolen.

## Lost phone, suspicious code, or account recovery

- **Lost or replaced phone:** immediately remove that administrator's TOTP
  mapping and database role. Verify their identity through an independent
  channel, enroll a new seed, deploy it, then restore only the necessary role.
- **Suspected stolen session or seed:** remove the affected admin, replace the
  seed, and rotate `SECRET_KEY` to invalidate every active admin session.
- **No remaining super admin:** use the protected hosting/database recovery
  process with two authorized operators present. Do not create a bypass token.

Store a documented, access-controlled recovery procedure with your operations
team. Avoid printing or centrally collecting raw TOTP seeds as "backup codes".

## Deploy and verify

1. In GitHub, create the **production** Environment, add the required values
   as Environment secrets, and require a trusted reviewer before deployment.
   In particular, add `ADMIN_TOTP_SECRETS` and a separate random
   `BOT_CONFIG_SECRET_KEY`; do not recreate the retired
   `ADMIN_PANEL_ACCESS_TOKEN` secret.
2. Apply secret and hosting changes with the API and bot still stopped.
3. Deploy this code and verify an admin login rejects requests without a fresh
   Telegram assertion, with a wrong TOTP code, and with an expired code.
4. Verify a correct code works only through the Telegram Mini App. Verify old
   desktop browser access, `X-Admin-Token`, `X-Telegram-Id`, and old
   browser-session tokens no longer work.
5. Remove payment screenshots retained under any previous public static upload
   directory from the production host/storage.
6. Review Supabase, hosting, bot, and Git provider logs for the compromise
   window; revoke unknown admin rows and investigate withdrawals/deposits.

This patch is an emergency containment measure. Before multi-instance scale,
replace the in-memory rate limiter and signed admin sessions with Redis-backed
sessions/revocation, and put the admin hostname behind an identity provider
with WebAuthn/passkeys and network access controls.
