"""frontend read access to daily_route_metrics

The frontend displays the real date range behind the "All Time" period as a
"since <date>" subtext. daily_route_metrics is the only small table carrying
that range (min/max service date), and frontend_reader predates the setup
script's default privileges, so it needs an explicit grant.

Revision ID: 000000000010
Revises: 000000000009
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "000000000010"
down_revision: Union[str, Sequence[str], None] = "000000000009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Conditional: local/dev DBs often lack the read-only role.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'frontend_reader') THEN
                GRANT SELECT ON daily_route_metrics TO frontend_reader;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'frontend_reader') THEN
                REVOKE SELECT ON daily_route_metrics FROM frontend_reader;
            END IF;
        END $$;
        """
    )
