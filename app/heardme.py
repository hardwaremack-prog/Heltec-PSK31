"""
"Heard Me" for LoPSK: asks PSKReporter (pskreporter.info) which stations have
reported hearing your callsign, and keeps a permanent local log of them.

PSKReporter only keeps the last 24 hours, and asks programs not to query more
than once every 5 minutes, so the app caches results and merges every report
it ever sees into ~/.lopsk_heard.csv.

Reports only come from stations whose software uploads them (fldigi, WSJT-X,
JTDX, Skimmers...). If nobody listening on your frequency uploads, the list
stays empty even when people hear you.
"""
import csv
import math
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

QUERY_URL = "https://retrieve.pskreporter.info/query"
MAP_URL = "https://pskreporter.info/pskmap.html"
MIN_INTERVAL = 300            # PSKReporter's request: at most one query per 5 minutes
LOG_PATH = os.path.join(os.path.expanduser("~"), ".lopsk_heard.csv")
FIELDS = ["time", "receiver", "locator", "country", "freq_hz", "mode", "snr", "sender", "sender_locator"]
USER_AGENT = "LoPSK/1.1 (+https://github.com/hardwaremack-prog/lopsk)"


# ---------------------------------------------------------------- geography

def locator_to_latlon(loc):
    """Maidenhead locator (4, 6 or 8 characters) -> (lat, lon) of the square's centre, or None."""
    loc = (loc or "").strip().upper()
    if len(loc) < 4 or len(loc) % 2:
        return None
    try:
        if not ("A" <= loc[0] <= "R" and "A" <= loc[1] <= "R" and loc[2].isdigit() and loc[3].isdigit()):
            return None
        lon = (ord(loc[0]) - 65) * 20 - 180 + int(loc[2]) * 2
        lat = (ord(loc[1]) - 65) * 10 - 90 + int(loc[3])
        w, h = 2.0, 1.0
        if len(loc) >= 6:
            a, b = loc[4], loc[5]
            if not ("A" <= a <= "X" and "A" <= b <= "X"):
                return lat + h / 2, lon + w / 2
            w, h = w / 24, h / 24
            lon += (ord(a) - 65) * w
            lat += (ord(b) - 65) * h
        if len(loc) >= 8 and loc[6].isdigit() and loc[7].isdigit():
            w, h = w / 10, h / 10
            lon += int(loc[6]) * w
            lat += int(loc[7]) * h
        return lat + h / 2, lon + w / 2
    except (IndexError, ValueError):
        return None


def distance_bearing(a, b):
    """Great-circle distance (km) and bearing (degrees) from point a to point b."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon = lon2 - lon1
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    km = 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return km, (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][int((deg + 11.25) // 22.5) % 16]


# ---------------------------------------------------------------- PSKReporter

class TooSoon(Exception):
    pass


def build_query(call, seconds, mode=None):
    q = {"senderCallsign": call.upper(), "flowStartSeconds": -int(seconds),
         "rronly": 1, "noactive": 1, "nolocator": 0}
    if mode:
        q["mode"] = mode
    return QUERY_URL + "?" + urllib.parse.urlencode(q)


def map_link(call, seconds):
    q = {"callsign": call.upper(), "timerange": int(seconds), "distunit": "miles", "hideunrec": 1}
    return MAP_URL + "?preset&" + urllib.parse.urlencode(q)


def parse_reports(xml_text):
    """PSKReporter XML -> list of report dicts. Raises ValueError with a readable message."""
    text = (xml_text or "").strip()
    if not text.startswith("<"):
        # PSKReporter answers rate-limit and other problems with plain text
        raise ValueError(text[:200] or "empty reply from PSKReporter")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"couldn't read PSKReporter's reply ({e})")
    out = []
    for r in root.iter("receptionReport"):
        a = r.attrib
        try:
            t = int(a.get("flowStartSeconds", "0"))
        except ValueError:
            continue
        out.append({
            "time": t,
            "receiver": a.get("receiverCallsign", "?"),
            "locator": a.get("receiverLocator", ""),
            "country": a.get("receiverDXCC", ""),
            "freq_hz": int(float(a.get("frequency", "0") or 0)),
            "mode": a.get("mode", ""),
            "snr": a.get("sNR", ""),
            "sender": a.get("senderCallsign", ""),
            "sender_locator": a.get("senderLocator", ""),
        })
    out.sort(key=lambda r: r["time"], reverse=True)
    return out


class Reporter:
    """Fetches with PSKReporter's 5-minute courtesy limit and a persistent log."""

    def __init__(self, log_path=LOG_PATH):
        self.log_path = log_path
        self.last_query = 0.0
        self.last_key = None
        self.cache = []

    def wait_left(self):
        return max(0, MIN_INTERVAL - (time.time() - self.last_query))

    def fetch(self, call, seconds, mode=None, timeout=30):
        key = (call.upper(), seconds, mode)
        if self.wait_left() > 0:
            if key == self.last_key:
                return self.cache, True            # same question: answer from cache
            raise TooSoon(int(self.wait_left()))
        url = build_query(call, seconds, mode)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        self.last_query = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        reports = parse_reports(body)
        self.cache, self.last_key = reports, key
        self.add_to_log(reports)
        return reports, False

    # ---- permanent log
    def load_log(self, call=None):
        rows = []
        try:
            with open(self.log_path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        r["time"] = int(r["time"])
                        r["freq_hz"] = int(r["freq_hz"] or 0)
                    except (ValueError, KeyError):
                        continue
                    if call and r.get("sender", "").upper() != call.upper():
                        continue
                    rows.append(r)
        except FileNotFoundError:
            pass
        rows.sort(key=lambda r: r["time"], reverse=True)
        return rows

    def add_to_log(self, reports):
        old = self.load_log()
        seen = {(r["time"], r["receiver"], r["sender"], int(r["freq_hz"]) // 100) for r in old}
        new = [r for r in reports if (r["time"], r["receiver"], r["sender"], r["freq_hz"] // 100) not in seen]
        if not new:
            return 0
        exists = os.path.exists(self.log_path)
        with open(self.log_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            for r in sorted(new, key=lambda r: r["time"]):
                w.writerow(r)
        return len(new)


def enrich(reports, my_locator):
    """Add distance/bearing from my_locator (or each report's sender locator)."""
    for r in reports:
        here = locator_to_latlon(my_locator) or locator_to_latlon(r.get("sender_locator", ""))
        there = locator_to_latlon(r.get("locator", ""))
        if here and there:
            km, brg = distance_bearing(here, there)
            r["km"], r["bearing"] = km, brg
        else:
            r["km"], r["bearing"] = None, None
    return reports


def summary(reports):
    if not reports:
        return "No reports."
    stations = {r["receiver"] for r in reports}
    with_d = [r for r in reports if r.get("km") is not None]
    s = f"{len(reports)} report{'s' if len(reports) != 1 else ''} from {len(stations)} station{'s' if len(stations) != 1 else ''}"
    if with_d:
        far = max(with_d, key=lambda r: r["km"])
        s += f" — farthest {far['receiver']} at {far['km'] * 0.621371:,.0f} mi ({far['km']:,.0f} km)"
    return s
