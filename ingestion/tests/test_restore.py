"""restore_state tests — seed a parquet archive (int-shaped rows), run restore
against a temp state dir, and assert the rebuilt store + baseline + current.json
match the live poller's local state.
"""

import json
from datetime import date

import pytest

import scripts.restore_state as restore_state
import poller.archives as archives
import poller.rollup as rollup
import poller.s3 as s3
from poller.state import ObservationsDB

STATIC = {
    "routes": {
        "bus42": {"route_name": "42", "route_type": 3},
    },
    "stops": {
        "S1": {"stop_name": "Front & Chestnut", "stop_lat": 39.952, "stop_lon": -75.165},
    },
}

DATES = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27),
         date(2026, 8, 28), date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31),
         date(2026, 9, 1)]  # 08-24 .. 09-01 (9 dates)
CURRENT_SD = "2026-09-01"
WINDOW_DAYS = DATES[-7:]  # 08-26 .. 09-01
FOLDED = DATES[:-7]  # 08-24, 08-25


def _row(trip_id, delay, category, poll_ts, service_date):
    return (
        trip_id, 1, service_date, "bus42", "S1", delay, category, None, poll_ts, poll_ts,
    )


def _daily_doc(sd, total, category="on_time"):
    """A totals-only daily chronicle payload (the shape add_to_baseline folds)."""
    totals = {
        "total_observations": total,
        "on_time_count": total if category == "on_time" else 0,
        "early_count": total if category == "early" else 0,
        "late_count": total if category == "late" else 0,
        "delay_sum": 0,
    }
    return {
        "service_date": sd,
        "updated_at": "2026-09-01T00:00:00-04:00",
        "as_of_poll": 1,
        "routes": {"bus42": dict(totals)},
        "stops": {"S1": dict(totals)},
    }


def _seed_daily(fake, tmp_path, sd, total):
    """Publish a daily chronicle for `sd` on the fake S3 (as a local file)."""
    p = tmp_path / f"daily-{sd}.json"
    p.write_text(json.dumps(_daily_doc(sd, total)))
    fake.objects[f"state/daily/{sd}.json"] = str(p)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Seed 9 archive parquet dates + monkeypatch restore deps.

    Restore streams via `stream_observation`, which restore_state imports at
    module scope; we redirect that to the local seed files (real pyarrow
    streaming, no S3). `s3.filesystem()` itself is still exercised — it is
    constructed (with fake creds) under the covers of every apply run.
    """
    archive_dir = tmp_path / "seed_archives"
    rows_by_date = {}
    for d in DATES:
        base = d.toordinal()
        rows = [
            _row(f"t{d.isoformat()}a", 60, "on_time", base, d.isoformat()),
            _row(f"t{d.isoformat()}b", 400, "late", base, d.isoformat()),
            _row(f"t{d.isoformat()}c", -120, "early", base, d.isoformat()),
        ]
        archives.write_observations(rows, archive_dir)
        rows_by_date[d] = rows

    def stream_from_seed(key, filesystem=None):
        path = archive_dir / key.rsplit("/", 1)[-1]
        yield from archives.stream_observation(path)

    def meta_from_seed(key, filesystem=None):
        path = archive_dir / key.rsplit("/", 1)[-1]
        return archives.read_archive_meta(path)

    fake = FakeS3Client()
    for d in DATES:
        p = archive_dir / f"{d.isoformat()}.parquet"
        fake.objects[f"archive/observations/{d.isoformat()}.parquet"] = str(p)

    monkeypatch.setattr(s3, "_make_client", lambda: fake)
    monkeypatch.setattr(restore_state, "stream_observation", stream_from_seed)
    monkeypatch.setattr(restore_state, "read_archive_meta", meta_from_seed)
    monkeypatch.setenv("S3_BUCKET", "deviated-septa-dev")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "SK")

    monkeypatch.setattr(restore_state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(restore_state, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(restore_state, "load_local_metadata", lambda _dir: STATIC)
    return fake


class _NoSuchKey(Exception):
    pass


class _Exceptions:
    NoSuchKey = _NoSuchKey


class _Body:
    """Minimal stand-in for botocore's StreamingBody over a local file."""

    def __init__(self, path):
        self._fh = open(path, "rb")

    def read(self):
        return self._fh.read()

    def close(self):
        self._fh.close()


