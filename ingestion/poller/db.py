import io
import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_connection():
    return psycopg2.connect(DATABASE_URL)


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

