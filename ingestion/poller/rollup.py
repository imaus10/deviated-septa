"""Rollup JSON builders for the frontend.

Periods come from GROUP BY over the observations store — no running-totals
cache. Shared route/stop display metadata is emitted once under `metadata`;
each period carries only totals.

current.json shape:
    {
      "updated_at":            <ISO-8601 Eastern>,
      "current_service_date":  "YYYY-MM-DD" (newest service date seen in the feed),
      "data_range":            {"min": "...", "max": "..."},
      "metadata":              {"routes": {id: name/type}, "stops": {id: name/coords}},
      "periods": {
        "hour": {"routes": {id: totals}, "stops": {id: totals}},   # last-60min polls
        "day":  {...},                                             # current_service_date
        "week": {...},                                             # store, last 7 service dates
        "all":  {...}                                              # baseline + store
      }
    }

The SQLite store keeps only the 7-date week window. `week` reads the store
directly, so it always reflects in-progress and not-yet-pruned days. The
all-time baseline is a fixed-size accumulator: every service date that ages
out of the window is folded into it and deleted (prune_window), so `all` =
baseline + whatever the store still holds.

Archives (private):
    state/daily/YYYY-MM-DD.json      — totals-only chronicle, rewritten whenever the
                                       store advances for a day (as_of_poll tracks it)
"""

import json
import pathlib
from datetime import date, datetime, timedelta

from poller.constants import CATEGORY_COUNT_KEYS, EASTERN
from poller.state import to_iso_date

TOTAL_KEYS = ("total_observations", *CATEGORY_COUNT_KEYS, "delay_sum")


def merge_totals(base: dict, add: dict) -> dict:
    """Sum two totals dicts (counts + delay_sum, stable key order)."""
    return {
        k: base.get(k, 0) + add.get(k, 0)
        for k in TOTAL_KEYS
    }


def merge_entity_map(*maps: dict[str, dict]) -> dict[str, dict]:
    """Merge per-id totals maps, summing totals for ids present in several."""
    out: dict[str, dict] = {}
    for m in maps:
        for key, totals in (m or {}).items():
            out[key] = merge_totals(out[key], totals) if key in out else dict(totals)
    return out


def build_totals(obs_db, service_date) -> dict[str, dict]:
    """{routes, stops} totals for a service date."""
    return {
        "routes": obs_db.rollup_routes(service_date),
        "stops": obs_db.rollup_stops(service_date),
    }


def build_totals_since(obs_db, unix_ts) -> dict[str, dict]:
    """{routes, stops} totals for observations polled at/after unix_ts."""
    return {
        "routes": obs_db.rollup_routes_since(unix_ts),
        "stops": obs_db.rollup_stops_since(unix_ts),
    }


