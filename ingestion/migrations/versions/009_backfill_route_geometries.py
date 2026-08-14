"""backfill route geometries

One-time data backfill: regenerate the full-coverage spider polylines into
route_geometries right after 008 creates the table, so a fresh DB (e.g. the
prod deploy that introduces this table) has geometry without a manual
`uv run python -m poller.route_geometries` step.

Requires transaction_per_migration=True (see migrations/env.py) so that 008's
CREATE TABLE commits before this migration's separate connection writes to it.

Revision ID: 000000000009
Revises: 000000000008
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import context, op

revision: str = "000000000009"
down_revision: Union[str, Sequence[str], None] = "000000000008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if context.is_offline_mode():
        return

    from poller.db import get_connection
    from poller.route_geometries import regenerate_route_geometries

    conn = get_connection()
    try:
        regenerate_route_geometries(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM route_geometries")
            if cur.fetchone()[0] == 0:
                raise RuntimeError("route_geometries backfill produced no rows")
    finally:
        conn.close()


def downgrade() -> None:
    # Data backfill only; the table schema is owned by 008.
    pass
