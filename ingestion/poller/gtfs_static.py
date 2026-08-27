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
import zipfile

import httpx

from poller.constants import ROUTE_SCOPE_TYPES

GTFS_URL = "https://www3.septa.org/developer/gtfs_public.zip"

# Bus + trolley scope — matches the aggregation filter.
# Type 0 = trolley (subway-surface), 3 = bus, 11 = trolleybus (59/66/75).


# ---------------------------------------------------------------------------
# CSV helper
# ---------------------------------------------------------------------------

def _parse_csv(raw: str, filter_fn=None):
    reader = csv.DictReader(io.StringIO(raw))
    if filter_fn:
        return (row for row in reader if filter_fn(row))
    return reader


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
        if rt not in ROUTE_SCOPE_TYPES:
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


def _parse_stop_times(rows) -> dict[tuple[str, int], dict]:
    """Parse stop_times from a pre-filtered row stream."""
    stop_times = {}
    for r in rows:
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

    # 4. Stop times — filtered to bus/trolley trips (streaming, low memory)
    stop_times_rows = _parse_csv(
        files.get("stop_times.txt", ""),
        filter_fn=lambda r: r["trip_id"] in trip_ids,
    )
    stop_times = _parse_stop_times(stop_times_rows)

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


def get_stored_freshness(data_dir: str) -> str | None:
    """Read the stored Last-Modified value from disk."""
    freshness_path = pathlib.Path(data_dir) / "freshness.txt"
    if not freshness_path.exists():
        return None
    return freshness_path.read_text().strip()

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
