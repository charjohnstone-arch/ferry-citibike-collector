"""Poll NYC Ferry, Citi Bike and (optionally) MTA bus feeds for a few minutes, appending to daily CSVs.

GitHub Actions starts this every 5 minutes. Each run polls every POLL_SECONDS for RUN_MINUTES,
so back to back runs give near continuous coverage.

Output, one folder per New York date (gzip compressed; pandas reads them directly):
  data/YYYY-MM-DD/ferry.csv.gz  NYC Ferry arrival predictions for landings in the next 30 minutes
  data/YYYY-MM-DD/bike.csv.gz   Citi Bike bikes / e-bikes / open docks at stations within 500 m of a
                                landing, plus comparison stations 800 m to 1.5 km away
  data/YYYY-MM-DD/bus.csv.gz    bus arrival predictions at stops within 500 m (only if MTA_BUSTIME_KEY is set)
  data/comparison_stations.csv  which comparison stations go with which landing (rebuilt every run)
A row is written only when a value changes. Rows are held in memory during a run and added to the
day's .gz file at the end, one compressed block per run.
"""
import csv
import gzip
import io
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

ROOT = Path(__file__).parent
DATA = ROOT / "data"
REF = ROOT / "reference" if (ROOT / "reference").is_dir() else ROOT  # works with or without the folder
STATE = DATA / "state.json"
NY = ZoneInfo("America/New_York")

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", 9))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", 30))
LOOKAHEAD_S = 30 * 60   # keep ferry predictions for arrivals in the next 30 min
LOOKBACK_S = 10 * 60    # and up to 10 min after
COMPARE_MIN_M = float(os.environ.get("COMPARE_MIN_M", 800))    # comparison stations: at least this far
COMPARE_MAX_M = float(os.environ.get("COMPARE_MAX_M", 1500))   # from every landing, and within this of one

FERRY_TU = "https://nycferry.connexionz.net/rtt/public/utility/gtfsrealtime.aspx/tripupdate"
GBFS = "https://gbfs.citibikenyc.com/gbfs/gbfs.json"
GBFS_STATUS_FALLBACK = "https://gbfs.citibikenyc.com/gbfs/en/station_status.json"
BUS_TU = "https://gtfsrt.prod.obanyc.com/tripUpdates?key={key}"
BUS_KEY = os.environ.get("MTA_BUSTIME_KEY", "").strip()

FIELDS = {
    "ferry": ["polled_at", "feed_ts", "trip_id", "start_date", "route_id", "stop_id",
              "stop_sequence", "arrival", "departure", "delay"],
    "bike": ["polled_at", "station_id", "last_reported", "bikes", "ebikes", "docks",
             "is_renting", "is_returning"],
    "bus": ["polled_at", "feed_ts", "trip_id", "start_date", "route_id", "stop_id",
            "arrival", "departure"],
}

S = requests.Session()
S.headers["User-Agent"] = "NYU Wagner student research: ferry first/last mile (GitHub Actions)"


def read_ids(name, col):
    with open(REF / name, newline="") as f:
        return {r[col] for r in csv.DictReader(f)}


LANDINGS = read_ids("ferry_landings.csv", "stop_id")
STATIONS = read_ids("bike_stations_near_ferry.csv", "station_id")
BUS_STOPS = read_ids("bus_stops_near_ferry.csv", "stop_id")
COMPARE_FILE = DATA / "comparison_stations.csv"
COMPARE_FIELDS = ["landing_id", "landing_name", "station_id", "short_name", "station_name",
                  "capacity", "lat", "lon", "distance_m"]

BUFFER = {}   # (day, kind) -> rows collected this run


def append(kind, rows):
    if rows:
        BUFFER.setdefault((datetime.now(NY).strftime("%Y-%m-%d"), kind), []).extend(rows)


def add_block(path, header, text_rows):
    """Append one gzip block to path. Concatenated gzip blocks read back as one file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    if not path.exists():
        buf.write(header)
    buf.writelines(text_rows)
    with open(path, "ab") as f:
        f.write(gzip.compress(buf.getvalue().encode(), compresslevel=9))


def flush():
    for (day, kind), rows in BUFFER.items():
        out = io.StringIO()
        csv.writer(out).writerows(rows)
        hdr = io.StringIO()
        csv.writer(hdr).writerow(FIELDS[kind])
        add_block(DATA / day / f"{kind}.csv.gz", hdr.getvalue(), [out.getvalue()])
    BUFFER.clear()


def compress_old_csvs():
    """Move any plain data/*/*.csv left from before compression into the .gz files."""
    for p in sorted(DATA.glob("*/*.csv")):
        lines = p.read_text().splitlines(keepends=True)
        if lines:
            add_block(p.with_suffix(".csv.gz"), lines[0], lines[1:])
        p.unlink()


def dist_m(lat1, lon1, lat2, lon2):
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2 + math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(a))


