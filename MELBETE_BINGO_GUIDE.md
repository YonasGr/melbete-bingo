# 🎉 Melbete Bingo - How to Play & Host

A real-time multiplayer Bingo game delivered as a Telegram Mini App, with
75-ball and 90-ball variants.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)
![Postgres](https://img.shields.io/badge/database-Postgres%20(Supabase)-336791.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

> Looking for developer setup, architecture, or the API? See
> [README.md](README.md), [API_REFERENCE.md](API_REFERENCE.md), and
> [DEPLOYMENT.md](DEPLOYMENT.md). This guide is about *playing and hosting*
> games once the bot is running.

## ✨ Features

- **Two variants**: 75-ball (5x5, US-style) and 90-ball (3x9, UK-style)
- **Real-time play**: numbers are called live over WebSocket, no refreshing
- **Multiple winning patterns**: lines, four corners, full house, and more
- **Auto-marking**: your card marks itself as numbers are called
- **Server-verified claims**: the server double-checks every "Bingo!" claim
  against its own record of called numbers, so nobody can fake a win
- **Auditable draws**: every room's random draw order is seeded and hashed,
  so a finished game's fairness can be independently verified

## 📱 Setting up the bot (for the person running it)

1. **Create the bot**: message [@BotFather](https://t.me/botfather) on
   Telegram, send `/newbot`, and follow the prompts to get a bot token.
2. **Deploy the app** (see [README.md](README.md) for local dev or
   [DEPLOYMENT.md](DEPLOYMENT.md) for hosting it publicly) so you have a
   public HTTPS URL.
3. **Point the bot's Menu Button at the Mini App**: send `/mybots` to
   BotFather -> select your bot -> *Bot Settings* -> *Menu Button* -> set
   the Web App URL to `https://<your-domain>/app`.
4. **(Optional) Set the command list** so Telegram shows autocomplete.
   Send this to BotFather via `/setcommands`:
   ```
   start - Start the bot
   play - Quick play
   create - Create new game room
   join - Join game with code
   help - Get help and instructions
   ```

## 🎮 How to play

### Creating a game (host)

1. Open a chat with the bot and send `/create`.
2. Choose 75-ball or 90-ball.
3. Tap the button to open the Mini App - your room is created and you get
   a short room code to share.
4. Wait for friends to join, then tap **Start** when ready.

### Joining a game

1. Get a room code from the host.
2. Send `/join <room_code>` to the bot, or just tap the Mini App button and
   enter the code there.
3. You'll receive your card(s) immediately; wait for the host to start.

### Playing a round

1. Numbers are called automatically (or manually, if the host disabled
   auto-draw) and shown at the top of the screen.
2. Matching numbers on your card are marked for you automatically.
3. Once you believe you've completed the room's winning pattern, tap
   **BINGO!**
4. The server checks your card against every number it actually called -
   if it's a genuine win, everyone sees the result instantly.

### Winning patterns

**75-ball:**
- Horizontal line, vertical line, diagonal line
- Four corners
- Full house (entire card)

**90-ball:**
- One line
- Two lines
- Full house (all 15 numbers)

The host picks the active pattern when creating the room.

## 🔧 Game configuration

Hosts creating a room via the API/Mini App can configure:

| Setting | Description | Default |
|---|---|---|
| `variant` | `"75"` or `"90"` | `"75"` |
| `cards_per_player` | How many cards each player gets | `1` |
| `pattern` | Winning pattern id (see above) | `horizontal_line` |
| `auto_draw` | Whether numbers call themselves on a timer | `true` |
| `draw_interval` | Seconds between automatic draws | `5` |

Server-wide defaults (`MAX_PLAYERS_PER_ROOM`, `DEFAULT_DRAW_INTERVAL`,
`AUTO_MARK_ENABLED`) live in environment variables - see `.env.example`.

## 🔒 Fairness & security

- Card values and the draw order are generated with `secrets.SystemRandom`
  (a cryptographically secure RNG), not Python's default `random`.
- The draw order is a seeded Fisher-Yates shuffle; the seed and full draw
  sequence are stored per room and hashed once the game finishes, so
  anyone can verify no numbers were reordered after the fact.
- Claims are validated entirely server-side against the database's record
  of `called_numbers` - a client can never "invent" a win.

## 🆘 Troubleshooting

- **Bot doesn't respond** - confirm `TELEGRAM_BOT_TOKEN` is correct and the
  bot process (`python main.py bot`) is actually running; check its logs.
- **"Open Melbete Bingo" button does nothing / wrong page** - check
  `PUBLIC_BASE_URL` matches your real deployed URL exactly (including
  `https://`, no trailing slash).
- **Room not found after creating it** - make sure the API and bot point at
  the *same* database (`DATABASE_URL`); a mismatch is the most common
  cause of "it worked a second ago" bugs.
- **Numbers not updating live** - the browser's WebSocket connection may
  have dropped; refreshing the Mini App reconnects it.

For developer-facing issues (setup, deployment, contributing), see
[README.md](README.md), [DEPLOYMENT.md](DEPLOYMENT.md), and
[CONTRIBUTING.md](CONTRIBUTING.md).

## 🗺️ Roadmap ideas

- Player profiles, statistics, and leaderboards
- Prize/tournament modes
- Custom card themes and sound effects
- Multi-language support

---

Made with ❤️ for the Bingo community.
