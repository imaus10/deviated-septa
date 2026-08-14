"""exclude unmeasurable trolley-subway stops from aggregation

SEPTA cannot measure trolley positions inside the Market St subway (13th St to
40th St Portal): GTFS-RT predictions there are model-inflated by ~10-25 min
(TransitView returns a "998" no-data sentinel for in-tunnel vehicles), and the
poller's "latest prediction per trip-stop" model never corrects them because
the feed stops re-predicting passed tunnel stops. Route and stop metrics built
from those predictions are distorted (e.g. trolley routes lose 3-8 on-time
points). Exclude the tunnel segment from all aggregations so every metric
reflects only GPS-measured stops.

The raw real_time_observations rows are untouched; only the aggregation
functions stop consuming them.

Revision ID: 000000000011
Revises: 000000000010
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "000000000011"
down_revision: Union[str, Sequence[str], None] = "000000000010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Market St trolley subway, 13th St to 40th St Portal (both directions).
_TUNNEL_STOP_IDS = (
    "283", "20659", "31140", "20660", "20646", "20661", "20645",
    "20662", "20643", "20658", "20642", "20733", "20732", "20734",
    "20731", "301", "20804", "20640", "20641", "20664", "287",
)

_EXCLUDE_PREDICATE = (
    "NOT EXISTS (SELECT 1 FROM unmeasured_stops u WHERE u.stop_id = st.stop_id)"
)


def _insert_tunnel_stops() -> str:
    values = ",\n        ".join(f"('{sid}')" for sid in _TUNNEL_STOP_IDS)
    return f"INSERT INTO unmeasured_stops (stop_id) VALUES\n        {values};"


_AGG_DAILY_PRE = """CREATE OR REPLACE FUNCTION public.agg_daily(poll_date date)
 RETURNS void
 LANGUAGE sql
AS $function$
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
        $function$"""

_AGG_HOURLY_PRE = """CREATE OR REPLACE FUNCTION public.agg_hourly(poll_date date)
 RETURNS void
 LANGUAGE sql
AS $function$
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
        $function$"""

_AGG_SNAPSHOT_HOURLY_PRE = """CREATE OR REPLACE FUNCTION public.agg_snapshot_hourly(now timestamp with time zone)
 RETURNS void
 LANGUAGE sql
AS $function$
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
                
    total_observations = EXCLUDED.total_observations,
    early_count        = EXCLUDED.early_count,
    on_time_count      = EXCLUDED.on_time_count,
    late_count         = EXCLUDED.late_count,
    on_time_percentage = EXCLUDED.on_time_percentage,
    avg_delay_seconds  = EXCLUDED.avg_delay_seconds,
    updated_at         = EXCLUDED.updated_at
;

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
                
    total_observations = EXCLUDED.total_observations,
    early_count        = EXCLUDED.early_count,
    on_time_count      = EXCLUDED.on_time_count,
    late_count         = EXCLUDED.late_count,
    on_time_percentage = EXCLUDED.on_time_percentage,
    avg_delay_seconds  = EXCLUDED.avg_delay_seconds,
    updated_at         = EXCLUDED.updated_at
;

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
        $function$"""

# 011 bodies: the pre-011 text plus the unmeasured-stop exclusion on every
# raw-observation join (agg_snapshot_hourly also needs it in its orphan-delete
# subqueries so tunnel stops are treated as gone, not merely un-inserted).
_AGG_DAILY = _AGG_DAILY_PRE.replace(
    "WHERE r.route_type IN (0, 3, 11)",
    "WHERE r.route_type IN (0, 3, 11)\n                AND " + _EXCLUDE_PREDICATE,
)
_AGG_HOURLY = _AGG_HOURLY_PRE.replace(
    "WHERE r.route_type IN (0, 3, 11)",
    "WHERE r.route_type IN (0, 3, 11)\n                AND " + _EXCLUDE_PREDICATE,
)
_AGG_SNAPSHOT_HOURLY = _AGG_SNAPSHOT_HOURLY_PRE.replace(
    "AND r.route_type IN (0, 3, 11)",
    "AND r.route_type IN (0, 3, 11)\n              AND " + _EXCLUDE_PREDICATE,
)

_TUNNEL_IDS_SQL = ",\n    ".join(f"'{sid}'" for sid in _TUNNEL_STOP_IDS)


def upgrade() -> None:
    op.execute("CREATE TABLE unmeasured_stops (stop_id text PRIMARY KEY)")
    op.execute(_insert_tunnel_stops())

    op.execute(_AGG_DAILY)
    op.execute(_AGG_HOURLY)
    op.execute(_AGG_SNAPSHOT_HOURLY)

    # Rebuild all metrics from raw observations excluding tunnel stops.
    op.execute(
        """
        SELECT agg_daily(service_date)
        FROM (SELECT DISTINCT service_date FROM real_time_observations ORDER BY service_date) d
        """
    )
    op.execute(
        """
        SELECT agg_hourly(service_date)
        FROM (SELECT DISTINCT service_date FROM real_time_observations ORDER BY service_date) d
        """
    )
    # agg_daily/agg_hourly upsert but never delete; drop stale tunnel-stop rows
    # that predate the exclusion (route rows survive because routes keep surface
    # stops, so only stop rows need purging).
    op.execute(
        f"DELETE FROM daily_stop_metrics WHERE stop_id IN ({_TUNNEL_IDS_SQL});\n"
        f"DELETE FROM hourly_stop_metrics WHERE stop_id IN ({_TUNNEL_IDS_SQL});"
    )
    # Refresh all snapshots (their orphan-deletes remove tunnel stops from
    # latest_snapshot).
    op.execute("SELECT agg_snapshot_daily(CURRENT_DATE, now())")
    op.execute("SELECT agg_snapshot_hourly(now())")
    op.execute("SELECT agg_snapshot_weekly(now())")
    op.execute("SELECT agg_snapshot_all(now())")


def downgrade() -> None:
    op.execute(_AGG_DAILY_PRE)
    op.execute(_AGG_HOURLY_PRE)
    op.execute(_AGG_SNAPSHOT_HOURLY_PRE)
    op.drop_table("unmeasured_stops")
