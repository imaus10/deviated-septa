#!/usr/bin/env python
"""Diagnose and repair service_date/delay rows affected by midnight trips.

Dry-run is the default. Permanent database writes happen only with --apply.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from psycopg2.extras import DictCursor, execute_values

INGESTION_DIR = Path(__file__).resolve().parents[1]
if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from poller.db import get_connection  # noqa: E402
from poller.gtfs_rt import infer_service_date, scheduled_to_ts  # noqa: E402


DEFAULT_THRESHOLD_SECONDS = 12 * 60 * 60
DEFAULT_BATCH_SIZE = 5000
DEFAULT_SAMPLE_LIMIT = 20


def log(message: str = "") -> None:
    print(message, flush=True)


@dataclass(frozen=True)
class RepairProposal:
    source_id: int
    trip_id: str
    stop_sequence: int
    old_service_date: date
    target_service_date: date
    old_delay_seconds: int
    target_delay_seconds: int
    predicted_time: datetime
    poll_timestamp: datetime
    vehicle_id: str | None
    arrival_time: str
    repairable: bool
    skip_reason: str | None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose and optionally repair real_time_observations rows whose "
            "delay_seconds look wrong because service_date was inferred from "
            "predicted_time."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform permanent updates; omitted means dry-run only",
    )
    parser.add_argument(
        "--date-from",
        type=parse_date,
        help="only consider rows with service_date on or after this YYYY-MM-DD",
    )
    parser.add_argument(
        "--date-to",
        type=parse_date,
        help="only consider rows with service_date on or before this YYYY-MM-DD",
    )
    parser.add_argument(
        "--threshold-seconds",
        type=int,
        default=DEFAULT_THRESHOLD_SECONDS,
        help=(
            "candidate threshold for ABS(delay_seconds); default "
            f"{DEFAULT_THRESHOLD_SECONDS}"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"rows to process per batch while staging; default {DEFAULT_BATCH_SIZE}",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=DEFAULT_SAMPLE_LIMIT,
        help=f"sample rows to print from each report; default {DEFAULT_SAMPLE_LIMIT}",
    )
    parser.add_argument(
        "--force-unresolved",
        action="store_true",
        help=(
            "allow applying rows whose recomputed delay is still outside the "
            "threshold; normally these are reported and skipped"
        ),
    )
    parser.add_argument(
        "--skip-metrics-rebuild",
        action="store_true",
        help="with --apply, do not rebuild daily/hourly metrics for affected dates",
    )
    return parser.parse_args(argv)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid YYYY-MM-DD date"
        ) from exc


def scoped_where(args: argparse.Namespace, alias: str = "r") -> tuple[str, list]:
    clauses = ["TRUE"]
    params: list = []
    if args.date_from:
        clauses.append(f"{alias}.service_date >= %s")
        params.append(args.date_from)
    if args.date_to:
        clauses.append(f"{alias}.service_date <= %s")
        params.append(args.date_to)
    return " AND ".join(clauses), params


def diagnose_delay_buckets(conn, args: argparse.Namespace) -> None:
    where_sql, params = scoped_where(args)
    threshold = args.threshold_seconds

    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS total_rows,
                COUNT(*) FILTER (WHERE delay_seconds < -%s) AS very_early_rows,
                COUNT(*) FILTER (WHERE delay_seconds > %s) AS very_late_rows,
                COUNT(*) FILTER (WHERE ABS(delay_seconds) > %s) AS candidates,
                MIN(delay_seconds) AS min_delay_seconds,
                MAX(delay_seconds) AS max_delay_seconds
            FROM real_time_observations r
            WHERE {where_sql}
            """,
            [threshold, threshold, threshold, *params],
        )
        row = cur.fetchone()

    log("Delay bucket diagnosis")
    log(f"  total_rows:        {row['total_rows']}")
    log(f"  candidates:        {row['candidates']}")
    log(f"  very_early_rows:   {row['very_early_rows']}")
    log(f"  very_late_rows:    {row['very_late_rows']}")
    log(f"  min_delay_seconds: {row['min_delay_seconds']}")
    log(f"  max_delay_seconds: {row['max_delay_seconds']}")


