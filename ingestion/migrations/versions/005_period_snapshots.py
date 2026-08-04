"""period snapshots

Adds a granularity column to latest_snapshot (hourly/daily/weekly)
and replaces agg_snapshot with per-period agg functions.

Revision ID: 000000000005
Revises: 000000000004
Create Date: 2026-08-03 17:28:40.710730

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000005'
down_revision: Union[str, Sequence[str], None] = '000000000004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AGG_SNAPSHOT_DAILY = """
    CREATE OR REPLACE FUNCTION agg_snapshot_daily(poll_date date, now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (granularity, route_id, route_name, route_type, total_observations,
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
        ON CONFLICT (granularity, route_id)
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
        WHERE granularity = 'daily'
          AND route_id NOT IN (
              SELECT route_id FROM daily_route_metrics WHERE date = poll_date
          );
    $$;
"""

_AGG_SNAPSHOT_HOURLY = """
    CREATE OR REPLACE FUNCTION agg_snapshot_hourly(now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (granularity, route_id, route_name, route_type, total_observations,
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
        ON CONFLICT (granularity, route_id)
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
        WHERE granularity = 'hourly'
          AND route_id NOT IN (
              SELECT DISTINCT t.route_id
              FROM real_time_observations o
              JOIN stop_times st ON st.trip_id = o.trip_id AND st.stop_sequence = o.stop_sequence
              JOIN trips t ON t.trip_id = st.trip_id
              WHERE o.poll_timestamp >= now - interval '1 hour'
          );
    $$;
"""

_AGG_SNAPSHOT_WEEKLY = """
    CREATE OR REPLACE FUNCTION agg_snapshot_weekly(now timestamptz)
    RETURNS void
    LANGUAGE sql
    AS $$
        INSERT INTO latest_snapshot
            (granularity, route_id, route_name, route_type, total_observations,
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
        ON CONFLICT (granularity, route_id)
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
        WHERE granularity = 'weekly'
          AND route_id NOT IN (
              SELECT route_id FROM daily_route_metrics
              WHERE date BETWEEN (now::date - 6) AND now::date
          );
    $$;
"""

# Prior agg_snapshot (from 003) — restored on downgrade so the old poller
# continues to work if we roll back.
_AGG_SNAPSHOT_ORIGINAL = """
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
"""


def upgrade() -> None:
    op.add_column('latest_snapshot', sa.Column('granularity', sa.Text(), nullable=True))
    op.execute("UPDATE latest_snapshot SET granularity = 'daily' WHERE granularity IS NULL")
    op.alter_column('latest_snapshot', 'granularity', nullable=False)

    op.drop_constraint('latest_snapshot_pkey', 'latest_snapshot', type_='primary')
    op.create_primary_key('latest_snapshot_pkey', 'latest_snapshot', ['granularity', 'route_id'])

    op.create_index('ix_obs_poll_timestamp', 'real_time_observations', ['poll_timestamp'], unique=False)

    op.execute("DROP FUNCTION IF EXISTS agg_snapshot(date, timestamptz)")
    op.execute(_AGG_SNAPSHOT_DAILY)
    op.execute(_AGG_SNAPSHOT_HOURLY)
    op.execute(_AGG_SNAPSHOT_WEEKLY)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_daily(date, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_hourly(timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS agg_snapshot_weekly(timestamptz)")
    op.execute(_AGG_SNAPSHOT_ORIGINAL)

    op.drop_index('ix_obs_poll_timestamp', table_name='real_time_observations')

    op.drop_constraint('latest_snapshot_pkey', 'latest_snapshot', type_='primary')
    op.create_primary_key('latest_snapshot_pkey', 'latest_snapshot', ['route_id'])
    op.drop_column('latest_snapshot', 'granularity')
