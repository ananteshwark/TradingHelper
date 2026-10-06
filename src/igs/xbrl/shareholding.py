"""Shareholding pattern XBRL -> one row per shareholder category.

STATUS: axis, member and measure names come from config/xbrl_concepts.yaml
and must be confirmed on a real SHP instance. A filing where no category can
be identified is refused loudly rather than loaded empty.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from igs.config import config_dir
from igs.dq import DQLog
from igs.xbrl.instance import Instance, parse_instance
from igs.xbrl.results import XbrlMappingError, _raw_config


def extract_shareholding(instance: Instance, dq: DQLog, fetch_id: str | None = None,
                         path: Path | None = None) -> tuple[dt.date, list[dict[str, Any]]]:
    cfg = _raw_config(str(path or config_dir() / "xbrl_concepts.yaml"))["shareholding"]
    axes = set(cfg["category_axes"])
    member_to_cat = {m: cat for cat, members in cfg["categories"].items() for m in members}
    name_to_measure = {n: meas for meas, names in cfg["measures"].items() for n in names}

    rows: dict[str, dict[str, Any]] = {}
    ends: set[dt.date] = set()
    unknown_members: set[str] = set()
    for f in instance.facts:
        measure = name_to_measure.get(f.name)
        if measure is None or f.nil or not f.value:
            continue
        ctx = instance.contexts[f.context]
        cat_dims = [m for axis, m in ctx.dims if axis in axes]
        if len(ctx.dims) == 0:
            category = "total"
        elif len(cat_dims) == 1 and len(ctx.dims) == 1:
            category = member_to_cat.get(cat_dims[0])
            if category is None:
                unknown_members.add(cat_dims[0])
                continue
        else:
            continue   # sub-categories and cross-dimensional breakdowns are not loaded
        try:
            value = float(f.value)
        except ValueError:
            dq.emit("warn", "xbrl_bad_number", f"SHP {f.name}={f.value!r}", fetch_id=fetch_id)
            continue
        ends.add(ctx.period_end)
        row = rows.setdefault(category, {"category": category})
        row.setdefault(measure, value)
    if unknown_members:
        dq.emit("info", "shp_unmapped_member",
                f"{len(unknown_members)} shareholder categories not mapped",
                fetch_id=fetch_id, details={"members": sorted(unknown_members)[:100]})
    if "promoter" not in rows:
        if rows.get('total', {}).get('shares', 0) <= 0 or len(rows) < 2:
            raise XbrlMappingError("shareholding filing without identifiable categories and total")
        dq.emit('warn', 'shp_promoter_unavailable',
                'Loaded disclosed categories; promoter category absent, not assumed zero',
                fetch_id=fetch_id)
    if len(ends) != 1:
        raise XbrlMappingError(f"shareholding facts span several dates {sorted(ends)}")
    # The standard ratio unit carries 1.0 for 100%. Anchor to the total row,
    # never to a small individual holding that could already be a percentage.
    if rows.get('total', {}).get('pct_of_total') == 1.0:
        for row in rows.values():
            if 'pct_of_total' in row:
                row['pct_of_total'] *= 100
    for row in rows.values():
        if row.get('shares', 0) > 0 and row.get('pledged_shares') is not None:
            row['pledged_pct'] = 100 * row['pledged_shares'] / row['shares']
    return ends.pop(), list(rows.values())


def parse_shareholding(content: bytes, dq: DQLog, fetch_id: str | None = None):
    """Validate every context used by ownership measures, ignoring unrelated metadata.

    Legacy NSE exports refer to absent OneD/OneI contexts for company metadata and
    foreign-ownership limits. Do not fabricate these contexts or relax validation
    of a share count, percentage, pledge or holder-count fact.
    """
    cfg = _raw_config(str(config_dir() / 'xbrl_concepts.yaml'))['shareholding']
    names = {name for values in cfg['measures'].values() for name in values}
    return extract_shareholding(parse_instance(content, fact_names=names), dq, fetch_id)
