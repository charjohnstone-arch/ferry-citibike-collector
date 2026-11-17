"""Turn the collected CSVs into analysis tables.

Run after you download the repository (or in Google Colab):
    pip install pandas numpy
    python analyze.py                                  # live data only
    python analyze.py --trips 202611-citibike-tripdata*.csv   # add Citi Bike trip files when posted

Outputs (out/):
  ferry_arrivals.csv     one row per observed landing: actual time, scheduled time, minutes late
  dock_change.csv        landing x nearby station: bikes and docks just before and 15 min after,
                         plus the usual 15 min change at that station and hour (baseline)
  landing_summary.csv    one row per landing: arrivals seen, avg extra bikes taken after a landing
  trip_response.csv      (with --trips) trips starting 0-15 min after each landing vs baseline
  bus_connection.csv     (if bus data was collected) wait for the next actual bus after each landing
"""
import argparse
import glob
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA, OUT = ROOT / "data", ROOT / "out"
REF = ROOT / "reference" if (ROOT / "reference").is_dir() else ROOT  # works with or without the folder
GTFS = REF / "ferry_gtfs" if (REF / "ferry_gtfs").is_dir() else REF
TZ = "America/New_York"
AFTER = 15     # minutes after a landing counted as "leaving the ferry"
BEFORE = 20    # minutes before a landing for riders arriving to board
WALK_MPS = 1.3


def ns(s):
    return s.astype(f"datetime64[ns, {TZ}]")


def local(epoch):
    return ns(pd.to_datetime(epoch, unit="s", utc=True).dt.tz_convert(TZ))


def load(kind):
    files = sorted(glob.glob(str(DATA / "*" / f"{kind}.csv")))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_csv(f, dtype={"stop_id": str, "trip_id": str, "start_date": str,
                                            "station_id": str, "route_id": str}) for f in files],
                     ignore_index=True)


def observed_events(df):
    """Last prediction logged before the vessel/bus got there = best estimate of the actual time."""
    df = df.copy()
    df["event"] = df["arrival"].fillna(df["departure"])
    df = df.dropna(subset=["event"])
    # NYC Ferry leaves start_date blank; use the New York date of the event instead
    day = pd.to_datetime(df["event"], unit="s", utc=True).dt.tz_convert(TZ).dt.strftime("%Y%m%d")
    df["start_date"] = df["start_date"].where(df["start_date"].notna() & (df["start_date"] != ""), day)
    last_poll = df.polled_at.max()
    df = df[df.polled_at <= df.event + 120]
    df = df.sort_values("polled_at").groupby(["trip_id", "start_date", "stop_id"], as_index=False).last()
    df = df[df.event - df.polled_at <= 600]
    return df[df.event <= last_poll]   # boat hadn't arrived yet when collection stopped