def comparison_stations(feeds):
    """Stations 800 m to 1.5 km from a landing and at least 800 m from every landing.
    Rebuilt from Citi Bike's station list each run, so new stations are picked up. If the list
    can't be fetched, the last saved file is used."""
    try:
        url = next(f["url"] for f in feeds if f["name"] == "station_information")
        info = S.get(url, timeout=25).json()["data"]["stations"]
        with open(REF / "ferry_landings.csv", newline="") as f:
            lands = [(r["stop_id"], r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
                     for r in csv.DictReader(f)]
        rows = []
        for st in info:
            if st["station_id"] in STATIONS or st.get("lat") is None:
                continue
            d = [(dist_m(la, lo, st["lat"], st["lon"]), lid, name) for lid, name, la, lo in lands]
            if min(x[0] for x in d) < COMPARE_MIN_M:
                continue
            for m, lid, name in d:
                if m <= COMPARE_MAX_M:
                    rows.append([lid, name, st["station_id"], st.get("short_name"), st.get("name"),
                                 st.get("capacity"), st["lat"], st["lon"], round(m)])
        if rows:
            DATA.mkdir(exist_ok=True)
            with open(COMPARE_FILE, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(COMPARE_FIELDS)
                w.writerows(sorted(rows, key=lambda r: (r[1], r[8])))
            return {r[2] for r in rows}
    except Exception as e:
        print("comparison stations: could not refresh,", type(e).__name__, e, flush=True)
    if COMPARE_FILE.exists():
        return read_ids_path(COMPARE_FILE, "station_id")
    return set()


def read_ids_path(path, col):
    with open(path, newline="") as f:
        return {r[col] for r in csv.DictReader(f)}


def fetch_rt(url):
    r = S.get(url, timeout=25)
    r.raise_for_status()
    m = gtfs_realtime_pb2.FeedMessage()
    m.ParseFromString(r.content)
    return m


def stop_updates(msg, keep):
    for e in msg.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        for s in tu.stop_time_update:
            if s.stop_id not in keep:
                continue
            arr = s.arrival.time if s.HasField("arrival") and s.arrival.time else None
            dep = s.departure.time if s.HasField("departure") and s.departure.time else None
            delay = s.arrival.delay if s.HasField("arrival") and s.arrival.HasField("delay") else None
            yield tu.trip, s, arr, dep, delay


def gbfs_feeds():
    try:
        d = S.get(GBFS, timeout=20).json()["data"]
        return d.get("en", next(iter(d.values())))["feeds"]
    except Exception:
        return [{"name": "station_status", "url": GBFS_STATUS_FALLBACK},
                {"name": "station_information", "url": GBFS_STATUS_FALLBACK.replace("station_status", "station_information")}]


def status_url(feeds):
    return next((f["url"] for f in feeds if f["name"] == "station_status"), GBFS_STATUS_FALLBACK)


def poll_ferry(state, now):
    m = fetch_rt(FERRY_TU)
    rows = []
    for trip, s, arr, dep, delay in stop_updates(m, LANDINGS):
        t = arr or dep
        if t is None or not (now - LOOKBACK_S <= t <= now + LOOKAHEAD_S):
            continue
        k = f"{trip.trip_id}|{trip.start_date}|{s.stop_id}"
        v = [arr, dep]
        if state.get(k) != v:
            state[k] = v
            rows.append([now, m.header.timestamp, trip.trip_id, trip.start_date, trip.route_id,
                         s.stop_id, s.stop_sequence, arr, dep, delay])
    append("ferry", rows)
    return len(rows)


def poll_bike(state, now, url, keep):
    rows = []
    for s in S.get(url, timeout=25).json()["data"]["stations"]:
        sid = s["station_id"]
        if sid not in keep:
            continue
        v = [s.get("num_bikes_available"), s.get("num_ebikes_available"), s.get("num_docks_available")]
        if state.get(sid) != v:
            state[sid] = v
            rows.append([now, sid, s.get("last_reported"), *v,
                         int(bool(s.get("is_renting", 1))), int(bool(s.get("is_returning", 1)))])
    append("bike", rows)
    return len(rows)


def poll_bus(state, now):
    m = fetch_rt(BUS_TU.format(key=BUS_KEY))
    rows = []
    for trip, s, arr, dep, _ in stop_updates(m, BUS_STOPS):
        t = arr or dep
        if t is None or not (now - LOOKBACK_S <= t <= now + LOOKAHEAD_S):
            continue
        k = f"{trip.trip_id}|{trip.start_date}|{s.stop_id}"
        v = [arr, dep]
        if state.get(k) != v:
            state[k] = v
            rows.append([now, m.header.timestamp, trip.trip_id, trip.start_date, trip.route_id,
                         s.stop_id, arr, dep])
    append("bus", rows)
    return len(rows)


def main():
    DATA.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for k in ("ferry", "bike", "bus"):
        state.setdefault(k, {})
    compress_old_csvs()
    feeds = gbfs_feeds()
    url = status_url(feeds)
    compare = comparison_stations(feeds)
    keep = STATIONS | compare
    print(f"Citi Bike stations: {len(STATIONS)} near ferry, {len(compare)} comparison", flush=True)
    end = time.time() + RUN_MINUTES * 60
    try:
        poll_loop(state, url, keep, end)
    finally:
        flush()   # save what was collected even if the loop stops early
    # drop ferry/bus state for events more than 2 hours old so the file stays small
    cutoff = time.time() - 7200
    for kind in ("ferry", "bus"):
        state[kind] = {k: v for k, v in state[kind].items() if (v[0] or v[1] or 0) > cutoff}
    STATE.write_text(json.dumps(state))


def poll_loop(state, url, keep, end):
    while True:
        now = int(time.time())
        counts = {}
        for kind, fn in [("ferry", lambda: poll_ferry(state["ferry"], now)),
                         ("bike", lambda: poll_bike(state["bike"], now, url, keep)),
                         ("bus", lambda: poll_bus(state["bus"], now) if BUS_KEY else 0)]:
            try:
                counts[kind] = fn()
            except Exception as e:  # keep going if one feed hiccups
                counts[kind] = f"error: {type(e).__name__}: {e}"[:160]
        print(datetime.now(NY).strftime("%H:%M:%S"), counts, flush=True)
        if time.time() + POLL_SECONDS > end:
            break
        time.sleep(max(1, POLL_SECONDS - (time.time() - now)))


if __name__ == "__main__":
    main()
