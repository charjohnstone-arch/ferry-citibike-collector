"""Poll NYC Ferry, Citi Bike and (optionally) MTA bus feeds for a few minutes, appending to daily CSVs.

GitHub Actions starts this every 5 minutes. Each run polls every POLL_SECONDS for RUN_MINUTES,
so back to back runs give near continuous coverage.

Output, one folder per New York date:
  data/YYYY-MM-DD/ferry.csv  NYC Ferry arrival predictions for landings in the next 30 minutes
  data/YYYY-MM-DD/bike.csv   Citi Bike bikes / e-bikes / open docks at stations within 500 m of a landing
  data/YYYY-MM-DD/bus.csv    bus arrival predictions at stops within 500 m (only if MTA_BUSTIME_KEY is set)
A row is written only when a value changes, so files stay small.
"""
import csv
import json
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


def append(kind, rows):
    if not rows:
        return
    day = datetime.now(NY).strftime("%Y-%m-%d")
    path = DATA / day / f"{kind}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS[kind])
        w.writerows(rows)


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


def status_url():
    try:
        d = S.get(GBFS, timeout=20).json()["data"]
        feeds = d.get("en", next(iter(d.values())))["feeds"]
        return next(f["url"] for f in feeds if f["name"] == "station_status")
    except Exception:
        return GBFS_STATUS_FALLBACK


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


def poll_bike(state, now, url):
    rows = []
    for s in S.get(url, timeout=25).json()["data"]["stations"]:
        sid = s["station_id"]
        if sid not in STATIONS:
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
    url = status_url()
    end = time.time() + RUN_MINUTES * 60
    while True:
        now = int(time.time())
        counts = {}
        for kind, fn in [("ferry", lambda: poll_ferry(state["ferry"], now)),
                         ("bike", lambda: poll_bike(state["bike"], now, url)),
                         ("bus", lambda: poll_bus(state["bus"], now) if BUS_KEY else 0)]:
            try:
                counts[kind] = fn()
            except Exception as e:  # keep going if one feed hiccups
                counts[kind] = f"error: {type(e).__name__}: {e}"[:160]
        print(datetime.now(NY).strftime("%H:%M:%S"), counts, flush=True)
        if time.time() + POLL_SECONDS > end:
            break
        time.sleep(max(1, POLL_SECONDS - (time.time() - now)))
    # drop ferry/bus state for events more than 2 hours old so the file stays small
    cutoff = time.time() - 7200
    for kind in ("ferry", "bus"):
        state[kind] = {k: v for k, v in state[kind].items() if (v[0] or v[1] or 0) > cutoff}
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
