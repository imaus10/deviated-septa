"""fix agg_snapshot to remove orphaned routes

Revision ID: 000000000003
Revises: 000000000002
Create Date: 2026-07-23

"""

from typing import Sequence, Union

from alembic import op

revision: str = "000000000003"
down_revision: Union[str, Sequence[str], None] = "000000000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION agg_snapshot(poll_date date, now timestamptz)
        RETURNS void
        LANGUAGE sql
        AS $$
            INSERT INTO latest_snapshot
                (route_id, route_name, route_type, total_observations,
                 early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                m.route_id,
                r.route_short_name,
                r.route_type,
                m.total_observations,
                m.early_count,
                m.on_time_count,
                m.late_count,
                m.on_time_percentage,
                m.avg_delay_seconds,
                now AS updated_at
            FROM daily_route_metrics m
            LEFT JOIN routes r ON r.route_id = m.route_id
            WHERE m.date = poll_date
            ON CONFLICT (route_id)
            DO UPDATE SET
                route_name         = EXCLUDED.route_name,
                route_type         = EXCLUDED.route_type,
                total_observations = EXCLUDED.total_observations,
                early_count       = EXCLUDED.early_count,
                on_time_count     = EXCLUDED.on_time_count,
                late_count        = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds,
                updated_at        = EXCLUDED.updated_at;

            DELETE FROM latest_snapshot
            WHERE route_id NOT IN (
                SELECT route_id FROM daily_route_metrics WHERE date = poll_date
            );
        $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION agg_snapshot(poll_date date, now timestamptz)
        RETURNS void
        LANGUAGE sql
        AS $$
            INSERT INTO latest_snapshot
                (route_id, route_name, route_type, total_observations,
                 early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                m.route_id,
                r.route_short_name,
                r.route_type,
                m.total_observations,
                m.early_count,
                m.on_time_count,
                m.late_count,
                m.on_time_percentage,
                m.avg_delay_seconds,
                now AS updated_at
            FROM daily_route_metrics m
            LEFT JOIN routes r ON r.route_id = m.route_id
            WHERE m.date = poll_date
            ON CONFLICT (route_id)
            DO UPDATE SET
                route_name         = EXCLUDED.route_name,
                route_type         = EXCLUDED.route_type,
                total_observations = EXCLUDED.total_observations,
                early_count       = EXCLUDED.early_count,
                on_time_count     = EXCLUDED.on_time_count,
                late_count        = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds,
                updated_at        = EXCLUDED.updated_at;
        $$;
    """)
