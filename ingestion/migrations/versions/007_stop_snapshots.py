"""add stop metrics to latest snapshot

Revision ID: 000000000007
Revises: 000000000006
Create Date: 2026-08-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "000000000007"
down_revision: Union[str, Sequence[str], None] = "000000000006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


METRIC_COLUMNS = """
    total_observations = EXCLUDED.total_observations,
    early_count        = EXCLUDED.early_count,
    on_time_count      = EXCLUDED.on_time_count,
    late_count         = EXCLUDED.late_count,
    on_time_percentage = EXCLUDED.on_time_percentage,
    avg_delay_seconds  = EXCLUDED.avg_delay_seconds,
    updated_at         = EXCLUDED.updated_at
"""

# Pre-007 function bodies (route-only, no stop tables). Restored on downgrade so
# rolling back to 006 leaves a working DB.
_AGG_DAILY_PRE_007 = """
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
"""

_AGG_HOURLY_PRE_007 = """
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

_AGG_SNAPSHOT_DAILY_PRE_007 = """
    CREATE OR REPLACE FUNCTION agg_snapshot_daily(poll_date date, now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (period, route_id, route_name, route_type, total_observations,
             early_count, on_time_count, late_count,
             on_time_percentage, avg_delay_seconds, updated_at)
        SELECT
            'daily',
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
        ON CONFLICT (period, route_id)
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
        WHERE period = 'daily'
          AND route_id NOT IN (
              SELECT route_id FROM daily_route_metrics WHERE date = poll_date
          );
    $$;
"""

_AGG_SNAPSHOT_HOURLY_PRE_007 = """
    CREATE OR REPLACE FUNCTION agg_snapshot_hourly(now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (period, route_id, route_name, route_type, total_observations,
             early_count, on_time_count, late_count,
             on_time_percentage, avg_delay_seconds, updated_at)
        SELECT
            'hourly',
            t.route_id,
            r.route_short_name,
            r.route_type,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE o.delay_seconds < -60) AS early,
            COUNT(*) FILTER (WHERE o.delay_seconds BETWEEN -60 AND 300) AS on_time,
            COUNT(*) FILTER (WHERE o.delay_seconds > 300) AS late,
            ROUND(100.0 * COUNT(*) FILTER (WHERE o.delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
            ROUND(AVG(o.delay_seconds)::numeric, 1),
            now AS updated_at
        FROM real_time_observations o
        JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
        JOIN trips t ON t.trip_id = st.trip_id
        LEFT JOIN routes r ON r.route_id = t.route_id
        WHERE o.poll_timestamp >= now - interval '1 hour'
        GROUP BY t.route_id, r.route_short_name, r.route_type
        ON CONFLICT (period, route_id)
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
        WHERE period = 'hourly'
          AND route_id NOT IN (
              SELECT DISTINCT t.route_id
              FROM real_time_observations o
              JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
              JOIN trips t ON t.trip_id = st.trip_id
              WHERE o.poll_timestamp >= now - interval '1 hour'
          );
    $$;
"""

_AGG_SNAPSHOT_WEEKLY_PRE_007 = """
    CREATE OR REPLACE FUNCTION agg_snapshot_weekly(now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (period, route_id, route_name, route_type, total_observations,
             early_count, on_time_count, late_count,
             on_time_percentage, avg_delay_seconds, updated_at)
        SELECT
            'weekly',
            m.route_id,
            r.route_short_name,
            r.route_type,
            SUM(m.total_observations) AS total,
            SUM(m.early_count) AS early,
            SUM(m.on_time_count) AS on_time,
            SUM(m.late_count) AS late,
            ROUND(100.0 * SUM(m.on_time_count) / NULLIF(SUM(m.total_observations), 0), 1),
            ROUND(SUM(m.avg_delay_seconds * m.total_observations)::numeric / NULLIF(SUM(m.total_observations), 0), 1),
            now AS updated_at
        FROM daily_route_metrics m
        LEFT JOIN routes r ON r.route_id = m.route_id
        WHERE m.date BETWEEN (now::date - 6) AND now::date
        GROUP BY m.route_id, r.route_short_name, r.route_type
        ON CONFLICT (period, route_id)
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
        WHERE period = 'weekly'
          AND route_id NOT IN (
              SELECT route_id FROM daily_route_metrics
              WHERE date BETWEEN (now::date - 6) AND now::date
          );
    $$;
"""

