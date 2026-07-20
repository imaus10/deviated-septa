import csv
import io
import json
import os
from datetime import datetime
from urllib.parse import urlparse, urlunparse

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
    url = DATABASE_URL
    if "supabase.co" in url and ":6543" not in url:
        parsed = urlparse(url)
        if ":" not in (parsed.netloc.split("@")[-1] if "@" in parsed.netloc else parsed.netloc):
            host_part = parsed.netloc.split("@")[-1] if "@" in parsed.netloc else parsed.netloc
            new_host_part = f"{host_part}:6543"
            if "@" in parsed.netloc:
                user_part = parsed.netloc.split("@")[0]
                parsed = parsed._replace(netloc=f"{user_part}@{new_host_part}")
            else:
                parsed = parsed._replace(netloc=new_host_part)
            url = urlunparse(parsed)
    return psycopg2.connect(url)


def is_pg(db):
    return hasattr(db, "cursor")


def upsert_table(conn, table, rows, pk_cols, page_size=5000):
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
        execute_values(cur, sql, values, page_size=page_size)
    conn.commit()


def copy_upsert(conn, table, rows, pk_cols):
    if not rows:
        return
    cols = list(rows[0].keys())
    col_str = ", ".join(cols)
    conflict_str = ", ".join(pk_cols)
    update_cols = [c for c in cols if c not in pk_cols]
    update_str = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)

    buf = io.StringIO()
    for row in rows:
        buf.write("\t".join(_txt_val(row[c]) for c in cols) + "\n")
    buf.seek(0)

    with conn.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE _stg (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.copy_from(buf, "_stg", sep="\t", null="\\N", columns=cols)
        cur.execute(
            f"INSERT INTO {table} ({col_str}) SELECT {col_str} FROM _stg "
            f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
        )
    conn.commit()


def _txt_val(v):
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


class DatetimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def json_dumps(obj):
    return json.dumps(obj, cls=DatetimeEncoder)
