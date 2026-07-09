import csv
import io
import zipfile
from pathlib import Path

import httpx

from poller.db import Session
from poller.models import Route, Trip, Stop, StopTime, Calendar

GTFS_URL = "https://www3.septa.org/developer/gtfs_public.zip"


def download_zip() -> bytes:
    resp = httpx.get(GTFS_URL, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    return resp.content


def parse_csv(raw: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw))
    return [row for row in reader]


def import_routes(session, rows: list[dict]) -> int:
    count = 0
    for r in rows:
        session.merge(
            Route(
                route_id=r["route_id"],
                route_short_name=r.get("route_short_name", ""),
                route_long_name=r.get("route_long_name"),
                route_type=int(r.get("route_type", 3)),
            )
        )
        count += 1
    return count


def import_trips(session, rows: list[dict]) -> int:
    count = 0
    for r in rows:
        session.merge(
            Trip(
                trip_id=r["trip_id"],
                route_id=r["route_id"],
                service_id=r["service_id"],
                direction_id=int(r.get("direction_id", 0)),
                trip_headsign=r.get("trip_headsign"),
            )
        )
        count += 1
    return count


def import_stops(session, rows: list[dict]) -> int:
    count = 0
    for r in rows:
        session.merge(
            Stop(
                stop_id=r["stop_id"],
                stop_name=r.get("stop_name", ""),
                stop_lat=float(r["stop_lat"]) if r.get("stop_lat") else None,
                stop_lon=float(r["stop_lon"]) if r.get("stop_lon") else None,
            )
        )
        count += 1
    return count


def import_stop_times(session, rows: list[dict]) -> int:
    count = 0
    batch = []
    for r in rows:
        batch.append(
            StopTime(
                trip_id=r["trip_id"],
                stop_sequence=int(r["stop_sequence"]),
                stop_id=r["stop_id"],
                arrival_time=r.get("arrival_time"),
                departure_time=r.get("departure_time"),
                pickup_type=int(r["pickup_type"]) if r.get("pickup_type") else None,
                drop_off_type=int(r["drop_off_type"])
                if r.get("drop_off_type")
                else None,
            )
        )
        count += 1
        if len(batch) >= 1000:
            session.bulk_save_objects(batch)
            session.flush()
            batch.clear()
    if batch:
        session.bulk_save_objects(batch)
        session.flush()
    return count


def import_calendar(session, rows: list[dict]) -> int:
    count = 0
    for r in rows:
        session.merge(
            Calendar(
                service_id=r["service_id"],
                monday=int(r.get("monday", 0)),
                tuesday=int(r.get("tuesday", 0)),
                wednesday=int(r.get("wednesday", 0)),
                thursday=int(r.get("thursday", 0)),
                friday=int(r.get("friday", 0)),
                saturday=int(r.get("saturday", 0)),
                sunday=int(r.get("sunday", 0)),
                start_date=r.get("start_date", ""),
                end_date=r.get("end_date", ""),
            )
        )
        count += 1
    return count


IMPORT_FUNCS = {
    "routes.txt": import_routes,
    "trips.txt": import_trips,
    "stops.txt": import_stops,
    "stop_times.txt": import_stop_times,
    "calendar.txt": import_calendar,
}


def run(gtfs_zip=None):
    if gtfs_zip is None:
        print("Downloading GTFS static data from SEPTA...")
        gtfs_zip = download_zip()

    counts = {}
    session = Session()

    try:
        with zipfile.ZipFile(io.BytesIO(gtfs_zip)) as outer:
            # SEPTA's GTFS is a zip-of-zips — CSVs live inside google_bus.zip.
            # GTFS was created by Google; most agencies name their archive
            # gtfs.zip, but SEPTA kept the legacy google_*.zip convention.
            inner_raw = outer.read("google_bus.zip")
            with zipfile.ZipFile(io.BytesIO(inner_raw)) as z:
                for filename, func in IMPORT_FUNCS.items():
                    if filename not in z.namelist():
                        print(f"  skipping {filename} (not in zip)")
                        continue
                    raw = z.read(filename).decode("utf-8-sig")
                    rows = parse_csv(raw)
                    n = func(session, rows)
                    counts[filename] = n
                    print(f"  imported {n} rows from {filename}")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return counts


if __name__ == "__main__":
    counts = run()
    print(f"\nDone: {counts}")
