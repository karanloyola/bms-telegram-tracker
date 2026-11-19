"""Telegram bot that watches BookMyShow and pings you when seats open up.

Commands
  /track  – set up a new tracker (guided)
  /list   – show your trackers
  /check  – run all your trackers right now
  /stop   – remove a tracker
  /cancel – abort the /track setup
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

import bms

load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
DB = Path(os.getenv("TRACKERS_FILE", Path(__file__).with_name("trackers.json")))
INTERVALS = [5, 15, 30, 60]
TIME_WINDOWS = {"morning": (0, 1200), "afternoon": (1200, 1600),
                "evening": (1600, 1900), "night": (1900, 2400)}
PAGE = 8
SEAT_MAP = os.getenv("ENABLE_SEAT_MAP", "true").lower() not in ("0", "false", "no")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bmsbot")

(URL, DATE, THEATRE, THEATRE_TEXT, SHOW, CATEGORY, ROWS, TICKETS, FREQ, CONFIRM, MOVIE) = range(11)


# ── storage ──────────────────────────────────────────────────────────
def load() -> dict:
    try:
        return json.loads(DB.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(DB)


def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


def esc(s) -> str:
    return html.escape(str(s))


def target_of(t: dict) -> bms.Target:
    return bms.Target(event_code=t["event"], region_slug=t["region"], url_slug=t.get("slug", ""))


# ── matching ─────────────────────────────────────────────────────────
def matching(t: dict, snap: bms.Snapshot):
    """Yield (show, category) pairs that satisfy the tracker's filters."""
    for s in snap.shows:
        if s.date_code and s.date_code != t["date"]:
            continue
        if t.get("venue_code"):
            if s.venue_code != t["venue_code"]:
                continue
        elif t.get("venue_query"):
            if t["venue_query"].lower() not in s.venue_name.lower():
                continue
        if t.get("sessions"):
            if s.session_id not in t["sessions"]:
                continue
        elif t.get("window") and t["window"] != "any":
            lo, hi = TIME_WINDOWS[t["window"]]
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = -1
            if not lo <= tc < hi:
                continue
        for c in s.categories:
            if t.get("categories") and c.name not in t["categories"]:
                continue
            yield s, c


def describe(t: dict) -> str:
    venue = t.get("venue_name") or (f"name contains “{t['venue_query']}”" if t.get("venue_query") else "Any theatre")
    if t.get("sessions"):
        show = ", ".join(t.get("session_labels", t["sessions"]))
    else:
        show = (t.get("window") or "any").title() if t.get("window") != "any" else "Any show"
    cats = ", ".join(t["categories"]) if t.get("categories") else "Any"
    return (f"🎬 <b>{esc(t['movie'])}</b>\n"
            f"📅 {esc(bms.pretty_date(t['date']))}\n"
            f"🏛 {esc(venue)}\n"
            f"🕒 {esc(show)}\n"
            f"💺 Class: {esc(cats)}\n"
            f"↕️ Rows: {esc(t.get('rows') or 'Any')}\n"
            f"🎟 Tickets: {t['tickets']}\n"
            f"⏱ Every {t['interval']} min")


def update_tracker(tid: str, **fields) -> bool:
    """Re-read the store and update just this tracker, so concurrent checks don't
    overwrite each other (no await between load and save, so it's atomic in asyncio).
    Returns False if the tracker was removed meanwhile."""
    data = load()
    if tid not in data:
        return False
    data[tid].update(fields)
    save(data)
    return True


