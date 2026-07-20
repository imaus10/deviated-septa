"""dedup_arrival_records_add_unique_index

Revision ID: bc04ad0f1825
Revises: ec5b041f3bda
Create Date: 2026-07-17 14:27:52.852956

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bc04ad0f1825'
down_revision: Union[str, Sequence[str], None] = 'ec5b041f3bda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Supabase limits statement_timeout to ~30-60s, so raise it for this
    # session before the dedup loop.
    op.execute("SET statement_timeout = '600s'")

    # 1. Create a temporary non-unique index so the dedup EXISTS lookups
    #    can use index scans instead of scanning the full table per row.
    #    Use Alembic DDL API to avoid read-only transaction restriction
    #    from raw op.execute() on Supabase.
    # scheduled_time is timestamptz, so ::date is STABLE (depends on session
    # timezone) and can't be used in an index expression. trip_id is unique
    # per GTFS scheduled departure (one trip_id per direction per hour per
    # route per calendar date), so (trip_id, stop_sequence) is sufficient
    # as a unique key — no need for scheduled_time::date.

    # 1. Create a temporary non-unique index so the dedup EXISTS lookups
    #    can use index scans instead of scanning the full table per row.
    op.create_index(
        "tmp_dedup_idx",
        "arrival_records",
        ["trip_id", "stop_sequence"],
    )

    # 2. Deduplicate in batches — delete the oldest duplicate(s) per
    #    arrival event, 10K rows at a time.
    op.execute("""
        DO $$
        DECLARE
            n INTEGER;
        BEGIN
            LOOP
                WITH dupe AS (
                    SELECT a.id FROM arrival_records a
                    WHERE EXISTS (
                        SELECT 1 FROM arrival_records b
                        WHERE b.trip_id = a.trip_id
                          AND b.stop_sequence = a.stop_sequence
                          AND b.poll_timestamp > a.poll_timestamp
                    )
                    LIMIT 10000
                )
                DELETE FROM arrival_records
                WHERE id IN (SELECT id FROM dupe);

                GET DIAGNOSTICS n = ROW_COUNT;
                EXIT WHEN n < 10000;
            END LOOP;
        END
        $$;
    """)

    # 3. Drop the temp index — the unique index (step 5) supersedes it.
    op.drop_index("tmp_dedup_idx", table_name="arrival_records")

    # 4. Drop the composite index (route_id, poll_timestamp) which is less
    #    useful now — the unique index covers the dedup, and individual
    #    indexes on route_id and poll_timestamp already exist.
    op.drop_index("ix_arrival_records_route_date", table_name="arrival_records")

    # 5. Create the unique index to enforce dedup at write time going forward
    op.create_index(
        "uq_arrival_event",
        "arrival_records",
        ["trip_id", "stop_sequence"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_arrival_event", table_name="arrival_records")
    op.create_index(
        "ix_arrival_records_route_date",
        "arrival_records",
        ["route_id", "poll_timestamp"],
    )
    # No way to restore deleted duplicates in downgrade — data loss warning.
