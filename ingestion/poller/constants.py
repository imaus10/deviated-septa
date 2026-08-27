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