_AGG_SNAPSHOT_ALL_PRE_007 = """
    CREATE OR REPLACE FUNCTION agg_snapshot_all(now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (period, route_id, route_name, route_type, total_observations,
             early_count, on_time_count, late_count,
             on_time_percentage, avg_delay_seconds, updated_at)
        SELECT
            'all',
            m.route_id,
            r.route_short_name,
            r.route_type,
            SUM(m.total_observations) AS total,
            SUM(m.early_count) AS early,
            SUM(m.on_time_count) AS on_time,
            SUM(m.late_count) AS late,
            ROUND(100.0 * SUM(m.on_time_count) / NULLIF(SUM(m.total_observations), 0), 1),
            ROUND(SUM(m.avg_delay_seconds * m.total_observations)::numeric / NULLIF(SUM(m.total_observations), 0), 1),
            now AS updated_at
        FROM daily_route_metrics m
        LEFT JOIN routes r ON r.route_id = m.route_id
        GROUP BY m.route_id, r.route_short_name, r.route_type
        ON CONFLICT (period, route_id)
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
        WHERE period = 'all'
          AND route_id NOT IN (
              SELECT route_id FROM daily_route_metrics
          );
    $$;
"""


def _agg_daily() -> str:
    return """
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
            ),
            enriched AS (
                SELECT l.delay_seconds, t.route_id, st.stop_id
                FROM latest l
                JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes r ON r.route_id = t.route_id
                WHERE r.route_type IN (0, 3, 11)
            )
            INSERT INTO daily_route_metrics
                (route_id, date, total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds)
            SELECT
                route_id,
                poll_date,
                COUNT(*),
                COUNT(*) FILTER (WHERE delay_seconds < -60),
                COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300),
                COUNT(*) FILTER (WHERE delay_seconds > 300),
                ROUND(100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(delay_seconds)::numeric, 1)
            FROM enriched
            GROUP BY route_id
            ON CONFLICT (route_id, date)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count        = EXCLUDED.early_count,
                on_time_count      = EXCLUDED.on_time_count,
                late_count         = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;

            WITH latest AS (
                SELECT DISTINCT ON (r.trip_id, r.stop_sequence)
                    r.trip_id, r.stop_sequence, r.delay_seconds
                FROM real_time_observations r
                WHERE r.service_date = poll_date
                ORDER BY r.trip_id, r.stop_sequence, r.poll_timestamp DESC
            ),
            enriched AS (
                SELECT l.delay_seconds, st.stop_id
                FROM latest l
                JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes r ON r.route_id = t.route_id
                WHERE r.route_type IN (0, 3, 11)
            )
            INSERT INTO daily_stop_metrics
                (stop_id, date, total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds)
            SELECT
                stop_id,
                poll_date,
                COUNT(*),
                COUNT(*) FILTER (WHERE delay_seconds < -60),
                COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300),
                COUNT(*) FILTER (WHERE delay_seconds > 300),
                ROUND(100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(delay_seconds)::numeric, 1)
            FROM enriched
            GROUP BY stop_id
            ON CONFLICT (stop_id, date)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count        = EXCLUDED.early_count,
                on_time_count      = EXCLUDED.on_time_count,
                late_count         = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;
        $$;
    """


