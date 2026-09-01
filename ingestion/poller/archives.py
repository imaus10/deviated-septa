"""Parquet archive writers for the eternal S3 ledger.

Layout (S3 `archive/`):
    archive/routes.parquet                  — consolidated append-only route registry
    archive/stops.parquet                   — consolidated append-only stop registry
    archive/observations/YYYY-MM-DD.parquet — raw observations, one file per service date

Observations carry the full store-row shape (route_id/stop_id/category baked in), so the raw
archive is self-sufficient for delay and needs only a registry join for display names.
The writer is source-agnostic: the poller's prune path and the Neon migration both call
`write_observations` with the same store-row tuples.

Registries are single consolidated, append-only files: rows are never deleted. A route/stop
is "valid" across a window [valid_from, valid_to] (valid_to NULL = open). valid_from is the
first service date the id is valid for; valid_to is the last service date it is valid for
(closed when it drops). Join: valid_from <= D AND (valid_to IS NULL OR D <= valid_to).
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Tuples come from ObservationsDB.export_day() in this exact column order.
OBSERVATION_COLUMNS = (
    "trip_id",
    "stop_sequence",
    "service_date",
    "route_id",
    "stop_id",
    "delay_seconds",
    "category",
    "vehicle_id",
    "predicted_time",
    "poll_timestamp",
)

OBSERVATION_SCHEMA = pa.schema(
    [
        ("trip_id", pa.string()),
        ("stop_sequence", pa.int64()),
        ("service_date", pa.string()),
        ("route_id", pa.string()),
        ("stop_id", pa.string()),
        ("delay_seconds", pa.int64()),
        ("category", pa.string()),
        ("vehicle_id", pa.string()),
        ("predicted_time", pa.int64()),
        ("poll_timestamp", pa.int64()),
    ]
)


def write_observations(rows, observations_dir) -> Path:
    """Write raw observation rows for one service date to parquet.

    `rows` are tuples from ObservationsDB.export_day() (empty rows raise). Returns
    the written path, atomically (tmp + rename).
    """
    if not rows:
        raise ValueError("cannot write an empty observations archive")
    service_date = str(rows[0][2])
    col_values = list(zip(*rows))
    table = pa.Table.from_arrays(
        [
            pa.array(v, type=OBSERVATION_SCHEMA.field(i).type)
            for i, v in enumerate(col_values)
        ],
        schema=OBSERVATION_SCHEMA,
    )
    return _atomic_write(table, observations_dir, f"{service_date}.parquet")


# ---------------------------------------------------------------------------
# Registries (routes / stops) — consolidated append-only
# ---------------------------------------------------------------------------

ROUTE_REGISTRY_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("name", pa.string()),
        ("route_type", pa.int64()),
        ("valid_from", pa.string()),
        ("valid_to", pa.string()),
    ]
)

STOP_REGISTRY_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("name", pa.string()),
        ("stop_lat", pa.float64()),
        ("stop_lon", pa.float64()),
        ("valid_from", pa.string()),
        ("valid_to", pa.string()),
    ]
)


def write_routes_registry(routes, archive_dir) -> Path:
    """Write the consolidated append-only route registry.

    `routes` is a dict of {route_id: {"name": ..., "route_type": ...,
    "valid_from": ..., "valid_to": ...}} — the en-window fields are set by the
    caller (poller refresh or migration). Always writes a full snapshot of the
    registry (append new / carry existing rows, never delete). Returns path.
    """
    return _write_registry(routes, ROUTE_REGISTRY_SCHEMA, archive_dir, "routes.parquet")


def write_stops_registry(stops, archive_dir) -> Path:
    return _write_registry(stops, STOP_REGISTRY_SCHEMA, archive_dir, "stops.parquet")


def _calendar_start(data) -> str | None:
    """Earliest start_date across the static calendar, as YYYY-MM-DD or None."""
    starts = [
        v.get("start_date")
        for v in data.get("calendar", {}).values()
        if v.get("start_date")
    ]
    if not starts:
        return None
    earliest = min(starts)
    return f"{earliest[:4]}-{earliest[4:6]}-{earliest[6:8]}"


def build_registries(data) -> tuple[dict, dict]:
    """Build route/stop registry dicts from a static feed dict.

    Returns (routes, stops) shaped for write_routes_registry/write_stops_registry,
    with present ids open-ended (valid_from = feed calendar start, valid_to = NULL).
    """
    valid_from = _calendar_start(data)

    routes = {
        rid: {
            "name": v.get("route_name", ""),
            "route_type": v.get("route_type"),
            "valid_from": valid_from,
            "valid_to": None,
        }
        for rid, v in data.get("routes", {}).items()
    }
    stops = {
        sid: {
            "name": v.get("stop_name", ""),
            "stop_lat": v.get("stop_lat"),
            "stop_lon": v.get("stop_lon"),
            "valid_from": valid_from,
            "valid_to": None,
        }
        for sid, v in data.get("stops", {}).items()
    }
    return routes, stops


def _write_registry(entries: dict, schema: pa.Schema, archive_dir: str, filename: str) -> Path:
    cols = {f.name: [] for f in schema}
    for _id, e in entries.items():
        cols["id"].append(str(_id))
        for field in schema:
            key = field.name
            if key == "id":
                continue
            cols[key].append(e.get(key))
    table = pa.table(cols, schema=schema)
    return _atomic_write(table, archive_dir, filename)


def _atomic_write(table: pa.Table, target_dir: str, filename: str) -> Path:
    d = Path(target_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / filename
    tmp = d / f".{filename}.tmp"
    pq.write_table(table, tmp)
    tmp.replace(out)
    return out


def read_observation(path) -> pa.Table:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no archive at {p}")
    return pq.read_table(p)
