from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: str = "43361a53ee61"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "routes",
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("route_short_name", sa.Text(), nullable=False),
        sa.Column("route_long_name", sa.Text(), nullable=True),
        sa.Column("route_type", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("route_id"),
    )

    op.create_table(
        "stops",
        sa.Column("stop_id", sa.Text(), nullable=False),
        sa.Column("stop_name", sa.Text(), nullable=False),
        sa.Column("stop_lat", sa.Float(), nullable=True),
        sa.Column("stop_lon", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("stop_id"),
    )

    op.create_table(
        "calendar",
        sa.Column("service_id", sa.Text(), nullable=False),
        sa.Column("monday", sa.Integer(), nullable=False),
        sa.Column("tuesday", sa.Integer(), nullable=False),
        sa.Column("wednesday", sa.Integer(), nullable=False),
        sa.Column("thursday", sa.Integer(), nullable=False),
        sa.Column("friday", sa.Integer(), nullable=False),
        sa.Column("saturday", sa.Integer(), nullable=False),
        sa.Column("sunday", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Text(), nullable=False),
        sa.Column("end_date", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("service_id"),
    )

    op.create_table(
        "trips",
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("service_id", sa.Text(), nullable=False),
        sa.Column("direction_id", sa.Integer(), nullable=False),
        sa.Column("trip_headsign", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("trip_id"),
    )
    op.create_index(op.f("ix_trips_route_id"), "trips", ["route_id"])
    op.create_index(op.f("ix_trips_service_id"), "trips", ["service_id"])

    op.create_table(
        "stop_times",
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("stop_sequence", sa.Integer(), nullable=False),
        sa.Column("stop_id", sa.Text(), nullable=False),
        sa.Column("arrival_time", sa.Text(), nullable=True),
        sa.Column("departure_time", sa.Text(), nullable=True),
        sa.Column("pickup_type", sa.Integer(), nullable=True),
        sa.Column("drop_off_type", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("trip_id", "stop_sequence"),
    )
    op.create_index(op.f("ix_stop_times_stop_id"), "stop_times", ["stop_id"])

    op.create_table(
        "arrival_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("poll_timestamp", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("direction_id", sa.Integer(), nullable=False),
        sa.Column("stop_id", sa.Text(), nullable=False),
        sa.Column("stop_sequence", sa.Integer(), nullable=False),
        sa.Column("scheduled_time", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("predicted_time", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delay_seconds", sa.Integer(), nullable=True),
        sa.Column("vehicle_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_arrival_records_poll_timestamp"),
        "arrival_records",
        ["poll_timestamp"],
    )
    op.create_index(
        op.f("ix_arrival_records_route_id"),
        "arrival_records",
        ["route_id"],
    )
    op.create_index(
        "ix_arrival_records_route_date",
        "arrival_records",
        ["route_id", "poll_timestamp"],
    )

    op.create_table(
        "daily_route_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("total_observations", sa.Integer(), nullable=False),
        sa.Column("early_count", sa.Integer(), nullable=False),
        sa.Column("on_time_count", sa.Integer(), nullable=False),
        sa.Column("late_count", sa.Integer(), nullable=False),
        sa.Column("on_time_percentage", sa.Float(), nullable=True),
        sa.Column("avg_delay_seconds", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("route_id", "date", name="uq_daily_route_date"),
    )

    op.create_table(
        "hourly_route_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("total_observations", sa.Integer(), nullable=False),
        sa.Column("early_count", sa.Integer(), nullable=False),
        sa.Column("on_time_count", sa.Integer(), nullable=False),
        sa.Column("late_count", sa.Integer(), nullable=False),
        sa.Column("on_time_percentage", sa.Float(), nullable=True),
        sa.Column("avg_delay_seconds", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "route_id", "date", "hour", name="uq_hourly_route_date_hour"
        ),
    )

    op.create_table(
        "latest_snapshot",
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("route_name", sa.Text(), nullable=True),
        sa.Column("route_type", sa.Integer(), nullable=True),
        sa.Column("total_observations", sa.Integer(), nullable=False),
        sa.Column("early_count", sa.Integer(), nullable=False),
        sa.Column("on_time_count", sa.Integer(), nullable=False),
        sa.Column("late_count", sa.Integer(), nullable=False),
        sa.Column("on_time_percentage", sa.Float(), nullable=True),
        sa.Column("avg_delay_seconds", sa.Float(), nullable=True),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("route_id"),
    )


    # --- Supabase Data API access for anonymous users ---
    # Supabase exposes tables to the public via its REST API using the "anon" role.
    # On vanilla Postgres (local dev), that role doesn't exist, so we create it
    # as a no-login role. On Supabase it already exists and this is a no-op.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
                CREATE ROLE anon WITH NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    exposed_tables = [
        "routes",
        "latest_snapshot",
        "daily_route_metrics",
        "hourly_route_metrics",
    ]
    for tbl in exposed_tables:
        op.execute(f"GRANT SELECT ON {tbl} TO anon")
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY \"anon_read_{tbl}\" ON {tbl} FOR SELECT TO anon USING (true)"
        )


def downgrade() -> None:
    exposed_tables = [
        "routes",
        "latest_snapshot",
        "daily_route_metrics",
        "hourly_route_metrics",
    ]
    for tbl in exposed_tables:
        op.execute(f"DROP POLICY IF EXISTS \"anon_read_{tbl}\" ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT ON {tbl} FROM anon")

    op.drop_table("latest_snapshot")
    op.drop_table("hourly_route_metrics")
    op.drop_table("daily_route_metrics")
    op.drop_index("ix_arrival_records_route_date", table_name="arrival_records")
    op.drop_index("ix_arrival_records_route_id", table_name="arrival_records")
    op.drop_index("ix_arrival_records_poll_timestamp", table_name="arrival_records")
    op.drop_table("arrival_records")
    op.drop_index("ix_stop_times_stop_id", table_name="stop_times")
    op.drop_table("stop_times")
    op.drop_index("ix_trips_service_id", table_name="trips")
    op.drop_index("ix_trips_route_id", table_name="trips")
    op.drop_table("trips")
    op.drop_table("calendar")
    op.drop_table("stops")
    op.drop_table("routes")