def _agg_hourly() -> str:
    return """
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
            ),
            enriched AS (
                SELECT
                    l.delay_seconds,
                    EXTRACT(HOUR FROM l.poll_timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                    t.route_id,
                    st.stop_id
                FROM latest l
                JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes r ON r.route_id = t.route_id
                WHERE r.route_type IN (0, 3, 11)
            )
            INSERT INTO hourly_route_metrics
                (route_id, date, hour, total_observations, early_count, on_time_count,
                 late_count, on_time_percentage, avg_delay_seconds)
            SELECT
                route_id,
                poll_date,
                hour,
                COUNT(*),
                COUNT(*) FILTER (WHERE delay_seconds < -60),
                COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300),
                COUNT(*) FILTER (WHERE delay_seconds > 300),
                ROUND(100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(delay_seconds)::numeric, 1)
            FROM enriched
            GROUP BY route_id, hour
            ON CONFLICT (route_id, date, hour)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count        = EXCLUDED.early_count,
                on_time_count      = EXCLUDED.on_time_count,
                late_count         = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;

            WITH latest AS (
                SELECT DISTINCT ON (r.trip_id, r.stop_sequence)
                    r.trip_id, r.stop_sequence, r.delay_seconds, r.poll_timestamp
                FROM real_time_observations r
                WHERE r.service_date = poll_date
                ORDER BY r.trip_id, r.stop_sequence, r.poll_timestamp DESC
            ),
            enriched AS (
                SELECT
                    l.delay_seconds,
                    EXTRACT(HOUR FROM l.poll_timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                    st.stop_id
                FROM latest l
                JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes r ON r.route_id = t.route_id
                WHERE r.route_type IN (0, 3, 11)
            )
            INSERT INTO hourly_stop_metrics
                (stop_id, date, hour, total_observations, early_count, on_time_count,
                 late_count, on_time_percentage, avg_delay_seconds)
            SELECT
                stop_id,
                poll_date,
                hour,
                COUNT(*),
                COUNT(*) FILTER (WHERE delay_seconds < -60),
                COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300),
                COUNT(*) FILTER (WHERE delay_seconds > 300),
                ROUND(100.0 * COUNT(*) FILTER (WHERE delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(delay_seconds)::numeric, 1)
            FROM enriched
            GROUP BY stop_id, hour
            ON CONFLICT (stop_id, date, hour)
            DO UPDATE SET
                total_observations = EXCLUDED.total_observations,
                early_count        = EXCLUDED.early_count,
                on_time_count      = EXCLUDED.on_time_count,
                late_count         = EXCLUDED.late_count,
                on_time_percentage = EXCLUDED.on_time_percentage,
                avg_delay_seconds  = EXCLUDED.avg_delay_seconds;
        $$;
    """


def _snapshot_daily() -> str:
    return """
        CREATE OR REPLACE FUNCTION agg_snapshot_daily(poll_date date, now timestamptz)
        RETURNS void
        LANGUAGE sql
        AS $$
            INSERT INTO latest_snapshot
                (period, entity_type, entity_id, route_id, route_name, route_type,
                 total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                'daily', 'route', m.route_id, m.route_id, r.route_short_name, r.route_type,
                m.total_observations, m.early_count, m.on_time_count, m.late_count,
                m.on_time_percentage, m.avg_delay_seconds, now
            FROM daily_route_metrics m
            LEFT JOIN routes r ON r.route_id = m.route_id
            WHERE m.date = poll_date
            ON CONFLICT (period, entity_type, entity_id)
            DO UPDATE SET
                route_id = EXCLUDED.route_id, route_name = EXCLUDED.route_name, route_type = EXCLUDED.route_type,
                stop_id = NULL, stop_name = NULL, stop_lat = NULL, stop_lon = NULL,
                """ + METRIC_COLUMNS + """;

            INSERT INTO latest_snapshot
                (period, entity_type, entity_id, stop_id, stop_name, stop_lat, stop_lon,
                 total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                'daily', 'stop', m.stop_id, m.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                m.total_observations, m.early_count, m.on_time_count, m.late_count,
                m.on_time_percentage, m.avg_delay_seconds, now
            FROM daily_stop_metrics m
            LEFT JOIN stops s ON s.stop_id = m.stop_id
            WHERE m.date = poll_date
            ON CONFLICT (period, entity_type, entity_id)
            DO UPDATE SET
                route_id = NULL, route_name = NULL, route_type = NULL,
                stop_id = EXCLUDED.stop_id, stop_name = EXCLUDED.stop_name,
                stop_lat = EXCLUDED.stop_lat, stop_lon = EXCLUDED.stop_lon,
                """ + METRIC_COLUMNS + """;

            DELETE FROM latest_snapshot
            WHERE period = 'daily'
              AND (
                (entity_type = 'route' AND entity_id NOT IN (SELECT route_id FROM daily_route_metrics WHERE date = poll_date))
                OR
                (entity_type = 'stop' AND entity_id NOT IN (SELECT stop_id FROM daily_stop_metrics WHERE date = poll_date))
              );
        $$;
    """


