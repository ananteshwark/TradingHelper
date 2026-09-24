"""Financial results XBRL -> point-in-time fact rows.

Mapping comes from config/xbrl_concepts.yaml. The extractor is strict:
  * unknown taxonomy year          -> TaxonomyMismatch (filing refused)
  * no standalone/consolidated tag -> XbrlMappingError
  * required concept missing       -> XbrlMappingError (e.g. no revenue: the
                                      mapping, not the company, is wrong)
  * unit inconsistent with concept -> fact skipped, DQ warning
  * numeric element not in mapping -> reported as xbrl_unmapped_element
  * two different values for one concept and period in one filing -> neither is kept
    (DQ warning); if that removes a required concept the filing is refused

NSE instances declare the same period on every column (see `nse_columns` in the
YAML): when a filing uses NSE's column ids, periods come from the id and the
filing's own reporting-period and financial-year dates. Columns whose meaning has
not been verified are skipped and reported.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from igs.config import config_dir
from igs.dq import DQLog
from igs.xbrl.instance import Context, Instance


class TaxonomyMismatch(ValueError):
    pass


class XbrlMappingError(ValueError):
    pass


@dataclass(frozen=True)
class ConceptMap:
    version: str
    concepts: dict[str, list[str]]
    metadata: dict[str, list[str]]
    element_to_concept: dict[str, str]
    nse_columns: dict[str, str] = field(default_factory=dict)


@cache
def _raw_config(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_concept_map(version: str, path: Path | None = None) -> ConceptMap:
    cfg = _raw_config(str(path or config_dir() / "xbrl_concepts.yaml"))
    versions = cfg["versions"]
    if version not in versions:
        raise TaxonomyMismatch(f"no concept mapping for taxonomy version {version}")
    chain, v = [], version
    while v is not None:
        chain.append(versions[v])
        v = versions[v].get("extends")
    concepts: dict[str, list[str]] = {}
    for layer in reversed(chain):
        for k, names in layer.items():
            if k == "extends":
                continue
            concepts[k] = list(names) + [n for n in concepts.get(k, []) if n not in names]
    e2c = {}
    for concept, names in concepts.items():
        for n in names:
            if n in e2c and e2c[n] != concept:
                raise XbrlMappingError(f"element {n} mapped to both {e2c[n]} and {concept}")
            e2c[n] = concept
    return ConceptMap(version, concepts, cfg["metadata"], e2c, dict(cfg.get("nse_columns", {})))


def mapping_version(instance: Instance, path: Path | None = None) -> str:
    cfg = _raw_config(str(path or config_dir() / "xbrl_concepts.yaml"))
    year = instance.taxonomy_year()
    version = cfg["taxonomy_years"].get(year) if year else None
    if version is None:
        if cfg.get("allow_unknown_taxonomy"):
            return max(cfg["versions"])
        raise TaxonomyMismatch(f"taxonomy year {year!r} from {instance.schema_refs} is not "
                               "mapped in xbrl_concepts.yaml")
    return version


PERCENT_CONCEPTS = {"gnpa_pct", "nnpa_pct"}
PER_SHARE_CONCEPTS = {"eps_basic", "eps_diluted", "face_value"}
REQUIRED = {"default": ["revenue", "pat"], "bank": ["interest_earned", "pat"],
            "nbfc": ["pat"]}


NSE_COLUMN_ID = re.compile(r"^(One|Two|Three|Four|Five|Six)(D|I)$")


def period_type(ctx: Context) -> str:
    if ctx.instant is not None:
        return "INSTANT"
    assert ctx.start is not None and ctx.end is not None
    return duration_type(ctx.start, ctx.end)


def duration_type(start: dt.date, end: dt.date) -> str:
    days = (end - start).days + 1
    for label, lo, hi in (("Q", 85, 95), ("H1", 175, 190), ("9M", 265, 280), ("FY", 360, 370)):
        if lo <= days <= hi:
            return label
    return "OTHER"


def _unit_ok(concept: str, unit: str | None) -> bool:
    if unit is None:
        return False
    u = unit.upper()
    if concept in PERCENT_CONCEPTS:
        return u == "PURE"
    if concept in PER_SHARE_CONCEPTS:
        return u.startswith("INR")
    return u == "INR"


@dataclass
class ResultsFiling:
    taxonomy_version: str
    statement_basis: str
    results_format: str
    period_start: dt.date | None
    period_end: dt.date
    metadata: dict[str, str]
    facts: list[dict[str, Any]] = field(default_factory=list)


def _meta(instance: Instance, cmap: ConceptMap) -> dict[str, str]:
    """Filing metadata. Q4 instances state the reporting period twice, for the quarter
    (OneD) and for the year (FourD): the current column's value is taken whatever the
    order of the facts in the file."""
    current = {cid for cid, kind in cmap.nse_columns.items()
               if kind in ("current", "current_instant")}
    out = {}
    for key, names in cmap.metadata.items():
        for n in names:
            found = [f for f in instance.by_name.get(n, [])
                     if f.value and not instance.contexts[f.context].dims]
            found.sort(key=lambda f: f.context not in current)     # stable: file order next
            if found:
                out[key] = found[0].value or ""
                break
    return out


def results_form(instance: Instance, present: set[str], path: Path | None = None) -> str:
    """default / bank / nbfc. Integrated Filing instances name their form in the entry-point
    namespace (`result_forms` in the YAML); otherwise it is read from the line items: a bank
    reports InterestEarned and no RevenueFromOperations, an NBFC (Ind AS Division III) reports
    impairment on financial instruments. NBFCs also tag interest income as InterestEarned,
    so that element alone does not make a bank."""
    cfg = _raw_config(str(path or config_dir() / "xbrl_concepts.yaml"))
    for marker, form in (cfg.get("result_forms") or {}).items():
        if any(f"/{marker}/" in ns for ns in instance.namespaces):    # a whole path segment
            return form
    if "interest_earned" in present and "revenue" not in present:
        return "bank"
    if present & {"interest_income", "impairment_on_financial_instruments"}:
        return "nbfc"
    return "default"


def extract_results(instance: Instance, dq: DQLog, fetch_id: str | None = None,
                    path: Path | None = None) -> ResultsFiling:
    cmap = load_concept_map(mapping_version(instance, path), path)
    meta = _meta(instance, cmap)
    nature = (meta.get("nature_of_report") or "").strip().lower()
    if nature not in ("standalone", "consolidated"):
        raise XbrlMappingError(f"NatureOfReport is {meta.get('nature_of_report')!r}; cannot tell "
                               "standalone from consolidated")

    columns = _column_periods(instance, meta, cmap, dq, fetch_id)
    facts: list[dict[str, Any]] = []
    dimensional = 0
    skipped_columns: set[str] = set()
    for concept, names in cmap.concepts.items():
        for name in names:
            found = [f for f in instance.by_name.get(name, []) if not f.nil]
            if not found:
                continue
            for f in found:
                ctx = instance.contexts[f.context]
                if ctx.dims:
                    dimensional += 1
                    continue
                unit = instance.units.get(f.unit or "")
                if not _unit_ok(concept, unit):
                    dq.emit("warn", "xbrl_unit_mismatch",
                            f"{name} ({concept}) has unit {unit!r}; fact skipped",
                            fetch_id=fetch_id)
                    continue
                try:
                    value = float(f.value or "")
                except ValueError:
                    dq.emit("warn", "xbrl_bad_number", f"{name}={f.value!r} is not numeric",
                            fetch_id=fetch_id)
                    continue
                if columns is not None:
                    col = columns.get(ctx.id)
                    if col is None:
                        skipped_columns.add(ctx.id)
                        continue
                    start, end, ptype = col
                else:
                    start, end, ptype = ctx.start, ctx.period_end, period_type(ctx)
                if ptype in ("OTHER", "H1_YTD", "9M_YTD"):
                    if ptype == "OTHER":
                        dq.emit("info", "xbrl_unusual_period",
                                f"{name} period {start}..{end} skipped", fetch_id=fetch_id)
                    continue
                facts.append({"concept": concept, "source_element": f"{f.namespace}#{name}",
                              "value": value, "unit": unit, "decimals": _decimals(f.decimals),
                              "period_start": start, "period_end": end,
                              "period_type": ptype})
            break   # first candidate element present wins

    facts = _dedupe(facts, dq, fetch_id)
    present = {f["concept"] for f in facts}
    fmt = results_form(instance, present, path)
    missing = [c for c in REQUIRED[fmt] if c not in present]
    if missing:
        raise XbrlMappingError(f"{fmt}-format results without {missing}: mapping does not match "
                               "this filing's taxonomy")

    mapped_names = set(cmap.element_to_concept) | {n for v in cmap.metadata.values() for n in v}
    unmapped = sorted({f.name for f in instance.facts
                       if f.unit and f.name not in mapped_names
                       and not instance.contexts[f.context].dims})
    if unmapped:
        dq.emit("info", "xbrl_unmapped_element",
                f"{len(unmapped)} numeric elements not in the concept mapping",
                fetch_id=fetch_id, details={"elements": unmapped[:200]})
    if skipped_columns:
        dq.emit("info", "xbrl_columns_skipped",
                f"columns {sorted(skipped_columns)} not loaded: their periods are not stated in "
                "NSE instances and their meaning is not verified (nse_columns)",
                fetch_id=fetch_id)
    if dimensional:
        dq.emit("info", "xbrl_dimensional_skipped",
                f"{dimensional} dimensional facts (segments etc.) not loaded", fetch_id=fetch_id)

    reporting = [f for f in facts if f["period_type"] != "INSTANT"]
    period_end = _meta_date(meta.get("period_end")) or max(
        (f["period_end"] for f in reporting), default=None)
    if period_end is None:
        raise XbrlMappingError("cannot determine the reporting period end")
    return ResultsFiling(taxonomy_version=cmap.version, statement_basis=nature,
                         results_format=fmt, period_start=_meta_date(meta.get("period_start")),
                         period_end=period_end, metadata=meta, facts=facts)


def _dedupe(facts: list[dict[str, Any]], dq: DQLog, fetch_id: str | None) -> list[dict]:
    """One value per concept/period within a filing. Two different values for the same
    concept and period cannot both be right and neither can be chosen safely: both are
    dropped and reported."""
    seen: dict[tuple, dict[str, Any]] = {}
    conflicts: dict[tuple, set[float]] = {}
    for f in facts:
        key = (f["concept"], f["period_end"], f["period_type"])
        if key not in seen:
            seen[key] = f
        elif seen[key]["value"] != f["value"]:
            conflicts.setdefault(key, {seen[key]["value"]}).add(f["value"])
    for key, values in conflicts.items():
        dq.emit("warn", "xbrl_conflicting_values",
                f"{key}: values {sorted(values)} in one filing; none kept", fetch_id=fetch_id)
        del seen[key]
    return list(seen.values())


def _column_periods(instance: Instance, meta: dict[str, str], cmap: ConceptMap, dq: DQLog,
                    fetch_id: str | None) -> dict[str, tuple] | None:
    """Context id -> (start, end, period_type) for NSE-style instances, else None (use the
    declared context periods). Only ids configured in nse_columns are mapped."""
    ids = {c.id for c in instance.contexts.values() if not c.dims}
    if "OneD" not in ids or not any(NSE_COLUMN_ID.match(i) for i in ids):
        return None
    start, end = _meta_date(meta.get("period_start")), _meta_date(meta.get("period_end"))
    if start is None or end is None:
        raise XbrlMappingError("NSE-style instance without DateOfStart/EndOfReportingPeriod: "
                               "column periods cannot be established")
    fy_start = _meta_date(meta.get("fy_start"))
    out: dict[str, tuple] = {}
    for cid, meaning in cmap.nse_columns.items():
        if cid not in ids:
            continue
        if meaning == "current":
            out[cid] = (start, end, duration_type(start, end))
        elif meaning == "current_instant":
            out[cid] = (None, end, "INSTANT")
        elif meaning == "same_period_last_year":
            ps, pe = _minus_year(start), _minus_year(end)
            out[cid] = (ps, pe, duration_type(ps, pe))
        elif meaning == "year_to_date":
            if fy_start is None:
                dq.emit("info", "xbrl_columns_skipped",
                        f"{cid}: no DateOfStartOfFinancialYear; year-to-date column skipped",
                        fetch_id=fetch_id)
                continue
            if fy_start == start:
                continue            # first period of the year: same as the current column
            kind = duration_type(fy_start, end)
            # Year to date that is not a full year (6 or 9 months) is not stored.
            out[cid] = (fy_start, end, kind if kind == "FY" else f"{kind}_YTD")
        else:
            raise XbrlMappingError(f"nse_columns: unknown meaning {meaning!r} for {cid}")
    return out


def _decimals(d: str | None) -> int | None:
    if d is None or d == "INF":
        return None
    try:
        return int(d)
    except ValueError:
        return None


def _meta_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def _minus_year(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:          # 29 February
        return d.replace(year=d.year - 1, day=28)
