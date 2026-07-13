# Deviated SEPTA — Setup

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Node.js 18+
- A [Supabase](https://supabase.com) free-tier project

---

## 1. Environment variables

```bash
cp .env.example .env
```

Fill in the values (see `.env.example` for format).

| Variable | Where used |
|---|---|
| `SUPABASE_URL` | Poller runtime (REST fallback) + GitHub Secrets (disabled) |
| `SUPABASE_SERVICE_KEY` | Poller runtime (REST fallback) + GitHub Secrets (disabled) |
| `DATABASE_URL` | Poller runtime (Pi, primary) + Alembic (local migrations) |
| `VITE_SUPABASE_URL` | Frontend |
| `VITE_SUPABASE_ANON_KEY` | Frontend |

The poller auto-detects which backend to use: if `DATABASE_URL` is set it connects via direct Postgres; otherwise it falls back to the Supabase REST API using `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`.

---

## 2. Install dependencies

```bash
cd ingestion && uv sync --extra dev
cd ../frontend && npm install
```

---

## 3. Run migrations (locally, requires IPv6)

```bash
cd ingestion && uv run alembic upgrade head
```

This creates tables + RPC functions in Supabase. You must have IPv6 to reach `db.*.supabase.co:6543`.

---

## 4. Import GTFS static + test poller (local)

```bash
cd ingestion && uv run python -m poller.main
```

The poller auto-detects direct Postgres (`DATABASE_URL`) or Supabase REST API (`SUPABASE_URL` + `SUPABASE_SERVICE_KEY`) based on which env vars are set.

Verify:

```bash
curl -s -H "apikey: $VITE_SUPABASE_ANON_KEY" \
  "$SUPABASE_URL/rest/v1/latest_snapshot?select=route_id,on_time_percentage" | jq .
```

---

## 5. Raspberry Pi (primary poller)

The poller runs every minute from a Pi with direct Postgres access (no IPv6 restriction).

### Initial setup

```bash
git clone <repo-url> deviated-septa
cd deviated-septa
cp .env.example .env
# edit .env — set DATABASE_URL (Postgres connection string)
# SUPABASE_URL / SUPABASE_SERVICE_KEY are optional (REST fallback)
cd ingestion && uv sync --frozen
```

Debian-based Pis need `libpq-dev` for `psycopg2-binary`:

```bash
sudo apt update && sudo apt install -y libpq-dev
```

### Cron (every minute)

```bash
crontab -e
```

Add:

```
* * * * * flock -n /tmp/poller.lock sh -c 'cd /home/pi/deviated-septa/ingestion && /home/pi/.local/bin/uv run python -m poller.main' >> /tmp/poller.log 2>&1
```

Notes on the cron command:
- `flock -n` skips a cycle if the previous one is still running (safety net in case a poll ever exceeds 60s).
- `sh -c '...'` is needed because `flock` exec's the next argument as a binary, but `cd` is a shell built-in — the shell wrapper makes it work.
- **Adjust paths** — replace `/home/pi/` with your actual Pi home directory. Run `echo $HOME` to confirm.
- **Find `uv`** — cron's default PATH doesn't include `~/.local/bin`. Run `which uv` and substitute that absolute path.

### Monitor

```bash
tail -f /tmp/poller.log
```

### Ensure cron survives reboot

```bash
sudo systemctl enable cron
```

---

## 6. GitHub Actions (fallback, optional)

The workflow file at `.github/workflows/poll.yml.disabled` is renamed to prevent GitHub from running it. It exists as a fallback — if the Pi goes down, re-enable it:

```bash
mv .github/workflows/poll.yml.disabled .github/workflows/poll.yml
git add .github/workflows/poll.yml && git commit -m "reenable GHA poller" && git push
```

Add these secrets to the repo:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | `https://pjchwpsjxxacglfsnymg.supabase.co` |
| `SUPABASE_SERVICE_KEY` | service_role key (not anon!) |

No `DATABASE_URL` in CI — the poller uses the REST API when `DATABASE_URL` is unset.

---

## 7. Frontend dev & deploy

```bash
cd frontend
cp .env.example .env   # fill VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
npm run dev            # local dev (hot-reload)
npm run build          # production build → dist/
```

Deploy `frontend/dist/` to GitHub Pages with `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` as build env vars.

---

## 8. Schema change workflow

1. Edit `ingestion/poller/models.py`
2. `cd ingestion && alembic revision --autogenerate -m "description"`
3. Review the generated file in `ingestion/migrations/versions/`
4. Test locally: `cd ingestion && alembic upgrade head`
5. Commit the migration + any new function SQL
6. Run `alembic upgrade head` from a machine with IPv6 against Supabase
7. Push to GitHub

---

## Export production data to local

```bash
pg_dump --no-owner --no-acl "$DATABASE_URL" > prod_dump.sql
psql -d deviated_septa_dev < prod_dump.sql
```
