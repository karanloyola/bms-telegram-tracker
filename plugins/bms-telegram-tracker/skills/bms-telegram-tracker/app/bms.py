"""BookMyShow showtimes client.

Uses the same public showtimes endpoint as aviiciii/bms-ticket-notifier, but
through curl_cffi with a Chrome TLS fingerprint (plain `requests` gets a
Cloudflare 403).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from curl_cffi import requests

API_URL = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"

AVAIL = {
    "0": ("Sold out", "🔴"),
    "1": ("Almost full", "🟡"),
    "2": ("Filling fast", "🟠"),
    "3": ("Available", "🟢"),
}

DATE_STYLE = {
    "date-selected": "OPEN",
    "date-default": "OPEN",
    "date-disabled": "NOT_OPEN",
}

# slug -> (regionCode, canonical slug, lat, lon, geohash)
REGIONS = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
    "coimbatore": ("COIM", "coimbatore", "11.017", "76.956", "tf1"),
    "madurai": ("MADU", "madurai", "9.925", "78.120", "tc8"),
    "ahmedabad": ("AHD", "ahmedabad", "23.023", "72.571", "ts5"),
}


class BMSError(Exception):
    pass


@dataclass
class Category:
    name: str
    price: str
    status: str  # "0".."3"
    area_code: str = ""

    @property
    def available(self) -> bool:
        return self.status in ("1", "2", "3")

    @property
    def label(self) -> str:
        lbl, ico = AVAIL.get(self.status, ("Unknown", "⚪"))
        return f"{ico} {lbl}"


@dataclass
class Show:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    categories: list[Category] = field(default_factory=list)


@dataclass
class Snapshot:
    movie: str
    dates: dict[str, str]  # date_code -> OPEN / NOT_OPEN
    shows: list[Show]


@dataclass
class Target:
    event_code: str
    region_slug: str
    url_slug: str = ""

    @property
    def region(self):
        key = self.region_slug.lower()
        if key in REGIONS:
            return REGIONS[key]
        return (key.upper()[:6], key, "0", "0", "")


def parse_url(url: str) -> Target:
    """Accepts any BMS movie URL, e.g.
    https://in.bookmyshow.com/movies/chennai/jailer-2/ET00518674
    https://in.bookmyshow.com/movies/chennai/jailer-2/buytickets/ET00518674/20261120
    """
    parts = urlparse(url.strip()).path.strip("/").split("/")
    event = next((p for p in parts if re.fullmatch(r"ET\d{8,}", p)), None)
    if not event or "movies" not in parts:
        raise BMSError("That doesn't look like a BookMyShow movie link.")
    i = parts.index("movies")
    region = parts[i + 1] if i + 1 < len(parts) else ""
    slug = parts[i + 2] if i + 2 < len(parts) and parts[i + 2] != event else ""
    if not region:
        raise BMSError("Couldn't find the city in that link.")
    return Target(event_code=event, region_slug=region, url_slug=slug)


def fetch(target: Target, date_code: str = "") -> Snapshot:
    code, slug, lat, lon, geohash = target.region
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://in.bookmyshow.com/movies/{slug}/buytickets/{target.event_code}/",
        "x-app-code": "WEB",
        "x-region-code": code,
        "x-region-slug": slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
    }
    params = {
        "eventCode": target.event_code,
        "dateCode": date_code,
        "isDesktop": "true",
        "regionCode": code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        r = requests.get(API_URL, headers=headers, params=params,
                         impersonate="chrome", timeout=25)
    except Exception as e:  # network errors
        raise BMSError(f"Network error: {e}") from e
    if r.status_code != 200:
        raise BMSError(f"BookMyShow returned HTTP {r.status_code}")
    data = r.json()
    err = (data.get("metadata") or {}).get("error")
    body = data.get("data") or {}
    if err and not body.get("showtimeWidgets"):
        # e.g. "There are no shows available in this region." — treat as empty
        return Snapshot(movie=target.url_slug or target.event_code, dates={}, shows=[])
    return Snapshot(movie=_movie_name(body, target), dates=_dates(body), shows=_shows(body))


def seat_layout_url(target: Target, show: Show) -> str:
    _, slug, *_ = target.region
    return (f"https://in.bookmyshow.com/movies/{slug}/seat-layout/"
            f"{target.event_code}/{show.venue_code}/{show.session_id}/{show.date_code}")


def buytickets_url(target: Target, date_code: str = "") -> str:
    _, slug, *_ = target.region
    name = target.url_slug or "movie"
    return (f"https://in.bookmyshow.com/movies/{slug}/{name}/buytickets/"
            f"{target.event_code}/{date_code}")


# ── parsers ──────────────────────────────────────────────────────────
def _movie_name(body, target):
    for w in body.get("bottomSheetData", {}).get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle" and d.get("text"):
                    return d["text"]
    return (target.url_slug or target.event_code).replace("-", " ").title()


def _dates(body):
    out = {}
    for w in body.get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            if item.get("id") and re.fullmatch(r"\d{8}", str(item["id"])):
                out[item["id"]] = DATE_STYLE.get(item.get("styleId", ""), "UNKNOWN")
    return out


def _shows(body):
    shows = []
    for w in body.get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                va = card.get("additionalData", {})
                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(sa.get("showDateCode") or sa.get("dateCode") or "")
                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]
                    show = Show(
                        venue_code=va.get("venueCode", ""),
                        venue_name=va.get("venueName", "Unknown"),
                        session_id=str(sa.get("sessionId", "")),
                        date_code=date_code,
                        time=sa.get("showTime") or st.get("title", ""),
                        time_code=str(sa.get("showTimeCode", "")),
                    )
                    for c in sa.get("categories", []):
                        show.categories.append(Category(
                            name=c.get("priceDesc", ""),
                            price=c.get("curPrice", "0"),
                            status=str(c.get("availStatus", "")),
                            area_code=c.get("areaCatCode", ""),
                        ))
                    shows.append(show)
    return shows


# ── seat layout (row-level) ──────────────────────────────────────────
# The seat page loads its map from doTrans.aspx (GETSEATLAYOUT); the payload is
# AES-CBC encrypted with a static key + zero IV that ships in BMS's web bundle.
SEAT_URL = "https://services-in.bookmyshow.com/doTrans.aspx"
SEAT_KEY = b"kYp3s6v9y$B&E)H+MbQeThWmZq4t7w!z"
SEAT_AVAILABLE = {"1", "4"}  # 1 = available, 4 = "bestseller" (still free)


@dataclass
class Seat:
    number: str  # seat number shown to the user ("" for gangway)
    status: str  # 0 gangway, 1 free, 2 booked, 3 other class, 4 free (best), 7/8/9 special
    area_code: str

    @property
    def free(self) -> bool:
        return self.status in SEAT_AVAILABLE


@dataclass
class Row:
    label: str
    seats: list[Seat]


def _decrypt(b64: str) -> str:
    import base64
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    ct = base64.b64decode(b64)
    d = Cipher(algorithms.AES(SEAT_KEY), modes.CBC(bytes(16))).decryptor()
    pt = d.update(ct) + d.finalize()
    return pt[:-pt[-1]].decode()


def fetch_seat_layout(venue_code: str, session_id: str) -> list[Row]:
    from curl_cffi import CurlMime
    fields = dict(strAppCode="WEB", lngTransactionIdentifier="0", strCommand="GETSEATLAYOUT",
                  strVenueCode=venue_code, strParam1=session_id, strParam2="WEB", strParam3="",
                  strParam4="", strParam5="Y", strParam6="", strParam7="", strParam8="",
                  strParam9="", strParam10="", strFormat="json")
    mp = CurlMime()
    for k, v in fields.items():
        mp.addpart(name=k, data=v.encode())
    try:
        r = requests.post(SEAT_URL, multipart=mp, impersonate="chrome", timeout=25,
                          headers={"Origin": "https://in.bookmyshow.com",
                                   "Referer": "https://in.bookmyshow.com/"})
    except Exception as e:
        raise BMSError(f"Seat layout network error: {e}") from e
    if r.status_code != 200:
        raise BMSError(f"Seat layout HTTP {r.status_code}")
    j = (r.json() or {}).get("BookMyShow", {})
    if j.get("blnSuccess") != "true" or not j.get("strData"):
        raise BMSError(f"Seat layout error: {j.get('strException') or 'no data'}")
    return parse_seat_layout(_decrypt(j["strData"]))


def parse_seat_layout(text: str) -> list[Row]:
    """Format (from BMS's own parser): "<areas>||<rows>".
    areas: "desc:areaId:areaCode:areaNo:...|..."; rows: "rowLabel:rowNo:SEAT:SEAT...|..."
    SEAT: "<areaId><status><seatNo>+<cinemaSeatNo>[+rank]".
    """
    parts = text.split("||")
    if len(parts) < 2:
        return []
    area_codes = {}
    for a in filter(None, parts[0].split("|")):
        f = a.split(":")
        if len(f) >= 3:
            area_codes[f[1]] = f[2]
    rows = []
    for r in filter(None, parts[1].split("|")):
        f = r.split(":")
        if len(f) < 2:
            continue
        seats = []
        for tok in f[2:]:
            head = tok.split("+")[0]
            if len(head) < 2:
                seats.append(Seat("", "0", ""))
                continue
            seats.append(Seat(number=head[2:], status=head[1], area_code=area_codes.get(head[0], head[0])))
        rows.append(Row(label=f[0], seats=seats))
    return rows


def parse_row_pref(pref: str) -> list[tuple[str, str]]:
    """'H-M, P' -> [('H','M'), ('P','P')]"""
    out = []
    for part in re.split(r"[,\s]+", (pref or "").upper().strip()):
        if not part:
            continue
        a, _, b = part.partition("-")
        out.append((a, b or a))
    return out


def rows_in_pref(rows: list[Row], pref: str) -> list[Row]:
    ranges = parse_row_pref(pref)
    if not ranges:
        return [r for r in rows if r.label]
    order = {r.label: i for i, r in enumerate(rows) if r.label}

    def key(label):  # layout order when known, else alphabetical-ish
        return order.get(label, (len(label), label))

    picked = []
    for r in rows:
        if not r.label:
            continue
        for a, b in ranges:
            if a in order and b in order:
                lo, hi = sorted((order[a], order[b]))
                if lo <= order[r.label] <= hi:
                    picked.append(r); break
            elif (len(a), a) <= (len(r.label), r.label) <= (len(b), b):
                picked.append(r); break
    return picked


@dataclass
class SeatMatch:
    row: str
    seats: list[str]   # seat numbers of the best block
    together: bool
    free_in_row: int


def find_seats(rows: list[Row], pref: str, tickets: int, area_codes: set[str] | None = None) -> list[SeatMatch]:
    """Rows from `pref` with >= `tickets` free seats; prefers side-by-side blocks."""
    out = []
    for r in rows_in_pref(rows, pref):
        best, run = [], []
        free = 0
        for s in r.seats + [Seat("", "0", "")]:
            ok = s.free and (not area_codes or s.area_code in area_codes)
            if ok:
                run.append(s.number); free += 1
            else:
                if len(run) > len(best):
                    best = run
                run = []
        if free < tickets:
            continue
        together = len(best) >= tickets
        out.append(SeatMatch(row=r.label, seats=best[:tickets] if together else [], together=together,
                             free_in_row=free))
    out.sort(key=lambda m: not m.together)
    return out


def pretty_date(date_code: str) -> str:
    from datetime import datetime
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return date_code
