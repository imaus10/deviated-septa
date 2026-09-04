# Deviated SEPTA — Agent Guide

## What this is

On-time performance dashboard for SEPTA bus+trolley routes. Polls SEPTA's GTFS-RT API every minute,
computes delay per stop, aggregates by route, serves a Vue 3 dashboard. **No database service** —
all compute is local to the Pi; S3 stores the static JSON the frontend reads plus the eternal
parquet ledger.

## Architecture

```
SEPTA GTFS-RT ──→ Pi (cron every 1m)
SEPTA GTFS static ──→   │
                        ├── StaticDB (SQLite: stop_times + trips, streamed — never in RAM)
                        ├── in-memory dicts (routes, stops, calendar — tiny)
                        ├── observations.db (SQLite: 7-service-date live window)
                        ├── baseline + daily chronicle (state/*.json)
                        └── S3 PUT (best-effort)
                              public/current.json, public/geometries.json   (frontend reads)
                              state/all-baseline.json, state/daily/<sd>.json
                              archive/routes.parquet, archive/stops.parquet
                              archive/observations/<sd>.parquet             (eternal ledger)
                              │
                              ▼
                        S3 bucket (public/ world-readable; state/ + archive/ 403)
                              │
                              ▼
                        GitHub Pages frontend (fetches public/* JSON)
```

## On-time window

- Early: delay < -60s
- On-time: -60s ≤ delay ≤ 300s
- Late: delay > 300s

Scope is bus/trolley only: `route_type IN (0, 3, 11)` (0 = trolley, 3 = bus, 11 = trolleybus
59/66/75). Rail (`route_type 1`) is excluded everywhere (an M1 rail-coded line drops out of the
migrated archives accordingly).

## S3 layout

```
s3://deviated-septa-{dev|prod}/
  public/current.json      # 4-period rollup (~6 MB), Cache-Control max-age=55, stale-while-revalidate=5
  public/geometries.json   # active-route polylines
  state/                   # private rollups: all-baseline.json, daily/<sd>.json
  archive/                 # private ledger: routes/stops registries + observations/<sd>.parquet
```

Serving: `public/*` is world-readable (via a bucket policy when `use_cloudfront=false`, or
through CloudFront OAC when `use_cloudfront=true`); `state/` + `archive/` are private. See
`infra/`.

## Data pipeline

### Poll cycle (every 1 minute via cron) — `poller/main.py`

1. **Static** — `gtfs_static.check_and_update(data_dir, static.db)`: HEAD the SEPTA feed's
   `Last-Modified`; if changed (or first run), download the zip, `import_to_sqlite` (streams
   stop_times/trips into `StaticDB`, scoped to bus/trolley), and regenerate + upload
   geometries/registries. Unchanged: just open the existing SQLite store — the hot path never
   materializes stop_times.
2. **Fetch + parse** GTFS-RT protobuf.
3. **Extract observations** — `gtfs_rt.extract_observations(feed, static)` does per-stop point
   lookups (`static.stop_time`) and bakes `stop_id` into each observation; `main` adds
   `route_id` (`static.route_for_trip`) + category.
4. **UPSERT** into `ObservationsDB` (one row per `(trip_id, stop_sequence, service_date)`, latest
   prediction wins).
5. **Archive elapsed dates** — any store date strictly before `current_sd` that isn't already on
   S3 is written to `archive/observations/<sd>.parquet` and uploaded, then the local copy is
   removed (S3 is the eternal ledger). Skipped if already present (exists-skip).
6. **Prune** — dates older than the 7-date window are folded into the all-time baseline
   (`all-baseline.json`), deleted from the store, and the daily chronicle is refreshed.
7. **Rollup** — `rollup.build_current` builds `current.json` (periods hour/day/week/all, all
   data-driven: `current_service_date` = newest service date in the feed, never wall-clock).
8. **Uploads** are best-effort (warn, never crash). `s3.upload` retries once.

### Key files (ingestion)

