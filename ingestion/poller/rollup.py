"""Current-day rollup JSON for the frontend.

Builds the current.json shape from the observations store, merging in
route/stop display metadata (names, coords) at write time. Weekly, daily,
and all-time rollups plus S3 upload are deferred to the storage phase.
"""

import json
import pathlib
from datetime import datetime

from poller.constants import EASTERN
from poller.state import to_iso_date


def build_rollup(obs_db, service_date, static: dict) -> dict:
    """Rollup totals for a service date, merged with display metadata.

    Args:
        obs_db: ObservationsDB instance
        service_date: date or ISO 'YYYY-MM-DD' string
        static: gtfs_static parse_zip() dict (routes/stops used for metadata)

    Returns dict with keys: service_date, updated_at, routes, stops.
    """
    routing_totals = obs_db.rollup_routes(service_date)
    stop_totals = obs_db.rollup_stops(service_date)

    source_date = to_iso_date(service_date)

    routes = {
        route_id: {**static["routes"].get(route_id, {}), **totals}
        for route_id, totals in routing_totals.items()
    }
    stops = {
        stop_id: {**static["stops"].get(stop_id, {}), **totals}
        for stop_id, totals in stop_totals.items()
    }

    return {
        "service_date": source_date,
        "updated_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "routes": routes,
        "stops": stops,
    }


def write_current(rollup: dict, out_dir) -> None:
    """Atomically write the current-day rollup to <out_dir>/current.json."""
    d = pathlib.Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "current.json.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(rollup, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(d / "current.json")