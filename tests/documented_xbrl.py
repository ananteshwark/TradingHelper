"""XBRL instance builders in the SEBI in-capmkt structure.

Not captured filings, but shaped like the real ones: a real NSE results instance
(2020 taxonomy, quarter to 2024-12-31) declared the SAME period on every column
context (OneD and FourD both 2024-10-01..2024-12-31, FourD holding the nine-month
figures) and carried DateOfStart/EndOfFinancialYear. The builders do the same, so
the parser is tested against that quirk rather than against a tidier format.
"""

from __future__ import annotations

import datetime as dt
import json
from xml.sax.saxutils import escape

NS = "http://www.sebi.gov.in/xbrl/{year}-03-31/in-capmkt"


def _quarter_start(end: dt.date) -> dt.date:
    m = end.month - 2
    return dt.date(end.year, m, 1)


def results_instance(
    symbol: str,
    period_end: dt.date,
    values: dict[str, float],
    *,
    basis: str = "Consolidated",
    year: int = 2022,
    comparatives: dict[str, float] | None = None,
    fy_values: dict[str, float] | None = None,
    balance_sheet: dict[str, float] | None = None,
    extra: str = "",
    units_override: dict[str, str] | None = None,
) -> bytes:
    """values: element local name -> current-quarter value (INR).
    comparatives: same-quarter-last-year values. fy_values: full-year values
    ending on period_end (Q4 filings). balance_sheet: instant values at period_end."""
    ns = NS.format(year=year)
    q_start = _quarter_start(period_end)
    fy_start = dt.date(period_end.year - (period_end.month < 4), 4, 1)
    fy_end = dt.date(fy_start.year + 1, 3, 31)
    same = (f"<xbrli:startDate>{q_start}</xbrli:startDate>"
            f"<xbrli:endDate>{period_end}</xbrli:endDate>")
    # As in real NSE instances, every duration column declares the current quarter.
    ctx = [
        ("OneD", same),
        ("ThreeD", same),     # same quarter last year (period not declared)
        ("FourD", same),      # year to date (period not declared)
        ("OneI", f"<xbrli:instant>{period_end}</xbrli:instant>"),
        ("SegD", same, "SegmentA"),
    ]
    contexts = []
    for c in ctx:
        seg = ""
        if len(c) == 3:
            seg = ('<xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:SegmentsAxis">'
                   f"in-capmkt:{c[2]}Member</xbrldi:explicitMember></xbrli:segment>")
        contexts.append(f'<xbrli:context id="{c[0]}"><xbrli:entity><xbrli:identifier '
                        f'scheme="http://www.nseindia.com">{symbol}</xbrli:identifier>{seg}'
                        f"</xbrli:entity><xbrli:period>{c[1]}</xbrli:period></xbrli:context>")
    units = ('<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
             '<xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>'
             '<xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>'
             "iso4217:INR</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator>"
             "<xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide>"
             "</xbrli:unit>")
    per_share = {"BasicEarningsLossPerShareFromContinuingOperations",
                 "DilutedEarningsLossPerShareFromContinuingOperations",
                 "FaceValueOfEquityShareCapital"}
    pct = {"PercentageOfGrossNpa", "PercentageOfNpa"}

    def unit_for(name: str) -> str:
        if units_override and name in units_override:
            return units_override[name]
        return "INRPerShare" if name in per_share else ("pure" if name in pct else "INR")

    facts = [
        f"<in-capmkt:Symbol contextRef=\"OneD\">{symbol}</in-capmkt:Symbol>",
        f"<in-capmkt:NameOfTheCompany contextRef=\"OneD\">{symbol} Limited"
        "</in-capmkt:NameOfTheCompany>",
        f"<in-capmkt:NatureOfReportStandaloneConsolidated contextRef=\"OneD\">{basis}"
        "</in-capmkt:NatureOfReportStandaloneConsolidated>",
        f"<in-capmkt:DateOfStartOfReportingPeriod contextRef=\"OneD\">{q_start}"
        "</in-capmkt:DateOfStartOfReportingPeriod>",
        f"<in-capmkt:DateOfEndOfReportingPeriod contextRef=\"OneD\">{period_end}"
        "</in-capmkt:DateOfEndOfReportingPeriod>",
        f"<in-capmkt:DateOfStartOfFinancialYear contextRef=\"OneD\">{fy_start}"
        "</in-capmkt:DateOfStartOfFinancialYear>",
        f"<in-capmkt:DateOfEndOfFinancialYear contextRef=\"OneD\">{fy_end}"
        "</in-capmkt:DateOfEndOfFinancialYear>",
    ]
    for group, context in ((values, "OneD"), (comparatives or {}, "ThreeD"),
                           (fy_values or {}, "FourD"), (balance_sheet or {}, "OneI")):
        for name, v in group.items():
            facts.append(f'<in-capmkt:{name} contextRef="{context}" unitRef="{unit_for(name)}" '
                         f'decimals="-5">{v}</in-capmkt:{name}>')
    # A segment-dimensioned fact that must not be loaded as a company total.
    if "RevenueFromOperations" in values:
        facts.append('<in-capmkt:RevenueFromOperations contextRef="SegD" unitRef="INR" '
                     'decimals="-5">1</in-capmkt:RevenueFromOperations>')
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        f'xmlns:in-capmkt="{ns}">'
        f'<link:schemaRef xlink:type="simple" xlink:href="{ns}/in-capmkt-ent-{year}-03-31.xsd"/>'
        + "".join(contexts) + units + "".join(facts) + extra + "</xbrli:xbrl>")
    return doc.encode()