# ── checking ─────────────────────────────────────────────────────────
async def run_check(app: Application, tid: str, manual: bool = False) -> str | None:
    data = load()
    t = data.get(tid)
    if not t:
        return None
    today = datetime.now().strftime("%Y%m%d")
    if t["date"] < today:
        await app.bot.send_message(t["chat_id"], f"⌛ Show date passed — stopped tracker:\n\n{describe(t)}",
                                   parse_mode=ParseMode.HTML)
        remove_tracker(app, tid)
        return None

    target = target_of(t)
    try:
        snap = await asyncio.to_thread(bms.fetch, target, t["date"])
    except Exception as e:
        fails = t.get("fails", 0) + 1
        if not update_tracker(tid, fails=fails):
            return None  # stopped while we were checking
        log.warning("check %s failed (%s): %s", tid, fails, e)
        if fails == 3:
            await app.bot.send_message(t["chat_id"], f"⚠️ Couldn't reach BookMyShow 3 times in a row for "
                                       f"<b>{esc(t['movie'])}</b> ({esc(e)}). I'll keep trying.",
                                       parse_mode=ParseMode.HTML)
        return f"⚠️ Check failed: {e}"

    hits = [(s, c) for s, c in matching(t, snap) if c.available]
    seat_info = await seat_check(t, hits)
    # Drop shows whose seat map was read and has nothing in the preferred rows.
    hits = [(s, c) for s, c in hits if seat_info.get(s.session_id, True) is not None]
    keys = sorted({f"{s.session_id}|{c.name}" for s, c in hits})
    new = set(keys) - set(t.get("last_keys", []))
    t.update(fails=0, last_keys=keys, last_check=datetime.now().isoformat(timespec="seconds"))
    if not update_tracker(tid, fails=0, last_keys=keys, last_check=t["last_check"]):
        return None  # stopped while we were checking

    if new:
        await app.bot.send_message(t["chat_id"], alert_text(t, target, hits, new, seat_info),
                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                   reply_markup=Kb([[Btn("🛑 Stop this tracker", callback_data=f"stop:{tid}")]]))
        return "🔔 Seats found — alert sent."
    if manual:
        n_shows = len({s.session_id for s, _ in matching(t, snap)})
        if hits:
            return "✅ Still available (already alerted)."
        if n_shows:
            return f"🔴 {n_shows} matching show(s) listed, but sold out."
        return "⏳ No matching shows listed yet."
    return None


MAX_SEAT_CHECKS = 8  # seat-map requests per tracker per check


async def seat_check(t: dict, hits) -> dict:
    """session_id -> list[SeatMatch] (rows OK), None (map read, no fit), or
    missing key (not checked / failed -> fall back to class-level)."""
    by_show: dict[str, tuple[bms.Show, set[str]]] = {}
    for s, c in hits:
        by_show.setdefault(s.session_id, (s, set()))[1].add(c.area_code)
    out = {}
    if not SEAT_MAP:
        return out
    for sid, (s, areas) in list(by_show.items())[:MAX_SEAT_CHECKS]:
        try:
            rows = await asyncio.to_thread(bms.fetch_seat_layout, s.venue_code, s.session_id)
        except Exception as e:
            log.info("seat map %s/%s unavailable: %s", s.venue_code, sid, e)
            continue
        if not rows:
            continue
        restrict = {a for a in areas if a} if t.get("categories") else None
        matches = bms.find_seats(rows, t.get("rows", ""), t["tickets"], restrict)
        out[sid] = matches or None
    return out


def _seat_range(nums: list[str]) -> str:
    try:
        ns = sorted(nums, key=int)
    except ValueError:
        ns = sorted(nums)
    return ns[0] if len(ns) == 1 else f"{ns[0]}–{ns[-1]}" if len(ns) == 2 or _consecutive(ns) else ", ".join(ns)


def _consecutive(ns) -> bool:
    try:
        v = [int(x) for x in ns]
        return v == list(range(v[0], v[0] + len(v)))
    except ValueError:
        return False


def alert_text(t, target, hits, new, seat_info=None) -> str:
    seat_info = seat_info or {}
    by_show: dict[str, tuple[bms.Show, list[bms.Category]]] = {}
    for s, c in hits:
        by_show.setdefault(s.session_id, (s, []))[1].append(c)
    lines = [f"🎉 <b>Tickets available!</b>\n", describe(t), ""]
    for sid, (s, cats) in by_show.items():
        tag = " 🆕" if any(f"{sid}|{c.name}" in new for c in cats) else ""
        lines.append(f"<b>{esc(s.venue_name)}</b> — {esc(s.time)}{tag}")
        for c in cats:
            lines.append(f"   {esc(c.name)} ₹{esc(c.price)}: {c.label}")
        matches = seat_info.get(sid)
        if matches:
            for m in matches[:4]:
                if m.together:
                    lines.append(f"   💺 Row {esc(m.row)}: seats {esc(_seat_range(m.seats))} together "
                                 f"({m.free_in_row} free in row)")
                else:
                    lines.append(f"   💺 Row {esc(m.row)}: {m.free_in_row} free, not side by side")
            if len(matches) > 4:
                lines.append(f"   …and {len(matches) - 4} more rows")
        elif sid not in seat_info:
            lines.append("   ⚠️ Couldn't read the seat map — rows not verified")
        lines.append(f'   <a href="{esc(bms.seat_layout_url(target, s))}">👉 Open seat layout</a>')
    return "\n".join(lines)


