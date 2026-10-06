"""Look-ahead test harness.

`check_no_lookahead(fn, dataset, as_of_dates)` runs a computation
`fn(view) -> pl.DataFrame` at each date T three ways:

  1. on the full dataset, then asserts the view's audit saw nothing after T;
  2. on the dataset physically truncated to rows known at T;
  3. on the dataset with every future float value corrupted.

All three outputs must be identical. A computation that filters on
period_end instead of filed_at, applies a split before its ex-date, or reads
restated numbers before the restatement was filed fails at least one of them.
Every registered factor goes through this before scoring may run (see gate.py).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable

import polars as pl
from polars.testing import assert_frame_equal

from igs.pit.view import LookAheadError, PitDataset, PitView

Computation = Callable[[PitView], pl.DataFrame]


def _normalise(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(df.columns, nulls_last=True) if df.height else df


def _same(a: pl.DataFrame, b: pl.DataFrame) -> str | None:
    try:
        assert_frame_equal(_normalise(a), _normalise(b), check_row_order=True,
                           check_exact=False, rel_tol=1e-9, abs_tol=1e-12)
    except AssertionError as exc:
        return str(exc).splitlines()[0]
    return None


def check_no_lookahead(
    fn: Computation,
    dataset: PitDataset,
    as_of_dates: Iterable[dt.datetime],
    name: str = "computation",
) -> None:
    for as_of in as_of_dates:
        full_view = PitView(dataset, as_of)
        full = fn(full_view)
        full_view.audit.assert_clean()

        truncated = fn(PitView(dataset.truncate(as_of), as_of))
        diff = _same(full, truncated)
        if diff:
            raise LookAheadError(
                f"{name} at {as_of.isoformat()}: output changes when future rows are removed "
                f"({diff})")

        poisoned = fn(PitView(dataset.poison_future(as_of), as_of))
        diff = _same(full, poisoned)
        if diff:
            raise LookAheadError(
                f"{name} at {as_of.isoformat()}: output changes when future values are "
                f"corrupted ({diff})")
