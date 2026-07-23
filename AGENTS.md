# Deviated SEPTA — Agent Guide

## What this is

On-time performance dashboard for SEPTA bus+trolley routes. Polls SEPTA's GTFS-RT API every minute, computes delay per stop, aggregates by route, serves a Vue 3 dashboard.

## Architecture

```
SEPTA GTFS-RT API  ──→  poller (Python, runs on Raspberry Pi via cron)
SEPTA GTFS static   ──→        │
                                ▼
                          Neon PostgreSQL
                                │
                                ▼
                    Vue 3 frontend (reads Neon directly via @neondatabase/serverless HTTP)
```

## Database (Neon PostgreSQL)

All tables in `public` schema. Two roles: `neondb_owner` (full access, used by poller) and `frontend_reader` (SELECT on `latest_snapshot` only).

### Core tables

| Table | Purpose | Size |
|-------|---------|------|
| `stop_times` | GTFS static schedule. PK `(trip_id, stop_sequence)`. Purely static — upserted each cycle but values don't change. | ~221 MB |
| `real_time_observations` | One row per (trip_id, stop_sequence, service_date). Updated each poll cycle with latest prediction. This is the raw data. | ~169 MB, growing ~800K rows/day |
| `routes` | Route metadata from GTFS static | tiny |
| `trips` | Trip metadata from GTFS static | ~3.4 MB |
| `stops` | Stop names/coords from GTFS static | ~1.8 MB |
| `calendar` | Service day definitions (Mon-Fri, etc.) | tiny |
| `service_cycle` | Freshness tracking for GTFS static feed | tiny |

### Aggregation tables

| Table | Purpose |
|-------|---------|
| `daily_route_metrics` | Aggregated from `real_time_observations` via `agg_daily()`. One row per route per day. |
| `hourly_route_metrics` | Aggregated via `agg_hourly()`. One row per route per hour. |
| `latest_snapshot` | One row per route, latest daily metrics. Built by `agg_snapshot()`. **This is the only table the frontend reads.** |

### Aggregation SQL functions

- `agg_daily(poll_date date)` — aggregates observations into `daily_route_metrics`
- `agg_hourly(poll_date date)` — same but bucketed by hour into `hourly_route_metrics`
- `agg_snapshot(poll_date date, now timestamptz)` — upserts into `latest_snapshot` from `daily_route_metrics`

All three query `real_time_observations r JOIN stop_times st ON (trip_id, stop_sequence) JOIN trips t ON trip_id` to get route_id.

### On-time window

- Early: delay < -60s
- On-time: -60s ≤ delay ≤ 300s
- Late: delay > 300s

## Data pipeline

### Poll cycle (every 1 minute via cron)

1. **Connect** to Neon (pooled `DATABASE_URL`)
2. **Static check** — HEAD request to SEPTA GTFS URL to get `Last-Modified` header. Compare with `service_cycle.last_modified`. If changed, download full zip and re-import all static tables (routes, trips, stops, stop_times, calendar). Record new freshness in `service_cycle`.
3. **Fetch** SEPTA GTFS-RT protobuf (`rtTripUpdates.pb`)
4. **Load stop_times** — `SELECT trip_id, stop_sequence, arrival_time, stop_id FROM stop_times WHERE trip_id = ANY(trip_ids)` into a dict cache
5. **Extract observations** — for each entity in the feed, for each stop_time_update with a valid arrival, compute delay = predicted_timestamp - scheduled_timestamp
6. **Update predictions** — COPY observations into a temp table, INSERT ON CONFLICT (trip_id, stop_sequence, service_date) DO UPDATE into `real_time_observations`
7. **Run aggregations** — call `agg_daily`, `agg_hourly`, `agg_snapshot`

### GTFS static feed regeneration

SEPTA publishes a new static GTFS feed periodically (usually weekly). The feed is a zip-within-a-zip: `gtfs_public.zip` contains `google_bus.zip`, which contains the GTFS text files (routes.txt, trips.txt, stops.txt, stop_times.txt, calendar.txt).

