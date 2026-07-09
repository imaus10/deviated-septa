from pathlib import Path

import alembic.config
import alembic.command

from poller.db import Session
from poller.models import StopTime, ArrivalRecord
from poller import gtfs_static, gtfs_rt


def run_migrations():
    alembic_cfg = alembic.config.Config(
        str(Path(__file__).parent.parent / "alembic.ini")
    )
    alembic.command.upgrade(alembic_cfg, "head")


def ensure_gtfs_static():
    session = Session()
    try:
        existing = session.query(StopTime).first()
        if existing is not None:
            print("GTFS static data already loaded, skipping import")
            return
    finally:
        session.close()

    print("GTFS static data not found — downloading and importing...")
    gtfs_static.run()
    print("GTFS static import complete")


def run_poll():
    session = Session()
    try:
        print("Fetching GTFS-RT trip updates...")
        raw = gtfs_rt.fetch_protobuf(gtfs_rt.BUS_TRIP_UPDATES)
        feed = gtfs_rt.parse_trip_updates(raw)
        print(f"  feed timestamp: {feed.header.timestamp}")
        print(f"  entities: {len(feed.entity)}")

        trip_ids = set()
        for entity in feed.entity:
            if entity.HasField("trip_update"):
                trip_ids.add(entity.trip_update.trip.trip_id)

        print(f"  unique trip_ids: {len(trip_ids)}")

        if not trip_ids:
            print("  no trip updates to process")
            return

        stop_times_cache = gtfs_rt.load_stop_times(session, trip_ids)
        print(f"  matched {len(stop_times_cache)} trips to stop_times")

        observations = gtfs_rt.extract_observations(feed, stop_times_cache)
        print(f"  extracted {len(observations)} stop observations")

        if not observations:
            print("  no observations to insert")
            return

        records = [ArrivalRecord(**obs) for obs in observations]
        session.bulk_save_objects(records)
        session.commit()
        print(f"  inserted {len(records)} arrival records")

        print("Building aggregations...")
        gtfs_rt.build_aggregations(session)
        print("  aggregations updated")

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main():
    print("=" * 50)
    print("SEPTA Reliability Poller")
    print("=" * 50)

    run_migrations()
    print("Migrations up to date")

    ensure_gtfs_static()

    run_poll()
    print("Done")


if __name__ == "__main__":
    main()
