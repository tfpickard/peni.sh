"""Every parameter of the organism, with bounds, in one place.

Durations are in ticks. The tick length itself is a constant, not a tunable:
lifecycle rules are written in ticks, and a changed interval would make a replay
disagree with the run it replays.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TICK_SECONDS = 600
TICKS_PER_DAY = 86_400 // TICK_SECONDS

LAYERS: tuple[str, ...] = (
    "whole",
    "facet:subject",
    "facet:setting",
    "facet:style",
    "facet:mood",
    "need",
)


@dataclass(frozen=True, slots=True)
class Spec:
    lo: float
    hi: float
    meaning: str


@dataclass(frozen=True, slots=True)
class Tunables:
    candidates: int = 8
    affinity_floor: float = 0.05
    facet_bonus: float = 0.15
    need_damp: float = 0.8
    need_feed: float = 0.05
    need_drift: float = 0.002
    walkers: int = 1
    steps: int = 3
    alpha: float = 1.0
    beta: float = 2.0
    epsilon: float = 0.05
    rho: float = 0.01
    budget: float = 1.0
    wither_min: float = 0.02
    wither_idle: int = 50
    hypha_visible: float = 0.15
    colony_enter: float = 0.30
    colony_leave: float = 0.24
    fatigue_gamma: float = 0.8
    fatigue_ticks: int = 720
    traffic_per_click: float = 0.5
    traffic_edge_cap: float = 1.0
    traffic_share: float = 0.25
    fruit_ticks: int = 144
    grace_ticks: int = 288
    fruit_cap: int = 3
    min_size: int = 3
    max_size: int = 12
    remerge_size: int = 10
    admit_ticks: int = 6
    match_jaccard: float = 0.5
    drift_keep: float = 0.3333
    duplicate_jaccard: float = 0.7
    overgrowth_tau_min_h: float = 72.0
    overgrowth_sigma: float = 0.5
    overgrowth_cap: float = 0.85

    def as_dict(self) -> dict[str, float | int]:
        return dataclasses.asdict(self)


SPECS: dict[str, Spec] = {
    "candidates": Spec(2, 32, "nearest neighbors per picture per layer"),
    "affinity_floor": Spec(0.0, 0.5, "lowest affinity after rescale"),
    "facet_bonus": Spec(0.0, 1.0, "bonus for close in one facet and far in another"),
    "need_damp": Spec(0.0, 1.0, "how much a satisfied need pulls less"),
    "need_feed": Spec(0.0, 1.0, "share of the way to satisfied per tick of flow"),
    "need_drift": Spec(0.0, 0.5, "share of satisfaction lost per tick"),
    "walkers": Spec(0, 8, "walkers per picture per layer per tick"),
    "steps": Spec(1, 12, "steps per walker"),
    "alpha": Spec(0.0, 4.0, "weight on existing strength"),
    "beta": Spec(0.0, 6.0, "weight on affinity"),
    "epsilon": Spec(0.0, 1.0, "exploration floor"),
    "rho": Spec(0.0005, 0.2, "strength learning rate per tick"),
    "budget": Spec(0.1, 8.0, "cap on outgoing strength per picture per layer"),
    "wither_min": Spec(0.0, 0.2, "idle edges below this are dropped"),
    "wither_idle": Spec(1, 1000, "ticks without flow before a weak edge drops"),
    "hypha_visible": Spec(0.0, 1.0, "drawn above this"),
    "colony_enter": Spec(0.0, 1.0, "an edge latches at this"),
    "colony_leave": Spec(0.0, 1.0, "a latched edge unlatches below this"),
    "fatigue_gamma": Spec(0.0, 1.0, "deposit cut at full fatigue"),
    "fatigue_ticks": Spec(1, 10_000, "ticks latched to full fatigue"),
    "traffic_per_click": Spec(0.0, 4.0, "flow per recorded click"),
    "traffic_edge_cap": Spec(0.0, 8.0, "most traffic flow per edge per tick"),
    "traffic_share": Spec(0.0, 4.0, "traffic flow cap as a share of walker crossings"),
    "fruit_ticks": Spec(1, 10_000, "ticks a forming colony holds before it fruits"),
    "grace_ticks": Spec(1, 10_000, "ticks a fading gallery has to reconnect"),
    "fruit_cap": Spec(0, 100, "fruitings per rolling day, all layers"),
    "min_size": Spec(2, 50, "smallest colony"),
    "max_size": Spec(3, 200, "largest gallery"),
    "remerge_size": Spec(2, 200, "a full gallery admits again at or below this"),
    "admit_ticks": Spec(1, 1000, "ticks a join or leave must persist"),
    "match_jaccard": Spec(0.05, 1.0, "overlap that keeps identity"),
    "drift_keep": Spec(0.0, 1.0, "share of founders below which a gallery has drifted"),
    "duplicate_jaccard": Spec(0.0, 1.0, "overlap with another layer's gallery that stops fruiting"),
    "overgrowth_tau_min_h": Spec(1.0, 10_000.0, "floor of the overgrowth time constant, hours"),
    "overgrowth_sigma": Spec(0.0, 4.0, "spread from hypha neighbors"),
    "overgrowth_cap": Spec(0.0, 1.0, "drawn coverage at full overgrowth"),
}

DEFAULTS = Tunables()

_INTS = {f.name for f in dataclasses.fields(Tunables) if f.type in ("int", int)}


class TunableError(ValueError):
    pass


def coerce(name: str, value: Any) -> float | int:
    """One value checked against its bounds; ints stay ints."""
    spec = SPECS.get(name)
    if spec is None:
        raise TunableError(f"unknown tunable {name}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TunableError(f"{name} must be a number")
    out: float | int = float(value)
    # JSON bodies may carry NaN or Infinity, and int() of either raises a plain
    # ValueError or OverflowError, which would surface as a 500 rather than a 400.
    if not math.isfinite(out):
        raise TunableError(f"{name} must be a number")
    if name in _INTS:
        if out != int(value):
            raise TunableError(f"{name} must be a whole number")
        out = int(value)
    if not spec.lo <= out <= spec.hi:
        raise TunableError(f"{name} must be between {spec.lo:g} and {spec.hi:g}")
    return out


def with_overrides(base: Tunables, overrides: Mapping[str, Any]) -> Tunables:
    """A copy with overrides applied; unknown names and out-of-bounds values raise."""
    clean: dict[str, Any] = {k: coerce(k, v) for k, v in overrides.items()}
    out = dataclasses.replace(base, **clean)
    # Hysteresis only works one way round, and a cap below the floor is never full.
    if out.colony_leave > out.colony_enter:
        raise TunableError("colony_leave must not exceed colony_enter")
    # Fused is decided before wither, so a latched edge must never be weak enough to
    # wither, or its partner would stay fused to nothing for a tick.
    if out.wither_min > out.colony_leave:
        raise TunableError("wither_min must not exceed colony_leave")
    if out.remerge_size > out.max_size or out.min_size > out.max_size:
        raise TunableError("sizes must satisfy min_size <= remerge_size <= max_size")
    return out
