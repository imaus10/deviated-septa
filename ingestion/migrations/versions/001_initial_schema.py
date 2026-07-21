"""initial schema

Revision ID: 000000000001
Revises:
Create Date: 2026-07-21

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: str = "000000000001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Static GTFS tables ──────────────────────────────────────────

    op.create_table(
        "routes",
        sa.Column("route_id", sa.Text, primary_key=True),
        sa.Column("route_short_name", sa.Text, nullable=False),
        sa.Column("route_long_name", sa.Text),
        sa.Column("route_type", sa.Integer, nullable=False),
    )

    op.create_table(
        "calendar",
        sa.Column("service_id", sa.Text, primary_key=True),
        sa.Column("monday", sa.Integer, nullable=False),
        sa.Column("tuesday", sa.Integer, nullable=False),
        sa.Column("wednesday", sa.Integer, nullable=False),
        sa.Column("thursday", sa.Integer, nullable=False),
        sa.Column("friday", sa.Integer, nullable=False),
        sa.Column("saturday", sa.Integer, nullable=False),
        sa.Column("sunday", sa.Integer, nullable=False),
        sa.Column("start_date", sa.Text, nullable=False),
        sa.Column("end_date", sa.Text, nullable=False),
    )

    op.create_table(
        "stops",
        sa.Column("stop_id", sa.Text, primary_key=True),
        sa.Column("stop_name", sa.Text, nullable=False),
        sa.Column("stop_lat", sa.Float),
        sa.Column("stop_lon", sa.Float),
    )

    op.create_table(
        "trips",
        sa.Column("trip_id", sa.Text, primary_key=True),
        sa.Column("route_id", sa.Text, sa.ForeignKey("routes.route_id"), nullable=False),
        sa.Column("service_id", sa.Text, nullable=False),
        sa.Column("direction_id", sa.Integer, nullable=False),
        sa.Column("trip_headsign", sa.Text),
    )

    op.create_table(
        "stop_times",
        sa.Column("trip_id", sa.Text, sa.ForeignKey("trips.trip_id"), primary_key=True),
        sa.Column("stop_sequence", sa.Integer, primary_key=True),
        sa.Column("stop_id", sa.Text, sa.ForeignKey("stops.stop_id"), nullable=False),
        sa.Column("arrival_time", sa.Text),
        sa.Column("departure_time", sa.Text),
        sa.Column("pickup_type", sa.Integer),
        sa.Column("drop_off_type", sa.Integer),
    )

    # ── Service cycle tracking ──────────────────────────────────────

    op.create_table(
        "service_cycle",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("last_modified", sa.Text),
        sa.Column("checked_at", TIMESTAMP(timezone=True), nullable=False),
    )

    # ── Real-time observations ──────────────────────────────────────

    op.create_table(
        "real_time_observations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.Text, nullable=False),
        sa.Column("stop_sequence", sa.Integer, nullable=False),
        sa.Column("predicted_time", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("delay_seconds", sa.Integer, nullable=False),
        sa.Column("vehicle_id", sa.Text),
        sa.Column("poll_timestamp", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("service_date", sa.Date, nullable=False),
        sa.ForeignKeyConstraint(
            ["trip_id", "stop_sequence"],
            ["stop_times.trip_id", "stop_times.stop_sequence"],
        ),
        sa.UniqueConstraint("trip_id", "stop_sequence", "service_date",
                            name="uq_obs_trip_stop_date"),
    )

    # ── Metrics tables ──────────────────────────────────────────────

    op.create_table(
        "daily_route_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("route_id", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("total_observations", sa.Integer, nullable=False, server_default="0"),
        sa.Column("early_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("late_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_percentage", sa.Float),
        sa.Column("avg_delay_seconds", sa.Float),
        sa.UniqueConstraint("route_id", "date", name="uq_daily_route_date"),
    )

    op.create_table(
        "hourly_route_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("route_id", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("hour", sa.Integer, nullable=False),
        sa.Column("total_observations", sa.Integer, nullable=False, server_default="0"),
        sa.Column("early_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("late_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_percentage", sa.Float),
        sa.Column("avg_delay_seconds", sa.Float),
        sa.UniqueConstraint("route_id", "date", "hour", name="uq_hourly_route_date_hour"),
    )

    op.create_table(
        "latest_snapshot",
        sa.Column("route_id", sa.Text, primary_key=True),
        sa.Column("route_name", sa.Text),
        sa.Column("route_type", sa.Integer),
        sa.Column("total_observations", sa.Integer, nullable=False, server_default="0"),
        sa.Column("early_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("late_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_percentage", sa.Float),
        sa.Column("avg_delay_seconds", sa.Float),
        sa.Column("updated_at", TIMESTAMP(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("latest_snapshot")
    op.drop_table("hourly_route_metrics")
    op.drop_table("daily_route_metrics")
    op.drop_table("real_time_observations")
    op.drop_table("service_cycle")
    op.drop_table("stop_times")
    op.drop_table("trips")
    op.drop_table("stops")
    op.drop_table("calendar")
    op.drop_table("routes")
