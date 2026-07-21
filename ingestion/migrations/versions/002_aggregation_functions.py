"""aggregation functions

Revision ID: 000000000002
Revises: 000000000001
Create Date: 2026-07-21

"""

from typing import Sequence, Union

from alembic import op

revision: str = "000000000002"
down_revision: Union[str, Sequence[str], None] = "000000000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION agg_daily(poll_date date)
        RETURNS void
        LANGUAGE sql
        AS $$
            WITH latest AS (
                SELECT DISTINCT ON (r.trip_id, r.stop_sequence)
                    r.trip_id, r.stop_sequence, r.delay_seconds
                FROM real_time_observations r
                WHERE r.service_date = poll_date
                ORDER BY r.trip_id, r.stop_sequence, r.poll_timestamp DESC
            )
            INSERT INTO daily_route_metrics
                (route_id, date, total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds)
            SELECT
                t.route_id,
                poll_date,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE l.delay_seconds < -60) AS early,
                COUNT(*) FILTER (WHERE l.delay_seconds BETWEEN -60 AND 300) AS on_time,
                COUNT(*) FILTER (WHERE l.delay_seconds > 300) AS late,
                ROUND(100.0 * COUNT(*) FILTER (WHERE l.delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(l.delay_seconds)::numeric, 1)
            FROM latest l
            JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
            JOIN trips t ON t.trip_id = st.trip_id
            GROUP BY t.route_id
            ON CONFLICT (route_id, date)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count       = EXCLUDED.early_count,
                on_time_count     = EXCLUDED.on_time_count,
                late_count        = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;
        $$;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION agg_hourly(poll_date date)
        RETURNS void
        LANGUAGE sql
        AS $$
            WITH latest AS (
                SELECT DISTINCT ON (r.trip_id, r.stop_sequence)
                    r.trip_id, r.stop_sequence, r.delay_seconds, r.poll_timestamp
                FROM real_time_observations r
                WHERE r.service_date = poll_date
                ORDER BY r.trip_id, r.stop_sequence, r.poll_timestamp DESC
            )
            INSERT INTO hourly_route_metrics
                (route_id, date, hour, total_observations, early_count, on_time_count,
                 late_count, on_time_percentage, avg_delay_seconds)
            SELECT
                t.route_id,
                poll_date,
                EXTRACT(HOUR FROM l.poll_timestamp)::int AS hour,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE l.delay_seconds < -60) AS early,
                COUNT(*) FILTER (WHERE l.delay_seconds BETWEEN -60 AND 300) AS on_time,
                COUNT(*) FILTER (WHERE l.delay_seconds > 300) AS late,
                ROUND(100.0 * COUNT(*) FILTER (WHERE l.delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(l.delay_seconds)::numeric, 1)
            FROM latest l
            JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
            JOIN trips t ON t.trip_id = st.trip_id
            GROUP BY t.route_id, EXTRACT(HOUR FROM l.poll_timestamp)
            ON CONFLICT (route_id, date, hour)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count       = EXCLUDED.early_count,
                on_time_count     = EXCLUDED.on_time_count,
                late_count        = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;
        $$;
    """)

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


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot")
    op.execute("DROP FUNCTION IF EXISTS agg_hourly")
    op.execute("DROP FUNCTION IF EXISTS agg_daily")
