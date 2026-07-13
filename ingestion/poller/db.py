import json
import os
from datetime import datetime

import httpx
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_client() -> httpx.Client:
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    return httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers=headers,
        timeout=60,
    )


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def is_pg(db):
    return hasattr(db, "cursor")


def upsert_table(conn, table, rows, pk_cols):
    if not rows:
        return
    cols = list(rows[0].keys())
    col_str = ", ".join(cols)
    conflict_str = ", ".join(pk_cols)
    update_cols = [c for c in cols if c not in pk_cols]
    update_str = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)

    sql = (
        f"INSERT INTO {table} ({col_str}) VALUES %s "
        f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
    )
    values = [[r[c] for c in cols] for r in rows]
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    conn.commit()


class DatetimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def json_dumps(obj):
    return json.dumps(obj, cls=DatetimeEncoder)