def print_candidate_dates(conn, args: argparse.Namespace) -> None:
    where_sql, params = scoped_where(args)
    threshold = args.threshold_seconds

    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            f"""
            SELECT service_date, COUNT(*) AS candidate_rows
            FROM real_time_observations r
            WHERE {where_sql}
              AND ABS(delay_seconds) > %s
            GROUP BY service_date
            ORDER BY service_date
            """,
            [*params, threshold],
        )
        rows = cur.fetchall()

    log("\nCandidate rows by current service_date")
    if not rows:
        log("  none")
        return
    for row in rows:
        log(f"  {row['service_date']}: {row['candidate_rows']}")


def create_candidate_table(conn, args: argparse.Namespace) -> int:
    where_sql, params = scoped_where(args)

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _repair_source")
        cur.execute(
            """
            CREATE TEMP TABLE _repair_source (
                id bigint PRIMARY KEY,
                trip_id text NOT NULL,
                stop_sequence int NOT NULL,
                old_service_date date NOT NULL,
                old_delay_seconds int NOT NULL,
                predicted_time timestamptz NOT NULL,
                poll_timestamp timestamptz NOT NULL,
                vehicle_id text,
                arrival_time text NOT NULL
            ) ON COMMIT DROP
            """
        )
        cur.execute(
            f"""
            INSERT INTO _repair_source (
                id, trip_id, stop_sequence, old_service_date,
                old_delay_seconds, predicted_time, poll_timestamp,
                vehicle_id, arrival_time
            )
            SELECT
                r.id, r.trip_id, r.stop_sequence, r.service_date,
                r.delay_seconds, r.predicted_time, r.poll_timestamp,
                r.vehicle_id, st.arrival_time
            FROM real_time_observations r
            JOIN stop_times st
              ON st.trip_id = r.trip_id
             AND st.stop_sequence = r.stop_sequence
            WHERE {where_sql}
              AND ABS(r.delay_seconds) > %s
              AND st.arrival_time IS NOT NULL
            """,
            [*params, args.threshold_seconds],
        )
        cur.execute("SELECT COUNT(*) FROM _repair_source")
        return cur.fetchone()[0]


def create_stage_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _repair_stage")
        cur.execute(
            """
            CREATE TEMP TABLE _repair_stage (
                source_id bigint PRIMARY KEY,
                trip_id text NOT NULL,
                stop_sequence int NOT NULL,
                old_service_date date NOT NULL,
                target_service_date date NOT NULL,
                old_delay_seconds int NOT NULL,
                target_delay_seconds int NOT NULL,
                predicted_time timestamptz NOT NULL,
                poll_timestamp timestamptz NOT NULL,
                vehicle_id text,
                arrival_time text NOT NULL,
                repairable boolean NOT NULL,
                skip_reason text
            ) ON COMMIT DROP
            """
        )


def build_proposal(row, args: argparse.Namespace) -> RepairProposal:
    predicted_ts = int(row["predicted_time"].timestamp())
    target_service_date = infer_service_date(row["arrival_time"], predicted_ts)
    scheduled_ts = scheduled_to_ts(row["arrival_time"], target_service_date)
    target_delay_seconds = predicted_ts - scheduled_ts

    repairable = True
    skip_reason = None
    if (
        abs(target_delay_seconds) > args.threshold_seconds
        and not args.force_unresolved
    ):
        repairable = False
        skip_reason = "recomputed delay still outside threshold"

    return RepairProposal(
        source_id=row["id"],
        trip_id=row["trip_id"],
        stop_sequence=row["stop_sequence"],
        old_service_date=row["old_service_date"],
        target_service_date=target_service_date,
        old_delay_seconds=row["old_delay_seconds"],
        target_delay_seconds=target_delay_seconds,
        predicted_time=row["predicted_time"],
        poll_timestamp=row["poll_timestamp"],
        vehicle_id=row["vehicle_id"],
        arrival_time=row["arrival_time"],
        repairable=repairable,
        skip_reason=skip_reason,
    )


def stage_proposals(conn, args: argparse.Namespace) -> int:
    create_stage_table(conn)

    read_cur = conn.cursor(name="repair_candidate_cursor", cursor_factory=DictCursor)
    read_cur.itersize = args.batch_size
    read_cur.execute(
        """
        SELECT *
        FROM _repair_source
        ORDER BY id
        """
    )

    staged = 0
    try:
        while True:
            rows = read_cur.fetchmany(args.batch_size)
            if not rows:
                break
            proposals = [build_proposal(row, args) for row in rows]
            insert_proposals(conn, proposals)
            staged += len(proposals)
    finally:
        read_cur.close()

    return staged