class FakeS3Client:
    exceptions = _Exceptions()

    def __init__(self):
        self.objects = {}
        self.uploads = []

    def list_objects_v2(self, Bucket=None, Prefix=None, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise _NoSuchKey(f"no such key: {Key}")
        return {"Body": _Body(self.objects[Key])}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((key, str(path)))


def test_restore_builds_7_window_store_and_folds_old(fake, tmp_path):
    restore_state.restore(argparse_namespace())

    db = ObservationsDB(tmp_path / "state" / "observations.db")
    stats = db.service_date_stats()
    store_dates = [sd for sd, _ in stats]
    assert store_dates == [d.isoformat() for d in WINDOW_DAYS]

    baseline = restore_state.load_baseline(tmp_path / "state")
    assert baseline["routes"]["bus42"]["total_observations"] == 2 * 3
    assert baseline["min_service_date"] == FOLDED[0].isoformat()
    assert baseline["max_service_date"] == FOLDED[-1].isoformat()
    db.close()


def test_restore_current_json_periods(fake, tmp_path):
    restore_state.restore(argparse_namespace())

    current = json.loads((tmp_path / "state" / "current.json").read_text())
    assert current["current_service_date"] == CURRENT_SD

    day = current["periods"]["day"]["routes"]["bus42"]["total_observations"]
    assert day == 3

    assert current["periods"]["week"]["routes"]["bus42"]["total_observations"] == 7 * 3

    all_routes = current["periods"]["all"]["routes"]["bus42"]
    assert all_routes["total_observations"] == 9 * 3


def test_restore_dry_run_writes_nothing(tmp_path, fake):
    restore_state.restore(argparse_namespace(dry_run=True))

    assert not (tmp_path / "state" / "observations.db").exists()
    assert not (tmp_path / "state" / "current.json").exists()


def test_restore_uploads_artifacts(fake, tmp_path):
    restore_state.restore(argparse_namespace())

    keys = {k for k, _ in fake.uploads}
    assert "public/current.json" in keys
    assert "state/all-baseline.json" in keys
    for d in WINDOW_DAYS[:-1]:
        assert f"state/daily/{d.isoformat()}.json" in keys


def test_restore_single_date(fake, tmp_path):
    restore_state.restore(argparse_namespace(date=FOLDED[0]))

    db = ObservationsDB(tmp_path / "state" / "observations.db")
    stats = db.service_date_stats()
    assert [sd for sd, _ in stats] == [FOLDED[0].isoformat()]
    db.close()


# --- daily-chronicle preference (2026-09-08 fidelity) ---

class TestDailyChroniclePreference:
    def test_folds_from_daily_when_present(self, fake, tmp_path, capsys):
        # 08-25's daily says 5 observations, its parquet only holds 3 — the
        # daily is authoritative for totals, so the baseline must show 5.
        _seed_daily(fake, tmp_path, FOLDED[1].isoformat(), 5)

        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        # 08-24 (no daily -> parquet, 3) + 08-25 (daily, 5)
        assert baseline["routes"]["bus42"]["total_observations"] == 8
        assert "folded totals from daily chronicle" in capsys.readouterr().out

    def test_falls_back_to_parquet_without_daily(self, fake, tmp_path, capsys):
        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        assert baseline["routes"]["bus42"]["total_observations"] == 2 * 3
        assert "no daily chronicle" in capsys.readouterr().out

    def test_warns_when_raw_archive_is_short(self, fake, tmp_path, capsys):
        # the 09-08 shape: daily totals exceed the surviving raw rows
        _seed_daily(fake, tmp_path, FOLDED[1].isoformat(), 5)

        restore_state.restore(argparse_namespace())

        out = capsys.readouterr().out
        assert "WARNING daily totals (5) exceed raw archive rows (3)" in out

    def test_surviving_archive_gets_no_warning(self, fake, tmp_path, capsys):
        # daily total == parquet rows -> nothing to flag
        _seed_daily(fake, tmp_path, FOLDED[1].isoformat(), 3)

        restore_state.restore(argparse_namespace())

        assert "WARNING" not in capsys.readouterr().out


# --- full restore must never fold on top of an existing baseline ---

class TestNoDoubleFold:
    def _preload_stale_state(self, tmp_path, sd="2026-08-23", total=100):
        """A pre-existing baseline that already folded `sd`, plus its local daily."""
        state = tmp_path / "state"
        (state / "daily").mkdir(parents=True, exist_ok=True)
        (state / "all-baseline.json").write_text(json.dumps({
            "min_service_date": sd,
            "max_service_date": sd,
            "updated_at": f"{sd}T00:00:00-04:00",
            "routes": {"bus42": {
                "total_observations": total, "on_time_count": total,
                "early_count": 0, "late_count": 0, "delay_sum": 0,
            }},
            "stops": {},
        }))
        (state / "daily" / f"{sd}.json").write_text(json.dumps(_daily_doc(sd, total)))

    def test_date_with_a_local_daily_is_counted_exactly_once(self, fake, tmp_path, capsys):
        """08-23 has no S3 archive, so its local daily is the only surviving copy.

        The old baseline already held 100 for it. A correct restore wipes the
        baseline and folds that one copy once -> 100 + 3 + 3 = 106. Not wiping
        would give 206; trusting the baseline *and* re-folding would give 206 too.
        """
        self._preload_stale_state(tmp_path)

        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        assert baseline["routes"]["bus42"]["total_observations"] == 100 + 2 * 3
        assert baseline["min_service_date"] == "2026-08-23"
        assert "wiped stale all-baseline.json" in capsys.readouterr().out

    def test_archive_backed_date_replaces_rather_than_adds(self, fake, tmp_path):
        """A date with a real S3 archive is rebuilt from the archive, not added to
        whatever the stale baseline claimed for it."""
        self._preload_stale_state(tmp_path, sd=FOLDED[0].isoformat(), total=100)

        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        # 08-24 contributes its 3 archive rows, not the stale 100
        assert baseline["routes"]["bus42"]["total_observations"] == 2 * 3

    def test_stale_dailies_do_not_survive_a_full_restore(self, fake, tmp_path):
        self._preload_stale_state(tmp_path)
        stale = tmp_path / "state" / "daily" / "2026-08-23.json"

        restore_state.restore(argparse_namespace())

        assert not stale.exists()  # rebuilt from the store, not left to pollute the week rollup
        assert not list((tmp_path / "state").glob("daily.stage-*"))

    def test_single_date_restore_leaves_baseline_alone(self, fake, tmp_path):
        self._preload_stale_state(tmp_path)

        restore_state.restore(argparse_namespace(date=FOLDED[0]))

        baseline = restore_state.load_baseline(tmp_path / "state")
        assert baseline["routes"]["bus42"]["total_observations"] == 100
        assert (tmp_path / "state" / "daily" / "2026-08-23.json").exists()


# --- a missing archive must not make a date disappear ---
# (the failure mode: state/daily/ wiped a date whose S3 parquet was gone, and
#  `_archive_dates` never listed it, so its totals silently vanished)

class TestMissingArchiveRecovery:
    LOCAL_ONLY = "2026-08-20"  # folded range, before every seeded archive

    def _seed_local_daily(self, tmp_path, sd, total):
        state = tmp_path / "state"
        (state / "daily").mkdir(parents=True, exist_ok=True)
        (state / "daily" / f"{sd}.json").write_text(json.dumps(_daily_doc(sd, total)))

    def _restorable(self, fake, only=None):
        return restore_state._restorable_dates(only)

    def test_date_with_only_an_s3_daily_is_listed(self, fake, tmp_path):
        _seed_daily(fake, tmp_path, self.LOCAL_ONLY, 7)

        dates, archives = self._restorable(fake)

        assert date.fromisoformat(self.LOCAL_ONLY) in dates
        assert date.fromisoformat(self.LOCAL_ONLY) not in archives

    def test_date_with_only_a_local_daily_is_listed(self, fake, tmp_path):
        self._seed_local_daily(tmp_path, self.LOCAL_ONLY, 7)

        dates, archives = self._restorable(fake)

        assert date.fromisoformat(self.LOCAL_ONLY) in dates
        assert date.fromisoformat(self.LOCAL_ONLY) not in archives

    def test_s3_daily_saves_a_date_whose_archive_vanished(self, fake, tmp_path, capsys):
        _seed_daily(fake, tmp_path, FOLDED[0].isoformat(), 7)
        del fake.objects[f"archive/observations/{FOLDED[0].isoformat()}.parquet"]

        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        # 08-24 folded from its S3 daily (7) instead of being dropped; 08-25 from parquet (3)
        assert baseline["routes"]["bus42"]["total_observations"] == 10
        assert "folded totals from daily chronicle" in capsys.readouterr().out

    def test_local_daily_is_the_last_copy_and_gets_reuploaded(self, fake, tmp_path, capsys):
        # no S3 archive and no S3 daily: the staged local file is all that's left
        self._seed_local_daily(tmp_path, self.LOCAL_ONLY, 7)

        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        assert baseline["routes"]["bus42"]["total_observations"] == 7 + 2 * 3
        out = capsys.readouterr().out
        assert "the last copy" in out
        # the eternal ledger regains its copy
        assert f"state/daily/{self.LOCAL_ONLY}.json" in {k for k, _ in fake.uploads}

    def test_window_date_without_archive_is_flagged_but_not_folded(self, fake, tmp_path, capsys):
        """A recent date's archive is deleted, so the store can't hold it.

        Its totals are deliberately NOT folded into the baseline: prune only folds
        dates newer than baseline.max_service_date, so advancing that marker past
        the older store dates would strand them — they'd drain without ever being
        folded and lose their totals. So the day is flagged and the baseline stays
        strictly behind the store window.
        """
        gone = WINDOW_DAYS[2].isoformat()
        _seed_daily(fake, tmp_path, gone, 5)
        del fake.objects[f"archive/observations/{gone}.parquet"]

        restore_state.restore(argparse_namespace())

        baseline = restore_state.load_baseline(tmp_path / "state")
        assert baseline["routes"]["bus42"]["total_observations"] == 2 * 3
        # baseline stays strictly behind the oldest store date, so prune can still
        # fold 08-26..08-29 when they age out
        assert baseline["max_service_date"] == FOLDED[-1].isoformat()
        assert date.fromisoformat(gone) > date.fromisoformat(baseline["max_service_date"])
        assert f"{gone}: WARNING no raw archive on S3" in capsys.readouterr().out

        db = ObservationsDB(tmp_path / "state" / "observations.db")
        assert gone not in [sd for sd, _ in db.service_date_stats()]
        db.close()

    def test_prune_can_still_fold_the_store_after_a_restore(self, fake, tmp_path):
        """The invariant the previous test protects: after a restore with a hole in
        the store window, prune still folds the remaining window dates."""
        gone = WINDOW_DAYS[2].isoformat()
        _seed_daily(fake, tmp_path, gone, 5)
        del fake.objects[f"archive/observations/{gone}.parquet"]
        restore_state.restore(argparse_namespace())

        # roll the service date forward so every window date ages out at once
        state = tmp_path / "state"
        db = ObservationsDB(state / "observations.db")
        baseline, changed = rollup.prune_window(db, state, "2026-09-08")
        db.close()

        assert changed
        # 08-26..08-29 + 08-31..09-01 survived in the store, so all get folded
        assert baseline["routes"]["bus42"]["total_observations"] == 2 * 3 + 6 * 3
        assert baseline["max_service_date"] == "2026-09-01"

    def test_single_date_restore_recovers_a_local_daily_to_s3(self, fake, tmp_path, capsys):
        """--date for a date with no S3 archive and no S3 daily at all.

        The totals are pushed back to the ledger (the real recovery), but the date
        lands in the store window so it is not folded — see _store_window_date.
        """
        self._seed_local_daily(tmp_path, self.LOCAL_ONLY, 7)

        restore_state.restore(argparse_namespace(date=date.fromisoformat(self.LOCAL_ONLY)))

        # the eternal ledger regains the copy it lost
        assert f"state/daily/{self.LOCAL_ONLY}.json" in {k for k, _ in fake.uploads}
        assert "re-uploaded local daily chronicle to S3" in capsys.readouterr().out

        baseline = restore_state.load_baseline(tmp_path / "state")
        assert baseline["routes"] == {}  # window dates are never folded
        # and the local copy is left in place for a --date restore
        assert (tmp_path / "state" / "daily" / f"{self.LOCAL_ONLY}.json").exists()

    def test_staged_dailies_survive_a_failed_restore(self, fake, tmp_path, monkeypatch):
        self._seed_local_daily(tmp_path, self.LOCAL_ONLY, 7)

        def boom(*a, **kw):
            raise RuntimeError("s3 exploded")

        monkeypatch.setattr(restore_state, "stream_observation", boom)

        with pytest.raises(RuntimeError):
            restore_state.restore(argparse_namespace())

        staged = list((tmp_path / "state").glob("daily.stage-*"))
        assert len(staged) == 1
        assert (staged[0] / f"{self.LOCAL_ONLY}.json").exists()


class _NS:
    pass


def argparse_namespace(**kw):
    ns = _NS()
    ns.dry_run = kw.get("dry_run", False)
    ns.date = kw.get("date", None)
    return ns
