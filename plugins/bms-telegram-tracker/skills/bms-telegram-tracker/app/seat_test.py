"""Manual check: fetch one live show's seat layout and print rows.

Usage: .venv/bin/python seat_test.py <movie-url> <YYYYMMDD> [venue_code] [rows] [tickets]
"""
import sys

import bms

url, date = sys.argv[1], sys.argv[2]
venue = sys.argv[3] if len(sys.argv) > 3 else ""
pref = sys.argv[4] if len(sys.argv) > 4 else ""
n = int(sys.argv[5]) if len(sys.argv) > 5 else 2

t = bms.parse_url(url)
snap = bms.fetch(t, date)
shows = [s for s in snap.shows if not venue or s.venue_code == venue]
s = shows[0]
print(f"{s.venue_name} {s.time} session={s.session_id}  classes:",
      [(c.name, c.area_code, c.label) for c in s.categories])
rows = bms.fetch_seat_layout(s.venue_code, s.session_id)
for r in rows:
    if not r.label:
        continue
    free = sum(x.free for x in r.seats)
    taken = sum(x.status == "2" for x in r.seats)
    print(f"  row {r.label:>3}: {free:3d} free, {taken:3d} booked  areas={sorted({x.area_code for x in r.seats if x.number})}")
print("matches for rows", repr(pref), "tickets", n, ":")
for m in bms.find_seats(rows, pref, n)[:5]:
    print("  ", m)

if "--alert" in sys.argv:  # render the bot's real alert for this show
    import asyncio
    import bot
    tr = {"movie": snap.movie, "event": t.event_code, "region": t.region_slug, "slug": t.url_slug,
          "date": date, "venue_code": s.venue_code, "venue_name": s.venue_name, "window": "any",
          "categories": [], "rows": pref, "tickets": n, "interval": 15}
    hits = [(x, c) for x, c in bot.matching(tr, snap) if c.available]
    info = asyncio.run(bot.seat_check(tr, hits))
    hits = [(x, c) for x, c in hits if info.get(x.session_id, True) is not None]
    print("\n--- alert ---")
    print(bot.alert_text(tr, t, hits, {f"{x.session_id}|{c.name}" for x, c in hits}, info) if hits else "(no alert: nothing fits)")
