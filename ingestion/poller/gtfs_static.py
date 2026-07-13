import csv
import io
import zipfile

import httpx

from poller.db import get_client, is_pg, json_dumps, upsert_table

GTFS_URL = "https://www3.septa.org/developer/gtfs_public.zip"


def download_zip() -> bytes:
    resp = httpx.get(GTFS_URL, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    return resp.content


def parse_csv(raw: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw))
    return [row for row in reader]


def _prep_routes(rows):
    return [
        {
            "route_id": r["route_id"],
            "route_short_name": r.get("route_short_name", ""),
            "route_long_name": r.get("route_long_name"),
            "route_type": int(r.get("route_type", 3)),
        }
        for r in rows
    ]


def import_routes(db, rows):
    prep = _prep_routes(rows)
    if is_pg(db):
        upsert_table(db, "routes", prep, pk_cols=["route_id"])
        return len(prep)
    batch = []
    for r in prep:
        batch.append(r)
        if len(batch) >= 500:
            resp = db.post("/routes", content=json_dumps(batch),
                           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            resp.raise_for_status()
            batch.clear()
    if batch:
        resp = db.post("/routes", content=json_dumps(batch),
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        resp.raise_for_status()
    return len(prep)


def _prep_trips(rows):
    return [
        {
            "trip_id": r["trip_id"],
            "route_id": r["route_id"],
            "service_id": r["service_id"],
            "direction_id": int(r.get("direction_id", 0)),
            "trip_headsign": r.get("trip_headsign"),
        }
        for r in rows
    ]


def import_trips(db, rows):
    prep = _prep_trips(rows)
    if is_pg(db):
        upsert_table(db, "trips", prep, pk_cols=["trip_id"])
        return len(prep)
    batch = []
    for r in prep:
        batch.append(r)
        if len(batch) >= 2000:
            resp = db.post("/trips", content=json_dumps(batch),
                           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            resp.raise_for_status()
            batch.clear()
    if batch:
        resp = db.post("/trips", content=json_dumps(batch),
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        resp.raise_for_status()
    return len(prep)


def _prep_stops(rows):
    return [
        {
            "stop_id": r["stop_id"],
            "stop_name": r.get("stop_name", ""),
            "stop_lat": float(r["stop_lat"]) if r.get("stop_lat") else None,
            "stop_lon": float(r["stop_lon"]) if r.get("stop_lon") else None,
        }
        for r in rows
    ]


def import_stops(db, rows):
    prep = _prep_stops(rows)
    if is_pg(db):
        upsert_table(db, "stops", prep, pk_cols=["stop_id"])
        return len(prep)
    batch = []
    for r in prep:
        batch.append(r)
        if len(batch) >= 500:
            resp = db.post("/stops", content=json_dumps(batch),
                           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            resp.raise_for_status()
            batch.clear()
    if batch:
        resp = db.post("/stops", content=json_dumps(batch),
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        resp.raise_for_status()
    return len(prep)


def _prep_stop_times(rows):
    return [
        {
            "trip_id": r["trip_id"],
            "stop_sequence": int(r["stop_sequence"]),
            "stop_id": r["stop_id"],
            "arrival_time": r.get("arrival_time"),
            "departure_time": r.get("departure_time"),
            "pickup_type": int(r["pickup_type"]) if r.get("pickup_type") else None,
            "drop_off_type": int(r["drop_off_type"]) if r.get("drop_off_type") else None,
        }
        for r in rows
    ]


def import_stop_times(db, rows):
    prep = _prep_stop_times(rows)
    if is_pg(db):
        upsert_table(db, "stop_times", prep, pk_cols=["trip_id", "stop_sequence"])
        return len(prep)
    batch = []
    total = len(prep)
    last_pct = 0
    for r in prep:
        batch.append(r)
        if len(batch) >= 2000:
            resp = db.post("/stop_times", content=json_dumps(batch),
                           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            resp.raise_for_status()
            pct = (total - len(batch)) * 100 // total
            if pct >= last_pct + 10:
                print(f"  stop_times: {total - len(batch)}/{total} ({pct}%)", flush=True)
                last_pct = pct
            batch.clear()
    if batch:
        resp = db.post("/stop_times", content=json_dumps(batch),
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        resp.raise_for_status()
    return len(prep)


def _prep_calendar(rows):
    return [
        {
            "service_id": r["service_id"],
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
        for r in rows
    ]


def import_calendar(db, rows):
    prep = _prep_calendar(rows)
    if is_pg(db):
        upsert_table(db, "calendar", prep, pk_cols=["service_id"])
        return len(prep)
    batch = []
    for r in prep:
        batch.append(r)
        if len(batch) >= 500:
            resp = db.post("/calendar", content=json_dumps(batch),
                           headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            resp.raise_for_status()
            batch.clear()
    if batch:
        resp = db.post("/calendar", content=json_dumps(batch),
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        resp.raise_for_status()
    return len(prep)


IMPORT_FUNCS = {
    "routes.txt": import_routes,
    "trips.txt": import_trips,
    "stops.txt": import_stops,
    "stop_times.txt": import_stop_times,
    "calendar.txt": import_calendar,
}


def is_static_loaded(db):
    if is_pg(db):
        with db.cursor() as cur:
            cur.execute("SELECT route_id FROM routes LIMIT 1")
            return cur.fetchone() is not None
    resp = db.get("/routes", params={"select": "route_id", "limit": 1})
    resp.raise_for_status()
    return len(resp.json()) > 0


def run(db, gtfs_zip=None):
    if gtfs_zip is None:
        print("Downloading GTFS static data from SEPTA...")
        gtfs_zip = download_zip()

    counts = {}

    with zipfile.ZipFile(io.BytesIO(gtfs_zip)) as outer:
        inner_raw = outer.read("google_bus.zip")
        with zipfile.ZipFile(io.BytesIO(inner_raw)) as z:
            for filename, func in IMPORT_FUNCS.items():
                if filename not in z.namelist():
                    print(f"  skipping {filename} (not in zip)")
                    continue
                raw = z.read(filename).decode("utf-8-sig")
                rows = parse_csv(raw)
                n = func(db, rows)
                counts[filename] = n
                print(f"  imported {n} rows from {filename}")

    return counts
