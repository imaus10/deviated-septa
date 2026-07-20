"""grant service_role table permissions

Tables created by Alembic (as postgres) don't automatically grant access
to the service_role database role that Supabase's service_role JWT maps to.
Without these grants, the Data API returns 403 "permission denied for table".

Run this after any migration that creates new tables the poller needs to write to.
"""

from typing import Sequence, Union
from alembic import op

revision: str = "ec5b041f3bda"
down_revision: Union[str, None] = "13d786d4b2d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA public TO service_role")
    op.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role")
    op.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role")


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM service_role")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM service_role")
    op.execute("REVOKE USAGE ON SCHEMA public FROM service_role")
