"""How current a device's last reading is: ONE rule (spec A30.34 D8, A30.35).

`/runtime` has classified device freshness since A2 -- no reading is
unknown, fifteen minutes or less is fresh, anything older is stale -- and
it was the only place that did. Machine Attention needs the same answer
for every device it describes, and two copies of a threshold are two
answers the first time either is edited. So the rule lives here, and both
callers ask it.

It reads `last_seen_at`: when the SITE last heard from the device. Never
`snapshot_at`, which is when Central Command last copied the row -- a Site
Manager that keeps polling keeps that fresh however long the device has
been silent.

Context, never authority. Nothing gates on freshness, nothing is ordered
by it, and a stale device is not an unauthorized one. Unknown is never
read as fresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"
FRESHNESS_STATES = frozenset({FRESH, STALE, UNKNOWN})

#: The `/runtime` window, unchanged: a reading this recent or more is fresh.
FRESHNESS_WINDOW = timedelta(minutes=15)


def freshness_state(last_seen_at: Any, now: datetime) -> str:
    """`fresh`, `stale` or `unknown` for one device's last reading.

    No reading -- or anything that is not a timestamp -- is UNKNOWN, not
    stale and not fresh. The platform stores UTC, and an engine that drops
    the zone (sqlite) hands back a naive value, which is UTC and is read as
    such rather than failing to compare.
    """
    if not isinstance(last_seen_at, datetime):
        return UNKNOWN
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if (now - last_seen_at) <= FRESHNESS_WINDOW:
        return FRESH
    return STALE
