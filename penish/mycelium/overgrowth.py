"""How far a picture is overgrown: time unseen, spread by its hypha neighbors.

Pure. Base is computed at read time from `last_viewed_at`, so a view clears it
at once; the tick stores only the neighbor level. `now` is always passed in.
"""

from __future__ import annotations

import math
from datetime import datetime

from penish.mycelium.tunables import Tunables


def views_per_day(views_last_7_days: float) -> float:
    """Mean daily views over the last week, floored at 1 so an empty week cannot divide by zero."""
    return max(1.0, views_last_7_days / 7.0)


def tau_hours(live_pictures: int, views_per_day: float, tunables: Tunables) -> float:
    # About two full rounds of the gallery at the current pace before a picture
    # is mostly overgrown; a quiet week cannot shrink it below the floor.
    vpd = max(1.0, views_per_day)
    return max(tunables.overgrowth_tau_min_h, 2.0 * live_pictures / vpd * 24.0)


def base_from_hours(hours_unseen: float, tau: float) -> float:
    # A last view in the future (clock skew) counts as a view just now.
    return 1.0 - math.exp(-max(0.0, hours_unseen) / tau)


def base_level(last_viewed_at: datetime, now: datetime, tau: float) -> float:
    hours = (now - last_viewed_at).total_seconds() / 3600.0
    return base_from_hours(hours, tau)


def level(base: float, neighbor_level: float, tunables: Tunables) -> float:
    return min(1.0, base * (1.0 + tunables.overgrowth_sigma * neighbor_level))


def coverage(level: float, tunables: Tunables) -> float:
    """Share of the picture drawn over: a fully overgrown picture still shows through."""
    return level * tunables.overgrowth_cap
