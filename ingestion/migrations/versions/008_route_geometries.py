"""route geometry table

Revision ID: 000000000008
Revises: 000000000007
Create Date: 2026-08-13 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "000000000008"
down_revision: Union[str, Sequence[str], None] = "000000000007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Table only — rows are populated at runtime by
# poller.route_geometries.regenerate_route_geometries(), which builds one
# full-coverage "spider" polyline per bus/trolley route (every stop the route
# serves lies on its line). The poller regenerates after every static-feed
# import; existing DBs can be reconciled with a single explicit run of
# `uv run python -m poller.route_geometries` (from ingestion/).


def upgrade() -> None:
    op.create_table(
        "route_geometries",
        sa.Column("route_id", sa.Text, primary_key=True),
        sa.Column("route_short_name", sa.Text, nullable=True),
        sa.Column("coordinates", JSONB, nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Conditional: local/dev DBs often lack the read-only role, and default
    # privileges already cover it on prod when the owner role created the table.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'frontend_reader') THEN
                GRANT SELECT ON route_geometries TO frontend_reader;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_table("route_geometries")