def schedule(app: Application, tid: str, interval: int, first: float = 2) -> None:
    for j in app.job_queue.get_jobs_by_name(tid):
        j.schedule_removal()

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await run_check(ctx.application, tid)
        except Exception:
            log.exception("tracker %s crashed", tid)

    app.job_queue.run_repeating(job, interval=interval * 60, first=first, name=tid)


def remove_tracker(app: Application, tid: str) -> dict | None:
    data = load()
    t = data.pop(tid, None)
    save(data)
    for j in app.job_queue.get_jobs_by_name(tid):
        j.schedule_removal()
    return t


# ── conversation ─────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text(f"Sorry, this bot is private. Your chat id is {update.effective_chat.id} — "
                                        "the owner can add it to ALLOWED_CHAT_IDS.")
        return
    await update.message.reply_text(
        "👋 I watch BookMyShow and ping you when seats open up.\n\n"
        "/track – set up a new tracker\n/list – your trackers\n/check – check now\n/stop – remove a tracker\n/myid – show your chat id")


async def myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")


async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return ConversationHandler.END
    ctx.user_data["t"] = {}
    await update.message.reply_text(
        "Send me a BookMyShow link:\n\n"
        "• a <b>movie</b> link, e.g. https://in.bookmyshow.com/movies/chennai/jailer-2/ET00518674\n"
        "• or a <b>cinema</b> link to watch one theatre, e.g. "
        "https://in.bookmyshow.com/cinemas/CHEN/rohini-silver-screens-koyambedu/buytickets/RSSC/20261120\n\n"
        "/cancel to abort", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return URL


async def got_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "/cinemas/" in update.message.text:
        return await _got_cinema(update, ctx)
    try:
        target = bms.parse_url(update.message.text)
    except bms.BMSError as e:
        await update.message.reply_text(f"❌ {e} Try again or /cancel.")
        return URL
    msg = await update.message.reply_text("🔎 Looking it up…")
    try:
        snap = await asyncio.to_thread(bms.fetch, target)
    except bms.BMSError as e:
        await msg.edit_text(f"❌ {e}. Try again or /cancel.")
        return URL
    ctx.user_data["t"] = {"event": target.event_code, "region": target.region_slug,
                          "slug": target.url_slug, "movie": snap.movie}
    rows, row = [], []
    for d, st in snap.dates.items():
        row.append(Btn(("✅ " if st == "OPEN" else "🔒 ") + bms.pretty_date(d), callback_data=f"d:{d}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    txt = (f"🎬 <b>{esc(snap.movie)}</b> ({esc(target.region_slug)})\n\nPick a date "
           "(✅ booking open, 🔒 not yet) — or type one like <code>28-11</code> or <code>2026-11-28</code>.")
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=Kb(rows) if rows else None)
    return DATE


# ── cinema-link flow: theatre fixed, user names the movie ────────────
async def _got_cinema(update: Update, ctx):
    try:
        cin = bms.parse_cinema_url(update.message.text)
    except bms.BMSError as e:
        await update.message.reply_text(f"❌ {e} Try again or /cancel.")
        return URL
    ctx.user_data["cinema"] = cin
    when = f" on {bms.pretty_date(cin.date_code)}" if cin.date_code else ""
    await update.message.reply_text(
        f"🏛 <b>{esc(cin.venue_name)}</b>{esc(when)}\n\nWhich movie should I watch for here? "
        "Paste its BookMyShow movie link (most reliable), or type its name.",
        parse_mode=ParseMode.HTML)
    return MOVIE


async def got_movie_text(update: Update, ctx):
    cin: bms.Cinema = ctx.user_data["cinema"]
    text = update.message.text.strip()
    if "bookmyshow.com" in text:
        try:
            target = bms.parse_url(text)
        except bms.BMSError as e:
            await update.message.reply_text(f"❌ {e} Try again or /cancel.")
            return MOVIE
        msg = await update.message.reply_text("🔎 Checking…")
        return await _cinema_movie_chosen(msg, ctx, target.event_code, target.url_slug)
    msg = await update.message.reply_text("🔎 Searching…")
    hits = await asyncio.to_thread(bms.search_movies, cin.region_slug, text)
    if not hits:
        await msg.edit_text("Couldn't find that in BookMyShow's movie lists for this city. "
                            "Paste the movie's BookMyShow link instead.")
        return MOVIE
    if len(hits) == 1:
        return await _cinema_movie_chosen(msg, ctx, hits[0][2], hits[0][1])
    ctx.user_data["movie_hits"] = {code: slug for _, slug, code in hits}
    rows = [[Btn(name[:60], callback_data=f"m:{code}")] for name, _, code in hits[:8]]
    await msg.edit_text("Which one?", reply_markup=Kb(rows))
    return MOVIE


async def got_movie_btn(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    code = q.data[2:]
    return await _cinema_movie_chosen(q.message, ctx, code, ctx.user_data.get("movie_hits", {}).get(code, ""))


async def _cinema_movie_chosen(msg, ctx, event_code, slug):
    cin: bms.Cinema = ctx.user_data["cinema"]
    target = bms.Target(event_code=event_code, region_slug=cin.region_slug, url_slug=slug)
    try:
        snap = await asyncio.to_thread(bms.fetch, target, cin.date_code)
    except bms.BMSError as e:
        await msg.edit_text(f"❌ {e}. Try again or /cancel.")
        return MOVIE
    date = cin.date_code or datetime.now().strftime("%Y%m%d")
    shows = [s for s in snap.shows if s.venue_code == cin.venue_code and s.date_code in ("", date)]
    venue_name = shows[0].venue_name if shows else cin.venue_name
    ctx.user_data["t"] = {"event": event_code, "region": cin.region_slug, "slug": slug, "movie": snap.movie,
                          "date": date, "venue_code": cin.venue_code, "venue_name": venue_name}
    if shows:
        ctx.user_data["picked"] = set()
        ctx.user_data["venue_shows"] = shows
        await msg.edit_text(f"✅ {snap.movie} is already listed at {venue_name} on {bms.pretty_date(date)}.")
        msg = await msg.reply_text("…")
        await _show_picker(msg, ctx)
        return SHOW
    await msg.edit_text(f"⏳ <b>{esc(snap.movie)}</b> isn't listed at {esc(venue_name)} on "
                        f"{esc(bms.pretty_date(date))} yet — I'll alert you when it's added and bookable.",
                        parse_mode=ParseMode.HTML)
    msg = await msg.reply_text("…")
    return await _ask_window(msg, ctx)


def _parse_date(text: str) -> str | None:
    text = text.strip()
    now = datetime.now()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", text)
    if m:
        d = datetime(now.year, int(m[2]), int(m[1]))
        if d.date() < now.date():
            d = d.replace(year=now.year + 1)
        return d.strftime("%Y%m%d")
    return None


async def got_date_text(update: Update, ctx):
    d = _parse_date(update.message.text)
    if not d or d < datetime.now().strftime("%Y%m%d"):
        await update.message.reply_text("Couldn't read that date (or it's in the past). Try 28-11 or 2026-11-28.")
        return DATE
    return await _after_date(update.message, ctx, d, new_msg=True)


async def got_date_btn(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    return await _after_date(q.message, ctx, q.data[2:], new_msg=False)


async def _after_date(message, ctx, date_code, new_msg):
    t = ctx.user_data["t"]
    t["date"] = date_code
    msg = await message.reply_text("🔎 Loading theatres…") if new_msg else await message.edit_text("🔎 Loading theatres…")
    try:
        snap = await asyncio.to_thread(bms.fetch, target_of(t), date_code)
    except bms.BMSError as e:
        await msg.edit_text(f"❌ {e}. Pick the date again or /cancel.")
        return DATE
    venues = {}
    for s in snap.shows:
        if s.date_code in ("", date_code):
            venues.setdefault(s.venue_code, s.venue_name)
    ctx.user_data["venues"] = sorted(venues.items(), key=lambda kv: kv[1])
    ctx.user_data["shows"] = [s for s in snap.shows if s.date_code in ("", date_code)]
    if not venues:
        await msg.edit_text(
            f"No shows listed for {bms.pretty_date(date_code)} yet — booking probably isn't open.\n\n"
            "Type part of the theatre name to watch (e.g. <code>Palazzo</code>, <code>PVR</code>), "
            "or <code>any</code> for any theatre.", parse_mode=ParseMode.HTML)
        return THEATRE_TEXT
    await _theatre_page(msg, ctx, 0)
    return THEATRE


async def _theatre_page(msg, ctx, page):
    venues = ctx.user_data["venues"]
    chunk = venues[page * PAGE:(page + 1) * PAGE]
    rows = [[Btn(name[:60], callback_data=f"v:{code}")] for code, name in chunk]
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"vp:{page - 1}"))
    if (page + 1) * PAGE < len(venues):
        nav.append(Btn("Next ➡️", callback_data=f"vp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("🔤 Type a name", callback_data="v:__type"), Btn("🌐 Any theatre", callback_data="v:__any")])
    await msg.edit_text(f"🏛 Pick a theatre ({len(venues)} showing on "
                        f"{bms.pretty_date(ctx.user_data['t']['date'])}):", reply_markup=Kb(rows))


async def got_theatre_btn(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    t = ctx.user_data["t"]
    if q.data.startswith("vp:"):
        await _theatre_page(q.message, ctx, int(q.data[3:]))
        return THEATRE
    code = q.data[2:]
    if code == "__type":
        await q.message.edit_text("Type part of the theatre name (e.g. <code>Palazzo</code>):",
                                  parse_mode=ParseMode.HTML)
        return THEATRE_TEXT
    if code == "__any":
        t["venue_name"] = "Any theatre"
        return await _ask_window(q.message, ctx)
    t["venue_code"] = code
    t["venue_name"] = dict(ctx.user_data["venues"])[code]
    shows = [s for s in ctx.user_data["shows"] if s.venue_code == code]
    ctx.user_data["picked"] = set()
    ctx.user_data["venue_shows"] = shows
    await _show_picker(q.message, ctx)
    return SHOW


async def got_theatre_text(update: Update, ctx):
    t = ctx.user_data["t"]
    text = update.message.text.strip()
    if text.lower() == "any":
        t["venue_name"] = "Any theatre"
    else:
        t["venue_query"] = text
    msg = await update.message.reply_text("…")
    return await _ask_window(msg, ctx)


async def _ask_window(msg, ctx):
    rows = [[Btn("🌅 Morning", callback_data="w:morning"), Btn("☀️ Afternoon", callback_data="w:afternoon")],
            [Btn("🌇 Evening", callback_data="w:evening"), Btn("🌙 Night", callback_data="w:night")],
            [Btn("Any time", callback_data="w:any")]]
    await msg.edit_text("🕒 Which show time?\nMorning <12, Afternoon 12–4, Evening 4–7, Night 7+",
                        reply_markup=Kb(rows))
    return SHOW


async def _show_picker(msg, ctx):
    picked = ctx.user_data["picked"]
    shows = ctx.user_data["venue_shows"]
    rows, row = [], []
    for s in shows:
        status = "🟢" if any(c.available for c in s.categories) else "🔴"
        mark = "☑️ " if s.session_id in picked else ""
        row.append(Btn(f"{mark}{s.time} {status}", callback_data=f"s:{s.session_id}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([Btn("✅ Done" if picked else "Any show", callback_data="s:__done")])
    await msg.edit_text(f"🕒 {ctx.user_data['t']['venue_name']}\nTap one or more showtimes, then Done "
                        "(or 'Any show'):", reply_markup=Kb(rows))


async def got_show_btn(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    t = ctx.user_data["t"]
    if q.data.startswith("w:"):
        t["window"] = q.data[2:]
        return await _ask_rows(q.message, ctx)
    sid = q.data[2:]
    if sid != "__done":
        picked = ctx.user_data["picked"]
        picked.symmetric_difference_update({sid})
        await _show_picker(q.message, ctx)
        return SHOW
    shows = ctx.user_data["venue_shows"]
    picked = ctx.user_data["picked"]
    if picked:
        t["sessions"] = [s.session_id for s in shows if s.session_id in picked]
        t["session_labels"] = [s.time for s in shows if s.session_id in picked]
        relevant = [s for s in shows if s.session_id in picked]
    else:
        t["window"] = "any"
        relevant = shows
    cats = []
    for s in relevant:
        for c in s.categories:
            if c.name not in [x[0] for x in cats]:
                cats.append((c.name, c.price))
    if not cats:
        return await _ask_rows(q.message, ctx)
    ctx.user_data["cats"] = cats
    ctx.user_data["cat_picked"] = set()
    await _cat_picker(q.message, ctx)
    return CATEGORY


async def _cat_picker(msg, ctx):
    picked = ctx.user_data["cat_picked"]
    rows = [[Btn(f"{'☑️ ' if i in picked else ''}{n} ₹{p}", callback_data=f"c:{i}")]
            for i, (n, p) in enumerate(ctx.user_data["cats"])]
    rows.append([Btn("✅ Done" if picked else "Any class", callback_data="c:__done")])
    await msg.edit_text("💺 Seat class? Tap one or more, then Done:", reply_markup=Kb(rows))


async def got_cat_btn(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    v = q.data[2:]
    if v != "__done":
        ctx.user_data["cat_picked"].symmetric_difference_update({int(v)})
        await _cat_picker(q.message, ctx)
        return CATEGORY
    cats = ctx.user_data["cats"]
    ctx.user_data["t"]["categories"] = [cats[i][0] for i in sorted(ctx.user_data["cat_picked"])]
    return await _ask_rows(q.message, ctx)


async def _ask_rows(msg, ctx):
    await msg.edit_text("↕️ Preferred rows? Type e.g. <code>H-M</code> or <code>J,K,L</code>, "
                        "or <code>any</code>.", parse_mode=ParseMode.HTML)
    return ROWS


async def got_rows(update: Update, ctx):
    text = update.message.text.strip().upper()
    ctx.user_data["t"]["rows"] = "" if text in ("ANY", "-", "NO", "NONE") else text
    rows = [[Btn(str(n), callback_data=f"n:{n}") for n in range(1, 6)],
            [Btn(str(n), callback_data=f"n:{n}") for n in range(6, 11)]]
    await update.message.reply_text("🎟 How many tickets?", reply_markup=Kb(rows))
    return TICKETS


async def got_tickets(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    ctx.user_data["t"]["tickets"] = int(q.data[2:])
    rows = [[Btn(f"Every {m} min", callback_data=f"f:{m}") for m in INTERVALS[:2]],
            [Btn(f"Every {m} min", callback_data=f"f:{m}") for m in INTERVALS[2:]]]
    await q.message.edit_text("⏱ How often should I check?", reply_markup=Kb(rows))
    return FREQ


async def got_freq(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    t = ctx.user_data["t"]
    t["interval"] = int(q.data[2:])
    await q.message.edit_text("Confirm this tracker?\n\n" + describe(t), parse_mode=ParseMode.HTML,
                              reply_markup=Kb([[Btn("✅ Start tracking", callback_data="ok:yes"),
                                                Btn("✖️ Cancel", callback_data="ok:no")]]))
    return CONFIRM


async def got_confirm(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "ok:no":
        await q.message.edit_text("Cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    t = ctx.user_data["t"]
    tid = uuid.uuid4().hex[:6]
    t.update(id=tid, chat_id=q.message.chat_id, created=datetime.now().isoformat(timespec="seconds"),
             last_keys=[], fails=0)
    data = load()
    data[tid] = t
    save(data)
    schedule(ctx.application, tid, t["interval"], first=1)
    await q.message.edit_text(f"✅ Tracking started (id <code>{tid}</code>). I'll check now and every "
                              f"{t['interval']} min.\n\n" + describe(t), parse_mode=ParseMode.HTML)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END


# ── management commands ──────────────────────────────────────────────
def mine(chat_id: int) -> dict:
    return {k: v for k, v in load().items() if v["chat_id"] == chat_id}


async def list_cmd(update: Update, ctx):
    if not allowed(update):
        return
    ts = mine(update.effective_chat.id)
    if not ts:
        await update.message.reply_text("No trackers yet. Use /track.")
        return
    for tid, t in ts.items():
        status = f"Currently available: {len(t.get('last_keys', []))} show/class combos" if t.get("last_keys") else "Nothing available yet"
        last = t.get("last_check", "never")
        await update.message.reply_text(
            f"<code>{tid}</code>\n{describe(t)}\n\n{status}\nLast check: {esc(last)}",
            parse_mode=ParseMode.HTML,
            reply_markup=Kb([[Btn("🔄 Check now", callback_data=f"chk:{tid}"),
                              Btn("🛑 Stop", callback_data=f"stop:{tid}")]]))


async def stop_cmd(update: Update, ctx):
    if not allowed(update):
        return
    ts = mine(update.effective_chat.id)
    if not ts:
        await update.message.reply_text("No trackers to stop.")
        return
    rows = [[Btn(f"🛑 {t['movie'][:25]} · {bms.pretty_date(t['date'])} · {(t.get('venue_name') or t.get('venue_query') or '')[:20]}",
                 callback_data=f"stop:{tid}")] for tid, t in ts.items()]
    await update.message.reply_text("Which tracker should I stop?", reply_markup=Kb(rows))


async def check_cmd(update: Update, ctx):
    if not allowed(update):
        return
    ts = mine(update.effective_chat.id)
    if not ts:
        await update.message.reply_text("No trackers yet. Use /track.")
        return
    for tid, t in ts.items():
        res = await run_check(ctx.application, tid, manual=True)
        if res:
            await update.message.reply_text(f"<code>{tid}</code> {esc(t['movie'])}: {esc(res)}", parse_mode=ParseMode.HTML)


async def action_btn(update: Update, ctx):
    q = update.callback_query
    kind, tid = q.data.split(":", 1)
    t = load().get(tid)
    if not t or t["chat_id"] != q.message.chat_id:
        await q.answer("That tracker no longer exists.")
        return
    if kind == "stop":
        remove_tracker(ctx.application, tid)
        await q.answer("Stopped.")
        await q.message.reply_text(f"🛑 Stopped tracking {t['movie']} on {bms.pretty_date(t['date'])}.")
    else:
        await q.answer("Checking…")
        res = await run_check(ctx.application, tid, manual=True)
        if res:
            await q.message.reply_text(res)


async def post_init(app: Application):
    for tid, t in load().items():
        schedule(app, tid, t["interval"], first=10)
    log.info("Rescheduled %d tracker(s)", len(load()))
    await app.bot.set_my_commands([("track", "Track a new show"), ("list", "Your trackers"),
                                   ("check", "Check all now"), ("stop", "Stop a tracker"),
                                   ("cancel", "Cancel setup")])


def main():
    app = (Application.builder().token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(40)
           .build())
    conv = ConversationHandler(
        entry_points=[CommandHandler("track", track)],
        states={
            URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_url)],
            DATE: [CallbackQueryHandler(got_date_btn, pattern=r"^d:"),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, got_date_text)],
            THEATRE: [CallbackQueryHandler(got_theatre_btn, pattern=r"^(v|vp):")],
            THEATRE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_theatre_text)],
            SHOW: [CallbackQueryHandler(got_show_btn, pattern=r"^(s|w):")],
            CATEGORY: [CallbackQueryHandler(got_cat_btn, pattern=r"^c:")],
            ROWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_rows)],
            TICKETS: [CallbackQueryHandler(got_tickets, pattern=r"^n:")],
            FREQ: [CallbackQueryHandler(got_freq, pattern=r"^f:")],
            CONFIRM: [CallbackQueryHandler(got_confirm, pattern=r"^ok:")],
            MOVIE: [CallbackQueryHandler(got_movie_btn, pattern=r"^m:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_movie_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("track", track)],
        conversation_timeout=timedelta(minutes=15),
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CallbackQueryHandler(action_btn, pattern=r"^(stop|chk):"))
    log.info("Bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