def _snapshot_hourly() -> str:
    return """
        CREATE OR REPLACE FUNCTION agg_snapshot_hourly(now timestamptz)
        RETURNS void
        LANGUAGE sql
        AS $$
            INSERT INTO latest_snapshot
                (period, entity_type, entity_id, route_id, route_name, route_type,
                 total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                'hourly', 'route', t.route_id, t.route_id, r.route_short_name, r.route_type,
                COUNT(*),
                COUNT(*) FILTER (WHERE o.delay_seconds < -60),
                COUNT(*) FILTER (WHERE o.delay_seconds BETWEEN -60 AND 300),
                COUNT(*) FILTER (WHERE o.delay_seconds > 300),
                ROUND(100.0 * COUNT(*) FILTER (WHERE o.delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(o.delay_seconds)::numeric, 1),
                now
            FROM real_time_observations o
            JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
            JOIN trips t ON t.trip_id = st.trip_id
            JOIN routes r ON r.route_id = t.route_id
            WHERE o.poll_timestamp >= now - interval '1 hour'
              AND r.route_type IN (0, 3, 11)
            GROUP BY t.route_id, r.route_short_name, r.route_type
            ON CONFLICT (period, entity_type, entity_id)
            DO UPDATE SET
                route_id = EXCLUDED.route_id, route_name = EXCLUDED.route_name, route_type = EXCLUDED.route_type,
                stop_id = NULL, stop_name = NULL, stop_lat = NULL, stop_lon = NULL,
                """ + METRIC_COLUMNS + """;

            INSERT INTO latest_snapshot
                (period, entity_type, entity_id, stop_id, stop_name, stop_lat, stop_lon,
                 total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                'hourly', 'stop', st.stop_id, st.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                COUNT(*),
                COUNT(*) FILTER (WHERE o.delay_seconds < -60),
                COUNT(*) FILTER (WHERE o.delay_seconds BETWEEN -60 AND 300),
                COUNT(*) FILTER (WHERE o.delay_seconds > 300),
                ROUND(100.0 * COUNT(*) FILTER (WHERE o.delay_seconds BETWEEN -60 AND 300) / NULLIF(COUNT(*), 0), 1),
                ROUND(AVG(o.delay_seconds)::numeric, 1),
                now
            FROM real_time_observations o
            JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
            JOIN trips t ON t.trip_id = st.trip_id
            JOIN routes r ON r.route_id = t.route_id
            LEFT JOIN stops s ON s.stop_id = st.stop_id
            WHERE o.poll_timestamp >= now - interval '1 hour'
              AND r.route_type IN (0, 3, 11)
            GROUP BY st.stop_id, s.stop_name, s.stop_lat, s.stop_lon
            ON CONFLICT (period, entity_type, entity_id)
            DO UPDATE SET
                route_id = NULL, route_name = NULL, route_type = NULL,
                stop_id = EXCLUDED.stop_id, stop_name = EXCLUDED.stop_name,
                stop_lat = EXCLUDED.stop_lat, stop_lon = EXCLUDED.stop_lon,
                """ + METRIC_COLUMNS + """;

            DELETE FROM latest_snapshot
            WHERE period = 'hourly'
              AND (
                (entity_type = 'route' AND entity_id NOT IN (
                    SELECT DISTINCT t.route_id
                    FROM real_time_observations o
                    JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
                    JOIN trips t ON t.trip_id = st.trip_id
                    JOIN routes r ON r.route_id = t.route_id
                    WHERE o.poll_timestamp >= now - interval '1 hour'
                      AND r.route_type IN (0, 3, 11)
                ))
                OR
                (entity_type = 'stop' AND entity_id NOT IN (
                    SELECT DISTINCT st.stop_id
                    FROM real_time_observations o
                    JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
                    JOIN trips t ON t.trip_id = st.trip_id
                    JOIN routes r ON r.route_id = t.route_id
                    WHERE o.poll_timestamp >= now - interval '1 hour'
                      AND r.route_type IN (0, 3, 11)
                ))
              );
        $$;
    """


