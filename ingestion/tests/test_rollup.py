import json
from datetime import date, datetime, timezone

from poller.rollup import (
    add_to_baseline,
    build_current,
    finalize_day,
    load_baseline,
    load_daily,
    merge_entity_map,
    merge_totals,
    prune_window,
    rebuild_baseline_from_dailies,
    refresh_daily_chronicle,
    save_baseline,
    write_json,
)
from poller.state import ObservationsDB

STATIC = {
    "routes": {
        "bus42": {"route_name": "42", "route_type": 3},
        "trolley10": {"route_name": "10", "route_type": 0},
    },
    "stops": {
        "S1": {"stop_name": "Front & Chestnut", "stop_lat": 39.952, "stop_lon": -75.165},
        "S2": {"stop_name": "10th & Main", "stop_lat": 39.960, "stop_lon": -75.170},
    },
}

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _row(
    trip_id,
    route_id,
    stop_id,
    delay,
    category,
    service_date,
    poll_ts,
    stop_seq=1,
):
    return {
        "trip_id": trip_id,
        "stop_sequence": stop_seq,
        "service_date": service_date,
        "route_id": route_id,
        "stop_id": stop_id,
        "delay_seconds": delay,
        "category": category,
        "vehicle_id": None,
        "predicted_time": poll_ts,
        "poll_timestamp": poll_ts,
    }


def _today_db(tmp_path):
    db = ObservationsDB(tmp_path / "obs.db")
    db.upsert(
        [
            _row("t1", "bus42", "S1", 60, "on_time", date(2026, 8, 20),
                 datetime(2026, 8, 20, 11, 30, tzinfo=timezone.utc)),
            _row("t2", "bus42", "S2", 400, "late", date(2026, 8, 20),
                 datetime(2026, 8, 20, 11, 31, tzinfo=timezone.utc)),
            _row("t3", "trolley10", "S1", -120, "early", date(2026, 8, 20),
                 datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc), stop_seq=1),
            _row("t9", "bus42", "S1", 10, "on_time", date(2026, 8, 19),
                 datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)),
            _row("t8", "bus42", "S1", 10, "on_time", date(2026, 8, 19),
                 datetime(2026, 8, 19, 12, 5, tzinfo=timezone.utc)),
            _row("t7", "bus42", "S2", 400, "late", date(2026, 8, 14),
                 datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)),
            _row("t6", "bus42", "S2", 400, "late", date(2026, 8, 14),
                 datetime(2026, 8, 14, 12, 5, tzinfo=timezone.utc)),
            _row("t5", "trolley10", "S1", -120, "early", date(2026, 8, 10),
                 datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)),
        ]
    )
    return db


class TestMerge:
    def test_merge_totals_sums(self):
        base = {"total_observations": 1, "on_time_count": 1, "early_count": 0,
                "late_count": 0, "delay_sum": 60}
        add = {"total_observations": 2, "on_time_count": 0, "early_count": 1,
               "late_count": 1, "delay_sum": -120}
        out = merge_totals(base, add)
        assert out["total_observations"] == 3
        assert out["on_time_count"] == 1
        assert out["early_count"] == 1
        assert out["late_count"] == 1
        assert out["delay_sum"] == -60

    def test_merge_entity_map(self):
        a = {"42": {"total_observations": 1, "on_time_count": 1, "early_count": 0,
                    "late_count": 0, "delay_sum": 60}}
        b = {"42": {"total_observations": 1, "on_time_count": 0, "early_count": 0,
                    "late_count": 1, "delay_sum": 400},
             "55": {"total_observations": 1, "on_time_count": 0, "early_count": 1,
                    "late_count": 0, "delay_sum": -120}}
        out = merge_entity_map(a, b)
        assert out["42"]["total_observations"] == 2
        assert out["42"]["delay_sum"] == 460
        assert out["55"]["early_count"] == 1