| File | Role |
|------|------|
| `poller/main.py` | One poll cycle (steps above). |
| `poller/gtfs_static.py` | `StaticDB` (SQLite stop_times+trips, point lookups + iteration), `check_and_update()`, `import_to_sqlite()`, `load_local_metadata()` (routes/stops/calendar only), `active_route_ids()`. |
| `poller/gtfs_rt.py` | Fetch/parse protobuf, `extract_observations(feed, static)`, `infer_service_date`/`scheduled_to_ts`, `classify()`. |
| `poller/state.py` | `ObservationsDB` (SQLite observations store), `load_archive()`, `last_service_date_for_routes()`, state.json. |
| `poller/rollup.py` | `build_current()`, `prune_window()`, `refresh_daily_chronicle()`, baseline helpers. |
| `poller/s3.py` | Upload/read S3 (explicit `S3_*` creds, never the default chain), pyarrow filesystem. |
| `poller/archives.py` | Parquet writers/readers; consolidation-aware `build_registries()` (active open-ended, dropped routes closed with observation-derived `valid_to` windows, existing rows never deleted/reopened). |
| `poller/route_geometries.py` | Spider-walk polyline generator: `build_geometries(static, metadata)` streams stop_times from StaticDB. |
| `scripts/restore_state.py` | Rebuild local state from the S3 ledger (bootstrap/DR); streams parquet in bounded batches. |
| `scripts/cutover.py` | One-shot Pi cutover: preflight → static/bootstrap+geometries → drop today's partial archive → restore → verify. Dry-run default, `--apply` to execute. |
| `scripts/migrate_neon.py` | **Temporary** — historical Neon→S3 import (needs `psycopg2` + `DATABASE_URL*`). Remove once the migration is fully retired. |

## Frontend

Vue 3 (Composition API `<script setup>`), pure JS, Vite, Leaflet. Lives in `frontend/`.

| File | Role |
|------|------|
| `src/lib/current.js` | Pure transform of `current.json` → flat period-tagged rows (`buildSnapshot`, `deriveTotals`). |
| `src/composables/useDashboardData.js` | Fetches `${VITE_PUBLIC_URL}/current.json` + `/geometries.json` every 60s; same return shape consumed by the components. |
| `src/components/*` | RouteTable, RouteMap, KpiHeader, TopStops. |
| `vite.config.js` | `[vue()]` + base + envDir only — no data layer, no database. |

Frontend reads root `.env` via Vite's `envDir: '..'`. `VITE_PUBLIC_URL` is the S3 `public/` URL
(e.g. `https://deviated-septa-prod.s3.amazonaws.com/public`); unset → fetch fails → error banner.

## Deployment

### Raspberry Pi (primary poller)

- User: `austinblanton`, host: `plant1.local` (Tailscale `pi@100.71.198.128`)
- Repo: `/home/austinblanton/Desktop/deviated-septa`
- Cron: `* * * * * timeout 420 flock -n /tmp/poller.lock sh -c 'cd /home/austinblanton/Desktop/deviated-septa/ingestion && uv run python -m poller.main' >> /tmp/poller.log 2>&1`
- Logs: `/tmp/poller.log`. `timeout 420` catches hangs; `flock -n` prevents overlap.
- WiFi recovery: `nmcli connection modify "Verizon_CK4G7P" connection.autoconnect-retries -1` +
  `ingestion/scripts/wifi-watchdog.sh` from cron (pings, reconnects after 3 failures, reboots
  after 6). Log: `/var/log/wifi-watchdog.log`.

### Infra (OpenTofu, `infra/`)

- Buckets `deviated-septa-{dev|prod}`, poller IAM user (bucket-scoped), monthly budget, optional
  CloudFront.
- `variable "use_cloudfront"` (default false): direct-S3 serving (public bucket policy + CORS,
  `public_access_block` relaxed) vs CloudFront OAC (fully private bucket + OAC-only policy).
  Dev/prod are separate local states: `terraform.tfstate` (dev, default) and
  `terraform-prod.tfstate` (`-state=terraform-prod.tfstate -var environment=prod`).

### CI/CD

- `.github/workflows/deploy.yml`: builds the frontend with the `VITE_PUBLIC_URL` secret and
  publishes to GitHub Pages. Triggers only on `frontend/**` pushes + manual `workflow_dispatch`.
- `.github/workflows/poll.yml.disabled` is disabled; the Pi is the primary poller.

## Environment variables (root `.env`)

| Variable | Used by | Purpose |
|----------|---------|---------|
| `S3_BUCKET` | Poller/scripts | S3 bucket name (`deviated-septa-dev` or `-prod`) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | Poller/scripts | Poller IAM creds (explicit, never default chain) |
| `VITE_PUBLIC_URL` | Frontend | S3 `public/` URL (CloudFront domain or bucket endpoint) |
| `DATABASE_URL` / `DATABASE_URL_UNPOOLED` | `migrate_neon.py` | **Temporary** — Neon strings for the one-off historical import |

Prod S3 creds live in `.env.prod` (gitignored); the poller/cutover scripts load it via
`--env-file` or pre-exported vars. `.env` and `.env.prod` are gitignored.