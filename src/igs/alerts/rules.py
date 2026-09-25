"""Alert rules. Each returns Alert records; persistence deduplicates them.

Rules compare the latest score run with the previous one, and look at filings
published since the previous run. Wording states facts from the data only.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import psycopg

from igs.config import AlertsConfig
from igs.guardrails import assert_no_advice_language
from igs.score.explain import check_label


@dataclass(frozen=True)
class Alert:
    kind: str
    company_id: int | None
    message: str
    dedupe_key: str


def _rows(conn: psycopg.Connection, sql: str, params: tuple) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _mk(kind: str, company_id: int | None, message: str, key: str) -> Alert:
    return Alert(kind, company_id, assert_no_advice_language(message), f"{kind}:{key}")


def top_decile_entrants(conn, run_id: int, prev_run_id: int | None, cfg: dict) -> list[Alert]:
    pct = cfg.get("top_pct", 10) / 100
    cur = _rows(conn, """select company_id, symbol, rank, scored, composite from score_result
                         where run_id = %s and rank is not null
                           and rank::float8 / scored <= %s""", (run_id, pct))
    prev = set()
    if prev_run_id is not None:
        prev = {r["company_id"] for r in _rows(
            conn, """select company_id from score_result where run_id = %s
                     and rank is not null and rank::float8 / scored <= %s""",
            (prev_run_id, pct))}
    return [_mk("top_decile_entry", r["company_id"],
                f"{r['symbol']} entered the top {pct:.0%} of the screen: rank {r['rank']} of "
                f"{r['scored']}, composite {r['composite']:+.2f}.",
                f"{r['company_id']}:{run_id}")
            for r in cur if r["company_id"] not in prev]


def watchlist_red_flags(conn, run_id: int, prev_run_id: int | None, cfg: dict) -> list[Alert]:
    cur = _rows(conn, """select f.company_id, r.symbol, f.flag, f.message, f.severity
                         from red_flag_result f join watchlist w using (company_id)
                         join score_result r on r.run_id = f.run_id
                          and r.company_id = f.company_id
                         where f.run_id = %s and f.status = 'tripped'""", (run_id,))
    prev = set()
    if prev_run_id is not None:
        prev = {(r["company_id"], r["flag"]) for r in _rows(
            conn, """select company_id, flag from red_flag_result
                     where run_id = %s and status = 'tripped'""", (prev_run_id,))}
    kind = {"reject": "red flag", "caution": "caution"}
    return [_mk("watchlist_red_flag", r["company_id"],
                f"Watchlist: {r['symbol']} {kind.get(r['severity'], 'red flag')} "
                f"'{check_label(r['flag'])}': {r['message']}",
                f"{r['company_id']}:{r['flag']}:{run_id}")
            for r in cur if (r["company_id"], r["flag"]) not in prev]


def run_health(conn, run_id: int, cfg: dict) -> list[Alert]:
    rows = _rows(conn, "select coalesce(health->'issues', '[]') as issues from score_run "
                       "where run_id = %s", (run_id,))
    issues = rows[0]["issues"] if rows else []
    if not issues:
        return []
    return [_mk("run_health", None, f"Run {run_id}: High conviction withheld - "
                + "; ".join(issues), str(run_id))]


def high_conviction_changes(conn, run_id: int, prev_run_id: int | None,
                            cfg: dict) -> list[Alert]:
    if prev_run_id is None:
        return []
    rows = _rows(conn, """select coalesce(a.company_id, b.company_id) as company_id,
                                 coalesce(a.symbol, b.symbol) as symbol, a.tier as now,
                                 b.tier as before, a.tier_reason
                          from (select * from score_result where run_id = %s) a
                          full join (select * from score_result where run_id = %s) b
                            using (company_id)
                          where coalesce(a.tier = 'High conviction', false)
                                <> coalesce(b.tier = 'High conviction', false)""",
                 (run_id, prev_run_id))
    out = []
    for r in rows:
        if r["now"] == "High conviction":
            msg = f"{r['symbol']} is now High conviction (was {r['before'] or 'not ranked'})."
        else:
            msg = (f"{r['symbol']} is no longer High conviction: now {r['now'] or 'not ranked'}"
                   + (f" ({r['tier_reason']})" if r["tier_reason"] else "") + ".")
        out.append(_mk("high_conviction_change", r["company_id"], msg,
                       f"{r['company_id']}:{run_id}"))
    return out


def watchlist_results_filed(conn, since: dt.datetime, until: dt.datetime,
                            cfg: dict) -> list[Alert]:
    rows = _rows(conn, """
        select f.filing_id, f.company_id, c.name, f.period_end, f.statement_basis, f.filed_at,
               f.source_url
        from filing f join watchlist w using (company_id) join company c using (company_id)
        where f.filing_type = 'financial_results' and f.filed_at > %s and f.filed_at <= %s
        order by f.filed_at""", (since, until))
    return [_mk("watchlist_results", r["company_id"],
                f"Watchlist: {r['name']} filed {r['statement_basis'] or ''} results for the "
                f"period ending {r['period_end']} at {r['filed_at']:%Y-%m-%d %H:%M} UTC"
                + (f" ({r['source_url']})" if r["source_url"] else "") + ".",
                str(r["filing_id"]))
            for r in rows]


def pledge_changes(conn, since: dt.datetime, until: dt.datetime, cfg: dict) -> list[Alert]:
    scope = "join watchlist w using (company_id)" if cfg.get("scope", "watchlist") == \
        "watchlist" else ""
    rows = _rows(conn, f"""
        with p as (
            select s.company_id, s.period_end, s.filed_at, s.filing_id,
                   coalesce(s.pledged_pct, 0)::float8 as pledged,
                   row_number() over (partition by s.company_id order by s.period_end desc,
                                      s.filed_at desc) as rn
            from shareholding s {scope}
            where s.category = 'promoter' and s.filed_at <= %s)
        select a.company_id, c.name, a.period_end, a.pledged as now, b.pledged as prev,
               a.filed_at, a.filing_id
        from p a join p b on a.company_id = b.company_id and b.rn = 2
        join company c on c.company_id = a.company_id
        where a.rn = 1 and a.filed_at > %s""", (until, since))
    limit = cfg.get("min_change_pp", 2.0)
    return [_mk("pledge_change", r["company_id"],
                f"{r['name']}: promoter pledge {r['prev']:.1f}% -> {r['now']:.1f}% of promoter "
                f"holding (shareholding for {r['period_end']}).", str(r["filing_id"]))
            for r in rows if abs(r["now"] - r["prev"]) >= limit]


def watchlist_announcement_notes(conn, since: dt.datetime, until: dt.datetime,
                                 cfg: dict) -> list[Alert]:
    """Announcements on watchlist names that the optional assistant read as high materiality
    or as raising a governance concern. The reading is labelled as such; it is never used
    in scoring."""
    rows = _rows(conn, """
        select s.company_id, c.name, n.symbol, n.filed_at, n.subject, n.materiality,
               n.summary, n.concerns
        from announcement_note n
        join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = n.symbol
         and n.filed_at::date >= si.valid_from
         and (si.valid_to is null or n.filed_at::date < si.valid_to)
        join security s on s.security_id = si.security_id
        join watchlist w on w.company_id = s.company_id
        join company c on c.company_id = s.company_id
        where n.created_at > %s and n.created_at <= %s
          and (n.materiality = any(%s) or cardinality(n.concerns) > 0)
        order by n.filed_at""", (since, until, list(cfg.get("materiality", ["high"]))))
    out = []
    for r in rows:
        concerns = ", ".join(c.replace("_", " ") for c in r["concerns"])
        out.append(_mk("announcement_note", r["company_id"],
                       f"Watchlist: {r['name']} ({r['symbol']}) announcement of "
                       f"{r['filed_at']:%Y-%m-%d}, read by the assistant as {r['materiality']} "
                       f"materiality" + (f", concerns: {concerns}" if concerns else "")
                       + f": {r['summary']} (AI reading; not used in ranking.)",
                       f"{r['symbol']}:{r['filed_at'].isoformat()}:{r['subject'][:80]}"))
    return out


INSIDER_VERB = {"buy": "acquired", "sell": "disposed of"}


def watchlist_insider_trades(conn, since: dt.datetime, until: dt.datetime,
                             cfg: dict) -> list[Alert]:
    """Open-market trades in equity by promoters, directors and key managers of watchlist
    companies, from insider-trading disclosures loaded in the window."""
    sides = [s for s in ("buy", "sell") if cfg.get(f"include_{s}s", s == "buy")]
    rows = _rows(conn, """
        select s.company_id, c.name, t.symbol, t.person_name, t.person_category, t.side,
               t.quantity::float8 as quantity, t.value_inr::float8 as value_inr, t.filed_at,
               t.trade_from
        from insider_trade t
        join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = t.symbol
         and t.filed_at::date >= si.valid_from
         and (si.valid_to is null or t.filed_at::date < si.valid_to)
        join security s on s.security_id = si.security_id
        join watchlist w on w.company_id = s.company_id
        join company c on c.company_id = s.company_id
        where t.ingested_at > %s and t.ingested_at <= %s and t.open_market
          and t.insider_role in ('promoter', 'director_kmp') and t.side = any(%s)
          and lower(coalesce(t.security_type, '')) like 'equity%%'
        order by t.filed_at""", (since, until, sides))
    out = []
    for r in rows:
        value = "" if r["value_inr"] is None else f" (Rs {r['value_inr'] / 1e7:,.2f} cr)"
        out.append(_mk("insider_trade", r["company_id"],
                       f"Watchlist: {r['name']} ({r['symbol']}): {r['person_name']} "
                       f"({r['person_category']}) {INSIDER_VERB[r['side']]} "
                       f"{r['quantity']:,.0f} shares{value} in the open market on "
                       f"{r['trade_from']:%Y-%m-%d}, disclosed {r['filed_at']:%Y-%m-%d}.",
                       f"{r['symbol']}:{r['person_name']}:{r['filed_at'].isoformat()}:"
                       f"{r['side']}:{r['quantity']:.0f}"))
    return out


def evaluate(conn, cfg: AlertsConfig, run_id: int, prev_run_id: int | None,
             since: dt.datetime, until: dt.datetime) -> list[Alert]:
    out: list[Alert] = []
    r = cfg.rules
    if r.get("top_decile_entrants", {}).get("enabled"):
        out += top_decile_entrants(conn, run_id, prev_run_id, r["top_decile_entrants"])
    if r.get("run_health", {}).get("enabled"):
        out += run_health(conn, run_id, r["run_health"])
    if r.get("high_conviction_changes", {}).get("enabled"):
        out += high_conviction_changes(conn, run_id, prev_run_id, r["high_conviction_changes"])
    if r.get("watchlist_red_flags", {}).get("enabled"):
        out += watchlist_red_flags(conn, run_id, prev_run_id, r["watchlist_red_flags"])
    if r.get("watchlist_results_filed", {}).get("enabled"):
        lookback = dt.timedelta(days=r["watchlist_results_filed"].get("lookback_days", 3))
        out += watchlist_results_filed(conn, min(since, until - lookback), until,
                                       r["watchlist_results_filed"])
    if r.get("pledge_changes", {}).get("enabled"):
        out += pledge_changes(conn, since, until, r["pledge_changes"])
    if r.get("watchlist_announcement_notes", {}).get("enabled"):
        out += watchlist_announcement_notes(conn, since, until,
                                            r["watchlist_announcement_notes"])
    if r.get("watchlist_insider_trades", {}).get("enabled"):
        out += watchlist_insider_trades(conn, since, until, r["watchlist_insider_trades"])
    return out


def record_new(conn, alerts: list[Alert], run_id: int,
               channels: tuple[str, ...] = ()) -> list[Alert]:
    """Insert into alert_log; return only alerts not seen before (dedupe_key)."""
    fresh = []
    with conn.cursor() as cur:
        for a in alerts:
            cur.execute("""insert into alert_log (kind, company_id, run_id, message, dedupe_key)
                           values (%s, %s, %s, %s, %s) on conflict (dedupe_key) do nothing
                           returning alert_id""",
                        (a.kind, a.company_id, run_id, a.message, a.dedupe_key))
            if cur.rowcount:
                alert_id = cur.fetchone()[0]
                fresh.append(a)
                for channel in channels:
                    cur.execute("insert into alert_outbox (alert_id, channel) values (%s, %s)",
                                (alert_id, channel))
    conn.commit()
    return fresh