def write_json(obj: dict, path) -> None:
    """Atomically write a JSON dict to path (tmp file + replace)."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Daily archives + baseline accumulator
# ---------------------------------------------------------------------------

def load_daily(state_dir, service_date) -> dict | None:
    """Load a finalized daily archive, or None if the day was never written."""
    path = pathlib.Path(state_dir) / "daily" / f"{to_iso_date(service_date)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def finalize_day(obs_db, service_date, state_dir, now=None, as_of_poll=None) -> dict:
    """Write the daily archive snapshot for a service date; return it.

    Archives are a totals-only chronicle, idempotently rewritten whenever the
    store advances for that date. `as_of_poll` records the newest observation
    poll included so callers can detect later stragglers.
    """
    now = now or datetime.now(EASTERN)
    if as_of_poll is None:
        stats = dict(obs_db.service_date_stats())
        as_of_poll = stats.get(to_iso_date(service_date))
    daily = {
        "service_date": to_iso_date(service_date),
        "updated_at": now.isoformat(timespec="seconds"),
        "as_of_poll": as_of_poll,
        **build_totals(obs_db, service_date),
    }
    write_json(daily, pathlib.Path(state_dir) / "daily" / f"{daily['service_date']}.json")
    return daily


def prune_window(obs_db, state_dir, current_sd, baseline=None, now=None) -> tuple[dict, bool]:
    """Fold out-of-window service dates into the baseline, then drop them.

    The store keeps only the 7-date week window (current_sd through -6).
    Every older service date is folded into the all-time baseline and its
    rows + local daily archive are deleted. Fold-then-delete makes this
    idempotent: a date exists in exactly one of (store, baseline).

    Returns (baseline, changed).
    """
    baseline = baseline if baseline is not None else load_baseline(state_dir)
    window_low = date.fromisoformat(current_sd) - timedelta(days=6)
    changed = False
    for sd, _ in obs_db.service_date_stats():
        if date.fromisoformat(sd) < window_low:
            totals = build_totals(obs_db, sd)
            if totals["routes"] or totals["stops"]:
                baseline = add_to_baseline(baseline, {"service_date": sd, **totals}, now=now)
            obs_db.delete_service_date(sd)
            daily = pathlib.Path(state_dir) / "daily" / f"{sd}.json"
            if daily.exists():
                daily.unlink()
            changed = True
    return baseline, changed


def refresh_daily_chronicle(obs_db, state_dir, current_sd, now=None) -> list[str]:
    """Rewrite daily/<sd>.json for non-current dates whose store advanced.

    Returns the list of service dates whose archive was rewritten, so the
    caller can upload them.
    """
    rewritten = []
    for sd, mx in obs_db.service_date_stats():
        if sd >= current_sd:
            continue
        archive = load_daily(state_dir, sd)
        if archive is None or (mx or 0) > (archive.get("as_of_poll") or 0):
            finalize_day(obs_db, sd, state_dir, now=now, as_of_poll=mx)
            rewritten.append(sd)
    return rewritten


def _baseline_defaults() -> dict:
    return {
        "min_service_date": None,
        "max_service_date": None,
        "updated_at": None,
        "routes": {},
        "stops": {},
    }


def load_baseline(state_dir) -> dict:
    defaults = _baseline_defaults()
    path = pathlib.Path(state_dir) / "all-baseline.json"
    if not path.exists():
        return defaults
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults
    return {**defaults, **baseline}


def save_baseline(state_dir, baseline) -> None:
    write_json(baseline, pathlib.Path(state_dir) / "all-baseline.json")


def add_to_baseline(baseline: dict, daily: dict, now=None) -> dict:
    """Fold a finalized daily archive into the all-time baseline."""
    sd = daily["service_date"]
    baseline["routes"] = merge_entity_map(baseline["routes"], daily["routes"])
    baseline["stops"] = merge_entity_map(baseline["stops"], daily["stops"])
    existing = [d for d in (baseline["min_service_date"], baseline["max_service_date"]) if d]
    baseline["min_service_date"] = min([sd, *existing]) if existing else sd
    baseline["max_service_date"] = max([sd, *existing]) if existing else sd
    baseline["updated_at"] = (now or datetime.now(EASTERN)).isoformat(timespec="seconds")
    return baseline


def rebuild_baseline_from_dailies(state_dir) -> dict:
    """Rebuild the baseline by summing every daily archive on disk."""
    baseline = _baseline_defaults()
    daily_dir = pathlib.Path(state_dir) / "daily"
    if not daily_dir.is_dir():
        return baseline
    dates = sorted(p.stem for p in daily_dir.glob("*.json"))
    for sd in dates:
        daily = load_daily(state_dir, sd)
        if daily:
            add_to_baseline(baseline, daily)
    return baseline


# ---------------------------------------------------------------------------
# Current rollup (4 periods)
# ---------------------------------------------------------------------------

def build_current(obs_db, static: dict, state_dir, now=None, since_seconds: int = 3600, current_sd=None) -> dict:
    """Build the 4-period current.json dict.

    Args:
        obs_db: ObservationsDB instance
        static: metadata dict from gtfs_static.load_local_metadata()
            (routes/stops used as shared metadata)
        state_dir: local state dir (daily archives + baseline live here)
        now: tz-aware datetime (Eastern); default now
        since_seconds: how far back 'hour' reaches (default 3600)
        current_sd: service date to treat as "today" (defaults to the newest
            service date in the store)
    """
    now = now or datetime.now(EASTERN)
    hour_since = int(now.timestamp()) - since_seconds

    store_dates = obs_db.store_dates()
    if current_sd is None:
        current_sd = store_dates[-1] if store_dates else now.date().isoformat()

    hour = build_totals_since(obs_db, hour_since)
    day = build_totals(obs_db, current_sd)

    # week = today's store rollup (already `day`) + the six prior dates'
    # finalized daily archives. refresh_daily_chronicle runs just before this
    # and rewrites any prior date whose store advanced, so the archives are
    # current; a date without one (rare) falls back to a store scan.
    week_dates = _week_dates(current_sd)
    week_routes = dict(day["routes"])
    week_stops = dict(day["stops"])
    for sd in week_dates[1:]:
        daily = load_daily(state_dir, sd)
        if daily:
            week_routes = merge_entity_map(week_routes, daily["routes"])
            week_stops = merge_entity_map(week_stops, daily["stops"])
        else:
            week_routes = merge_entity_map(week_routes, obs_db.rollup_routes(sd))
            week_stops = merge_entity_map(week_stops, obs_db.rollup_stops(sd))
    week = {"routes": week_routes, "stops": week_stops}

    baseline = load_baseline(state_dir)
    # The store is pruned to the same 7-date window as `week`, so reuse the
    # week rollup for `all` instead of scanning the window twice. Only on the
    # first poll after a restore do store_dates and the week window differ.
    recent = week if set(store_dates) == set(week_dates) else {
        "routes": obs_db.rollup_routes_for_dates(store_dates),
        "stops": obs_db.rollup_stops_for_dates(store_dates),
    }
    all_totals = {
        "routes": merge_entity_map(baseline["routes"], recent["routes"]),
        "stops": merge_entity_map(baseline["stops"], recent["stops"]),
    }

    mins = [m for m in (store_dates[0] if store_dates else None, baseline.get("min_service_date")) if m]
    data_range = {"min": min(mins) if mins else current_sd, "max": current_sd}

    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "current_service_date": current_sd,
        "data_range": data_range,
        "metadata": {
            "routes": static["routes"],
            "stops": static["stops"],
        },
        "periods": {
            "hour": hour,
            "day": day,
            "week": week,
            "all": all_totals,
        },
    }


def _week_dates(today: str, count: int = 6) -> list[str]:
    """The 7-date week window: today plus the count days before it."""
    return [today, *_prev_dates(today, count)]


def _prev_dates(today: str, count: int) -> list[str]:
    d = datetime.strptime(today, "%Y-%m-%d").date()
    return [(d - timedelta(days=i)).isoformat() for i in range(1, count + 1)]