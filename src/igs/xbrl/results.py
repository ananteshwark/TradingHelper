"""Financial results XBRL -> point-in-time fact rows.

Mapping comes from config/xbrl_concepts.yaml. The extractor is strict:
  * unknown taxonomy year          -> TaxonomyMismatch (filing refused)
  * no standalone/consolidated tag -> XbrlMappingError
  * required concept missing       -> XbrlMappingError (e.g. no revenue: the
                                      mapping, not the company, is wrong)
  * unit inconsistent with concept -> fact skipped, DQ warning
  * numeric element not in mapping -> reported as xbrl_unmapped_element
"""

from __future__ import annotations

import datetime as dt
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


@cache
def _raw_config(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())


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
    return ConceptMap(version, concepts, cfg["metadata"], e2c)


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


def period_type(ctx: Context) -> str:
    if ctx.instant is not None:
        return "INSTANT"
    assert ctx.start is not None and ctx.end is not None
    days = (ctx.end - ctx.start).days + 1
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
    out = {}
    for key, names in cmap.metadata.items():
        for n in names:
            for f in instance.by_name.get(n, []):
                if f.value and not instance.contexts[f.context].dims:
                    out[key] = f.value
                    break
            if key in out:
                break
    return out


def extract_results(instance: Instance, dq: DQLog, fetch_id: str | None = None,
                    path: Path | None = None) -> ResultsFiling:
    cmap = load_concept_map(mapping_version(instance, path), path)
    meta = _meta(instance, cmap)
    nature = (meta.get("nature_of_report") or "").strip().lower()
    if nature not in ("standalone", "consolidated"):
        raise XbrlMappingError(f"NatureOfReport is {meta.get('nature_of_report')!r}; cannot tell "
                               "standalone from consolidated")

    facts: list[dict[str, Any]] = []
    dimensional = 0
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
                ptype = period_type(ctx)
                if ptype == "OTHER":
                    dq.emit("info", "xbrl_unusual_period",
                            f"{name} period {ctx.start}..{ctx.end} skipped", fetch_id=fetch_id)
                    continue
                facts.append({"concept": concept, "source_element": f"{f.namespace}#{name}",
                              "value": value, "unit": unit, "decimals": _decimals(f.decimals),
                              "period_start": ctx.start, "period_end": ctx.period_end,
                              "period_type": ptype})
            break   # first candidate element present wins

    facts = _dedupe(facts, dq, fetch_id)
    present = {f["concept"] for f in facts}
    fmt = "bank" if "interest_earned" in present else (
        "nbfc" if "interest_income" in present and "revenue" not in present else "default")
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
    """One value per concept/period within a filing; conflicting duplicates are reported."""
    seen: dict[tuple, dict[str, Any]] = {}
    for f in facts:
        key = (f["concept"], f["period_end"], f["period_type"])
        if key not in seen:
            seen[key] = f
        elif seen[key]["value"] != f["value"]:
            dq.emit("warn", "xbrl_conflicting_values",
                    f"{key}: {seen[key]['value']} vs {f['value']} in one filing; first kept",
                    fetch_id=fetch_id)
    return list(seen.values())


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
