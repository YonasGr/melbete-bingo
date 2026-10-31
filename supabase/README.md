# Fresh Supabase database setup

This Bingo-only release is designed for a **new, empty Supabase project/database**. The old incremental migration files (including migrations for removed games) are intentionally not included.

1. Create a new Supabase project.
2. Open **SQL Editor → New query**.
3. Paste the complete contents of [`schema.sql`](schema.sql) and run it once.
4. Copy the project's PostgreSQL connection string into the backend `DATABASE_URL` secret. Use the transaction pooler connection when appropriate, and keep `DB_USE_PREPARED_STATEMENTS=false` for transaction-pooler connections.
5. Configure the required Telegram, application-secret, and super-admin environment variables before starting the app.

The schema creates empty Bingo, wallet, transaction, admin, payment-method, referral, and audit tables. It does not insert demo players, balances, transactions, or payment accounts. The app creates its default system-settings row and broadcast status row on first startup.

**Important:** This is a fresh-install procedure, not a database reset script. Do not run it against a database containing data you need to keep, and do not use `DROP SCHEMA` or destructive reset commands on an existing project.
