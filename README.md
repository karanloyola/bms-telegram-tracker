# BookMyShow → Telegram ticket tracker (Claude Code skill)

A Claude Code skill that sets up **your own** Telegram bot to watch BookMyShow and ping you when seats open up, filtered by:

- movie, city and date (including dates where booking hasn't opened yet)
- theatre and showtime(s)
- seat class (e.g. ELITE / BUDGET)
- **preferred rows** (`H-M`, `J,K,L`, or any) and **number of tickets**, preferring side-by-side seats
- check frequency: every 5, 15, 30 or 60 minutes

Alerts look like:

```
🎉 Tickets available!
<theatre> — 07:00 PM
   ELITE ₹183.80: 🟢 Available
   💺 Row J: seats 13–15 together (18 free in row)
   👉 Open seat layout
```

Everything runs on your machine with your own bot token. No third-party server is involved.

## Install

### Option A: Ask Claude Code
Paste this into Claude Code:

> Install the bms-telegram-tracker skill from https://github.com/OWNER/bms-telegram-tracker and set up my BookMyShow ticket tracker.

### Option B: Plugin commands
```
/plugin marketplace add OWNER/bms-telegram-tracker
/plugin install bms-telegram-tracker@bms-telegram-tracker
```
Then ask Claude: "set up my BookMyShow ticket tracker".

### Option C: Manual
Copy `plugins/bms-telegram-tracker/skills/bms-telegram-tracker` to `~/.claude/skills/`, then ask Claude to set it up.

During setup Claude will ask you to create a bot with **@BotFather** in Telegram and give it the token. It then installs the bot in `~/bms-tracker`, starts it, and locks it to your chat ID. It can also set the bot to run in the background.

## Requirements
- Python 3.10+
- A Telegram account
- Movies listed on in.bookmyshow.com (India)

## How it works
- Showtimes and seat-class availability come from the same web endpoint BookMyShow's site uses. Requests go through [`curl_cffi`](https://github.com/lexiforest/curl_cffi) with a Chrome fingerprint, because plain HTTP clients are blocked.
- For shows that pass the class check, the bot reads the seat map the seat page loads (AES-encrypted with a key in BookMyShow's web bundle) to check rows and ticket count. Set `ENABLE_SEAT_MAP=false` to turn this off.
- Showtimes parsing is adapted from [aviiciii/bms-ticket-notifier](https://github.com/aviiciii/bms-ticket-notifier) (MIT).

## ⚠️ Read this
- **Unofficial.** It isn't affiliated with or endorsed by BookMyShow and doesn't use a public API. BookMyShow's terms prohibit automated access, and the row check reads their encrypted seat data. Use it for personal, low-frequency tracking at your own risk.
- Too many trackers or very short intervals can get your IP rate-limited or blocked by BookMyShow.
- Keep the bot private with `ALLOWED_CHAT_IDS`. Don't run it as a public bot for many users.
- It only alerts. It never books or pays.
- It may break whenever BookMyShow changes their site.

MIT licensed. See [LICENSE](LICENSE).
