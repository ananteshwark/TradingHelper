"""Generic XBRL 2.1 instance parser (stdlib only).

Extracts contexts (entity, period, dimensions), units and item facts. Nothing
here knows about SEBI concepts; mapping lives in igs.xbrl.mapping.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
_SKIP_NS = {XBRLI, LINK}


class XbrlError(ValueError):
    pass


@dataclass(frozen=True)
class Context:
    id: str
    entity: str | None
    start: dt.date | None
    end: dt.date | None
    instant: dt.date | None
    dims: tuple[tuple[str, str], ...] = ()

    @property
    def period_end(self) -> dt.date:
        d = self.instant or self.end
        assert d is not None
        return d


@dataclass(frozen=True)
class Fact:
    namespace: str
    name: str
    context: str
    unit: str | None
    decimals: str | None
    value: str | None
    nil: bool


@dataclass
class Instance:
    facts: list[Fact]
    contexts: dict[str, Context]
    units: dict[str, str]
    schema_refs: list[str]
    root_tag: str = ""
    by_name: dict[str, list[Fact]] = field(default_factory=dict)
    # Every namespace URI declared in the document (Integrated Filing instances name their
    # form, e.g. IntegratedFinance_NBFC, only in the entry-point namespace).
    namespaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for f in self.facts:
            self.by_name.setdefault(f.name, []).append(f)

    def taxonomy_year(self) -> str | None:
        for href in self.schema_refs:
            m = re.search(r"(20\d\d)-\d\d-\d\d", href) or re.search(r"(20\d\d)", href)
            if m:
                return m[1]
        return None


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, name = tag[1:].split("}", 1)
        return ns, name
    return "", tag


def _date(text: str | None) -> dt.date | None:
    if not text:
        return None
    return dt.date.fromisoformat(text.strip()[:10])


def _qname_local(text: str) -> str:
    return text.strip().split(":")[-1]


def parse_instance(content: bytes) -> Instance:
    head = content[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise XbrlError("DTD/entity declarations are not allowed in XBRL instances")
    namespaces: list[str] = []
    try:
        parser = ET.iterparse(io.BytesIO(content), events=("start-ns",))
        for _, (_prefix, uri) in parser:
            if uri not in namespaces:
                namespaces.append(uri)
        root = parser.root
    except ET.ParseError as exc:
        raise XbrlError(f"not well-formed XML: {exc}") from exc
    ns, name = _local(root.tag)
    if ns != XBRLI or name != "xbrl":
        raise XbrlError(f"root element is {root.tag}, not xbrli:xbrl")

    contexts: dict[str, Context] = {}
    for c in root.findall(f"{{{XBRLI}}}context"):
        ent = c.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
        per = c.find(f"{{{XBRLI}}}period")
        if per is None:
            raise XbrlError(f"context {c.get('id')} has no period")
        dims = []
        for holder in (c.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}segment"),
                       c.find(f"{{{XBRLI}}}scenario")):
            if holder is None:
                continue
            for m in holder.findall(f"{{{XBRLDI}}}explicitMember"):
                dims.append((_qname_local(m.get("dimension", "")), _qname_local(m.text or "")))
            for m in holder.findall(f"{{{XBRLDI}}}typedMember"):
                val = "".join(m.itertext()).strip()
                dims.append((_qname_local(m.get("dimension", "")), f"typed:{val}"))
        contexts[c.get("id", "")] = Context(
            id=c.get("id", ""),
            entity=ent.text.strip() if ent is not None and ent.text else None,
            start=_date(per.findtext(f"{{{XBRLI}}}startDate")),
            end=_date(per.findtext(f"{{{XBRLI}}}endDate")),
            instant=_date(per.findtext(f"{{{XBRLI}}}instant")),
            dims=tuple(sorted(dims)),
        )

    units: dict[str, str] = {}
    for u in root.findall(f"{{{XBRLI}}}unit"):
        measures = [_qname_local(m.text or "") for m in u.iter(f"{{{XBRLI}}}measure")]
        div = u.find(f"{{{XBRLI}}}divide")
        if div is not None and len(measures) == 2:
            units[u.get("id", "")] = f"{measures[0]}/{measures[1]}"
        else:
            units[u.get("id", "")] = "*".join(measures)

    schema_refs = [e.get(f"{{{XLINK}}}href", "") for e in root.findall(f"{{{LINK}}}schemaRef")]

    facts: list[Fact] = []
    for el in root:
        fns, fname = _local(el.tag)
        if fns in _SKIP_NS or el.get("contextRef") is None:
            continue
        ctx = el.get("contextRef", "")
        if ctx not in contexts:
            raise XbrlError(f"fact {fname} refers to unknown context {ctx}")
        nil = el.get(f"{{{XSI}}}nil") == "true"
        facts.append(Fact(namespace=fns, name=fname, context=ctx, unit=el.get("unitRef"),
                          decimals=el.get("decimals"),
                          value=None if nil else (el.text or "").strip(), nil=nil))
    return Instance(facts=facts, contexts=contexts, units=units, schema_refs=schema_refs,
                    root_tag=root.tag, namespaces=tuple(namespaces))
