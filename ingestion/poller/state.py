"""SQLite-backed observations store + state.json.

The SQLite file is the live dedup store — one row per
(trip_id, stop_sequence, service_date), UPSERTed each poll so the latest
prediction wins. Per-route/per-stop totals are computed with GROUP BY on
demand; the observed rows are the only source of truth.

state.json holds just session metadata (service date + last poll time).
The daily/weekly/all-time rollup files and the parquet raw archive are
written by the storage phase, not here.
"""

import json
import pathlib
import sqlite3
from datetime import datetime

from poller.constants import CATEGORY_COUNT_KEYS

DEFAULT_STATE = {"service_date": None, "last_poll_ts": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    trip_id        text NOT NULL,
    stop_sequence  integer NOT NULL,
    service_date   text NOT NULL,
    route_id       text NOT NULL,
    stop_id        text NOT NULL,
    delay_seconds  integer NOT NULL,
    category       text NOT NULL,
    vehicle_id     text,
    predicted_time integer,
    poll_timestamp integer,
    PRIMARY KEY (trip_id, stop_sequence, service_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_observations_routes ON observations (route_id, category);
CREATE INDEX IF NOT EXISTS idx_observations_stops ON observations (stop_id, category);
"""


def _to_unix(dt) -> int | None:
    return int(dt.timestamp()) if dt else None


def to_iso_date(service_date) -> str:
    if isinstance(service_date, datetime):
        service_date = service_date.date()
    return service_date.isoformat() if hasattr(service_date, "isoformat") else str(service_date)


class ObservationsDB:
    """SQLite observations store — point UPSERTs + GROUP BY rollups."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def upsert(self, rows: list[dict]) -> None:
        """Batch UPSERT observations — latest prediction wins per PK."""
        if not rows:
            return
        self.conn.executemany(
            "INSERT INTO observations (trip_id, stop_sequence, service_date, "
            "  route_id, stop_id, delay_seconds, category, vehicle_id, "
            "  predicted_time, poll_timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (trip_id, stop_sequence, service_date) DO UPDATE SET "
            "  route_id=excluded.route_id, "
            "  stop_id=excluded.stop_id, "
            "  delay_seconds=excluded.delay_seconds, "
            "  category=excluded.category, "
            "  vehicle_id=excluded.vehicle_id, "
            "  predicted_time=excluded.predicted_time, "
            "  poll_timestamp=excluded.poll_timestamp",
            [
                (
                    r["trip_id"],
                    r["stop_sequence"],
                    to_iso_date(r["service_date"]),
                    r["route_id"],
                    r["stop_id"],
                    r["delay_seconds"],
                    r["category"],
                    r.get("vehicle_id"),
                    _to_unix(r.get("predicted_time")),
                    _to_unix(r.get("poll_timestamp")),
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def count(self, service_date=None) -> int:
        if service_date is None:
            return self.conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE service_date = ?",
            (to_iso_date(service_date),),
        ).fetchone()[0]

    def _rollup_by(self, column: str, service_date) -> dict[str, dict]:
        rows = self.conn.execute(
            f"SELECT {column}, category, COUNT(*), SUM(delay_seconds) "
            "FROM observations WHERE service_date = ? "
            f"GROUP BY {column}, category",
            (to_iso_date(service_date),),
        ).fetchall()

        totals = {}
        for key, category, count, delay_sum in rows:
            t = totals.setdefault(
                key,
                {
                    "total_observations": 0,
                    **{k: 0 for k in CATEGORY_COUNT_KEYS},
                    "delay_sum": 0,
                },
            )
            t["total_observations"] += count
            t[f"{category}_count"] += count
            t["delay_sum"] += delay_sum or 0
        return totals

    def rollup_routes(self, service_date) -> dict[str, dict]:
        """Per-route totals for a service date, keyed by route_id."""
        return self._rollup_by("route_id", service_date)

    def rollup_stops(self, service_date) -> dict[str, dict]:
        """Per-stop totals for a service date, keyed by stop_id."""
        return self._rollup_by("stop_id", service_date)

    def export_day(self, service_date) -> list[tuple]:
        """Full rows for a service date (used by the parquet archive pass)."""
        return self.conn.execute(
            "SELECT trip_id, stop_sequence, service_date, route_id, stop_id, "
            "  delay_seconds, category, vehicle_id, predicted_time, poll_timestamp "
            "FROM observations WHERE service_date = ?",
            (to_iso_date(service_date),),
        ).fetchall()

    def delete_service_date(self, service_date) -> None:
        """Drop all rows for a service date (after archiving to parquet)."""
        self.conn.execute(
            "DELETE FROM observations WHERE service_date = ?",
            (to_iso_date(service_date),),
        )
        self.conn.commit()


def load_state(state_dir) -> dict:
    path = pathlib.Path(state_dir) / "state.json"
    if not path.exists():
        return dict(DEFAULT_STATE)
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError:
        return dict(DEFAULT_STATE)
    return {**DEFAULT_STATE, **state}


def save_state(state_dir, service_date, last_poll_ts) -> None:
    d = pathlib.Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    state = {
        "service_date": to_iso_date(service_date) if service_date else None,
        "last_poll_ts": int(last_poll_ts) if last_poll_ts else None,
    }
    (d / "state.json").write_text(json.dumps(state))