def insert_proposals(conn, proposals: Iterable[RepairProposal]) -> None:
    values = [
        (
            p.source_id,
            p.trip_id,
            p.stop_sequence,
            p.old_service_date,
            p.target_service_date,
            p.old_delay_seconds,
            p.target_delay_seconds,
            p.predicted_time,
            p.poll_timestamp,
            p.vehicle_id,
            p.arrival_time,
            p.repairable,
            p.skip_reason,
        )
        for p in proposals
    ]
    if not values:
        return

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO _repair_stage (
                source_id, trip_id, stop_sequence, old_service_date,
                target_service_date, old_delay_seconds, target_delay_seconds,
                predicted_time, poll_timestamp, vehicle_id, arrival_time,
                repairable, skip_reason
            )
            VALUES %s
            """,
            values,
        )


def print_stage_summary(conn, args: argparse.Namespace) -> None:
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS staged_rows,
                COUNT(*) FILTER (WHERE repairable) AS repairable_rows,
                COUNT(*) FILTER (WHERE NOT repairable) AS skipped_rows,
                COUNT(*) FILTER (
                    WHERE old_service_date <> target_service_date
                ) AS service_date_changes,
                COUNT(*) FILTER (
                    WHERE old_delay_seconds <> target_delay_seconds
                ) AS delay_changes
            FROM _repair_stage
            """
        )
        summary = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*) AS conflict_rows
            FROM _repair_stage s
            JOIN real_time_observations r
              ON r.trip_id = s.trip_id
             AND r.stop_sequence = s.stop_sequence
             AND r.service_date = s.target_service_date
             AND r.id <> s.source_id
            WHERE s.repairable
            """
        )
        conflicts = cur.fetchone()

    log("\nRepair proposal summary")
    log(f"  staged_rows:          {summary['staged_rows']}")
    log(f"  repairable_rows:      {summary['repairable_rows']}")
    log(f"  skipped_rows:         {summary['skipped_rows']}")
    log(f"  service_date_changes: {summary['service_date_changes']}")
    log(f"  delay_changes:        {summary['delay_changes']}")
    log(f"  conflict_rows:        {conflicts['conflict_rows']}")

    print_samples(conn, args)


def print_samples(conn, args: argparse.Namespace) -> None:
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            """
            SELECT
                source_id, trip_id, stop_sequence, arrival_time,
                predicted_time, old_service_date, target_service_date,
                old_delay_seconds, target_delay_seconds, repairable,
                skip_reason
            FROM _repair_stage
            ORDER BY ABS(old_delay_seconds) DESC, source_id
            LIMIT %s
            """,
            [args.sample_limit],
        )
        rows = cur.fetchall()

    log("\nSample proposals")
    if not rows:
        log("  none")
        return
    for row in rows:
        log(
            "  "
            f"id={row['source_id']} trip={row['trip_id']} "
            f"seq={row['stop_sequence']} arrival={row['arrival_time']} "
            f"predicted={row['predicted_time']} "
            f"service_date {row['old_service_date']} -> "
            f"{row['target_service_date']} "
            f"delay {row['old_delay_seconds']} -> "
            f"{row['target_delay_seconds']} "
            f"repairable={row['repairable']}"
        )
        if row["skip_reason"]:
            log(f"    skip_reason={row['skip_reason']}")


def apply_repairs(conn, batch_size: int) -> tuple[dict[str, int], set[date]]:
    stats = {
        "updated_in_place": 0,
        "moved_without_conflict": 0,
        "source_won_conflict": 0,
        "source_lost_conflict": 0,
        "skipped": 0,
    }
    affected_dates: set[date] = set()

    read_cur = conn.cursor(name="repair_apply_cursor", cursor_factory=DictCursor)
    read_cur.itersize = batch_size
    read_cur.execute(
        """
        SELECT *
        FROM _repair_stage
        ORDER BY target_service_date, trip_id, stop_sequence,
                 poll_timestamp DESC, source_id DESC
        """
    )

    try:
        while True:
            rows = read_cur.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                if not row["repairable"]:
                    stats["skipped"] += 1
                    continue
                applied = apply_one_repair(conn, row)
                stats[applied] += 1
                affected_dates.add(row["old_service_date"])
                affected_dates.add(row["target_service_date"])
    finally:
        read_cur.close()

    return stats, affected_dates


def list_candidate_service_dates(conn, args: argparse.Namespace) -> list[date]:
    where_sql, params = scoped_where(args)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.service_date
            FROM real_time_observations r
            WHERE {where_sql}
              AND ABS(r.delay_seconds) > %s
            GROUP BY r.service_date
            ORDER BY r.service_date
            """,
            [*params, args.threshold_seconds],
        )
        return [row[0] for row in cur.fetchall()]


