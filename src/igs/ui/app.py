"""Streamlit UI. Run with `igs ui` (or `streamlit run src/igs/ui/app.py`).

Reads only persisted score runs and point-in-time tables through
igs.service; it computes nothing itself, so what it shows is what was
scored, for a named run and as-of date.
"""

from __future__ import annotations

import os

import polars as pl
import streamlit as st

from igs import service
from igs.db import connect
from igs.guardrails import DISCLAIMER
from igs.score.explain import LABELS, fmt_value
from igs.timeutil import IST
from igs.ui import charts

PAGES = ["Rankings", "Stock", "AI calls", "News", "Ask", "Watchlist", "Saved screens",
         "Data quality", "Settings"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LOCAL_ADDRESSES = ("127.0.0.1", "localhost", "::1")
AI_NOTE = ("Written by the optional research assistant (Claude) from this run's stored data. "
           "It is not used in ranking and doesn't make recommendations; check the filings.")
FLAG_ICON = {"tripped": "⛔ tripped", "clear": "✅ clear", "data_unavailable": "❔ unavailable",
             "not_applicable": "➖ not applicable"}
CALL_NOTE = ("The AI's own judgement from everything the app holds on the stock at the run's "
             "date: buy (open or add now), hold (keep it if you own it, don't add) or sell "
             "(exit if you own it). It is not part of the ranking, and it is unproven until "
             "its record on the AI calls page says otherwise. The decision and its risk are "
             "yours.")
ACTION_LABEL = {"buy": "🟢 BUY", "hold": "🟡 HOLD", "sell": "🔴 SELL"}


def theme() -> str:
    try:
        kind = st.context.theme.type
    except AttributeError:
        kind = None
    return kind if kind in ("light", "dark") else "light"


@st.cache_resource
def _conn():
    return connect(autocommit=True)


def conn():
    c = _conn()
    if c.closed:
        st.cache_resource.clear()
        c = _conn()
    return c


def banner() -> None:
    st.warning(DISCLAIMER, icon="⚠️")


RESTART = ("The app was updated while it was running, so parts of it are still the old "
           "version and pages can fail (for example \"Extra inputs are not permitted\" in "
           "Settings). Stop it with Ctrl+C where you started it and run `uv run igs ui` "
           "again; that also updates the database.")


def update_banner() -> None:
    """Say how to fix the two states that make pages fail in confusing ways: code updated on
    disk under the running app, and a database older than the code."""
    try:
        from igs.db import pending_migrations
        from igs.ui import LOADED_AT, code_stamp
    except ImportError:              # this page is newer than the modules the app loaded
        st.error(RESTART, icon="🔄")
        return
    if code_stamp() > LOADED_AT:
        st.error(RESTART, icon="🔄")
        return
    try:
        pending = pending_migrations(conn())
    except Exception:  # noqa: BLE001 - an unreachable database is explained where it's used
        return
    if pending:
        st.error(f"The database is older than the app ({', '.join(pending)} not applied). "
                 "Run `uv run igs db migrate`, or restart the app with `uv run igs ui`, "
                 "which applies them.", icon="🗄️")


def health_banner(run: dict) -> None:
    issues = run.get("health_issues") or []
    if issues:
        st.error("Run health: High conviction is withheld for this run until these are "
                 "resolved - " + "; ".join(issues), icon="⛔")
    if "ic_status_generated_at" in run and not run["ic_status_generated_at"]:
        st.warning("Not yet validated: no backtest has measured these factors on real data. "
                   "The weights are starting assumptions taken from published Indian "
                   "evidence, so read the tiers as hypotheses to check, not findings.",
                   icon="🧪")


def readiness_panel() -> None:
    """What a score run needs, how much of it is loaded, and the command for the next step.
    Each line carries a word as well as a mark, so it does not rely on colour."""
    from igs.config import load_universe
    from igs.pit.gate import GateError, require_gate
    r = service.readiness(conn(), load_universe().min_filing_quarters)
    need, p = r["min_quarters"], r["prices"]
    try:
        gate = (True, f"passed on {require_gate().passed_at[:10]} for this version of the "
                      "code.")
    except GateError:
        gate = (False, "not passed for this version of the code (it has to be re-run after "
                       "every update). Run `uv run igs gate run`; it takes about a minute.")
    prices = ((True, f"{p['days']:,} trading days for {p['symbols']:,} symbols, latest "
                     f"{p['latest']:%d %b %Y}.") if p["days"] else
              (False, "none loaded. The NSE check in the sidebar loads recent days; "
                      "docs/DEPLOY.md 3.2 shows how to load a year or two of history."))
    listed = r["listed"].get("financial_results", 0)
    pending = r["pending"].get("financial_results", 0)
    fetch = (f" {pending:,} listed results documents are not loaded yet: `uv run igs ingest "
             "documents financial_results --limit 3000`, repeated until it fetches nothing "
             "new." if pending else "")
    if r["results_enough"]:
        results = (True, f"{r['results_enough']:,} companies have {need} or more quarters "
                         f"loaded ({r['results_some']:,} have at least one).{fetch}")
    elif r["results_some"]:
        results = (False, f"{r['results_some']:,} companies have results loaded, but the most "
                          f"any has is {r['results_most']} of the {need} quarters a ranking "
                          f"needs.{fetch} You can change the minimum in Settings → Rating "
                          "history, then create a new scoring run.")
    elif listed:
        results = (False, f"{listed:,} results filings listed, none loaded yet.{fetch}")
    else:
        results = (False, "no results filings listed. NSE's results listings come from "
                          "www.nseindia.com; if the sidebar shows them failing, NSE is "
                          f"refusing this connection. A company needs {need} quarters of "
                          "results to be ranked, so until they load a run has 0 companies.")
    holding = ((True, f"{r['shareholding']:,} companies.") if r["shareholding"] else
               (False, "none loaded. Market cap uses the share count from these filings, "
                       "so no company can be ranked without them: `uv run igs ingest static "
                       "nse_shareholding_index`, then `uv run igs ingest documents "
                       "shareholding`."))
    lines = [("Look-ahead gate", gate), ("Prices", prices), ("Quarterly results", results),
             ("Shareholding filings", holding)]
    st.markdown("\n".join(f"- {'✅ Ready' if ok else '❌ Missing'} - **{name}**: {text}"
                          for name, (ok, text) in lines))
    st.markdown("Then run `uv run igs score` and reload this page. A backtest "
                "(`igs backtest`) is optional for a first run: without one, the run is "
                "labelled *not yet validated*.")


def pick_run() -> dict | None:
    runs = service.runs(conn())
    if not runs:
        st.subheader("No score run yet")
        st.write("A score run is saved by `uv run igs score` (and by the daily job). Before "
                 "it can rank anything, it needs:")
        readiness_panel()
        document_status_panel()
        return None
    labels = {f"Run {r['run_id']} - as of {r['as_of']:%Y-%m-%d}": r for r in runs}
    choice = st.sidebar.selectbox("Score run", list(labels), key="run_label")
    return labels[choice]


def display(df: pl.DataFrame) -> pl.DataFrame:
    """Dates as plain dates, numbers rounded: tables are read, not computed on."""
    return df.with_columns(
        [pl.col(c).cast(pl.Date).cast(pl.Utf8) for c, t in df.schema.items()
         if t in (pl.Date, pl.Datetime)]
        + [pl.col(c).round(2) for c, t in df.schema.items() if t in (pl.Float32, pl.Float64)])


def open_stock(symbol: str) -> None:
    """Button callback: runs before the rerun, so widget state may still be set."""
    st.session_state["page"] = "Stock"
    st.session_state["stock_sym"] = symbol


def company_picker(label: str, key: str, run: dict) -> str | None:
    """A company by typing any part of its name or its symbol; returns the symbol."""
    names = {c["symbol"]: c["name"] for c in service.companies(conn(), run["run_id"])}
    picked = st.session_state.get(key)
    if picked and picked not in names:      # opened from a table under an older symbol
        names = {picked: picked, **names}
    return st.selectbox(
        label, list(names), index=None, key=key, placeholder="Type a company name or symbol",
        format_func=lambda s: s if names[s] == s else f"{names[s]} ({s})")


# --------------------------------------------------------------------------- pages


def page_rankings(run: dict) -> None:
    st.header("Ranked candidates")
    st.caption(f"Run {run['run_id']}, signals as of {run['as_of']:%Y-%m-%d %H:%M} UTC. Tiers "
               "summarise the screen; they are not recommendations.")
    health_banner(run)
    facets = service.facets(conn(), run["run_id"])
    c = st.columns([1, 1, 1, 1, 1.4, 0.8])
    filters = {
        "tier": c[0].selectbox("Tier", ["All", *facets["tier"]], key="f_tier"),
        "sector": c[1].selectbox("Sector", ["All", *facets["sector"]], key="f_sector"),
        "industry": c[2].selectbox("Industry", ["All", *facets["industry"]], key="f_industry"),
        "bucket": c[3].selectbox("Size", ["All", *facets["bucket"]], key="f_bucket"),
        "q": c[4].text_input("Search symbol or name", key="f_q"),
        "watchlist_only": c[5].checkbox("Watchlist only", key="f_watch"),
    }
    active = {k: v for k, v in filters.items() if v not in ("All", "", False, None)}
    _, rows = service.rankings(conn(), run["run_id"], **active)
    st.write(f"{len(rows)} companies")
    if not rows and not active:
        u = run.get("universe")
        why = "; ".join(f"{reason}: {n:,}" for reason, n in u["excluded"].items()) \
            if u and u["excluded"] else ""
        st.warning("No company made the universe in this run"
                   + (f". Of {u['seen']:,} companies with prices, left out: {why}." if why
                      else ".") + " What is loaded now:", icon="🔎")
        readiness_panel()
    if rows:
        calls = _latest_calls()
        df = pl.DataFrame(rows).with_columns(pl.col("company_id").map_elements(
            lambda cid: _call_label(calls.get(cid)), return_dtype=pl.Utf8).alias("ai_call")
        ).select(
            pl.col("rank").cast(pl.Utf8).fill_null("-"), "symbol", "name", "tier",
            pl.col("tier_reason").fill_null(""), "ai_call", "composite", "coverage",
            "industry", "bucket", "mcap_cr", "on_watchlist")
        st.caption("Tick a row (the box at its left) to see the stock's details or add it to "
                   "the watchlist; tick several to add or remove them together.")
        event = st.dataframe(
            df, hide_index=True, width="stretch", key="rank_table", on_select="rerun",
            selection_mode="multi-row", column_config={
                "rank": "Rank",
                "ai_call": st.column_config.TextColumn(
                    "AI call", help="The AI's latest buy / hold / sell call and its date: its "
                                    "own judgement, not the screen's (AI calls page)."),
                "composite": st.column_config.NumberColumn("Composite", format="%+.2f"),
                "coverage": st.column_config.ProgressColumn("Coverage", min_value=0,
                                                            max_value=1, format="percent"),
                "mcap_cr": st.column_config.NumberColumn("Mkt cap (Rs cr)", format="%,.0f"),
                "tier_reason": "Reason", "on_watchlist": "Watchlist"})
        flash = st.session_state.pop("rank_flash", None)
        if flash:
            st.success(flash)
        # A selection made before the filters changed can point past the end of the table.
        picked = [rows[i] for i in event.selection.rows if i < len(rows)]
        if picked:
            _rank_actions(picked, calls)
        st.download_button("Export CSV", service.rankings_csv(rows), "rankings.csv",
                           "text/csv", key="dl_rankings")
    with st.expander("Save these filters as a screen"):
        name = st.text_input("Screen name", key="screen_name")
        if st.button("Save screen", key="save_screen") and name:
            service.screen_save(conn(), name, active)
            st.success(f"Saved screen '{name}'")


def _watch(symbols: list[str], add: bool) -> None:
    """Button callback: add or remove symbols, then say so after the rerun."""
    for s in symbols:
        (service.watchlist_add if add else service.watchlist_remove)(conn(), s)
    st.session_state["rank_flash"] = (f"{'Added' if add else 'Removed'} "
                                      f"{', '.join(symbols)} "
                                      f"{'to' if add else 'from'} the watchlist.")


def _rank_actions(picked: list[dict], calls: dict[int, dict]) -> None:
    """What can be done with the rows ticked in the rankings table."""
    add = [r["symbol"] for r in picked if not r["on_watchlist"]]
    remove = [r["symbol"] for r in picked if r["on_watchlist"]]
    with st.container(border=True):
        if len(picked) == 1:
            r = picked[0]
            composite = "" if r["composite"] is None else f" · composite {r['composite']:+.2f}"
            st.markdown(f"**{_md(r['name'])} ({r['symbol']})** · rank {r['rank'] or '-'} · "
                        f"{r['tier']}{composite} · {r['industry'] or 'industry n/a'}")
            if r["tier_reason"]:
                st.caption(r["tier_reason"])
            call = calls.get(r["company_id"])
            if call:
                st.caption(f"AI call: {_call_label(call)}, confidence "
                           f"{call['confidence']:.0%}")
            b = st.columns([1, 1, 3])
            b[0].button("Open full details", key="rank_open", type="primary",
                        on_click=open_stock, args=(r["symbol"],))
            if add:
                b[1].button("Add to watchlist", key="rank_watch_add", on_click=_watch,
                            args=(add, True))
            else:
                b[1].button("Remove from watchlist", key="rank_watch_remove",
                            on_click=_watch, args=(remove, False))
            return
        st.markdown(f"**{len(picked)} selected:** "
                    + ", ".join(r["symbol"] for r in picked))
        b = st.columns([1, 1, 3])
        b[0].button(f"Add {len(add)} to watchlist", key="rank_watch_add", disabled=not add,
                    on_click=_watch, args=(add, True))
        b[1].button(f"Remove {len(remove)} from watchlist", key="rank_watch_remove",
                    disabled=not remove, on_click=_watch, args=(remove, False))
        st.caption("Tick a single row to see its details.")


def _flags_table(flags: list[dict]) -> None:
    if not flags:
        st.write("None configured.")
        return
    df = pl.DataFrame([{"Check": f.get("label") or f["flag"].replace("_", " "),
                        "Status": FLAG_ICON.get(f["status"], f["status"]),
                        "Evidence": f["message"],
                        "Source": ", ".join(f["source_urls"] or [])} for f in flags])
    st.dataframe(df, hide_index=True, width="stretch")


def _robustness(d: dict) -> None:
    rb = d["robustness"]
    if rb.get("weight_stability") is None:
        st.write("Not evaluated (the stock is rejected or has no composite score).")
        return
    m = st.columns(4)
    m[0].metric("Weight stability", f"{rb['weight_stability']:.0%}",
                help="Share of pillar-weight variations keeping it in the High conviction band")
    m[1].metric("Persistence", f"{rb.get('persist_hits') or 0} of {rb.get('persist_dates') or 0}",
                help="Previous month-ends at which it ranked near the top")
    m[2].metric("Pillars above zero",
                f"{rb.get('positive_pillars') or 0} of {rb.get('scored_pillars') or 0}")
    share = rb.get("top_factor_share")
    m[3].metric("Largest factor share", "n/a" if share is None else f"{share:.0%}",
                help=rb.get("top_factor") or None)


def page_stock(run: dict) -> None:
    symbol = company_picker("Company", "stock_sym", run)
    if not symbol:
        st.info("Type part of a company's name or its NSE symbol above, or open a stock from "
                "the rankings.")
        return
    try:
        d = service.stock_detail(conn(), symbol, run["run_id"])
    except service.NotFound as exc:
        st.error(str(exc))
        return
    co, th = d["company"], theme()
    health_banner(run)
    st.header(f"{co['name']} ({co['symbol']})")
    industry = co["industry"] or "industry n/a"
    if co.get("industry_source") == "announcement_label":
        industry += " (NSE announcement label; peers share that label)"
    st.caption(f"{industry} - {co['bucket'] or ''} cap - "
               f"run {run['run_id']} as of {run['as_of']:%Y-%m-%d}")
    m = st.columns(4)
    m[0].metric("Tier", co["tier"])
    m[1].metric("Composite", "n/a" if co["composite"] is None else f"{co['composite']:+.2f}")
    m[2].metric("Rank", "-" if co["rank"] is None else f"{co['rank']} / {co['scored']}")
    m[3].metric("Coverage", "n/a" if co["coverage"] is None else f"{co['coverage']:.0%}")
    if co.get("tier_reason"):
        st.write(f"**Reason:** {co['tier_reason']}")
    watched = any(w["company_id"] == co["company_id"] for w in service.watchlist(conn()))
    if st.button("Remove from watchlist" if watched else "Add to watchlist", key="watch_btn"):
        (service.watchlist_remove if watched else service.watchlist_add)(conn(), symbol)
        st.rerun()

    if d["hc_blockers"]:
        st.subheader("Why not High conviction")
        st.write("\n".join(f"- {b}" for b in d["hc_blockers"]))

    st.subheader("Why this stock")
    st.text(co["explanation"])
    with st.expander("Geopolitical news impact"):
        base = co.get("base_composite")
        delta = co.get("geopolitical_adjustment", 0)
        if base is not None:
            st.text(f"Base score {base:+.3f} · News adjustment {delta:+.3f}")
        st.caption("Experimental AI assessment of potential price pressure; not a return "
                   "forecast. Adjustments decay with age and cannot bypass rating checks.")
        evidence = co.get("geopolitical_evidence") or []
        if not evidence:
            st.text("No qualifying news assessment in this run. The base score is unchanged.")
        for item in evidence:
            st.text(item["title"])
            st.link_button("News source", item["url"], key=f"news_{item['assessment_id']}")
            st.text(f"Impact {item['impact']:+.2f} · Confidence {item['confidence']:.0%} "
                    f"· Channel: {item['channel']}")
            st.text(item["rationale"])
            st.text(f"Article evidence: {item['evidence']}")
            st.text(f"Company context: {item['exposure']}")
            st.link_button("Exposure source", item["exposure_url"],
                           key=f"exposure_{item['assessment_id']}")
            st.text(f"Published {item['published_at']} · Assessed {item['assessed_at']} "
                    f"· Model {item['model']}")
    _brief_panel(co["symbol"], run)
    _call_panel(co["symbol"], run)

    st.subheader("Robustness of the rank")
    _robustness(d)

    st.subheader("Red flags (a tripped flag rejects the stock)")
    _flags_table(d["red_flags"])
    st.subheader("Cautions (a tripped caution keeps it out of High conviction)")
    _flags_table(d["cautions"])

    st.subheader("Top contributing factors")
    top = [{"Factor": LABELS.get(f["factor"], (f["factor"],))[0],
            "Value": fmt_value(f["factor"], f["value"]),
            "Peer percentile": None if f["peer_percentile"] is None
            else round(100 * f["peer_percentile"]),
            "Peers": f"{f['peer_count']} {f['peer_group']} ({f['peer_level']})",
            "Contribution": round(f["contribution"], 3),
            "Source filing": (f["sources"][0]["source_url"] or "" if f["sources"] else "")}
           for f in d["top_contributions"]]
    st.dataframe(pl.DataFrame(top), hide_index=True, width="stretch",
                 column_config={"Source filing": st.column_config.LinkColumn("Source filing")})

    st.subheader("Factor breakdown")
    scored = [f for f in d["factors"] if f["contribution"] is not None]
    if scored:
        st.altair_chart(charts.contribution_chart(scored, th), width="stretch")
    with st.expander("All factors (table view)", expanded=not scored):
        st.caption("A factor with a z-score but no contribution is tracked at weight 0 "
                   "(no Indian evidence yet that it predicts returns); the backtest still "
                   "measures it.")
        st.dataframe(pl.DataFrame([{
            "factor": f["factor"], "pillar": f["pillar"], "status": f["status"],
            "value": fmt_value(f["factor"], f["value"]), "z": f["z"],
            "peer percentile": f["peer_percentile"], "contribution": f["contribution"]}
            for f in d["factors"]]), hide_index=True, width="stretch")

    st.subheader("Last eight quarters")
    if d["financials_8q"]:
        c1, c2 = st.columns(2)
        c1.altair_chart(charts.financials_chart(d["financials_8q"], th), width="stretch")
        if charts.has_margin(d["financials_8q"]):
            c2.altair_chart(charts.margin_chart(d["financials_8q"], th), width="stretch")
        else:
            c2.info("Operating (EBITDA) margin is not reported for this results format "
                    "(banks and other financials).")
        with st.expander("Table view"):
            st.dataframe(display(pl.DataFrame(d["financials_8q"])), hide_index=True)
    else:
        st.info("No quarterly results filed as of this run's date.")

    st.subheader("Shareholding")
    if d["shareholding"]:
        st.altair_chart(charts.shareholding_chart(d["shareholding"], th),
                        width="stretch")
        with st.expander("Table view", expanded=True):
            st.dataframe(display(pl.DataFrame(d["shareholding"])), hide_index=True)
    else:
        st.info("No shareholding filings as of this run's date.")

    if d["prices"]:
        st.altair_chart(charts.price_chart(d["prices"], th), width="stretch")

    st.subheader("Filings and announcements")
    st.dataframe(pl.DataFrame([{"filed (IST)": f"{f['filed_at'].astimezone(IST):%Y-%m-%d %H:%M}",
                                "kind": f["kind"], "title": f["title"],
                                "link": f["url"] or ""} for f in d["filings"]]),
                 hide_index=True, width="stretch",
                 column_config={"link": st.column_config.LinkColumn("link")})
    _notes_table(co["company_id"], run)
    _insider_table(co["symbol"], run)


def _insider_table(symbol: str, run: dict) -> None:
    trades = service.insider_trades(conn(), symbol, run["as_of"])
    st.subheader("Insider trades (SEBI PIT), last 12 months")
    if not trades:
        st.caption("No insider-trading disclosures in the last 12 months, or none loaded yet. "
                   "The NSE check loads new ones; docs/DEPLOY.md shows how to load history.")
        return
    st.caption("Disclosed under SEBI's insider-trading rules and dated by the exchange "
               "broadcast. Open-market purchases of equity by promoters, directors and key "
               "managers feed the ownership pillar; other trades are shown for context. A "
               "revision replaces the earlier row for the same person and trade date.")
    st.dataframe(pl.DataFrame([{
        "broadcast (IST)": f"{t['filed_at'].astimezone(IST):%Y-%m-%d %H:%M}",
        "person": t["person_name"], "category": t["person_category"],
        "type": {"buy": "acquired", "sell": "disposed of"}.get(t["side"],
                                                               t["transaction_type"]),
        "mode": t["acquisition_mode"],
        "security": t["security_type"], "quantity": t["quantity"],
        "value (Rs cr)": None if t["value_inr"] is None else round(t["value_inr"] / 1e7, 2),
        "holding after %": t["holding_after_pct"],
        "filing": t["submission_type"] or "",
        "counts": "replaced by a revision" if t["superseded"]
        else "yes" if (t["side"] == "buy" and t["open_market"]
                       and t["insider_role"] != "other"
                       and (t["security_type"] or "").lower().startswith("equity"))
        else ""} for t in trades]),
        hide_index=True, width="stretch")


def _assistant_enabled() -> bool:
    from igs.config import load_assistant
    try:
        return load_assistant().enabled
    except Exception:  # noqa: BLE001 - a broken assistant config must not break the UI
        return False


def _md(text: str) -> str:
    return text.replace("$", "\\$")          # Streamlit would read $...$ as maths


def _brief_panel(symbol: str, run: dict) -> None:
    stored = service.stored_brief(conn(), run["run_id"], symbol)
    if stored is None and not _assistant_enabled():
        return
    st.subheader("Plain-language brief (AI)")
    st.caption(AI_NOTE)
    if stored:
        st.markdown(_md(stored["text"]))
        st.caption(f"{stored['model']}, {stored['created_at'].astimezone(IST):%Y-%m-%d %H:%M}")
        return
    if st.button("Write a brief", key="brief_btn"):
        try:
            from igs.assistant.brief import brief
            from igs.assistant.llm import Assistant, AssistantError, AssistantUnavailable
        except ImportError:
            st.error("The assistant needs the Anthropic SDK: run `uv sync --all-groups`.")
            return
        with st.spinner("Writing the brief..."):
            try:
                b = brief(Assistant.open(conn()), symbol, run["run_id"])
            except (AssistantUnavailable, AssistantError) as exc:
                st.error(str(exc))
                return
        st.markdown(_md(b.text))


def _latest_calls() -> dict[int, dict]:
    from igs.assistant import calls as ai
    try:
        return ai.latest_by_company(conn())
    except Exception:  # noqa: BLE001 - calls not migrated yet: the column stays empty
        return {}


def _call_label(c: dict | None) -> str:
    if not c:
        return ""
    return f"{ACTION_LABEL[c['action']]} {c['created_at'].astimezone(IST):%d %b}"


def _since(outcome: dict) -> str:
    s = outcome.get("so_far")
    if not s or s["excess_pct"] is None:
        return ""
    return (f"{s['return_pct']:+.1f}% vs Nifty 500 {s['nifty500_pct']:+.1f}% "
            f"({s['excess_pct']:+.1f} points) to {outcome['as_of']:%Y-%m-%d}")


def _show_call(c: dict) -> None:
    from igs.assistant import calls as ai
    m = st.columns(4)
    m[0].metric("AI call", ACTION_LABEL[c["action"]])
    m[1].metric("Confidence", f"{c['confidence']:.0%}")
    m[2].metric("Horizon", f"{c['horizon_months']} months")
    m[3].metric("Last close it saw", "n/a" if c["price_close"] is None
                else f"Rs {c['price_close']:,.2f}")
    st.markdown(_md(c["summary"]))
    left, right = st.columns(2)
    left.markdown("**When to buy**\n" + "\n".join(f"- {_md(x)}" for x in c["buy_when"]))
    right.markdown("**When to sell**\n" + "\n".join(f"- {_md(x)}" for x in c["sell_when"]))
    with st.expander("Reasons, risks and data gaps"):
        for title, key in (("Reasons", "reasons"), ("Risks", "risks"),
                           ("Data gaps", "data_gaps")):
            if c[key]:
                st.markdown(f"**{title}**\n" + "\n".join(f"- {_md(x)}" for x in c[key]))
    since = _since(ai.outcome(conn(), c))
    made_by = "automatic" if c["trigger"] == "scheduled" else "on request"
    st.caption(f"Made {c['created_at'].astimezone(IST):%Y-%m-%d %H:%M} IST from run "
               f"{c['run_id']} ({made_by}"
               + (f": {c['reason']}" if c.get("reason") and made_by == "automatic" else "")
               + f") · {c['model']} · ~${c['cost_usd']:.3f}"
               + (f" · since then: {since}" if since else ""))


def _call_panel(symbol: str, run: dict) -> None:
    from igs.assistant import calls as ai
    past = ai.calls(conn(), symbol, limit=20)
    enabled = _assistant_enabled()
    if not past and not enabled:
        return
    st.subheader("AI call")
    st.caption(CALL_NOTE)
    if past:
        _show_call(past[0])
    if enabled and _ui_is_local() and st.button(
            "Ask the AI for a new call" if past else "Ask the AI for a call", key="call_btn"):
        try:
            from igs.assistant.llm import Assistant, AssistantError, AssistantUnavailable
        except ImportError:
            st.error("The assistant needs the Anthropic SDK: run `uv sync --all-groups`.")
            return
        with st.spinner("The AI is reading everything on this stock..."):
            try:
                ai.make_call(Assistant.open(conn()), symbol, run["run_id"])
            except (AssistantUnavailable, AssistantError, service.NotFound) as exc:
                st.error(str(exc))
                return
        st.rerun()
    if len(past) > 1:
        with st.expander(f"Earlier calls ({len(past) - 1})"):
            st.dataframe(pl.DataFrame([{
                "made (IST)": f"{c['created_at'].astimezone(IST):%Y-%m-%d}",
                "call": c["action"], "confidence": f"{c['confidence']:.0%}",
                "horizon (months)": c["horizon_months"],
                "close then": None if c["price_close"] is None else round(c["price_close"], 2),
                "since then": _since(ai.outcome(conn(), c))} for c in past[1:]]),
                hide_index=True, width="stretch")


def _due_panel() -> None:
    """Which stocks the daily job will call next, and why; and a button to call them now."""
    from igs.assistant import calls as ai
    from igs.config import load_assistant
    runs = service.runs(conn(), limit=1)
    if not runs:
        return
    try:
        cfg = load_assistant().features.call
        due = ai.due_for_call(conn(), runs[0]["run_id"], cfg.top_ranked, cfg.refresh_days)
    except Exception as exc:  # noqa: BLE001 - invalid settings or an old database
        st.info(f"Can't list the stocks due for an AI call: {str(exc).splitlines()[0]}")
        return
    st.subheader("Due for an AI call")
    st.caption(f"The daily job makes these calls automatically after scoring, most urgent "
               f"first, at most {cfg.max_per_day} a day"
               + ("" if cfg.scheduled else " (turned off in Settings)") + ". Covered: "
               f"watchlist stocks, the {cfg.top_ranked} best-ranked stocks, and stocks whose "
               "latest call is buy or hold. A stock is due when new results, shareholding, "
               "insider trades or material announcements arrived since its last call, its "
               "tier changed or a red flag tripped, or its last call is older than "
               f"{cfg.refresh_days} days.")
    if not due:
        st.write("None due for run "
                 f"{runs[0]['run_id']} ({runs[0]['as_of']:%Y-%m-%d}).")
        return
    st.dataframe(pl.DataFrame([{"symbol": d.symbol, "why": d.reason,
                                "on watchlist": d.watched, "rank": d.rank} for d in due]),
                 hide_index=True, width="stretch")
    if not (_assistant_enabled() and _ui_is_local()):
        return
    n = min(len(due), cfg.max_per_day)
    if st.button(f"Make the {n} most urgent calls now", key="calls_due_btn",
                 help="Counts toward today's automatic calls and the daily budget."):
        try:
            from igs.assistant.llm import Assistant, AssistantError, AssistantUnavailable
        except ImportError:
            st.error("The assistant needs the Anthropic SDK: run `uv sync --all-groups`.")
            return
        with st.spinner(f"The AI is working through {n} stocks..."):
            try:
                made = ai.scheduled_calls(Assistant.open(conn()), runs[0]["run_id"])
            except (AssistantUnavailable, AssistantError) as exc:
                st.error(str(exc))
                return
        (st.warning if made.issues else st.success)(str(made))


def page_calls() -> None:
    from igs.assistant import calls as ai
    st.header("AI calls")
    st.caption(CALL_NOTE)
    _due_panel()
    record = ai.track_record(conn())
    if not record["calls"]:
        st.info("No AI calls yet. The daily job makes them automatically once the assistant "
                "is on (Settings); you can also ask for one on any stock page.")
        return
    st.subheader("Record")
    st.caption("Each call is measured from the last close the AI saw, against the Nifty 500 "
               "over the same dates. A buy is right if the stock beat the index, a sell if "
               "it lagged; holds are not scored. Only horizons that have passed count.")
    if record["summary"]:
        st.dataframe(pl.DataFrame([{
            "call": s["action"], "after": s["horizon"], "calls": s["calls"],
            "right": "not scored" if s["right_pct"] is None else f"{s['right_pct']:.0f}%",
            "mean vs Nifty 500 (points)": s["mean_excess_pct"]}
            for s in record["summary"]]), hide_index=True, width="stretch")
    else:
        st.info("No call has reached its first horizon (one month) yet, so there is no "
                "record. Until there is, treat the calls as unproven.")
    st.subheader("All calls")
    names = {c["symbol"]: c["name"] for c in service.companies(conn())}
    rows = []
    for c in record["calls"]:
        h = c["outcome"]["horizons"]
        rows.append({
            "made (IST)": f"{c['created_at'].astimezone(IST):%Y-%m-%d}",
            "symbol": c["symbol"], "company": names.get(c["symbol"], ""), "call": c["action"],
            "confidence": f"{c['confidence']:.0%}", "horizon (months)": c["horizon_months"],
            "close then": None if c["price_close"] is None else round(c["price_close"], 2),
            "since then": _since(c["outcome"]),
            **{f"{k} vs Nifty 500": (h[k]["excess_pct"] if h.get(k) else None)
               for k in ai.HORIZONS},
            "why": c["reason"] if c["trigger"] == "scheduled" else "on request"})
    st.dataframe(pl.DataFrame(rows), hide_index=True, width="stretch")
    symbols = sorted({c["symbol"] for c in record["calls"]}, key=lambda s: names.get(s, s))
    pick = st.selectbox("Open a stock", symbols, key="calls_open",
                        format_func=lambda s: f"{names[s]} ({s})" if s in names else s)
    st.button("Open", key="calls_open_btn", on_click=open_stock, args=(pick,))


def _notes_table(company_id: int, run: dict) -> None:
    notes = service.announcement_notes(conn(), company_id, run["as_of"])
    if not notes:
        return
    st.caption("Assistant's reading of announcements (AI; not used in ranking)")
    st.dataframe(pl.DataFrame([{
        "filed (IST)": f"{n['filed_at'].astimezone(IST):%Y-%m-%d %H:%M}",
        "materiality": n["materiality"], "category": n["category"].replace("_", " "),
        "summary": n["summary"],
        "concerns": ", ".join(c.replace("_", " ") for c in n["concerns"])} for n in notes]),
        hide_index=True, width="stretch")


def page_ask(run: dict) -> None:
    st.header("Ask about this run")
    st.caption(AI_NOTE.replace("from this run's stored data", "using read-only lookups into "
                                                               "this run's stored results"))
    if not _assistant_enabled():
        st.info("The research assistant is off. To use it, open **Settings** in the "
                "sidebar, save an Anthropic API key and enable the assistant (model, daily "
                "budget and effort are there too). If the SDK is missing, run "
                "`uv sync --all-groups` first.")
        return
    try:
        from igs.assistant.ask import ask
        from igs.assistant.llm import Assistant, AssistantError, AssistantUnavailable
    except ImportError:
        st.error("The assistant needs the Anthropic SDK: run `uv sync --all-groups`.")
        return
    history = st.session_state.setdefault(f"ask_{run['run_id']}", [])
    for turn in history:
        with st.chat_message(turn["role"]):
            st.markdown(_md(turn["content"]))
    question = st.chat_input("Why is a stock where it is? What holds it back? What changed?")
    if question:
        with st.chat_message("user"):
            st.markdown(_md(question))
        with st.chat_message("assistant"):
            with st.spinner("Looking it up..."):
                try:
                    a = ask(Assistant.open(conn()), question, run["run_id"], history[-10:])
                except (AssistantUnavailable, AssistantError) as exc:
                    st.error(str(exc))
                    return
            st.markdown(_md(a.text))
            with st.expander(f"{len(a.tool_calls)} lookups, about ${a.cost_usd:.3f}, "
                             f"{a.model}"):
                st.json(a.tool_calls)
                for note in a.notes:
                    st.caption(note)
        history += [{"role": "user", "content": question},
                    {"role": "assistant", "content": a.text}]
    if history and st.button("Clear conversation", key="ask_clear"):
        history.clear()
        st.rerun()
    st.caption(DISCLAIMER)


def page_watchlist(run: dict) -> None:
    st.header("Watchlist")
    items = service.watchlist(conn())
    _, rows = service.rankings(conn(), run["run_id"], watchlist_only=True)
    by_id = {r["company_id"]: r for r in rows}
    table = [{"symbol": w["symbol"], "name": w["name"], "note": w["note"],
              "tier": by_id.get(w["company_id"], {}).get("tier", "not in this run's universe"),
              "rank": by_id.get(w["company_id"], {}).get("rank"), "added": w["added_at"]}
             for w in items]
    if table:
        st.dataframe(pl.DataFrame(table), hide_index=True, width="stretch")
    else:
        st.info("Nothing on the watchlist yet.")
    with st.form("add_watch", clear_on_submit=True):
        sym = company_picker("Company", "watch_pick", run)
        note = st.text_input("Note")
        if st.form_submit_button("Add") and sym:
            try:
                service.watchlist_add(conn(), sym, note)
                st.rerun()
            except service.NotFound as exc:
                st.error(str(exc))
    if items:
        names = {w["symbol"]: w["name"] for w in items}
        rm = st.selectbox("Remove", list(names), key="rm_watch",
                          format_func=lambda s: f"{names[s]} ({s})")
        if st.button("Remove", key="rm_btn"):
            service.watchlist_remove(conn(), rm)
            st.rerun()


def page_screens(run: dict) -> None:
    st.header("Saved screens")
    saved = service.screens(conn())
    if not saved:
        st.info("No saved screens. Save filters from the rankings page.")
        return
    name = st.selectbox("Screen", [s["name"] for s in saved], key="screen_pick")
    st.json(next(s["filters"] for s in saved if s["name"] == name))
    _, rows = service.screen_run(conn(), name, run["run_id"])
    st.write(f"{len(rows)} companies")
    if rows:
        st.dataframe(pl.DataFrame(rows).drop("company_id"), hide_index=True,
                     width="stretch")
        st.download_button("Export CSV", service.rankings_csv(rows), f"{name}.csv", "text/csv",
                           key="dl_screen")
    if st.button("Delete screen", key="del_screen"):
        service.screen_delete(conn(), name)
        st.rerun()



def document_status_panel() -> None:
    with st.expander("Document processing"):
        rows = service.document_processing_summary(conn())
        if rows:
            st.dataframe(pl.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No document processing attempts recorded since the recovery update.")
        failures = service.document_failures(conn())
        if failures:
            st.dataframe(pl.DataFrame(failures), hide_index=True, width="stretch")
            st.caption("After correcting the cause, retry stored financial results with "
                       "`uv run igs ingest replay-documents financial_results`. "
                       "This uses downloaded files without fetching them again.")

def page_quality(run: dict) -> None:
    st.header("Run details and data quality")
    document_status_panel()
    meta = next(r for r in service.runs(conn()) if r["run_id"] == run["run_id"])
    st.write(f"Signals as of **{meta['as_of']:%Y-%m-%d %H:%M} UTC**, created "
             f"{meta['created_at']:%Y-%m-%d %H:%M} UTC.")
    st.write("Backtest IC status used: "
             + (meta["ic_status_generated_at"] or "**none - factors not yet validated**"))
    dropped = meta["dropped_factors"] or []
    st.write("Factors dropped by the IC gate: " + (", ".join(dropped) if dropped else "none"))
    dq = meta["dq_summary"] or {}
    st.write(f"Data-quality issues during the run: {dq.get('error', 0)} errors, "
             f"{dq.get('warn', 0)} warnings.")
    for msg in dq.get("issues", []):
        st.write(f"- {msg}")
    health = meta.get("health_issues") or []
    st.subheader("Run health")
    if health:
        health_banner(meta)
    else:
        st.success("Inputs fresh and consistent with the previous run.", icon="✅")


def _ui_is_local() -> bool:
    """Settings (and the API key) may be changed only when the UI listens on this computer
    alone, as `igs ui` does by default."""
    try:
        return (st.get_option("server.address") or "") in LOCAL_ADDRESSES
    except Exception:  # noqa: BLE001
        return False


def _masked(key: str | None) -> str:
    if not key:
        return "not set"
    return f"set ({key[:7]}...{key[-4:]})" if len(key) > 16 else "set"


def _usage_panel(budget: float) -> None:
    from igs.timeutil import utc_now
    try:
        rows = conn().execute(
            """select (called_at at time zone 'Asia/Kolkata')::date as day, feature,
                      count(*) as calls, sum(input_tokens) as input_tokens,
                      sum(output_tokens) as output_tokens, sum(cost_usd)::float8 as cost_usd
               from llm_call where called_at >= now() - interval '7 days'
               group by 1, 2 order by 1 desc, 2""").fetchall()
    except Exception as exc:  # noqa: BLE001 - e.g. not migrated yet
        st.info(f"No usage available ({str(exc).splitlines()[0]}); run `igs db migrate`.")
        return
    today = utc_now().astimezone(IST).date()
    spent = sum(r[5] for r in rows if r[0] == today)
    st.write(f"Today (IST): about **\\${spent:.2f}** of the \\${budget:.2f} daily budget.")
    st.progress(min(spent / budget, 1.0) if budget else 1.0)
    if rows:
        st.dataframe(pl.DataFrame(rows, orient="row", schema=[
            "day", "feature", "calls", "input tokens", "output tokens", "est. cost (USD)"]),
            hide_index=True, width="stretch")
    else:
        st.caption("No calls in the last 7 days.")


def _save_key(env_path: str) -> None:
    """Button callback: runs before the page is drawn again, so the input can be cleared."""
    from pathlib import Path

    from igs import envfile
    key = (st.session_state.get("set_key") or "").strip()
    try:
        envfile.set_value(Path(env_path), "ANTHROPIC_API_KEY", key)
    except ValueError as exc:
        flash = [("error", str(exc))]
    else:
        flash = [("success", "Key saved.")]
        if not key.startswith("sk-ant-"):
            flash.append(("warning", "That doesn't look like an Anthropic API key (they start "
                                     "with sk-ant-); use Test connection to check it."))
    st.session_state["set_key"] = ""
    st.session_state["set_flash"] = flash


def _remove_key(env_path: str) -> None:
    from pathlib import Path

    from igs import envfile
    envfile.unset(Path(env_path), "ANTHROPIC_API_KEY")
    st.session_state["set_flash"] = [("success", "Key removed.")]


WHATSAPP_KEYS = ("IGS_WHATSAPP_PROVIDER", "IGS_WHATSAPP_TO", "IGS_WHATSAPP_TOKEN",
                 "IGS_WHATSAPP_PHONE_ID", "IGS_CALLMEBOT_APIKEY")


def _save_whatsapp(env_path: str) -> None:
    """Button callback. Blank key fields keep what is saved, so the number or service can
    change without typing the keys again."""
    from pathlib import Path

    from igs import envfile
    from igs.alerts import whatsapp
    state, path = st.session_state, Path(env_path)
    chosen = state.get("wa_provider", "meta")
    try:
        values = {"IGS_WHATSAPP_PROVIDER": chosen,
                  "IGS_WHATSAPP_TO": whatsapp.normalise_number(state.get("wa_to") or "")}
        for key, field in (("IGS_WHATSAPP_TOKEN", "wa_token"),
                           ("IGS_WHATSAPP_PHONE_ID", "wa_phone_id"),
                           ("IGS_CALLMEBOT_APIKEY", "wa_apikey")):
            if (state.get(field) or "").strip():
                values[key] = state[field].strip()
        for key, value in values.items():
            envfile.set_value(path, key, value)
    except ValueError as exc:
        state["wa_flash"] = [("error", str(exc))]
        return
    for field in ("wa_token", "wa_apikey"):
        state[field] = ""
    need = whatsapp.missing()
    state["wa_flash"] = [("success", "WhatsApp settings saved.")] + (
        [("warning", "Still needed before messages can go out: " + ", ".join(need))]
        if need else [])


def _remove_whatsapp(env_path: str) -> None:
    from pathlib import Path

    from igs import envfile
    for key in WHATSAPP_KEYS:
        envfile.unset(Path(env_path), key)
    st.session_state["wa_flash"] = [("success", "WhatsApp settings removed.")]


def _saved(key: str | None) -> str:
    return f"saved: {_masked(key)}; leave blank to keep it" if key else "not saved yet"


def _whatsapp_settings(local: bool) -> None:
    from igs import envfile
    from igs.alerts import whatsapp
    from igs.config import load_alerts
    st.subheader("WhatsApp alerts")
    st.caption("A detailed WhatsApp message for each new buy or sell call by the AI: a "
               "stock's first buy or sell, or a change to buy or sell (config/alerts.yaml, "
               "`whatsapp`). Messages go out after the daily job; failed ones are retried. "
               "docs/DEPLOY.md, \"WhatsApp messages for the AI's buy and sell calls\", sets "
               "up either service step by step.")
    for kind, text in st.session_state.pop("wa_flash", []):
        getattr(st, kind)(text)
    env_path = envfile.default_path()
    chosen, need = whatsapp.provider(), whatsapp.missing()
    if not need:
        st.write(f"Sending through **{whatsapp.PROVIDERS[chosen]}** to "
                 f"{os.environ['IGS_WHATSAPP_TO']}. Keys are kept in `{env_path}` and never "
                 "shown in full.")
    elif chosen or os.environ.get("IGS_WHATSAPP_TO"):
        st.write("Not ready yet; still needed: " + ", ".join(need))
    else:
        st.write("Not set up.")
    if not load_alerts().channels.get("whatsapp"):
        st.warning("`channels: whatsapp` is off in config/alerts.yaml, so nothing is sent.")
    options = list(whatsapp.PROVIDERS)
    st.radio("Service", options, key="wa_provider", horizontal=True, disabled=not local,
             index=options.index(chosen) if chosen in options else 0,
             format_func=lambda p: {"meta": "WhatsApp Cloud API (Meta, official)",
                                    "callmebot": "CallMeBot (free, personal use)"}[p])
    st.text_input("Your WhatsApp number, with country code", key="wa_to",
                  value=os.environ.get("IGS_WHATSAPP_TO", ""), placeholder="+919812345678",
                  disabled=not local)
    if st.session_state.get("wa_provider", options[0]) == "meta":
        c1, c2 = st.columns(2)
        c1.text_input("Access token", type="password", key="wa_token", disabled=not local,
                      placeholder=_saved(os.environ.get("IGS_WHATSAPP_TOKEN")),
                      help="A permanent token of a system user with the "
                           "whatsapp_business_messaging permission.")
        c2.text_input("Phone number ID", key="wa_phone_id", disabled=not local,
                      value=os.environ.get("IGS_WHATSAPP_PHONE_ID", ""),
                      help="Meta app, WhatsApp, API Setup: the ID under the From number "
                           "(not the phone number itself).")
    else:
        st.text_input("CallMeBot API key", type="password", key="wa_apikey",
                      disabled=not local,
                      placeholder=_saved(os.environ.get("IGS_CALLMEBOT_APIKEY")),
                      help="CallMeBot sends it on WhatsApp after you message its number "
                           "\"I allow callmebot to send me messages\".")
    b1, b2, b3 = st.columns(3)
    b1.button("Save WhatsApp settings", key="wa_save", disabled=not local,
              on_click=_save_whatsapp, args=(str(env_path),))
    if b2.button("Send a test message", key="wa_test", disabled=not local or bool(need)):
        try:
            st.success(f"Sent through {whatsapp.send_test(load_alerts().whatsapp)}; check "
                       "WhatsApp.")
        except whatsapp.WhatsAppError as exc:
            st.error(str(exc))
    b3.button("Remove WhatsApp settings", key="wa_remove",
              disabled=not local or not any(os.environ.get(k) for k in WHATSAPP_KEYS),
              on_click=_remove_whatsapp, args=(str(env_path),))


def page_settings() -> None:
    from pydantic import ValidationError

    from igs import envfile, settings
    from igs.config import load_assistant, settings_dir

    st.header("Settings")
    local = _ui_is_local()
    if not local:
        st.warning("This UI is reachable from other computers, so settings and the API key "
                   "can't be changed here. Start it with `igs ui` (this computer only) to "
                   "edit them.", icon="🔒")
    from igs.config import load_universe

    st.subheader("Rating history")
    with st.form("rating_history"):
        quarters = st.number_input("Minimum quarters of results", min_value=0,
                                   value=load_universe().min_filing_quarters,
                                   step=1, key="rating_quarters")
        save_history = st.form_submit_button("Save rating settings", disabled=not local)
    if save_history:
        settings.save_filing_quarters(int(quarters))
        st.success("Saved. New scoring runs use this threshold; "
                   "existing runs retain their settings.")
    st.caption("This controls eligibility for rankings. Factors requiring longer history "
               "remain unavailable until that history exists.")
    _whatsapp_settings(local)
    st.subheader("Research assistant (AI)")
    st.caption("Optional. It answers questions about a run, writes plain-language briefs and "
               "reads new announcements, using the Claude API (billed per use). Those "
               "never affect rankings; its assessments of geopolitical news (News page) "
               "can adjust ratings within a small cap. Saved changes are kept in "
               f"`{settings_dir() / 'assistant.yaml'}` on top of `config/assistant.yaml`.")
    try:
        cfg = load_assistant()
    except (ValidationError, ValueError) as exc:
        st.error(f"The assistant settings are invalid: {exc}")
        if "extra_forbidden" in str(exc) or "Extra inputs" in str(exc):
            st.info("A setting this version of the app doesn't know usually means the app "
                    "was updated while it was running: restart it (Ctrl+C, then `uv run igs "
                    "ui`) before resetting anything.")
        if local and st.button("Reset to the defaults in config/assistant.yaml",
                               key="set_reset_broken"):
            settings.reset_assistant()
            st.rerun()
        return
    feats = cfg.features

    st.markdown("**API key**")
    env_path = envfile.default_path()
    for kind, text in st.session_state.pop("set_flash", []):
        getattr(st, kind)(text)
    st.write(f"Anthropic API key: {_masked(os.environ.get('ANTHROPIC_API_KEY'))}. "
             f"Kept in `{env_path}`, readable by you only, and never shown in full.")
    new_key = st.text_input("New API key", type="password", key="set_key",
                            placeholder="sk-ant-...", disabled=not local,
                            help="Create one at console.anthropic.com (Settings, API keys).")
    k1, k2, k3 = st.columns(3)
    k1.button("Save key", key="set_key_save", disabled=not local or not new_key,
              on_click=_save_key, args=(str(env_path),))
    k2.button("Remove key", key="set_key_remove",
              disabled=not local or not os.environ.get("ANTHROPIC_API_KEY"),
              on_click=_remove_key, args=(str(env_path),))
    if k3.button("Test connection", key="set_test"):
        try:
            from igs.assistant.llm import AssistantError, AssistantUnavailable, check_connection
        except ImportError:
            st.error("The assistant needs the Anthropic SDK: run `uv sync --all-groups`.")
        else:
            try:
                st.success(f"Connected: {check_connection(cfg)} is available (no tokens used).")
            except (AssistantUnavailable, AssistantError) as exc:
                st.error(str(exc))

    st.markdown("**Assistant settings**")
    models = list(cfg.prices_usd_per_mtok)
    with st.form("assistant_settings"):
        enabled = st.toggle("Enable the research assistant", value=cfg.enabled,
                            key="set_enabled", disabled=not local)
        c1, c2 = st.columns(2)
        model = c1.selectbox(
            "Model", models, index=models.index(cfg.model), key="set_model",
            disabled=not local,
            help="Models with a price in config/assistant.yaml (the budget needs one). "
                 "claude-opus-5 is the default; claude-sonnet-5 costs less per token.")
        budget = c2.number_input("Daily spending threshold (USD)", min_value=0.0, max_value=1000.0,
                                 step=0.5, value=float(cfg.daily_budget_usd), key="set_budget",
                                 disabled=not local)
        fallbacks = st.toggle(
            "Refusal fallbacks", value=cfg.fallbacks == "default", key="set_fallbacks",
            disabled=not local,
            help="If the model's safety classifiers decline a request, the API retries it on "
                 "the recommended fallback model (claude-opus-5 and newer).")
        a, b, c = st.columns(3)
        a.caption("Ask")
        ask_effort = a.selectbox("Effort", EFFORTS, index=EFFORTS.index(feats.ask.effort),
                                 key="set_ask_effort", disabled=not local)
        ask_rounds = a.number_input("Tool rounds per question", 1, 30,
                                    value=feats.ask.max_tool_rounds, key="set_ask_rounds",
                                    disabled=not local)
        b.caption("Briefs")
        brief_effort = b.selectbox("Effort", EFFORTS, index=EFFORTS.index(feats.brief.effort),
                                   key="set_brief_effort", disabled=not local)
        c.caption("Announcement notes")
        ann_effort = c.selectbox("Effort", EFFORTS,
                                 index=EFFORTS.index(feats.announcements.effort),
                                 key="set_ann_effort", disabled=not local)
        ann_days = c.number_input("Days back", 1, 90, value=feats.announcements.days,
                                  key="set_ann_days", disabled=not local)
        ann_max = c.number_input("Most per run", 1, 2000,
                                 value=feats.announcements.max_per_run, key="set_ann_max",
                                 disabled=not local)
        scopes = ["universe", "watchlist"]
        ann_scope = c.selectbox("Companies", scopes,
                                index=scopes.index(feats.announcements.scope),
                                key="set_ann_scope", disabled=not local)
        d, e = st.columns(2)
        d.caption("AI buy / hold / sell calls")
        call_effort = d.selectbox(
            "Effort", EFFORTS, index=EFFORTS.index(feats.call.effort), key="set_call_effort",
            disabled=not local, help="How long the AI thinks before a call; xhigh and max "
                                     "cost more per call.")
        call_scheduled = e.toggle(
            "Automatic calls in the daily job", value=feats.call.scheduled,
            key="set_call_scheduled", disabled=not local,
            help="After scoring, the daily job makes a new call on each covered stock that "
                 "has new data since its last call (results, shareholding, insider trades, "
                 "material announcements, a tier change, a new red flag), no call yet, or a "
                 "call older than the days below. Covered: watchlist stocks, the top-ranked "
                 "stocks below, and stocks whose latest call is buy or hold.")
        call_top = d.number_input("Top-ranked stocks covered", 0, 500,
                                  value=feats.call.top_ranked, key="set_call_top",
                                  disabled=not local)
        call_days = e.number_input("New call after (days) without new data", 1, 90,
                                   value=feats.call.refresh_days, key="set_call_days",
                                   disabled=not local)
        call_max = e.number_input("Most automatic calls a day", 0, 200,
                                  value=feats.call.max_per_day, key="set_call_max",
                                  disabled=not local,
                                  help="Each costs roughly US$0.10-0.30 and counts toward "
                                       "the daily spending threshold above.")
        saved = st.form_submit_button("Save settings", disabled=not local)
    if saved and local:
        values = {"enabled": enabled, "model": model, "daily_budget_usd": float(budget),
                  "fallbacks": "default" if fallbacks else None,
                  "features": {
                      "ask": {"effort": ask_effort, "max_tool_rounds": int(ask_rounds)},
                      "brief": {"effort": brief_effort},
                      "announcements": {"effort": ann_effort, "days": int(ann_days),
                                        "max_per_run": int(ann_max), "scope": ann_scope},
                      "call": {"effort": call_effort, "scheduled": call_scheduled,
                               "top_ranked": int(call_top), "refresh_days": int(call_days),
                               "max_per_day": int(call_max)}}}
        try:
            cfg = settings.save_assistant(values)
        except (ValidationError, ValueError) as exc:
            st.error(f"Not saved: {exc}")
        else:
            st.success("Settings saved. They apply to the app, the CLI and the daily job.")
            if enabled and not os.environ.get("ANTHROPIC_API_KEY"):
                st.warning("The assistant is enabled but no API key is set.")
    if local and settings.assistant_path().is_file() and st.button(
            "Reset to the defaults in config/assistant.yaml", key="set_reset"):
        settings.reset_assistant()
        st.rerun()

    st.markdown("**Usage (estimated)**")
    st.caption("This is a soft spending threshold: requests already in progress can "
               "take the total above it.")
    _usage_panel(cfg.daily_budget_usd)
    st.caption(DISCLAIMER)


def _start_check() -> None:
    """Button callback: a manual check in the background (logs/sync.log)."""
    from igs.sync import start_background_sync
    start_background_sync("manual")
    st.session_state["sync_flash"] = "Check started; new data appears here when it finishes."


def sync_panel() -> None:
    """When NSE was last checked for new files, and a local-only Check now button."""
    import datetime as dt

    from igs.config import load_sync
    from igs.sync import check_running, last_check
    try:
        last = last_check(conn())
        busy = check_running(conn())          # the lock, not the row: a process can die
    except Exception:  # noqa: BLE001 - not migrated yet, or the table is missing
        conn().rollback()
        return
    cfg = load_sync()
    box = st.sidebar.container()
    if last is None:
        box.caption("NSE not checked for new files yet.")
    else:
        when = f"{last['started_at'].astimezone(IST):%d %b %H:%M} IST"
        if last["status"] == "running" and busy:
            box.caption(f"Checking NSE for new files (started {when}, {last['trigger']}).")
        elif last["status"] == "running":
            box.caption(f"The check started {when} ({last['trigger']}) did not finish: its "
                        "process ended. The next check runs normally.")
        else:
            failed = [x["step"] for x in last["steps"] if x["status"] == "failed"]
            box.caption(f"NSE last checked {when} ({last['trigger']}): {last['status']}, "
                        f"{last['new_rows']} new rows"
                        + (f"; failed: {', '.join(failed[:3])}"
                           + ("..." if len(failed) > 3 else "") if failed else "") + ".")
    if msg := st.session_state.pop("sync_flash", None):
        box.caption(msg)
    if not _ui_is_local():
        return
    wait = None
    if last is not None:
        from igs.timeutil import utc_now
        ready = last["started_at"] + dt.timedelta(minutes=cfg.min_interval_minutes)
        if utc_now() < ready:
            wait = f"{ready.astimezone(IST):%H:%M} IST"
    box.button("Check NSE now", key="sync_now", on_click=_start_check,
               disabled=busy or wait is not None,
               help=("A check is running." if busy
                     else f"Checks are at least {cfg.min_interval_minutes:g} min apart; "
                          f"the next can start at {wait}." if wait else
                     "Download any new NSE files now (no scoring)."))


def page_news() -> None:
    import json

    from igs.geopolitical import import_articles

    st.header("Geopolitical news")
    from igs.news import collect_news, news_status

    st.caption("Indian economy, defence and international RSS news is collected during "
               "startup, periodic sync checks and the "
               "daily job. The daily job assesses matching articles before updating ratings.")
    st.text("News is assessed from India's perspective: trade, energy imports, rupee "
            "movements and Indian industry sensitivity. RSS summaries are matched using "
            "observed company names and industry labels. "
            "Industry matches are estimates, not verified direct exposures. Feed-based AI "
            "confidence is capped at 65%; a weak summary may have no rating effect. "
            "Articles no rating used (no company matched, or never assessed) are deleted "
            "30 days after publication; assessed articles are kept as the evidence behind "
            "past ratings.")
    st.code("uv run igs news collect\nuv run igs news assess --limit 10\n"
            "uv run igs score", language="bash")
    status = news_status(conn())
    if status["feeds"]:
        st.subheader("Latest feed checks")
        st.dataframe(status["feeds"], hide_index=True, width="stretch")
    if status["articles"]:
        st.subheader("Recent articles")
        st.dataframe(status["articles"], hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Source")})
    else:
        st.info("No news collected yet. Use Collect news now, or wait for the next sync.")
    if not _ui_is_local():
        st.info("News imports and AI assessment are available on the local application.")
        return
    if st.button("Collect news now", key="news_collect"):
        with st.spinner("Collecting public news feeds..."):
            report = collect_news(conn())
        (st.warning if report.errors else st.success)(str(report))
        st.caption("Feed checks are at least an hour apart. Reopen this page to refresh the table.")
    st.caption("Optional: you can still import your own sourced articles below.")
    with st.form("news_import"):
        upload = st.file_uploader("News articles (JSON, up to 100 articles)", type=["json"])
        submitted = st.form_submit_button("Import articles")
    if submitted and upload is not None:
        try:
            if upload.size > 3_000_000:
                raise ValueError("news import must be under 3 MB")
            count = import_articles(conn(), json.loads(upload.getvalue()))
        except (ValueError, UnicodeError) as exc:
            st.error(str(exc))
        else:
            st.success(f"Imported {count} new articles. Duplicates were ignored.")
    st.caption("Assessment sends the supplied news text and company exposures to the "
               "configured AI provider and uses the assistant's spending threshold.")
    if st.button("Assess up to 10 recent articles", disabled=not _assistant_enabled()):
        try:
            from igs.assistant.geopolitical import assess_pending
            from igs.assistant.llm import Assistant, AssistantError, AssistantUnavailable
        except ImportError:
            st.error("Install the AI dependency with `uv sync --all-groups`.")
            return
        try:
            with st.spinner("Assessing geopolitical impact..."):
                count = assess_pending(Assistant.open(conn()))
        except (AssistantError, AssistantUnavailable, ValueError) as exc:
            st.error(str(exc))
        else:
            st.success(f"Stored {count} company assessments. Run scoring to update ratings.")
    if not _assistant_enabled():
        st.info("Enable the assistant in Settings to assess imported news.")


def main() -> None:
    st.set_page_config(page_title="IndiaGrowthScreener", layout="wide")
    banner()
    update_banner()
    page = st.sidebar.radio("Page", PAGES, key="page")
    sync_panel()
    if page == "News":
        page_news()
        return
    if page == "AI calls":              # needs no score run
        page_calls()
        st.sidebar.caption(DISCLAIMER)
        return
    if page == "Settings":              # needs no score run
        page_settings()
        st.sidebar.caption(DISCLAIMER)
        return
    run = pick_run()
    if run is None:
        return
    {"Rankings": page_rankings, "Stock": page_stock, "Ask": page_ask,
     "Watchlist": page_watchlist, "Saved screens": page_screens,
     "Data quality": page_quality}[page](run)
    st.sidebar.caption(DISCLAIMER)


main()
