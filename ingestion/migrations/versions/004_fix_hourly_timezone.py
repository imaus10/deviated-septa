"""fix agg_hourly to extract Eastern hour instead of UTC hour

Revision ID: 000000000004
Revises: 000000000003
Create Date: 2026-07-24

"""

from typing import Sequence, Union

from alembic import op

revision: str = "000000000004"
down_revision: Union[str, Sequence[str], None] = "000000000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AGG_HOURLY_FIXED = """
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
                EXTRACT(HOUR FROM l.poll_timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE l.delay_seconds < -60) AS early,
                COUNT(*) FILTER (WHERE l.delay_seconds BETWEEN -60 AND 300) AS on_time,
                COUNT(*) FILTER (WHERE l.delay_seconds > 300) AS late,
                ROUND(100.0 * COUNT(*) FILTER (WHERE l.delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(l.delay_seconds)::numeric, 1)
            FROM latest l
            JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
            JOIN trips t ON t.trip_id = st.trip_id
            GROUP BY t.route_id, EXTRACT(HOUR FROM l.poll_timestamp AT TIME ZONE 'America/New_York')
            ON CONFLICT (route_id, date, hour)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count       = EXCLUDED.early_count,
                on_time_count     = EXCLUDED.on_time_count,
                late_count        = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;
        $$;
    """

_AGG_HOURLY_ORIGINAL = """
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
    """


def upgrade() -> None:
    op.execute(_AGG_HOURLY_FIXED)


def downgrade() -> None:
    op.execute(_AGG_HOURLY_ORIGINAL)
