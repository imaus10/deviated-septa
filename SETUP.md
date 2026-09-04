# Deviated SEPTA — Setup

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Node.js 18+ (for frontend only)
- AWS: an S3 bucket + poller IAM creds (see `infra/`; dev or prod via OpenTofu)

---

## 1. Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
|---|---|
| `S3_BUCKET` | S3 bucket name (`deviated-septa-dev` or `-prod`) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | Poller IAM creds (bucket-scoped) |
| `VITE_PUBLIC_URL` | Frontend data source — the S3 `public/` URL the dashboard fetches `current.json` + `geometries.json` from |

Prod creds live in a separate `.env.prod` (gitignored); the poller/cutover scripts load it via
`--env-file` or pre-exported vars.

---

## 2. Install dependencies

```bash
cd ingestion && uv sync --extra dev
cd ../frontend && npm install
```

---

## 3. Bootstrap static data + run the poller

```bash
cd ingestion && uv run python -m poller.main
```

On first run this downloads SEPTA's GTFS static feed into `data/`, builds the SQLite `StaticDB`
(`state/static.db`), fetches real-time predictions, and rolls up `state/current.json`. It then
uploads `public/*` to S3 (best-effort).

Local state layout (all under `ingestion/`):

```
data/                  # GTFS static zip + freshness (mutable, downloaded)
state/                 # runtime + rollups
  observations.db      # 7-service-date live observations window (SQLite)
  static.db            # StaticDB (stop_times + trips)
  current.json         # 4-period rollup
  all-baseline.json    # all-time folded totals
  daily/<sd>.json      # per-service-date chronicle
  archive/             # local parquet scratch (deleted after upload)
```

---

## 4. Restore / cut over from the S3 ledger

If you're bootstrapping from the S3 archive (DR) or cutting the Pi over from Neon:

```bash
cd ingestion && uv run python -m scripts.cutover --apply --env-file ../.env.prod
```

`cutover.py` (dry-run by default) restores the 7-date store window + baseline from
`archive/observations/*.parquet`, uploads `public/geometries.json`, and verifies. To rebuild
local state from S3 only, use `scripts/restore_state.py`.

---

## 5. Raspberry Pi (primary poller)

The poller runs every minute from a Pi.

### Initial setup

```bash
git clone <repo-url> deviated-septa
cd deviated-septa
# write .env with the S3_* vars
cd ingestion && uv sync --extra dev
```

### Cron (every minute)

```
* * * * * timeout 420 flock -n /tmp/poller.lock sh -c 'cd /home/austinblanton/Desktop/deviated-septa/ingestion && /path/to/uv run python -m poller.main' >> /tmp/poller.log 2>&1
```

Notes:
- `timeout 420` kills a hung cycle; `flock -n` skips if the previous run is still going.
- Adjust paths to your Pi home; use the absolute `uv` path (`which uv`).

### WiFi resilience (recommended)

1. `sudo nmcli connection modify "Verizon_CK4G7P" connection.autoconnect-retries -1`
2. `ingestion/scripts/wifi-watchdog.sh` from cron every minute (reconnects after 3 failures,
   reboots after 6): `* * * * * flock -n /tmp/wifi-watchdog.lock sudo -n /home/austinblanton/Desktop/deviated-septa/ingestion/scripts/wifi-watchdog.sh`

---

## 6. Frontend dev & deploy

```bash
cd frontend
npm run dev      # local dev (hot-reload) — fetches from VITE_PUBLIC_URL
npm run build    # production build → dist/
```

Set `VITE_PUBLIC_URL` in root `.env` to a reachable data source (e.g. the dev bucket's
`https://deviated-septa-dev.s3.amazonaws.com/public`) to develop against real data.

Deploy: push to `main` touching `frontend/**` → `.github/workflows/deploy.yml` builds with the
`VITE_PUBLIC_URL` GitHub secret and publishes to GitHub Pages. Set the secret to the S3 `public/`
URL (CloudFront domain when `use_cloudfront=true`, otherwise the bucket endpoint).

---

## 7. Historical import from Neon (temporary)

The one-off Neon → S3 import lives in `ingestion/scripts/migrate_neon.py` (needs `psycopg2` +
`DATABASE_URL*`). It is being retired after the Pi cutover completes; do not build on it.