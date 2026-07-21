# Deviated SEPTA — Setup

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Node.js 18+ (for frontend only)
- A [Neon](https://neon.com) free-tier project

---

## 1. Environment variables

```bash
neon env pull
```

This writes `DATABASE_URL` and `DATABASE_URL_UNPOOLED` to `.env` from your Neon project. Alternatively, copy `.env.example` and fill in the values manually.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Pooled Postgres connection — poller runtime, app queries |
| `DATABASE_URL_UNPOOLED` | Unpooled connection — Alembic migrations, bulk imports |

---

## 2. Install dependencies

```bash
cd ingestion && uv sync --extra dev
cd ../frontend && npm install
```

---

## 3. Run migrations

```bash
cd ingestion && uv run alembic upgrade head
```

Creates all tables and aggregation functions in Neon.

---

## 4. Import GTFS static + test poller

```bash
cd ingestion && uv run python -m poller.main
```

Downloads SEPTA's GTFS static feed, imports it, fetches real-time predictions, and runs aggregations. Verify with:

```bash
psql "$DATABASE_URL" -c "SELECT route_id, on_time_percentage FROM latest_snapshot ORDER BY total_observations DESC LIMIT 5;"
```

---

## 5. Raspberry Pi (primary poller)

The poller runs every minute from a Pi with direct Postgres access.

### Initial setup

```bash
git clone <repo-url> deviated-septa
cd deviated-septa
neon env pull
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
* * * * * flock -n /tmp/poller.lock sh -c 'cd /home/austinblanton/Desktop/deviated-septa/ingestion && /path/to/uv run python -m poller.main' >> /tmp/poller.log 2>&1
```

Notes on the cron command:
- `flock -n` skips a cycle if the previous one is still running.
- `sh -c '...'` is needed because `flock` exec's the next argument as a binary, but `cd` is a shell built-in.
- **Adjust paths** — replace with your actual Pi home directory. Run `echo $HOME` to confirm.
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

## 6. Frontend dev & deploy

```bash
cd frontend
npm run dev            # local dev (hot-reload)
npm run build          # production build → dist/
```

---

## 7. Schema change workflow

1. Edit `ingestion/poller/models.py`
2. `cd ingestion && alembic revision --autogenerate -m "description"`
3. Review the generated file in `ingestion/migrations/versions/`
4. Test locally: `cd ingestion && alembic upgrade head`
5. Commit the migration
6. Run `alembic upgrade head` against Neon

---

## Export production data to local

```bash
pg_dump --no-owner --no-acl "$DATABASE_URL" > prod_dump.sql
psql -d deviated_septa_dev < prod_dump.sql
```