def fetch_candidate_batch_for_date(
    conn,
    args: argparse.Namespace,
    current_service_date: date,
) -> list:
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            """
            SELECT
                r.id,
                r.trip_id,
                r.stop_sequence,
                r.service_date AS old_service_date,
                r.delay_seconds AS old_delay_seconds,
                r.predicted_time,
                r.poll_timestamp,
                r.vehicle_id,
                st.arrival_time
            FROM real_time_observations r
            JOIN stop_times st
              ON st.trip_id = r.trip_id
             AND st.stop_sequence = r.stop_sequence
            WHERE r.service_date = %s
              AND ABS(r.delay_seconds) > %s
              AND st.arrival_time IS NOT NULL
            ORDER BY r.id
            LIMIT %s
            """,
            [current_service_date, args.threshold_seconds, args.batch_size],
        )
        return cur.fetchall()


def apply_repairs_in_chunks(conn, args: argparse.Namespace) -> tuple[dict[str, int], set[date]]:
    stats = {
        "updated_in_place": 0,
        "moved_without_conflict": 0,
        "source_won_conflict": 0,
        "source_lost_conflict": 0,
        "skipped": 0,
    }
    affected_dates: set[date] = set()

    candidate_dates = list_candidate_service_dates(conn, args)
    conn.commit()

    if not candidate_dates:
        log("\nNo candidate rows to apply")
        return stats, affected_dates

    log("\nApplying repairs in committed batches")
    for current_service_date in candidate_dates:
        log(f"  service_date {current_service_date}")
        repaired_for_date = 0

        while True:
            rows = fetch_candidate_batch_for_date(conn, args, current_service_date)
            if not rows:
                conn.commit()
                break

            proposals = [build_proposal(row, args) for row in rows]
            unrepairable = [proposal for proposal in proposals if not proposal.repairable]
            if unrepairable:
                conn.rollback()
                log("  encountered unrepairable rows; aborting this apply run")
                for proposal in unrepairable[: args.sample_limit]:
                    log(
                        "    "
                        f"id={proposal.source_id} trip={proposal.trip_id} "
                        f"seq={proposal.stop_sequence} "
                        f"delay {proposal.old_delay_seconds} -> "
                        f"{proposal.target_delay_seconds} "
                        f"reason={proposal.skip_reason}"
                    )
                raise RuntimeError(
                    "unrepairable rows remain; inspect dry run or use --force-unresolved"
                )

            same_key = [
                p for p in proposals if p.old_service_date == p.target_service_date
            ]
            if same_key:
                batch_update_delay(conn, same_key)
                stats["updated_in_place"] += len(same_key)
                for p in same_key:
                    affected_dates.add(p.old_service_date)

            diff_key = [
                p for p in proposals if p.old_service_date != p.target_service_date
            ]
            if diff_key:
                non_conflicting_intra, conflicting_intra = deduplicate_proposals(
                    diff_key
                )

                conflicting_source_ids = find_existing_conflicts(
                    conn, non_conflicting_intra
                )

                non_conflicting = [
                    p
                    for p in non_conflicting_intra
                    if p.source_id not in conflicting_source_ids
                ]
                conflicting_existing = [
                    p
                    for p in non_conflicting_intra
                    if p.source_id in conflicting_source_ids
                ]

                if non_conflicting:
                    batch_move_observations(conn, non_conflicting)
                    stats["moved_without_conflict"] += len(non_conflicting)
                    for p in non_conflicting:
                        affected_dates.add(p.old_service_date)
                        affected_dates.add(p.target_service_date)

                for proposal in conflicting_existing + conflicting_intra:
                    applied = apply_one_repair(conn, proposal_as_row(proposal))
                    stats[applied] += 1
                    affected_dates.add(proposal.old_service_date)
                    affected_dates.add(proposal.target_service_date)

            conn.commit()
            repaired_for_date += len(proposals)
            log(f"    committed {repaired_for_date} rows")

    return stats, affected_dates