The poller detects new static data via an HTTP HEAD request:
- `gtfs_static.get_freshness()` → HEAD request, returns `Last-Modified` header
- `gtfs_static.get_stored_freshness(conn)` → queries `service_cycle` table
- If they differ, `gtfs_static.run_and_record_freshness(conn)` runs:
  1. Downloads the full zip
  2. Extracts `google_bus.zip` from the outer zip
  3. Parses each GTFS text file (CSV)
  4. Upserts into the corresponding table via `upsert_table()` or `copy_upsert()`
  5. Records the new `Last-Modified` in `service_cycle`

Static tables are purely reference data — the poller re-imports them each cycle but values don't change unless SEPTA publishes a new feed.

### Key files (ingestion)

| File | Role |
|------|------|
| `ingestion/poller/main.py` | Entry point. Runs one poll cycle. |
| `ingestion/poller/db.py` | `get_connection()` with retries + TCP keepalives. `upsert_table()`, `copy_upsert()` helpers. |
| `ingestion/poller/gtfs_rt.py` | Fetch/parse protobuf, `extract_observations()`, `update_predictions()`, `build_aggregations()`, `load_stop_times()` |
| `ingestion/poller/gtfs_static.py` | Download/import GTFS static feed, `get_stored_freshness()`, `run_and_record_freshness()` |
| `ingestion/poller/models.py` | SQLAlchemy models (StopTime, RealTimeObservation, Route, Trip, Stop, Calendar, DailyRouteMetric, HourlyRouteMetric, LatestSnapshot, ServiceCycle) |
| `ingestion/migrations/versions/001_initial_schema.py` | All table definitions |
| `ingestion/migrations/versions/002_aggregation_functions.py` | SQL aggregation functions |
| `ingestion/scripts/setup_readonly_role.sql` | Creates `frontend_reader` role |

## Frontend

Vue 3 (Composition API `<script setup>`), pure JS (no TypeScript), Vite, Leaflet for map. Lives in `frontend/`.

### Key files (frontend)

| File | Role |
|------|------|
| `frontend/src/App.vue` | Entry — composes RouteMap, RouteTable, KpiHeader |
| `frontend/src/composables/useDashboardData.js` | Queries `latest_snapshot` via Neon HTTP, polls every 60s |
| `frontend/src/lib/neon.js` | Exports `sql` tagged template function from `@neondatabase/serverless` |
| `frontend/src/components/RouteTable.vue` | 7-column sortable table with search, badges |
| `frontend/src/components/RouteMap.vue` | Leaflet map with geojson overlays, markers |
| `frontend/src/components/KpiHeader.vue` | 4 KPI cards |
| `frontend/public/route-lines.json` | Pre-baked route geometries |
| `frontend/public/philly-boundary.json` | Map boundary |

Frontend reads from root `.env` via Vite's `envDir: '..'`. Only `VITE_NEON_URL` is exposed to client (Vite strips non-`VITE_` vars). The URL uses the `frontend_reader` role (read-only).

## Deployment

### Raspberry Pi (primary poller)

- User: `austinblanton`, host: `plant1.local` (also Tailscale `pi@100.71.198.128`)
- Repo: `/home/austinblanton/Desktop/deviated-septa`
- Cron: `* * * * * timeout 45 flock -n /tmp/poller.lock sh -c 'cd /home/austinblanton/Desktop/deviated-septa/ingestion && uv run python -m poller.main' >> /tmp/poller.log 2>&1`
- Logs: `/tmp/poller.log`
- `timeout 45` kills hung processes. `flock -n` prevents overlapping runs.
- Needs `libpq-dev` for `psycopg2-binary`

### Neon

- Org: `org-icy-tooth-31776829` (Austin)
- Project: `withered-lake-19396872` (deviated-SEPTA)
- Pooler URL in root `.env` as `DATABASE_URL`

### CI/CD

GitHub Actions workflow `.github/workflows/poll.yml.disabled` is DISABLED (`.disabled` suffix). Pi is the primary poller.

## Environment variables

Root `.env` (read by both backend and frontend):

| Variable | Used by | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | Backend | Neon pooled connection |
| `DATABASE_URL_UNPOOLED` | Backend | Neon unpooled (migrations, bulk imports) |
| `NEON_BRANCH` | Backend | Neon branch identifier |
| `FRONTEND_READER_PASSWORD` | Setup script | Password for `frontend_reader` role |
| `VITE_NEON_URL` | Frontend | Neon pooled connection as `frontend_reader` |