def gtfs_secs(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def main(trip_files):
    OUT.mkdir(exist_ok=True)
    landings = pd.read_csv(REF / "ferry_landings.csv", dtype={"stop_id": str})
    pairs = pd.read_csv(REF / "bike_stations_near_ferry.csv", dtype={"landing_id": str, "station_id": str, "short_name": str})

    # 1. Ferry arrivals
    fp = load("ferry")
    if fp.empty:
        print("no ferry data yet"); return
    arr = observed_events(fp).rename(columns={"stop_id": "landing_id"})
    # NYC Ferry's live feed leaves route_id blank; take it from the schedule
    if (GTFS / "trips.txt").exists():
        rt = pd.read_csv(GTFS / "trips.txt", dtype=str)[["trip_id", "route_id"]].drop_duplicates("trip_id")
        arr = arr.drop(columns="route_id").merge(rt, on="trip_id", how="left")
    arr = arr.merge(landings.rename(columns={"stop_id": "landing_id", "stop_name": "landing_name"})[["landing_id", "landing_name"]], on="landing_id", how="left")
    arr["actual"] = local(arr.event)
    st = pd.read_csv(GTFS / "stop_times.txt", dtype=str)[["trip_id", "stop_id", "arrival_time"]]
    arr = arr.merge(st.rename(columns={"stop_id": "landing_id", "arrival_time": "sched_time"}), on=["trip_id", "landing_id"], how="left")
    base = pd.to_datetime(arr.start_date, format="%Y%m%d").dt.tz_localize(TZ)
    secs = arr.sched_time.map(lambda t: gtfs_secs(t) if isinstance(t, str) else np.nan)
    arr["scheduled"] = ns(base + pd.to_timedelta(secs, unit="s"))
    arr["min_late"] = ((arr.actual - arr.scheduled).dt.total_seconds() / 60).round(1)
    arr = arr.sort_values("actual").reset_index(drop=True)
    arr["arrival_id"] = arr.index
    arr = arr[["arrival_id", "landing_id", "landing_name", "route_id", "trip_id", "actual", "scheduled", "min_late"]]
    arr.to_csv(OUT / "ferry_arrivals.csv", index=False)
    print(f"ferry arrivals observed: {len(arr)}")

    ax = arr.merge(pairs.drop(columns=["landing_name"]), on="landing_id")

    # 2. Dock change around each landing, against the usual change at that station and hour
    bk = load("bike")
    if not bk.empty and len(ax):
        bk["t"] = local(bk.polled_at)
        # minute-by-minute bike counts per station, carried forward between changes
        grid = (bk.sort_values("t").set_index("t").groupby("station_id")["bikes"]
                  .resample("1min").last().groupby(level=0).ffill().reset_index())
        grid["chg"] = grid.groupby("station_id")["bikes"].shift(-AFTER) - grid["bikes"]
        grid["hour"], grid["wkend"] = grid.t.dt.hour, grid.t.dt.dayofweek >= 5
        # baseline uses only windows that don't overlap a landing at a landing this station serves;
        # where there are fewer than 10 such minutes for a station-hour, fall back to all windows
        lands_by_station = pairs.groupby("station_id").landing_id.apply(set)
        t_by_landing = {k: np.sort(v.actual.values) for k, v in arr.groupby("landing_id")}
        def clean(g):
            ts = g.t.values
            ok = np.ones(len(ts), bool)
            for lid in lands_by_station.get(g.name, ()):
                a = t_by_landing.get(lid)
                if a is None:
                    continue
                i = np.searchsorted(a, ts)
                prev = a[np.clip(i - 1, 0, len(a) - 1)]
                nxt = a[np.clip(i, 0, len(a) - 1)]
                w = np.timedelta64(AFTER, "m")
                ok &= ~(((ts - prev) >= np.timedelta64(0, "m")) & ((ts - prev) < w)) & ~(((nxt - ts) >= np.timedelta64(0, "m")) & ((nxt - ts) < w))
            return pd.Series(ok, index=g.index)
        grid["clean"] = grid.groupby("station_id", group_keys=False).apply(clean)
        allb = grid.groupby(["station_id", "hour", "wkend"])["chg"].mean().rename("all")
        cl = grid[grid.clean].groupby(["station_id", "hour", "wkend"])["chg"].agg(["mean", "count"])
        base = pd.concat([allb, cl], axis=1)
        base["usual_change"] = np.where(base["count"].fillna(0) >= 10, base["mean"], base["all"])
        base = base[["usual_change"]].reset_index()
        g2 = grid[["station_id", "t", "bikes"]].sort_values("t")
        a = ax.sort_values("actual").copy()
        a = pd.merge_asof(a, g2.rename(columns={"bikes": "bikes_before"}), left_on="actual", right_on="t",
                          by="station_id", direction="backward", allow_exact_matches=False).drop(columns="t")
        a["t_after"] = ns(a.actual + timedelta(minutes=AFTER))
        a = pd.merge_asof(a.sort_values("t_after"), g2.rename(columns={"bikes": "bikes_after"}), left_on="t_after",
                          right_on="t", by="station_id", direction="backward").drop(columns="t")
        a["net_change"] = a.bikes_after - a.bikes_before   # negative = more bikes taken than returned
        a["hour"], a["wkend"] = a.actual.dt.hour, a.actual.dt.dayofweek >= 5
        a = a.merge(base, on=["station_id", "hour", "wkend"], how="left")
        a["extra_taken"] = -(a.net_change - a.usual_change)   # positive = more bikes left than usual
        keep = ["arrival_id", "landing_name", "route_id", "actual", "station_name", "distance_m", "capacity",
                "bikes_before", "bikes_after", "net_change", "usual_change", "extra_taken"]
        a = a[keep].sort_values(["actual", "distance_m"])
        a.to_csv(OUT / "dock_change.csv", index=False)

        per = a.groupby(["arrival_id", "landing_name"], as_index=False)["extra_taken"].sum()
        summ = per.groupby("landing_name").agg(arrivals=("arrival_id", "nunique"),
                                               avg_extra_bikes_taken=("extra_taken", "mean")).round(2)
        summ = summ.join(pairs.groupby("landing_name").agg(stations_500m=("station_id", "nunique"),
                                                           docks_500m=("capacity", "sum"),
                                                           closest_station_m=("distance_m", "min")))
        summ.reset_index().to_csv(OUT / "landing_summary.csv", index=False)
        print(summ.to_string())

    # 3. Citi Bike trip files: actual trips starting right after a landing
    if trip_files and len(ax):
        tr = pd.concat([pd.read_csv(f, dtype=str, usecols=["started_at", "ended_at", "start_station_id", "end_station_id"])
                        for f in trip_files], ignore_index=True)
        for c in ["started_at", "ended_at"]:
            tr[c] = ns(pd.to_datetime(tr[c], format="mixed").dt.tz_localize(TZ, ambiguous="NaT", nonexistent="NaT"))
        near = set(pairs.short_name)
        starts = tr[tr.start_station_id.isin(near)].rename(columns={"start_station_id": "short_name", "started_at": "t"})[["short_name", "t"]]
        ends = tr[tr.end_station_id.isin(near)].rename(columns={"end_station_id": "short_name", "ended_at": "t"})[["short_name", "t"]]
        lo, hi = tr.started_at.min(), tr.started_at.max()
        days = pd.Series(pd.date_range(lo.normalize(), hi.normalize(), freq="D").dayofweek >= 5).value_counts()

        def baseline(ev, minutes):
            ev = ev.assign(hour=ev.t.dt.hour, wkend=ev.t.dt.dayofweek >= 5)
            b = ev.groupby(["short_name", "hour", "wkend"]).size().rename("n").reset_index()
            b["per_window"] = b.n / b.wkend.map(days) * minutes / 60
            return b.drop(columns="n")

        def count(ev, t0, t1, sn):
            g = {k: np.sort(v.t.values) for k, v in ev.groupby("short_name")}
            return [0 if g.get(s) is None else int(np.searchsorted(g[s], b.to_datetime64()) - np.searchsorted(g[s], a.to_datetime64()))
                    for s, a, b in zip(sn, t0, t1)]

        x = ax[(ax.actual >= lo) & (ax.actual <= hi)].copy()
        if len(x):
            x["trips_started_after"] = count(starts, x.actual, x.actual + timedelta(minutes=AFTER), x.short_name)
            x["trips_ended_before"] = count(ends, x.actual - timedelta(minutes=BEFORE), x.actual, x.short_name)
            x["hour"], x["wkend"] = x.actual.dt.hour, x.actual.dt.dayofweek >= 5
            x = x.merge(baseline(starts, AFTER).rename(columns={"per_window": "usual_started"}), on=["short_name", "hour", "wkend"], how="left")
            x = x.merge(baseline(ends, BEFORE).rename(columns={"per_window": "usual_ended"}), on=["short_name", "hour", "wkend"], how="left")
            x[["usual_started", "usual_ended"]] = x[["usual_started", "usual_ended"]].fillna(0)
            x["extra_started"] = x.trips_started_after - x.usual_started
            x["extra_ended"] = x.trips_ended_before - x.usual_ended
            x[["arrival_id", "landing_name", "route_id", "actual", "station_name", "distance_m",
               "trips_started_after", "usual_started", "extra_started",
               "trips_ended_before", "usual_ended", "extra_ended"]].to_csv(OUT / "trip_response.csv", index=False)
            print(f"trip response rows: {len(x)}")
        else:
            print("no observed landings fall inside the trip file dates")

    # 4. Bus: next actual bus after each landing (only if bus data was collected)
    bp = load("bus")
    if not bp.empty:
        bus_pairs = pd.read_csv(REF / "bus_stops_near_ferry.csv", dtype={"stop_id": str})
        lid = landings.set_index("stop_name").stop_id
        bus_pairs["landing_id"] = bus_pairs.nearest_landing.map(lid)
        ab = observed_events(bp)
        ab["actual_bus"] = local(ab.event)
        ab = ab[["stop_id", "route_id", "actual_bus"]].sort_values("actual_bus")
        bx = arr.drop(columns=["route_id", "trip_id"]).merge(bus_pairs[["landing_id", "stop_id", "stop_name", "distance_m"]], on="landing_id")
        bx["ready"] = ns(bx.actual + pd.to_timedelta(bx.distance_m / WALK_MPS, unit="s"))
        bx = bx.merge(ab[["stop_id", "route_id"]].drop_duplicates(), on="stop_id")
        bx = pd.merge_asof(bx.sort_values("ready"), ab, left_on="ready", right_on="actual_bus",
                           by=["stop_id", "route_id"], direction="forward")
        bx["wait_min"] = ((bx.actual_bus - bx.ready).dt.total_seconds() / 60).round(1)
        bx.sort_values(["arrival_id", "wait_min"]).to_csv(OUT / "bus_connection.csv", index=False)
        print(f"bus connection rows: {len(bx)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", nargs="*", default=[])
    files = [f for p in ap.parse_args().trips for f in glob.glob(p)]
    main(files)
