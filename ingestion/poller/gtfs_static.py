"""GTFS static feed parser — downloads, parses, and stores SEPTA GTFS data.

The heavy feed tables (stop_times ~2.1M rows, trips) are streamed into a local
SQLite DB (`StaticDB`) once per feed change — never materialized as Python
dicts, which would spike RSS to ~1.4 GB and OOM the 1 GB Pi on every poll.
Routes/stops/calendar metadata is tiny and stays in-memory where used.

Usage:
    from poller.gtfs_static import check_and_update
    static, changed = check_and_update("data/", "state/static.db")
    arrival = static.stop_time(trip_id, stop_seq)     # {"arrival_time","stop_id"}
    route_id = static.route_for_trip(trip_id)
    metadata = load_local_metadata("data/")           # {"routes","stops","calendar"}
"""

import csv
import io
import os
import pathlib
import sqlite3
import zipfile

import httpx

from poller.constants import ROUTE_SCOPE_TYPES

GTFS_URL = "https://www3.septa.org/developer/gtfs_public.zip"


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


def _parse_stops(rows: list[dict]) -> dict[str, dict]:
    stops = {}
    for r in rows:
        stops[r["stop_id"]] = {
            "stop_name": r.get("stop_name", ""),
            "stop_lat": float(r["stop_lat"]) if r.get("stop_lat") else None,
            "stop_lon": float(r["stop_lon"]) if r.get("stop_lon") else None,
        }
    return stops


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
# Static SQLite store — stop_times + trips (the memory-heavy tables)
# ---------------------------------------------------------------------------

STATIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS stop_times (
    trip_id        text NOT NULL,
    stop_sequence  integer NOT NULL,
    arrival_time   text,
    stop_id        text NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trips (
    trip_id  text PRIMARY KEY,
    route_id text NOT NULL
) WITHOUT ROWID;
"""


class StaticDB:
    """SQLite-backed GTFS static lookups (stop_times + trips).

    Point lookups feed extract_observations during each poll; iteration feeds
    route-geometry generation on refresh. The heavy rows live on disk, never
    materialized as Python dicts.
    """

    def __init__(self, db_path):
        self.db_path = str(db_path)
        pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(STATIC_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def stop_time(self, trip_id: str, stop_sequence: int):
        """Schedule row for (trip_id, stop_sequence) or None.

        Returns {"arrival_time": str, "stop_id": str}, the shape extract
        observations expects.
        """
        row = self.conn.execute(
            "SELECT arrival_time, stop_id FROM stop_times "
            "WHERE trip_id = ? AND stop_sequence = ?",
            (trip_id, stop_sequence),
        ).fetchone()
        if row is None:
            return None
        return {"arrival_time": row[0], "stop_id": row[1]}

    def route_for_trip(self, trip_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT route_id FROM trips WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        return row[0] if row else None

    def iter_stop_times(self):
        """Yield (trip_id, stop_sequence, arrival_time, stop_id)."""
        return self.conn.execute(
            "SELECT trip_id, stop_sequence, arrival_time, stop_id FROM stop_times "
            "ORDER BY trip_id, stop_sequence"
        )

    def iter_trips(self):
        """Yield (trip_id, route_id)."""
        return self.conn.execute("SELECT trip_id, route_id FROM trips")


def _open_inner_zip(data_dir: str):
    """Open google_bus.zip from the local latest.zip (context manager)."""
    d = pathlib.Path(data_dir)
    zip_path = d / "latest.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"No GTFS zip found at {zip_path}")
    outer = zipfile.ZipFile(io.BytesIO(zip_path.read_bytes()))
    inner_raw = outer.read("google_bus.zip")
    outer.close()
    return zipfile.ZipFile(io.BytesIO(inner_raw))


def import_to_sqlite(data_dir: str, db_path: str) -> None:
    """(Re)build the static DB by streaming stop_times + trips from the zip.

    Scoped to bus/trolley routes (route_type IN 0, 3, 11) exactly like the old
    parse_zip. Decodes zip entries incrementally and pipes rows straight into
    executemany — peak RSS stays in the tens of MB instead of the ~1.4 GB the
    in-memory dict representation required.
    """
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("DROP TABLE IF EXISTS stop_times")
        conn.execute("DROP TABLE IF EXISTS trips")
        conn.executescript(STATIC_SCHEMA)

        with _open_inner_zip(data_dir) as z:
            route_ids = set(
                _parse_routes(_parse_csv(z.read("routes.txt").decode("utf-8-sig")))
            )

            with z.open("trips.txt") as f:
                rd = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                conn.executemany(
                    "INSERT OR REPLACE INTO trips (trip_id, route_id) VALUES (?, ?)",
                    (
                        (r["trip_id"], r["route_id"])
                        for r in rd
                        if r["route_id"] in route_ids
                    ),
                )
            conn.commit()
            trip_ids = {row[0] for row in conn.execute("SELECT trip_id FROM trips")}

            with z.open("stop_times.txt") as f:
                rd = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                conn.executemany(
                    "INSERT INTO stop_times "
                    "(trip_id, stop_sequence, arrival_time, stop_id) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        (
                            r["trip_id"],
                            int(r["stop_sequence"]),
                            r.get("arrival_time"),
                            r["stop_id"],
                        )
                        for r in rd
                        if r["trip_id"] in trip_ids
                    ),
                )
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Local storage — save/load raw zip + freshness + tiny metadata
# ---------------------------------------------------------------------------

def _save_zip(data_dir: str, zip_bytes: bytes, last_modified: str) -> None:
    """Save the raw GTFS zip and freshness marker to disk."""
    d = pathlib.Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.zip").write_bytes(zip_bytes)
    (d / "freshness.txt").write_text(last_modified)


def load_local_metadata(data_dir: str) -> dict:
    """Load routes + stops + calendar metadata from the local zip.

    All three are tiny (routes/stops/calendar only — never trips/stop_times),
    so peak memory stays at ~85 MB. Used by build_current (routes/stops
    metadata) and the parquet registries (routes/stops/calendar for validity
    windows). The heavy feed tables come from StaticDB instead.
    """
    with _open_inner_zip(data_dir) as z:
        routes = _parse_routes(_parse_csv(z.read("routes.txt").decode("utf-8-sig")))
        stops = _parse_stops(_parse_csv(z.read("stops.txt").decode("utf-8-sig")))
        calendar = _parse_calendar(_parse_csv(z.read("calendar.txt").decode("utf-8-sig")))
    return {"routes": routes, "stops": stops, "calendar": calendar}


def active_route_ids(data_dir: str) -> set[str]:
    """Route ids present in scoped trips (route_type IN 0, 3, 11) of the local feed.

    SEPTA drops routes by removing their trips while leaving the name row in
    routes.txt, so presence-in-trips is the true "active" test — used to seed
    observation-derived validity windows for dropped routes in the registries.
    """
    with _open_inner_zip(data_dir) as z:
        routes = _parse_routes(_parse_csv(z.read("routes.txt").decode("utf-8-sig")))
        rd = csv.DictReader(io.TextIOWrapper(z.open("trips.txt"), encoding="utf-8-sig"))
        return {r["route_id"] for r in rd if r["route_id"] in routes}


def get_stored_freshness(data_dir: str) -> str | None:
    """Read the stored Last-Modified value from disk."""
    freshness_path = pathlib.Path(data_dir) / "freshness.txt"
    if not freshness_path.exists():
        return None
    return freshness_path.read_text().strip()


def check_and_update(data_dir: str, db_path: str) -> tuple[StaticDB, bool]:
    """Ensure the static feed is current and its SQLite store is built.

    Returns (static, changed): a StaticDB backed by db_path, plus whether a new
    feed was downloaded and imported this call. The un-changed path only opens
    the existing store (no re-import, no feed materialization) — it is the
    every-minute hot path.
    """
    remote_freshness = fetch_freshness()
    stored_freshness = get_stored_freshness(data_dir)

    unchanged = (
        remote_freshness == stored_freshness
        and (pathlib.Path(data_dir) / "latest.zip").exists()
    )
    if unchanged:
        if not pathlib.Path(db_path).exists():
            import_to_sqlite(data_dir, db_path)
        return StaticDB(db_path), False

    print("  downloading GTFS static data...", flush=True)
    zip_bytes = download_zip()
    _save_zip(data_dir, zip_bytes, remote_freshness)
    import_to_sqlite(data_dir, db_path)
    return StaticDB(db_path), True