class TestBaseline:
    def test_defaults_when_missing(self, tmp_path):
        assert load_baseline(str(tmp_path))["routes"] == {}

    def test_add_sets_date_range(self, tmp_path):
        b = load_baseline(str(tmp_path))
        add_to_baseline(b, {"service_date": "2026-08-19", "routes": {}, "stops": {}})
        add_to_baseline(b, {"service_date": "2026-08-10", "routes": {}, "stops": {}})
        assert b["min_service_date"] == "2026-08-10"
        assert b["max_service_date"] == "2026-08-19"

    def test_accumulates_and_roundtrips(self, tmp_path):
        b = load_baseline(str(tmp_path))
        add_to_baseline(b, {
            "service_date": "2026-08-19",
            "routes": {"42": {"total_observations": 2, "on_time_count": 2,
                              "early_count": 0, "late_count": 0, "delay_sum": 0}},
            "stops": {},
        })
        add_to_baseline(b, {
            "service_date": "2026-08-20",
            "routes": {"42": {"total_observations": 3, "on_time_count": 0,
                              "early_count": 0, "late_count": 3, "delay_sum": 1200}},
            "stops": {},
        })
        save_baseline(str(tmp_path), b)
        loaded = load_baseline(str(tmp_path))
        assert loaded["routes"]["42"]["total_observations"] == 5
        assert loaded["routes"]["42"]["late_count"] == 3
        assert loaded["min_service_date"] == "2026-08-19"

    def test_rebuild_from_dailies(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            baseline = load_baseline(str(tmp_path))
            for sd in (date(2026, 8, 19), date(2026, 8, 14), date(2026, 8, 10)):
                daily = finalize_day(db, sd, str(tmp_path))
                add_to_baseline(baseline, daily)
            rebuilt = rebuild_baseline_from_dailies(str(tmp_path))
            assert rebuilt["routes"] == baseline["routes"]
            assert rebuilt["min_service_date"] == "2026-08-10"
            assert rebuilt["max_service_date"] == "2026-08-19"
        finally:
            db.close()


class TestFinalizeDay:
    def test_writes_totals_only_with_as_of(self, tmp_path):
        db = ObservationsDB(tmp_path / "obs.db")
        try:
            poll_ts = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
            db.upsert([_row("t1", "bus42", "S1", 60, "on_time", date(2026, 8, 19), poll_ts)])
            daily = finalize_day(db, "2026-08-19", str(tmp_path))
            assert set(daily.keys()) == {"service_date", "updated_at", "as_of_poll", "routes", "stops"}
            assert daily["as_of_poll"] == int(poll_ts.timestamp())
            assert daily["routes"]["bus42"]["total_observations"] == 1
            loaded = load_daily(str(tmp_path), "2026-08-19")
            assert loaded["routes"]["bus42"]["on_time_count"] == 1
            assert loaded["as_of_poll"] == int(poll_ts.timestamp())
        finally:
            db.close()

    def test_load_missing_returns_none(self, tmp_path):
        assert load_daily(str(tmp_path), "2026-01-01") is None


class TestPruneWindow:
    def test_folds_and_deletes_out_of_window(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            baseline, changed = prune_window(db, str(tmp_path), "2026-08-20")
            assert changed
            assert baseline["min_service_date"] == "2026-08-10"
            assert "bus42" not in baseline["routes"]
            assert baseline["routes"]["trolley10"]["total_observations"] == 1
            assert baseline["stops"]["S1"]["total_observations"] == 1
            assert [sd for sd, _ in db.service_date_stats()] == [
                "2026-08-14", "2026-08-19", "2026-08-20",
            ]
        finally:
            db.close()

    def test_drops_local_daily_for_pruned_date(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            finalize_day(db, "2026-08-10", str(tmp_path))
            prune_window(db, str(tmp_path), "2026-08-20")
            assert not (tmp_path / "daily" / "2026-08-10.json").exists()
        finally:
            db.close()

    def test_idempotent(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            baseline, _ = prune_window(db, str(tmp_path), "2026-08-20")
            save_baseline(str(tmp_path), baseline)
            baseline, changed = prune_window(db, str(tmp_path), "2026-08-20")
            assert not changed
            assert baseline["routes"]["trolley10"]["total_observations"] == 1
        finally:
            db.close()


class TestRefreshDailyChronicle:
    def test_rewrites_only_when_store_advances(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            first = refresh_daily_chronicle(db, str(tmp_path), "2026-08-20")
            assert "2026-08-19" in first
            expected = dict(db.service_date_stats())["2026-08-19"]
            assert load_daily(str(tmp_path), "2026-08-19")["as_of_poll"] == expected

            assert refresh_daily_chronicle(db, str(tmp_path), "2026-08-20") == []

            later = datetime(2026, 8, 19, 12, 10, tzinfo=timezone.utc)
            db.upsert([_row("t88", "bus42", "S1", 5, "on_time", date(2026, 8, 19), later)])
            assert "2026-08-19" in refresh_daily_chronicle(db, str(tmp_path), "2026-08-20")
        finally:
            db.close()


class TestBuildCurrent:
    def _setup_baseline(self, tmp_path):
        db = _today_db(tmp_path)
        baseline, _ = prune_window(db, str(tmp_path), "2026-08-20")
        save_baseline(str(tmp_path), baseline)
        return db

    def test_full_shape(self, tmp_path):
        db = self._setup_baseline(tmp_path)
        try:
            current = build_current(db, STATIC, str(tmp_path), now=NOW)

            assert set(current.keys()) == {
                "updated_at", "current_service_date", "data_range",
                "metadata", "periods",
            }
            assert current["current_service_date"] == "2026-08-20"
            assert current["data_range"] == {"min": "2026-08-10", "max": "2026-08-20"}
            assert current["metadata"]["routes"]["bus42"] == {"route_name": "42", "route_type": 3}
            assert current["metadata"]["stops"]["S1"]["stop_name"] == "Front & Chestnut"
            assert set(current["periods"].keys()) == {"hour", "day", "week", "all"}
        finally:
            db.close()

    def test_hour_filters_poll_window(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            current = build_current(db, STATIC, str(tmp_path), now=NOW)
            hour = current["periods"]["hour"]
            assert hour["routes"]["bus42"]["total_observations"] == 2
            assert "trolley10" not in hour["routes"]
            assert hour["stops"]["S1"]["total_observations"] == 1
        finally:
            db.close()

    def test_day_full_day(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            current = build_current(db, STATIC, str(tmp_path), now=NOW)
            day = current["periods"]["day"]
            assert day["routes"]["bus42"]["total_observations"] == 2
            assert day["routes"]["trolley10"]["total_observations"] == 1
        finally:
            db.close()

    def test_week_uses_store_window(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            prune_window(db, str(tmp_path), "2026-08-20")
            current = build_current(db, STATIC, str(tmp_path), now=NOW)
            week = current["periods"]["week"]
            assert week["routes"]["bus42"]["total_observations"] == 6
            assert week["routes"]["trolley10"]["total_observations"] == 1
            assert week["stops"]["S2"]["total_observations"] == 3
        finally:
            db.close()

    def test_all_baseline_plus_store(self, tmp_path):
        db = self._setup_baseline(tmp_path)
        try:
            current = build_current(db, STATIC, str(tmp_path), now=NOW)
            all_period = current["periods"]["all"]
            assert all_period["routes"]["bus42"]["total_observations"] == 6
            assert all_period["routes"]["trolley10"]["total_observations"] == 2
            assert all_period["stops"]["S1"]["total_observations"] == 5
        finally:
            db.close()

    def test_all_without_baseline_covers_store(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            current = build_current(db, STATIC, str(tmp_path), now=NOW)
            all_period = current["periods"]["all"]
            assert all_period["routes"]["bus42"]["total_observations"] == 6
            assert all_period["routes"]["trolley10"]["total_observations"] == 2
            assert all_period["stops"]["S1"]["total_observations"] == 5
            assert current["data_range"] == {"min": "2026-08-10", "max": "2026-08-20"}
        finally:
            db.close()

    def test_current_sd_override(self, tmp_path):
        db = _today_db(tmp_path)
        try:
            current = build_current(db, STATIC, str(tmp_path), now=NOW, current_sd="2026-08-19")
            assert current["current_service_date"] == "2026-08-19"
            assert current["data_range"]["max"] == "2026-08-19"
            day = current["periods"]["day"]
            assert day["routes"]["bus42"]["total_observations"] == 2
            assert "trolley10" not in day["routes"]
        finally:
            db.close()


class TestWriteJson:
    def test_writes_atomic_json(self, tmp_path):
        out = tmp_path / "nested" / "current.json"
        write_json({"a": 1}, str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == {"a": 1}
        assert not out.with_suffix(".json.tmp").exists()