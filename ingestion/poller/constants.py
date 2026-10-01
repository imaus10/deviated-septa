"""Shared constants for the poller pipeline — single source of truth."""

from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# On-time window: delay < -60s is early, delay > 300s is late.
EARLY_TOLERANCE_SECONDS = -60
LATE_TOLERANCE_SECONDS = 300

# Rollup count keys, in output order. Order here matches the totals dict
# shape so JSON output is stable.
CATEGORY_COUNT_KEYS = ("on_time_count", "early_count", "late_count")

# Bus + trolley scope (0 = trolley, 3 = bus, 11 = trolleybus 59/66/75).
ROUTE_SCOPE_TYPES = {0, 3, 11}

# An elapsed service date is archived only once it has been absent from the
# feed for this long (minutes), so the overnight straggler tail is captured
# instead of freezing the archive at the midnight switchover.
ARCHIVE_QUIET_WINDOW_MINUTES = 60

# Rows of an aged-out service date deleted per poll. The prune is incremental
# on purpose: a full-day delete (~800K rows) stalls the poll for ~30-45 min on
# the Pi's SD card and bloats the WAL. Deleting one small committed chunk per
# poll keeps each cycle on cadence while the date drains over ~an hour. Tune
# this to the card: too large -> slow polls, too small -> long drain.
PRUNE_DELETE_CHUNK = 5000