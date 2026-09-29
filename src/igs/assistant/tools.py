"""Read-only tools the assistant may call, pinned to one stored score run.

They read what `igs score` stored (through igs.service) and what was public at the run's
as-of date; none of them writes anything. Results are compact JSON.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg

from igs import service
from igs.score.explain import LABELS, check_label, fmt_value

MAX_RESULT_CHARS = 30_000
MAX_ROWS = 50


def _num(v: Any, digits: int = 4) -> Any:
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float):
        return None if v != v else round(v, digits)     # NaN -> null
    return v


def _default(o: Any) -> Any:
    if isinstance(o, dt.datetime | dt.date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def to_json(data: Any) -> str:
    text = json.dumps(data, default=_default, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '..."(truncated: ask for fewer rows)"'
    return text


def _nullable(kind: str, description: str) -> dict:
    """An optional argument under strict tool use: every property is required, so optional
    ones accept null."""
    return {"anyOf": [{"type": kind}, {"type": "null"}], "description": description}


def _tool(name: str, description: str, properties: dict) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False}}


SYMBOL = {"type": "string", "description": "NSE symbol, e.g. RELIANCE"}

TOOLS = [
    _tool("run_overview",
          "The score run being discussed: its as-of date, universe size, how many stocks are "
          "in each tier, run-health issues and any factors dropped by the backtest IC gate.",
          {}),
    _tool("find_stocks",
          "Stocks in the run ordered by rank, optionally filtered. Returns rank, symbol, name, "
          "tier and the reason for it, composite score, factor coverage, industry, market-cap "
          "bucket and whether the stock is on the user's watchlist.",
          {"tier": _nullable("string", "High conviction | Watchlist | Not shortlisted | "
                                       "Rejected"),
           "industry": _nullable("string", "exact industry name as stored in the run"),
           "sector": _nullable("string", "exact sector name as stored in the run"),
           "bucket": _nullable("string", "large | mid | small"),
           "name_contains": _nullable("string", "part of a symbol or company name"),
           "watchlist_only": _nullable("boolean", "only the user's watchlist"),
           "limit": _nullable("integer", f"rows to return, at most {MAX_ROWS} (default 20)")}),
    _tool("stock_detail",
          "One stock's result in the run: tier and reason, rank, composite, pillar scores, the "
          "factors that lift and hold back its score (with values, peer z-scores and peer "
          "group), checks tripped or not evaluable, robustness gates and what keeps it out of "
          "High conviction.",
          {"symbol": SYMBOL}),
    _tool("factor_table",
          "Every factor for one stock: value, status (ok, not applicable, insufficient data), "
          "z-score and percentile within its peer group, and contribution to the composite.",
          {"symbol": SYMBOL}),
    _tool("financials",
          "Up to eight quarters of revenue, EBITDA, profit, operating margin, other income and "
          "PBT for one stock, as filed by the run's date (rupees).",
          {"symbol": SYMBOL}),
    _tool("filings",
          "One stock's results and shareholding filings and its exchange announcements up to "
          "the run's date, newest first, with the assistant's earlier reading of each "
          "announcement where one exists.",
          {"symbol": SYMBOL,
           "limit": _nullable("integer", f"items of each kind, at most {MAX_ROWS} "
                                         "(default 15)")}),
    _tool("shareholding",
          "Up to eight quarters of shareholding by category (promoter, public, foreign and "
          "domestic institutions) and the promoter pledge, as filed by the run's date.",
          {"symbol": SYMBOL}),
]
TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


@dataclass
class Toolbox:
    conn: psycopg.Connection
    run: dict                                   # service.resolve_run(...)
    _details: dict[str, dict] = field(default_factory=dict)

    @property
    def run_id(self) -> int:
        return self.run["run_id"]

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """(JSON result, is_error). Errors go back to the model as tool errors."""
        if name not in TOOL_NAMES:
            return to_json({"error": f"unknown tool {name!r}"}), True
        try:
            return to_json(getattr(self, name)(**args)), False
        except service.NotFound as exc:
            return to_json({"error": str(exc)}), True
        except (TypeError, ValueError) as exc:
            return to_json({"error": f"bad arguments for {name}: {exc}"}), True

    # ------------------------------------------------------------------ helpers

    def _detail(self, symbol: str) -> dict:
        key = symbol.strip().upper()
        if key not in self._details:
            self._details[key] = service.stock_detail(self.conn, key, self.run_id)
        return self._details[key]

    def _run_ref(self) -> dict:
        return {"run_id": self.run_id, "as_of": self.run["as_of"]}

    @staticmethod
    def _factor(f: dict) -> dict:
        label = LABELS.get(f["factor"], (f["factor"],))[0]
        return {"factor": label, "pillar": f["pillar"], "status": f["status"],
                "value": fmt_value(f["factor"], _num(f["value"], 6)),
                "z": _num(f["z"], 2), "peer_percentile": _num(f["peer_percentile"], 2),
                "peer_group": f["peer_group"], "peer_count": f["peer_count"],
                "contribution": _num(f["contribution"], 3)}

    # ------------------------------------------------------------------ tools

    def run_overview(self) -> dict:
        with self.conn.cursor() as cur:
            cur.execute("""select tier, count(*) from score_result where run_id = %s
                           group by tier order by 2 desc""", (self.run_id,))
            tiers = {t or "unscored": n for t, n in cur.fetchall()}
            cur.execute("select dropped_factors from score_run where run_id = %s",
                        (self.run_id,))
            row = cur.fetchone()
        return {**self._run_ref(), "stocks_in_run": sum(tiers.values()), "tiers": tiers,
                "health_issues": self.run.get("health_issues") or [],
                "dropped_factors": (row[0] if row else None) or []}

    def find_stocks(self, tier=None, industry=None, sector=None, bucket=None,
                    name_contains=None, watchlist_only=None, limit=None) -> dict:
        limit = max(1, min(int(limit or 20), MAX_ROWS))
        _, rows = service.rankings(self.conn, self.run_id, tier=tier, sector=sector,
                                   industry=industry, bucket=bucket, q=name_contains,
                                   watchlist_only=bool(watchlist_only), limit=limit)
        keep = ("rank", "symbol", "name", "tier", "tier_reason", "industry", "bucket",
                "on_watchlist")
        return {**self._run_ref(), "count": len(rows),
                "stocks": [{**{k: r[k] for k in keep}, "composite": _num(r["composite"], 2),
                            "coverage": _num(r["coverage"], 2),
                            "mcap_cr": _num(r["mcap_cr"], 0)} for r in rows]}

    def stock_detail(self, symbol: str) -> dict:
        d = self._detail(symbol)
        c = d["company"]
        factors = [f for f in d["factors"] if f["contribution"] is not None]
        checks = d["red_flags"] + d["cautions"]
        return {
            **self._run_ref(),
            "stock": {"symbol": c["symbol"], "name": c["name"], "tier": c["tier"],
                      "tier_reason": c["tier_reason"], "rank": c["rank"],
                      "of_scored": c["scored"], "composite": _num(c["composite"], 2),
                      "coverage": _num(c["coverage"], 2), "industry": c["industry"],
                      "industry_source": c.get("industry_source"), "sector": c["sector"],
                      "bucket": c["bucket"], "mcap_cr": _num(c["mcap_cr"], 0)},
            "explanation": c["explanation"],
            "pillars": [{"pillar": p["pillar"], "score": _num(p["score"], 2),
                         "coverage": _num(p["coverage"], 2)} for p in d["pillars"]],
            "lifting": [self._factor(f) for f in factors[:5]],
            "holding_back": [self._factor(f) for f in factors[::-1][:5]
                             if (f["contribution"] or 0) < 0],
            "checks_tripped": [{"check": check_label(x["flag"]), "severity": x["severity"],
                                "message": x["message"]}
                               for x in checks if x["status"] == "tripped"],
            "checks_not_evaluable": [check_label(x["flag"]) for x in checks
                                     if x["status"] == "data_unavailable"],
            "robustness": {k: _num(v, 3) for k, v in d["robustness"].items()},
            "high_conviction_blockers": d["hc_blockers"],
        }

    def factor_table(self, symbol: str) -> dict:
        d = self._detail(symbol)
        return {**self._run_ref(), "symbol": d["company"]["symbol"],
                "factors": [self._factor(f) for f in d["factors"]]}

    def financials(self, symbol: str) -> dict:
        d = self._detail(symbol)
        return {**self._run_ref(), "symbol": d["company"]["symbol"], "unit": "INR",
                "quarters": [{k: _num(v, 4) for k, v in q.items()}
                             for q in d["financials_8q"]]}

    def shareholding(self, symbol: str) -> dict:
        d = self._detail(symbol)
        return {**self._run_ref(), "symbol": d["company"]["symbol"], "unit": "percent",
                "quarters": [{k: _num(v, 2) for k, v in q.items()} for q in d["shareholding"]]}

    def filings(self, symbol: str, limit=None) -> dict:
        d = self._detail(symbol)
        limit = max(1, min(int(limit or 15), MAX_ROWS))
        cid, as_of = d["company"]["company_id"], self.run["as_of"]
        with self.conn.cursor() as cur:
            cur.execute("""select filing_type, statement_basis, period_end, filed_at, source_url
                           from filing where company_id = %s and filed_at <= %s
                           order by filed_at desc limit %s""", (cid, as_of, limit))
            filings = [dict(zip(("type", "basis", "period_end", "filed_at", "url"), r,
                                strict=True)) for r in cur.fetchall()]
            cur.execute(
                """select a.filed_at, a.category, a.subject, a.attachment_url,
                          n.category, n.materiality, n.summary, n.concerns
                   from announcement a
                   join security_identifier si on si.id_type = 'NSE_SYMBOL'
                    and si.id_value = a.symbol and a.filed_at::date >= si.valid_from
                    and (si.valid_to is null or a.filed_at::date < si.valid_to)
                   join security s on s.security_id = si.security_id
                   left join announcement_note n on n.exchange = a.exchange
                    and n.symbol = a.symbol and n.filed_at = a.filed_at
                    and n.subject = a.subject
                   where s.company_id = %s and a.filed_at <= %s
                   order by a.filed_at desc limit %s""", (cid, as_of, limit))
            anns = []
            for filed, cat, subject, url, ncat, mat, summary, concerns in cur.fetchall():
                item = {"filed_at": filed, "category": cat, "subject": subject[:500],
                        "url": url}
                if summary:
                    item["assistant_reading"] = {"category": ncat, "materiality": mat,
                                                 "summary": summary, "concerns": concerns}
                anns.append(item)
        return {**self._run_ref(), "symbol": d["company"]["symbol"], "filings": filings,
                "announcements": anns}