def _snapshot_rollup(period: str, where_clause: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION agg_snapshot_{period}(now timestamptz)
        RETURNS void
        LANGUAGE sql
        AS $$
            INSERT INTO latest_snapshot
                (period, entity_type, entity_id, route_id, route_name, route_type,
                 total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                '{period}', 'route', m.route_id, m.route_id, r.route_short_name, r.route_type,
                SUM(m.total_observations),
                SUM(m.early_count),
                SUM(m.on_time_count),
                SUM(m.late_count),
                ROUND(100.0 * SUM(m.on_time_count) / NULLIF(SUM(m.total_observations), 0), 1),
                ROUND(SUM(m.avg_delay_seconds * m.total_observations)::numeric / NULLIF(SUM(m.total_observations), 0), 1),
                now
            FROM daily_route_metrics m
            LEFT JOIN routes r ON r.route_id = m.route_id
            {where_clause}
            GROUP BY m.route_id, r.route_short_name, r.route_type
            ON CONFLICT (period, entity_type, entity_id)
            DO UPDATE SET
                route_id = EXCLUDED.route_id, route_name = EXCLUDED.route_name, route_type = EXCLUDED.route_type,
                stop_id = NULL, stop_name = NULL, stop_lat = NULL, stop_lon = NULL,
                {METRIC_COLUMNS};

            INSERT INTO latest_snapshot
                (period, entity_type, entity_id, stop_id, stop_name, stop_lat, stop_lon,
                 total_observations, early_count, on_time_count, late_count,
                 on_time_percentage, avg_delay_seconds, updated_at)
            SELECT
                '{period}', 'stop', m.stop_id, m.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                SUM(m.total_observations),
                SUM(m.early_count),
                SUM(m.on_time_count),
                SUM(m.late_count),
                ROUND(100.0 * SUM(m.on_time_count) / NULLIF(SUM(m.total_observations), 0), 1),
                ROUND(SUM(m.avg_delay_seconds * m.total_observations)::numeric / NULLIF(SUM(m.total_observations), 0), 1),
                now
            FROM daily_stop_metrics m
            LEFT JOIN stops s ON s.stop_id = m.stop_id
            {where_clause}
            GROUP BY m.stop_id, s.stop_name, s.stop_lat, s.stop_lon
            ON CONFLICT (period, entity_type, entity_id)
            DO UPDATE SET
                route_id = NULL, route_name = NULL, route_type = NULL,
                stop_id = EXCLUDED.stop_id, stop_name = EXCLUDED.stop_name,
                stop_lat = EXCLUDED.stop_lat, stop_lon = EXCLUDED.stop_lon,
                {METRIC_COLUMNS};

            DELETE FROM latest_snapshot
            WHERE period = '{period}'
              AND (
                (entity_type = 'route' AND entity_id NOT IN (SELECT route_id FROM daily_route_metrics m {where_clause}))
                OR
                (entity_type = 'stop' AND entity_id NOT IN (SELECT stop_id FROM daily_stop_metrics m {where_clause}))
              );
        $$;
    """


def upgrade() -> None:
    op.create_table(
        "daily_stop_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("stop_id", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("total_observations", sa.Integer, nullable=False, server_default="0"),
        sa.Column("early_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("late_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_percentage", sa.Float),
        sa.Column("avg_delay_seconds", sa.Float),
        sa.UniqueConstraint("stop_id", "date", name="uq_daily_stop_date"),
    )
    op.create_table(
        "hourly_stop_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("stop_id", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("hour", sa.Integer, nullable=False),
        sa.Column("total_observations", sa.Integer, nullable=False, server_default="0"),
        sa.Column("early_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("late_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_percentage", sa.Float),
        sa.Column("avg_delay_seconds", sa.Float),
        sa.UniqueConstraint("stop_id", "date", "hour", name="uq_hourly_stop_date_hour"),
    )

    op.add_column("latest_snapshot", sa.Column("entity_type", sa.Text(), nullable=True))
    op.add_column("latest_snapshot", sa.Column("entity_id", sa.Text(), nullable=True))
    op.add_column("latest_snapshot", sa.Column("stop_id", sa.Text(), nullable=True))
    op.add_column("latest_snapshot", sa.Column("stop_name", sa.Text(), nullable=True))
    op.add_column("latest_snapshot", sa.Column("stop_lat", sa.Float(), nullable=True))
    op.add_column("latest_snapshot", sa.Column("stop_lon", sa.Float(), nullable=True))
    op.execute("UPDATE latest_snapshot SET entity_type = 'route', entity_id = route_id WHERE entity_type IS NULL")
    op.alter_column("latest_snapshot", "entity_type", nullable=False)
    op.alter_column("latest_snapshot", "entity_id", nullable=False)
    op.drop_constraint("latest_snapshot_pkey", "latest_snapshot", type_="primary")
    op.alter_column("latest_snapshot", "route_id", nullable=True)
    op.create_primary_key("latest_snapshot_pkey", "latest_snapshot", ["period", "entity_type", "entity_id"])
    op.create_index("ix_latest_snapshot_period_entity", "latest_snapshot", ["period", "entity_type"])

    op.execute(_agg_daily())
    op.execute(_agg_hourly())
    op.execute(_snapshot_daily())
    op.execute(_snapshot_hourly())
    op.execute(_snapshot_rollup("weekly", "WHERE m.date BETWEEN (now::date - 6) AND now::date"))
    op.execute(_snapshot_rollup("all", ""))

    # One-time backfill: agg_daily() now also populates daily_stop_metrics, but
    # only for the date it is called with. Historical service dates have no rows
    # in daily_stop_metrics, so weekly/all stop rankings would be wrong until
    # those dates are aggregated. Re-run agg_daily over every observed date
    # (idempotent for daily_route_metrics) and then refresh all snapshots so
    # stop rankings are correct immediately.
    op.execute(
        """
        SELECT agg_daily(service_date)
        FROM (SELECT DISTINCT service_date FROM real_time_observations ORDER BY service_date) d
        """
    )
    # Purge metric rows for routes that fell out of scope (e.g. rail), which
    # agg_daily() no longer produces but which may pre-date the scope filter.
    op.execute(
        """
        DELETE FROM daily_route_metrics
        WHERE route_id NOT IN (SELECT route_id FROM routes WHERE route_type IN (0, 3, 11));
        DELETE FROM hourly_route_metrics
        WHERE route_id NOT IN (SELECT route_id FROM routes WHERE route_type IN (0, 3, 11));
        """
    )
    op.execute("SELECT agg_snapshot_daily(CURRENT_DATE, now())")
    op.execute("SELECT agg_snapshot_hourly(now())")
    op.execute("SELECT agg_snapshot_weekly(now())")
    op.execute("SELECT agg_snapshot_all(now())")


def downgrade() -> None:
    # Restore the pre-007 function bodies first. 007 CREATE OR REPLACEd all of
    # them, so simply dropping leaves the DB on a broken "006" that references
    # the stop tables it is about to lose (or loses the functions entirely).
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_all(timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_daily(date, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_hourly(timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_weekly(timestamptz)")
    op.execute(_AGG_DAILY_PRE_007)
    op.execute(_AGG_HOURLY_PRE_007)
    op.execute(_AGG_SNAPSHOT_DAILY_PRE_007)
    op.execute(_AGG_SNAPSHOT_HOURLY_PRE_007)
    op.execute(_AGG_SNAPSHOT_WEEKLY_PRE_007)
    op.execute(_AGG_SNAPSHOT_ALL_PRE_007)

    op.execute("DELETE FROM latest_snapshot WHERE entity_type <> 'route'")
    op.drop_index("ix_latest_snapshot_period_entity", table_name="latest_snapshot")
    op.drop_constraint("latest_snapshot_pkey", "latest_snapshot", type_="primary")
    op.alter_column("latest_snapshot", "route_id", nullable=False)
    op.create_primary_key("latest_snapshot_pkey", "latest_snapshot", ["period", "route_id"])
    op.drop_column("latest_snapshot", "stop_lon")
    op.drop_column("latest_snapshot", "stop_lat")
    op.drop_column("latest_snapshot", "stop_name")
    op.drop_column("latest_snapshot", "stop_id")
    op.drop_column("latest_snapshot", "entity_id")
    op.drop_column("latest_snapshot", "entity_type")
    op.drop_table("hourly_stop_metrics")
    op.drop_table("daily_stop_metrics")