def nonfin_q(revenue: float, other: float = 0.0, margin: float = 0.2, fin: float = 0.01,
             dep: float = 0.03, tax_rate: float = 0.25) -> dict[str, float]:
    """A self-consistent quarterly P&L (INR) from a revenue number."""
    op_costs = revenue * (1 - margin)
    finance = revenue * fin
    depn = revenue * dep
    expenses = op_costs + finance + depn
    pbt = revenue + other - expenses
    tax = pbt * tax_rate
    return {
        "RevenueFromOperations": round(revenue), "OtherIncome": round(other),
        "Income": round(revenue + other), "FinanceCosts": round(finance),
        "DepreciationDepletionAndAmortisationExpense": round(depn),
        "OtherExpenses": round(op_costs), "Expenses": round(expenses),
        "ProfitBeforeExceptionalItemsAndTax": round(pbt), "ProfitBeforeTax": round(pbt),
        "CurrentTax": round(tax), "DeferredTax": 0, "TaxExpense": round(tax),
        "ProfitLossForPeriodFromContinuingOperations": round(pbt - tax),
        "ProfitLossForPeriod": round(pbt - tax),
        "ProfitOrLossAttributableToOwnersOfParent": round(pbt - tax),
        "BasicEarningsLossPerShareFromContinuingOperations": round((pbt - tax) / 1e7, 2),
        "FaceValueOfEquityShareCapital": 10,
    }


def shp_instance(symbol: str, period_end: dt.date, cats: dict[str, dict[str, float]],
                 year: int = 2022) -> bytes:
    """cats: member local name -> {measure element: value}; member '' = no dimension (total)."""
    ns = NS.format(year=year)
    contexts, facts = [], []
    for i, (member, measures) in enumerate(cats.items()):
        cid = f"C{i}"
        seg = ""
        if member:
            seg = ('<xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:'
                   f'ShareholdingPatternAxis">in-capmkt:{member}</xbrldi:explicitMember>'
                   "</xbrli:segment>")
        contexts.append(f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier '
                        f'scheme="http://www.nseindia.com">{symbol}</xbrli:identifier>{seg}'
                        f"</xbrli:entity><xbrli:period><xbrli:instant>{period_end}"
                        "</xbrli:instant></xbrli:period></xbrli:context>")
        for name, v in measures.items():
            unit = "pure" if "Percentage" in name or "AsAPercentage" in name else "shares"
            facts.append(f'<in-capmkt:{name} contextRef="{cid}" unitRef="{unit}" '
                         f'decimals="INF">{v}</in-capmkt:{name}>')
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
           'xmlns:link="http://www.xbrl.org/2003/linkbase" '
           'xmlns:xlink="http://www.w3.org/1999/xlink" '
           'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
           f'xmlns:in-capmkt="{ns}">'
           f'<link:schemaRef xlink:type="simple" xlink:href="{ns}/in-capmkt-shp-{year}-03-31.xsd"/>'
           '<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
           '<xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>'
           + "".join(contexts) + "".join(facts) + "</xbrli:xbrl>")
    return doc.encode()


def shp_simple(symbol: str, period_end: dt.date, total: float, promoter_pct: float,
               pledged_pct: float, fii_pct: float, dii_pct: float) -> bytes:
    def m(pct: float, pledged: float | None = None, holders: int = 10) -> dict[str, float]:
        d = {"NumberOfShares": round(total * pct / 100),
             "ShareholdingAsAPercentageOfTotalNumberOfShares": pct,
             "NumberOfShareholders": holders}
        if pledged is not None:
            d["NumberOfSharesPledgedOrOtherwiseEncumbered"] = round(total * pct / 100 * pledged
                                                                    / 100)
            d["SharesPledgedOrOtherwiseEncumberedAsAPercentageOfTotalNumberOfShares"] = pledged
        return d
    return shp_instance(symbol, period_end, {
        "": {"NumberOfShares": total, "ShareholdingAsAPercentageOfTotalNumberOfShares": 100},
        "ShareholdingOfPromoterAndPromoterGroupMember": m(promoter_pct, pledged_pct, 5),
        "PublicShareholdingMember": m(100 - promoter_pct, None, 50000),
        "InstitutionsForeignMember": m(fii_pct, None, 120),
        "InstitutionsDomesticMember": m(dii_pct, None, 45),
    })


def listing(rows: list[dict]) -> bytes:
    """NSE results/SHP listing JSON rows."""
    return json.dumps(rows).encode()


def results_row(symbol: str, period_end: dt.date, filed: str, url: str,
                consolidated: str = "Consolidated", seq: str = "1") -> dict:
    return {"symbol": symbol, "companyName": f"{symbol} Limited",
            "toDate": period_end.strftime("%d-%b-%Y"), "broadCastDate": filed,
            "xbrl": url, "consolidated": consolidated, "seqNumber": seq,
            "audited": "Un-Audited"}


def shp_row(symbol: str, period_end: dt.date, filed: str, url: str, rid: str = "1") -> dict:
    return {"symbol": symbol, "name": f"{symbol} Limited",
            "date": period_end.strftime("%d-%b-%Y"), "broadcastDate": filed, "xbrl": url,
            "recordId": rid}


def escape_text(s: str) -> str:
    return escape(s)
