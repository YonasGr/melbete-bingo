# 🎉 Melbete Bingo - Project Summary

## Overview
Melbete Bingo is a multiplayer Bingo game delivered as a Telegram Mini App.
It supports both 75-ball and 90-ball variants with real-time gameplay,
server-verified claims, and an auditable RNG. All persistent state lives
in Cloud Firestore, so there is no database server to install, patch, or
scale yourself.

## Technology Stack

```
Backend:
  - Python 3.11+
  - FastAPI (async REST + WebSocket)
  - Cloud Firestore (google-cloud-firestore, async client) - datastore
  - python-telegram-bot

Frontend:
  - HTML5/CSS3 + vanilla JavaScript
  - Telegram Web App SDK
  - WebSocket API for live updates

Infrastructure:
  - Docker (single image, no bundled database containers)
  - Deployable to Render, Railway, Fly.io, Cloud Run, or any container host
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Telegram Platform                     │
│  ┌──────────────┐              ┌──────────────┐          │
│  │ Telegram Bot │              │   Mini App   │          │
│  └──────┬───────┘              └──────┬───────┘          │
└─────────┼─────────────────────────────┼──────────────────┘
          │                             │
          ├─────────────────────────────┘
          │
┌─────────▼─────────────────────────────────────────────────┐
│              FastAPI (src/api/main.py)                     │
│      REST endpoints  +  WebSocket broadcaster               │
└─────────┬────────────────────────────────────────────────┬─┘
          │                                                 │
┌─────────▼────────────────┐                 ┌──────────────▼──┐
│  RoomService              │                 │ Telegram bot     │
│  (src/services)           │◄────────────────┤ handlers         │
│  - card generation        │  shared logic   │ (src/bot)        │
│  - RNG / draw engine       │                └──────────────────┘
│  - pattern verification    │
└─────────┬──────────────────┘
          │
┌─────────▼──────────────────────────────────────────────────┐
│           Repositories (src/repositories)                    │
│   PlayerRepository · RoomRepository · CardRepository ·       │
│   ClaimRepository · DrawLogRepository                        │
└─────────┬──────────────────────────────────────────────────┘
          │
┌─────────▼──────────────────────────────────────────────────┐
│                    Cloud Firestore                            │
│   Collections: players, game_rooms, cards, claims, draw_logs  │
└────────────────────────────────────────────────────────────┘
```

Each layer only talks to the one below it: API routes call `RoomService`,
`RoomService` calls repositories, and only repositories import the
Firestore client. This keeps the game rules and Firestore access
independently testable (see `src/tests/test_room_service.py`, which
exercises the full game flow against in-memory fakes, no cloud
credentials required).

## Feature Matrix

| Feature | Status | Notes |
|---|---|---|
| 75-Ball Bingo | ✅ | 5x5 grid, standard column ranges |
| 90-Ball Bingo | ✅ | 3x9 grid, 5 numbers per row |
| Real-time updates | ✅ | WebSocket broadcast per room |
| Pattern matching | ✅ | Lines, corners, full house, and more |
| Auto-marking | ✅ | Client marks cards as numbers are called |
| Server-side claim verification | ✅ | Clients are never trusted for wins |
| Room management | ✅ | Create/join via room code |
| Telegram bot | ✅ | Commands + Mini App launch buttons |
| Secure RNG | ✅ | `secrets.SystemRandom` + seeded, auditable shuffle |
| Audit trail | ✅ | Per-room draw log with a final SHA-256 hash |
| Cloud-native persistence | ✅ | Cloud Firestore, no self-hosted DB |

## File Structure

```
bingo/
├── src/
│   ├── api/main.py                 FastAPI app (REST + WebSocket)
│   ├── bot/telegram_bot.py         Telegram bot commands
│   ├── core/
│   │   ├── config.py               Environment-driven settings
│   │   └── firebase.py             Firestore client bootstrap
│   ├── models/schemas.py           Pydantic data models
│   ├── repositories/               One class per Firestore collection
│   ├── services/
│   │   ├── game_service.py         Card generation, RNG, pattern rules
│   │   └── room_service.py         Room lifecycle (create/join/draw/claim)
│   ├── static/                     Mini App frontend (CSS/JS)
│   ├── templates/index.html        Mini App HTML shell
│   └── tests/                      Unit tests (game rules + room service)
├── .env.example                    Configuration template
├── Dockerfile                      Single-image container build
├── docker-compose.yml              Local container run (API + bot)
├── main.py                         Entry point (`bot` | `api` | both)
├── quickstart.sh                   Local setup helper script
├── README.md                       Setup, architecture, local dev
├── DEPLOYMENT.md                   Firebase + hosting platform guide
├── API_REFERENCE.md                REST/WebSocket reference
├── CONTRIBUTING.md                 Contribution guidelines
└── MELBETE_BINGO_GUIDE.md            Player-facing how-to-play guide
```

## Security Notes

1. **Cryptographic RNG** - card values and draw order use
   `secrets.SystemRandom`; the draw shuffle is seeded and the seed is
   stored so any game can be independently re-verified.
2. **Server-side validation** - all claims are checked against the
   server's own `called_numbers`, never the client's.
3. **Audit trail** - each room gets a `DrawLog` document recording the
   seed and full draw sequence, finalized with a SHA-256 hash when the
   game ends.
4. **Credentials hygiene** - the Firebase service-account key is never
   committed (see `.gitignore`); production deployments should prefer the
   base64 environment-variable form or Application Default Credentials
   over checking in a JSON file.

## Testing

- `src/tests/test_game_service.py` - pure game-logic unit tests (card
  generation, RNG, pattern verification). No I/O.
- `src/tests/test_room_service.py` - full game-flow tests (create room ->
  join -> start -> draw -> claim) against in-memory fake repositories, so
  they run offline with no Firestore project needed.
- See README.md "Testing" for how to add integration tests against the
  Firestore emulator.

## Roadmap Ideas

- Pub/sub-backed WebSocket broadcast (e.g. Cloud Pub/Sub or Firestore
  listeners) to allow the API to scale to multiple instances without
  sticky sessions.
- Player statistics and leaderboards.
- Prize/tournament modes.
- Webhook-based bot deployment for lower-latency updates at scale.

## License

MIT License - see `LICENSE`.
