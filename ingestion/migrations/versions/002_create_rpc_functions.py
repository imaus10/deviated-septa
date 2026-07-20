"""create RPC functions for Supabase Data API poller access

The poller runs in GitHub Actions (IPv4-only) and cannot connect to
Supabase's IPv6-only Postgres directly. Instead it calls these functions
via the REST API (POST /rest/v1/rpc/function_name).

Run this migration from a machine with IPv6: DATABASE_URL=... alembic upgrade head
"""

from typing import Sequence, Union
from alembic import op

revision: str = "13d786d4b2d1"
down_revision: Union[str, None] = "43361a53ee61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STOP_TIMES_SQL = """
CREATE OR REPLACE FUNCTION get_stop_times(req_trip_ids text[])
RETURNS TABLE(trip_id text, stop_sequence integer, arrival_time text, stop_id text)
LANGUAGE sql STABLE
AS $$
    SELECT trip_id, stop_sequence, arrival_time, stop_id
    FROM stop_times
    WHERE trip_id = ANY(req_trip_ids)
    ORDER BY trip_id, stop_sequence;
$$;
"""


DAILY_SQL = """
CREATE OR REPLACE FUNCTION agg_daily(poll_date date)
RETURNS void
LANGUAGE sql
AS $$
    WITH latest AS (
        SELECT DISTINCT ON (trip_id, stop_id, stop_sequence, poll_timestamp::date)
            route_id, delay_seconds, poll_timestamp
        FROM arrival_records
        WHERE poll_timestamp >= poll_date
        ORDER BY trip_id, stop_id, stop_sequence, poll_timestamp::date, poll_timestamp DESC
    )
    INSERT INTO daily_route_metrics
        (route_id, date, total_observations, early_count, on_time_count, late_count,
         on_time_percentage, avg_delay_seconds)
    SELECT
        route_id,
        DATE(poll_timestamp) AS date,
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE delay_seconds < -60) AS early,
        COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) AS on_time,
        COUNT(*) FILTER (WHERE delay_seconds > 300) AS late,
        ROUND(100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
        ROUND(AVG(delay_seconds)::numeric, 1)
    FROM latest
    GROUP BY route_id, DATE(poll_timestamp)
    ON CONFLICT (route_id, date)
    DO UPDATE SET
        total_observations = EXCLUDED.total_observations,
        early_count       = EXCLUDED.early_count,
        on_time_count     = EXCLUDED.on_time_count,
        late_count        = EXCLUDED.late_count,
        on_time_percentage = EXCLUDED.on_time_percentage,
        avg_delay_seconds  = EXCLUDED.avg_delay_seconds;
$$;
"""


HOURLY_SQL = """
CREATE OR REPLACE FUNCTION agg_hourly(poll_date date)
RETURNS void
LANGUAGE sql
AS $$
    WITH latest AS (
        SELECT DISTINCT ON (trip_id, stop_id, stop_sequence, poll_timestamp::date)
            route_id, delay_seconds, poll_timestamp
        FROM arrival_records
        WHERE poll_timestamp >= poll_date
        ORDER BY trip_id, stop_id, stop_sequence, poll_timestamp::date, poll_timestamp DESC
    )
    INSERT INTO hourly_route_metrics
        (route_id, date, hour, total_observations, early_count, on_time_count,
         late_count, on_time_percentage, avg_delay_seconds)
    SELECT
        route_id,
        DATE(poll_timestamp) AS date,
        EXTRACT(HOUR FROM poll_timestamp)::int AS hour,
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE delay_seconds < -60) AS early,
        COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) AS on_time,
        COUNT(*) FILTER (WHERE delay_seconds > 300) AS late,
        ROUND(100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
        ROUND(AVG(delay_seconds)::numeric, 1)
    FROM latest
    GROUP BY route_id, DATE(poll_timestamp), EXTRACT(HOUR FROM poll_timestamp)
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


SNAPSHOT_SQL = """
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
"""


def upgrade() -> None:
    op.execute(STOP_TIMES_SQL)
    op.execute(DAILY_SQL)
    op.execute(HOURLY_SQL)
    op.execute(SNAPSHOT_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS get_stop_times")
    op.execute("DROP FUNCTION IF EXISTS agg_daily")
    op.execute("DROP FUNCTION IF EXISTS agg_hourly")
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot")
