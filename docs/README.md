# Melbete Bingo documentation

| Read this | When |
|---|---|
| [../README.md](../README.md) | Overview, local setup, running the tests |
| [deployment.md](deployment.md) | Installing and running it: database, secrets, Azure, other hosts, scaling, CI/CD |
| [operations.md](operations.md) | Running the admin panel: roles, audit log, daily reconciliation, CSV exports |
| [security.md](security.md) | Admin two-factor login, adding/removing admins, rotating secrets after a leak |
| [architecture.md](architecture.md) | How the code and the money model are organised |
| [changelog.md](changelog.md) | What changed, release by release |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Contributing, code style |
| [../PRODUCT.md](../PRODUCT.md), [../DESIGN.md](../DESIGN.md) | Product principles and design system (kept at the repo root because design tooling reads them there) |
| [../android/README.md](../android/README.md), [../supabase/README.md](../supabase/README.md) | The Android launcher app; the database schema |

The live API is described by FastAPI itself at `/docs` and `/openapi.json` on a running server.

## Archive (history only, not current)

These describe earlier versions of the product (room codes, `/create` and `/join`, 90-ball cards, Firestore) and are kept for reference. Each starts with a warning.

- [archive/api-reference-legacy.md](archive/api-reference-legacy.md): the old `/api/rooms/...` API
- [archive/player-guide-legacy.md](archive/player-guide-legacy.md): the old host/player guide
- [archive/migrating-from-firestore.md](archive/migrating-from-firestore.md): moving a Firestore deployment to Postgres
- [archive/security-audit-2026-07.md](archive/security-audit-2026-07.md): July 2026 audit
- [archive/impeccable-audit.md](archive/impeccable-audit.md): UI audit