def proposal_as_row(proposal: RepairProposal) -> dict:
    return {
        "source_id": proposal.source_id,
        "trip_id": proposal.trip_id,
        "stop_sequence": proposal.stop_sequence,
        "old_service_date": proposal.old_service_date,
        "target_service_date": proposal.target_service_date,
        "old_delay_seconds": proposal.old_delay_seconds,
        "target_delay_seconds": proposal.target_delay_seconds,
        "predicted_time": proposal.predicted_time,
        "poll_timestamp": proposal.poll_timestamp,
        "vehicle_id": proposal.vehicle_id,
        "arrival_time": proposal.arrival_time,
        "repairable": proposal.repairable,
        "skip_reason": proposal.skip_reason,
    }


def apply_one_repair(conn, row) -> str:
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM real_time_observations
            WHERE id = %s
            FOR UPDATE
            """,
            [row["source_id"]],
        )
        source = cur.fetchone()
        if source is None:
            return "source_lost_conflict"

        same_key = source["service_date"] == row["target_service_date"]
        if same_key:
            cur.execute(
                """
                UPDATE real_time_observations
                SET delay_seconds = %s
                WHERE id = %s
                """,
                [row["target_delay_seconds"], row["source_id"]],
            )
            return "updated_in_place"

        cur.execute(
            """
            SELECT *
            FROM real_time_observations
            WHERE trip_id = %s
              AND stop_sequence = %s
              AND service_date = %s
              AND id <> %s
            FOR UPDATE
            """,
            [
                row["trip_id"],
                row["stop_sequence"],
                row["target_service_date"],
                row["source_id"],
            ],
        )
        target = cur.fetchone()

        if target is None:
            cur.execute(
                """
                UPDATE real_time_observations
                SET service_date = %s,
                    delay_seconds = %s
                WHERE id = %s
                """,
                [
                    row["target_service_date"],
                    row["target_delay_seconds"],
                    row["source_id"],
                ],
            )
            return "moved_without_conflict"

        if source_wins(source, target):
            cur.execute(
                """
                UPDATE real_time_observations
                SET predicted_time = %s,
                    delay_seconds = %s,
                    vehicle_id = %s,
                    poll_timestamp = %s
                WHERE id = %s
                """,
                [
                    row["predicted_time"],
                    row["target_delay_seconds"],
                    row["vehicle_id"],
                    row["poll_timestamp"],
                    target["id"],
                ],
            )
            cur.execute(
                "DELETE FROM real_time_observations WHERE id = %s",
                [row["source_id"]],
            )
            return "source_won_conflict"

        cur.execute(
            "DELETE FROM real_time_observations WHERE id = %s",
            [row["source_id"]],
        )
        return "source_lost_conflict"


def source_wins(source, target) -> bool:
    source_key = (source["poll_timestamp"], source["id"])
    target_key = (target["poll_timestamp"], target["id"])
    return source_key > target_key


def batch_update_delay(conn, proposals: list[RepairProposal]) -> None:
    if not proposals:
        return
    values = [(p.source_id, p.target_delay_seconds) for p in proposals]
    with conn.cursor() as cur:
        values_str = ", ".join(cur.mogrify("(%s, %s)", v).decode() for v in values)
        cur.execute(
            f"""
            UPDATE real_time_observations AS t
            SET delay_seconds = v.delay_seconds
            FROM (VALUES {values_str}) AS v(id, delay_seconds)
            WHERE t.id = v.id
            """
        )


def load_proposals_temp(conn, proposals: list[RepairProposal]) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _repair_proposals")
        cur.execute(
            """
            CREATE TEMP TABLE _repair_proposals (
                source_id bigint PRIMARY KEY,
                trip_id text NOT NULL,
                stop_sequence int NOT NULL,
                target_service_date date NOT NULL
            ) ON COMMIT DROP
            """
        )
        if proposals:
            execute_values(
                cur,
                """
                INSERT INTO _repair_proposals
                    (source_id, trip_id, stop_sequence, target_service_date)
                VALUES %s
                """,
                [
                    (p.source_id, p.trip_id, p.stop_sequence, p.target_service_date)
                    for p in proposals
                ],
                page_size=5000,
            )


def find_existing_conflicts(
    conn, proposals: list[RepairProposal]
) -> set[int]:
    if not proposals:
        return set()

    load_proposals_temp(conn, proposals)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.source_id
            FROM _repair_proposals p
            JOIN real_time_observations r
              ON r.trip_id = p.trip_id
             AND r.stop_sequence = p.stop_sequence
             AND r.service_date = p.target_service_date
             AND r.id <> p.source_id
            """
        )
        return {row[0] for row in cur.fetchall()}


