---
name: bms-telegram-tracker
description: Set up, run and manage a personal Telegram bot that watches BookMyShow (India) for movie tickets and alerts the user when seats open up, filtered by date, theatre, showtime, seat class, preferred rows and number of tickets, checking every 5/15/30/60 minutes. Use when the user wants BookMyShow ticket alerts, to track a movie/show on BookMyShow, to set up or restart their BMS Telegram tracker bot, or says "bms tracker", "bookmyshow bot", "notify me when tickets open".
---

# BookMyShow → Telegram ticket tracker

This skill installs a small Python bot (in `app/` next to this file) that the **user owns and runs on their own machine with their own Telegram bot**. Nothing is shared with whoever published this skill.

What the bot does once running (all inside Telegram):
- `/track` with a **movie link**: the user picks a date, a theatre (or types part of a name if bookings aren't open yet), showtimes, seat class, rows (`H-M`, `J,K,L` or `any`), number of tickets and a check interval (5/15/30/60 min).
- `/track` with a **cinema link** (e.g. `https://in.bookmyshow.com/cinemas/CHEN/<cinema>/buytickets/<CODE>/<YYYYMMDD>`): the theatre and date come from the link, and the user pastes the movie's link or types its name. The bot alerts when that movie is added at that theatre and bookable. Name search only covers the ~10-movie "now showing" and "upcoming" city lists, so the movie link is the reliable option.
- On each check it reads BookMyShow's showtimes data for seat-class availability. For shows that pass, it reads the seat map and only alerts when the preferred rows have enough free seats, listing side-by-side seats first (e.g. "Row J: seats 13–15 together") with a direct seat-layout link.
- `/list`, `/check`, `/stop`, `/cancel`, `/myid`. Trackers are saved in `trackers.json` and survive restarts.

## Setup workflow

Follow these steps in order. Keep the user informed in one short line per step.

### 1. Pick an install folder
Default `~/bms-tracker` (ask only if the user has a preference). Copy everything in this skill's `app/` folder there, including `.env.example` and `.gitignore`. Never run the bot from inside the skill folder: plugin updates would wipe the user's `.env` and `trackers.json`.

### 2. Python environment
Needs Python 3.10+ (`python3 --version`). Then, in the install folder:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
On Windows use `.venv\Scripts\python` / `.venv\Scripts\pip` throughout.

### 3. The user creates their own Telegram bot
Claude cannot do this part. Tell the user:
1. Open Telegram, message **@BotFather**, send `/newbot`, and choose a name and a username ending in `bot`.
2. BotFather replies with a token like `123456789:AA...`.
3. Either paste the token here, or put it in `.env` themselves.

Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`. `chmod 600 .env` on macOS/Linux. Never print the token back in chat, commit it, or send it anywhere other than Telegram's API.

### 4. First run and lock it to the user
1. Start the bot in the background from the install folder: `.venv/bin/python bot.py > bot.log 2>&1`. Wait for `Application started` in `bot.log`.
2. Ask the user to open their bot in Telegram and send `/myid`. It replies with their chat ID.
3. Put that ID in `ALLOWED_CHAT_IDS` in `.env` (comma-separate extra IDs for friends), then restart the bot. Until this is set, anyone who finds the bot can use it.
4. Ask the user to send `/track` and walk through one tracker.

If `bot.log` shows `Conflict: terminated by other getUpdates request`, another copy of the bot is running with the same token. Find it (`pgrep -fl "bot.py"`), confirm it's the one from this install folder, and stop it.

### 5. Keep it running (optional, offer it)
The bot only checks while its process runs and the machine is awake. Offer:
- **macOS**: a LaunchAgent at `~/Library/LaunchAgents/com.user.bmstracker.plist`, with `ProgramArguments` = [`<install>/.venv/bin/python`, `<install>/bot.py`], `WorkingDirectory` = install folder, `RunAtLoad` and `KeepAlive` true, and stdout/stderr going to `<install>/bot.log`. Load it with `launchctl bootstrap gui/$(id -u) <plist>`.
- **Linux**: a `systemd --user` service with the same command and `Restart=always`.
- **Always on**: any small VPS, set up the same way.
Stop any manually started copy first, to avoid the Conflict error above.

## Configuration (`.env`)
| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | The user's own BotFather token (required) |
| `ALLOWED_CHAT_IDS` | Comma-separated chat IDs allowed to use the bot |
| `ENABLE_SEAT_MAP` | `true` (default) for row/ticket checks; `false` for seat-class alerts only |
| `TRACKERS_FILE` | Optional path for the trackers store |

## Troubleshooting
- **Checking a live show's rows**: `.venv/bin/python seat_test.py <movie-url> <YYYYMMDD> [venueCode] [rows] [tickets] [--alert]` prints every row's free/booked count and the alert the bot would send.
- **HTTP 403 from BookMyShow**: plain HTTP clients are blocked by Cloudflare. The bot uses `curl_cffi` with a Chrome fingerprint. If 403s persist, the IP may be rate-limited. Lengthen intervals and reduce trackers; don't rotate IPs or proxies to get around it.
- **City not recognised**: `bms.py` has a `REGIONS` map (slug → BMS region code, lat/lon). Add the city there if the movie link's city isn't listed. The region code is visible in BookMyShow seat-layout URLs, e.g. `/movies/chen/seat-layout/...`.
- **Seat map unreadable** ("rows not verified" in alerts): BookMyShow may have changed how the seat page is encoded. Seat-class alerts still work. Set `ENABLE_SEAT_MAP=false` to silence the warning.

## Be upfront with the user about
- This is unofficial and uses BookMyShow's website endpoints, not a public API. BookMyShow's terms prohibit automated access, and the row check reads their encrypted seat map. It's meant for personal, low-frequency use. Heavy use can get the user's IP blocked.
- Keep it to the user plus a few friends (via `ALLOWED_CHAT_IDS`). Running it as a public bot for many users multiplies requests from one IP.
- It only alerts. It never books or pays. The user books through the link.
