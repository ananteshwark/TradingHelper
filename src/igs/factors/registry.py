"""Factor registry.

Every factor is a pure function `fn(view: PitView) -> pl.DataFrame` registered
with `@factor(...)`. Registration is what puts a factor under the look-ahead
gate: the gate test runs every registered factor through the harness, and the
scoring layer only uses registered factors.

Factor modules must not import I/O (database, HTTP, raw store); a test
enforces that boundary so a factor cannot bypass the point-in-time view.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import polars as pl

from igs.pit.view import PitView

Pillar = Literal["growth", "quality", "valuation", "momentum", "ownership"]
FactorFn = Callable[[PitView], pl.DataFrame]


@dataclass(frozen=True)
class FactorSpec:
    name: str
    pillar: Pillar
    higher_is_better: bool
    fn: FactorFn
    description: str


REGISTRY: dict[str, FactorSpec] = {}


def factor(name: str, pillar: Pillar, higher_is_better: bool, description: str):
    def register(fn: FactorFn) -> FactorFn:
        if name in REGISTRY:
            raise ValueError(f"factor {name!r} registered twice")
        REGISTRY[name] = FactorSpec(name, pillar, higher_is_better, fn, description)
        return fn
    return register