def deduplicate_proposals(
    proposals: list[RepairProposal],
) -> tuple[list[RepairProposal], list[RepairProposal]]:
    by_key: dict[tuple, list[RepairProposal]] = defaultdict(list)
    for p in proposals:
        key = (p.trip_id, p.stop_sequence, p.target_service_date)
        by_key[key].append(p)

    non_conflicting: list[RepairProposal] = []
    conflicting: list[RepairProposal] = []
    for group in by_key.values():
        if len(group) == 1:
            non_conflicting.append(group[0])
        else:
            group.sort(key=lambda p: (p.poll_timestamp, p.source_id), reverse=True)
            non_conflicting.append(group[0])
            conflicting.extend(group[1:])

    return non_conflicting, conflicting


def batch_move_observations(conn, proposals: list[RepairProposal]) -> None:
    if not proposals:
        return
    values = [
        (p.target_service_date, p.target_delay_seconds, p.source_id)
        for p in proposals
    ]
    with conn.cursor() as cur:
        values_str = ", ".join(cur.mogrify("(%s, %s, %s)", v).decode() for v in values)
        cur.execute(
            f"""
            UPDATE real_time_observations AS t
            SET service_date = v.target_service_date,
                delay_seconds = v.target_delay_seconds
            FROM (VALUES {values_str}) AS v(
                target_service_date, target_delay_seconds, id
            )
            WHERE t.id = v.id
            """
        )


def rebuild_metrics(conn, affected_dates: set[date]) -> None:
    if not affected_dates:
        log("\nNo affected dates; skipping metrics rebuild")
        return

    with conn.cursor() as cur:
        for affected_date in sorted(affected_dates):
            log(f"  rebuilding metrics for {affected_date}")
            cur.execute("DELETE FROM daily_route_metrics WHERE date = %s", [affected_date])
            cur.execute("DELETE FROM hourly_route_metrics WHERE date = %s", [affected_date])
            cur.execute("SELECT agg_daily(%s)", [affected_date])
            cur.execute("SELECT agg_hourly(%s)", [affected_date])

        today = datetime.now(timezone.utc).date()
        if today in affected_dates:
            cur.execute("SELECT agg_snapshot(%s, %s)", [today, datetime.now(timezone.utc)])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    if args.threshold_seconds <= 0:
        raise SystemExit("--threshold-seconds must be positive")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.sample_limit < 0:
        raise SystemExit("--sample-limit cannot be negative")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise SystemExit("--date-from cannot be after --date-to")

    conn = get_connection()
    try:
        diagnose_delay_buckets(conn, args)
        print_candidate_dates(conn, args)
        candidate_count = create_candidate_table(conn, args)
        staged_count = stage_proposals(conn, args)
        log(f"\nStaged {staged_count} proposals from {candidate_count} candidates")
        print_stage_summary(conn, args)

        if not args.apply:
            log("\nDry run only. Re-run with --apply to repair rows.")
            conn.rollback()
            return 0

        conn.rollback()

        stats, affected_dates = apply_repairs_in_chunks(conn, args)
        for key, value in stats.items():
            log(f"  {key}: {value}")

        if args.skip_metrics_rebuild:
            log("\nSkipping metrics rebuild by request")
        else:
            log("\nRebuilding metrics")
            rebuild_metrics(conn, affected_dates)

        conn.commit()
        log("\nRepair committed")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
