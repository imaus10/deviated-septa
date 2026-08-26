"""GTFS static feed parser — downloads, parses, and stores SEPTA GTFS data.

Parses the GTFS zip into in-memory dicts keyed by primary key(s).
The raw zip is stored locally for restart recovery and S3 archival.

Usage:
    from poller.gtfs_static import check_and_update
    data, changed = check_and_update("data/")
    # data = {"routes": {...}, "trips": {...}, "stops": {...},
    #         "stop_times": {...}, "calendar": {...}}
"""

import csv
import io
import os
import pathlib
import time
import zipfile

import httpx

GTFS_URL = "https://www3.septa.org/developer/gtfs_public.zip"

# Bus + trolley scope — matches the aggregation filter.
# Type 0 = trolley (subway-surface), 3 = bus, 11 = trolleybus (59/66/75).
_ROUTE_SCOPE_TYPES = {0, 3, 11}


# ---------------------------------------------------------------------------
# CSV helper
# ---------------------------------------------------------------------------

def _parse_csv(raw: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(raw)))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_zip() -> bytes:
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        resp = client.get(GTFS_URL, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def fetch_freshness() -> str:
    """HTTP HEAD to get the remote feed's Last-Modified header."""
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        resp = client.head(GTFS_URL, follow_redirects=True)
        resp.raise_for_status()
    lm = resp.headers.get("last-modified")
    if not lm:
        raise ValueError("SEPTA GTFS feed missing Last-Modified header")
    return lm


# ---------------------------------------------------------------------------
# Per-file parsers — each returns a dict keyed by primary key(s)
# ---------------------------------------------------------------------------

def _parse_routes(rows: list[dict]) -> dict[str, dict]:
    routes = {}
    for r in rows:
        rt = int(r.get("route_type", 3))
        if rt not in _ROUTE_SCOPE_TYPES:
            continue
        routes[r["route_id"]] = {
            "route_name": r.get("route_short_name", ""),
            "route_type": rt,
        }
    return routes


def _parse_trips(rows: list[dict], route_ids: set[str]) -> dict[str, dict]:
    trips = {}
    for r in rows:
        if r["route_id"] not in route_ids:
            continue
        trips[r["trip_id"]] = {
            "route_id": r["route_id"],
            "service_id": r["service_id"],
            "direction_id": int(r.get("direction_id", 0)),
            "trip_headsign": r.get("trip_headsign"),
        }
    return trips


def _parse_stops(rows: list[dict]) -> dict[str, dict]:
    stops = {}
    for r in rows:
        stops[r["stop_id"]] = {
            "stop_name": r.get("stop_name", ""),
            "stop_lat": float(r["stop_lat"]) if r.get("stop_lat") else None,
            "stop_lon": float(r["stop_lon"]) if r.get("stop_lon") else None,
        }
    return stops


def _parse_stop_times(rows: list[dict], trip_ids: set[str]) -> dict[tuple[str, int], dict]:
    stop_times = {}
    for r in rows:
        if r["trip_id"] not in trip_ids:
            continue
        key = (r["trip_id"], int(r["stop_sequence"]))
        stop_times[key] = {
            "arrival_time": r.get("arrival_time"),
            "stop_id": r["stop_id"],
        }
    return stop_times


def _parse_calendar(rows: list[dict]) -> dict[str, dict]:
    cal = {}
    for r in rows:
        cal[r["service_id"]] = {
            "monday": int(r.get("monday", 0)),
            "tuesday": int(r.get("tuesday", 0)),
            "wednesday": int(r.get("wednesday", 0)),
            "thursday": int(r.get("thursday", 0)),
            "friday": int(r.get("friday", 0)),
            "saturday": int(r.get("saturday", 0)),
            "sunday": int(r.get("sunday", 0)),
            "start_date": r.get("start_date", ""),
            "end_date": r.get("end_date", ""),
        }
    return cal


# ---------------------------------------------------------------------------
# Main parser — orchestrates the per-file parsers
# ---------------------------------------------------------------------------

def parse_zip(gtfs_zip: bytes) -> dict:
    """Parse SEPTA GTFS zip into in-memory dicts.

    Returns dict with keys: routes, trips, stops, stop_times, calendar.
    Only bus/trolley routes are included (route_type IN 0, 3, 11).
    Trips and stop_times are filtered to those routes.
    Stops are NOT filtered (a stop may serve both bus and rail).
    """
    with zipfile.ZipFile(io.BytesIO(gtfs_zip)) as outer:
        inner_raw = outer.read("google_bus.zip")
        with zipfile.ZipFile(io.BytesIO(inner_raw)) as z:
            files = {name: z.read(name).decode("utf-8-sig") for name in z.namelist()}

    # 1. Routes — filtered to bus/trolley
    routes = _parse_routes(_parse_csv(files.get("routes.txt", "")))
    route_ids = set(routes.keys())

    # 2. Trips — filtered to bus/trolley routes
    trips = _parse_trips(_parse_csv(files.get("trips.txt", "")), route_ids)
    trip_ids = set(trips.keys())

    # 3. Stops — all stops (unfiltered)
    stops = _parse_stops(_parse_csv(files.get("stops.txt", "")))

    # 4. Stop times — filtered to bus/trolley trips
    stop_times = _parse_stop_times(_parse_csv(files.get("stop_times.txt", "")), trip_ids)

    # 5. Calendar — all
    calendar = _parse_calendar(_parse_csv(files.get("calendar.txt", "")))

    return {
        "routes": routes,
        "trips": trips,
        "stops": stops,
        "stop_times": stop_times,
        "calendar": calendar,
    }


# ---------------------------------------------------------------------------
# Local storage — save/load raw zip + freshness
# ---------------------------------------------------------------------------

def _save_zip(data_dir: str, zip_bytes: bytes, last_modified: str) -> None:
    """Save the raw GTFS zip and freshness marker to disk."""
    d = pathlib.Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.zip").write_bytes(zip_bytes)
    (d / "freshness.txt").write_text(last_modified)


def load_local(data_dir: str) -> dict:
    """Load and parse GTFS data from the local zip on disk."""
    d = pathlib.Path(data_dir)
    zip_path = d / "latest.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"No GTFS zip found at {zip_path}")
    return parse_zip(zip_path.read_bytes())


# get_stored_freshness() is defined in the DB layer below — handles both
# string paths (local) and connection objects (Postgres).


# ---------------------------------------------------------------------------
# Orchestrator — check freshness, download if needed, return parsed data
# ---------------------------------------------------------------------------

def check_and_update(data_dir: str) -> tuple[dict, bool]:
    """Check if the GTFS feed has updated, download if so, return parsed data.

    Returns (data, changed) where:
      - data is the parsed GTFS dicts
      - changed is True if a new feed was downloaded
    """
    remote_freshness = fetch_freshness()
    stored_freshness = get_stored_freshness(data_dir)

    if remote_freshness == stored_freshness and (pathlib.Path(data_dir) / "latest.zip").exists():
        data = load_local(data_dir)
        return data, False

    print("  downloading GTFS static data...", flush=True)
    zip_bytes = download_zip()
    _save_zip(data_dir, zip_bytes, remote_freshness)
    data = parse_zip(zip_bytes)
    return data, True


# ---------------------------------------------------------------------------
# DB import layer — temporary compatibility for local testing with Postgres.
# These functions upsert parsed data into Postgres tables via db.py.
# ---------------------------------------------------------------------------

def _db_import_routes(conn, routes: dict[str, dict]):
    from poller.db import upsert_table
    rows = [{"route_id": k, "route_short_name": v["route_name"],
             "route_long_name": None, "route_type": v["route_type"]}
            for k, v in routes.items()]
    upsert_table(conn, "routes", rows, pk_cols=["route_id"])


def _db_import_trips(conn, trips: dict[str, dict]):
    from poller.db import upsert_table
    rows = [{"trip_id": k, "route_id": v["route_id"], "service_id": v["service_id"],
             "direction_id": v["direction_id"], "trip_headsign": v["trip_headsign"]}
            for k, v in trips.items()]
    upsert_table(conn, "trips", rows, pk_cols=["trip_id"])


def _db_import_stops(conn, stops: dict[str, dict]):
    from poller.db import upsert_table
    rows = [{"stop_id": k, "stop_name": v["stop_name"],
             "stop_lat": v["stop_lat"], "stop_lon": v["stop_lon"]}
            for k, v in stops.items()]
    upsert_table(conn, "stops", rows, pk_cols=["stop_id"])


def _db_import_stop_times(conn, stop_times: dict[tuple[str, int], dict]):
    from poller.db import copy_upsert_chunked
    def _rows():
        for (trip_id, seq), v in stop_times.items():
            yield {"trip_id": trip_id, "stop_sequence": seq,
                   "arrival_time": v["arrival_time"], "departure_time": None,
                   "stop_id": v["stop_id"], "pickup_type": None, "drop_off_type": None}
    copy_upsert_chunked(conn, "stop_times", _rows(), pk_cols=["trip_id", "stop_sequence"])


def _db_import_calendar(conn, calendar: dict[str, dict]):
    from poller.db import upsert_table
    rows = [{"service_id": k, **v} for k, v in calendar.items()]
    upsert_table(conn, "calendar", rows, pk_cols=["service_id"])


def run(db, gtfs_zip=None):
    """Download (if needed), parse, and upsert GTFS static into Postgres."""
    if gtfs_zip is None:
        print("Downloading GTFS static data from SEPTA...", flush=True)
        gtfs_zip = download_zip()

    data = parse_zip(gtfs_zip)
    t0 = time.perf_counter()
    _db_import_routes(db, data["routes"])
    print(f"  imported {len(data['routes'])} routes in {time.perf_counter() - t0:.1f}s", flush=True)
    t0 = time.perf_counter()
    _db_import_trips(db, data["trips"])
    print(f"  imported {len(data['trips'])} trips in {time.perf_counter() - t0:.1f}s", flush=True)
    t0 = time.perf_counter()
    _db_import_stops(db, data["stops"])
    print(f"  imported {len(data['stops'])} stops in {time.perf_counter() - t0:.1f}s", flush=True)
    t0 = time.perf_counter()
    _db_import_stop_times(db, data["stop_times"])
    print(f"  imported {len(data['stop_times'])} stop_times in {time.perf_counter() - t0:.1f}s", flush=True)
    t0 = time.perf_counter()
    _db_import_calendar(db, data["calendar"])
    print(f"  imported {len(data['calendar'])} calendar entries in {time.perf_counter() - t0:.1f}s", flush=True)

    return {k: len(v) for k, v in data.items()}


def is_static_loaded(db):
    with db.cursor() as cur:
        cur.execute("SELECT route_id FROM routes LIMIT 1")
        return cur.fetchone() is not None


def get_freshness() -> str:
    return fetch_freshness()


def get_stored_freshness(conn_or_data_dir) -> str | None:
    """Read stored freshness from DB connection or local data directory path."""
    if isinstance(conn_or_data_dir, str):
        freshness_path = pathlib.Path(conn_or_data_dir) / "freshness.txt"
        if not freshness_path.exists():
            return None
        return freshness_path.read_text().strip()
    with conn_or_data_dir.cursor() as cur:
        cur.execute("SELECT last_modified FROM service_cycle ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None


def update_freshness(conn, last_modified: str | None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO service_cycle (last_modified, checked_at) VALUES (%s, NOW())",
            (last_modified,),
        )
        conn.commit()


def run_and_record_freshness(conn, gtfs_zip=None):
    from poller.route_geometries import regenerate_route_geometries
    counts = run(conn, gtfs_zip)
    regenerate_route_geometries(conn)
    last_modified = fetch_freshness()
    update_freshness(conn, last_modified)
    return counts
