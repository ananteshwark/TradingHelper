"""Streamlit UI. Run with `igs ui` (or `streamlit run src/igs/ui/app.py`).

Reads only persisted score runs and point-in-time tables through
igs.service; it computes nothing itself, so what it shows is what was
scored, for a named run and as-of date.
"""

from __future__ import annotations

import polars as pl
import streamlit as st

from igs import service
from igs.db import connect
from igs.guardrails import DISCLAIMER
from igs.score.explain import LABELS, fmt_value
from igs.timeutil import IST
from igs.ui import charts

PAGES = ["Rankings", "Stock", "Watchlist", "Saved screens", "Data quality"]
FLAG_ICON = {"tripped": "⛔ tripped", "clear": "✅ clear", "data_unavailable": "❔ unavailable",
             "not_applicable": "➖ not applicable"}


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


def health_banner(run: dict) -> None:
    issues = run.get("health_issues") or []
    if issues:
        st.error("Run health: High conviction is withheld for this run until these are "
                 "resolved - " + "; ".join(issues), icon="⛔")


def pick_run() -> dict | None:
    runs = service.runs(conn())
    if not runs:
        st.info("No score run yet. Ingest data, run `igs backtest`, then `igs score`.")
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
    if rows:
        df = pl.DataFrame(rows).select(
            pl.col("rank").cast(pl.Utf8).fill_null("-"), "symbol", "name", "tier",
            pl.col("tier_reason").fill_null(""), "composite", "coverage",
            "industry", "bucket", "mcap_cr", "on_watchlist")
        st.dataframe(df, hide_index=True, use_container_width=True, column_config={
            "rank": "Rank",
            "composite": st.column_config.NumberColumn("Composite", format="%+.2f"),
            "coverage": st.column_config.ProgressColumn("Coverage", min_value=0, max_value=1,
                                                        format="percent"),
            "mcap_cr": st.column_config.NumberColumn("Mkt cap (Rs cr)", format="%,.0f"),
            "tier_reason": "Reason", "on_watchlist": "Watchlist"})
        st.download_button("Export CSV", service.rankings_csv(rows), "rankings.csv",
                           "text/csv", key="dl_rankings")
        symbols = [r["symbol"] for r in rows if r["symbol"]]
        pick = st.selectbox("Open stock detail", symbols, key="pick_symbol")
        st.button("Open", key="open_stock", on_click=open_stock, args=(pick,))
    with st.expander("Save these filters as a screen"):
        name = st.text_input("Screen name", key="screen_name")
        if st.button("Save screen", key="save_screen") and name:
            service.screen_save(conn(), name, active)
            st.success(f"Saved screen '{name}'")


def _flags_table(flags: list[dict]) -> None:
    if not flags:
        st.write("None configured.")
        return
    df = pl.DataFrame([{"Check": f.get("label") or f["flag"].replace("_", " "),
                        "Status": FLAG_ICON.get(f["status"], f["status"]),
                        "Evidence": f["message"],
                        "Source": ", ".join(f["source_urls"] or [])} for f in flags])
    st.dataframe(df, hide_index=True, use_container_width=True)


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
    symbol = st.text_input("Symbol", key="stock_sym")
    if not symbol:
        st.info("Enter an NSE symbol or open one from the rankings.")
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
    st.dataframe(pl.DataFrame(top), hide_index=True, use_container_width=True,
                 column_config={"Source filing": st.column_config.LinkColumn("Source filing")})

    st.subheader("Factor breakdown")
    scored = [f for f in d["factors"] if f["contribution"] is not None]
    if scored:
        st.altair_chart(charts.contribution_chart(scored, th), use_container_width=True)
    with st.expander("All factors (table view)", expanded=not scored):
        st.dataframe(pl.DataFrame([{
            "factor": f["factor"], "pillar": f["pillar"], "status": f["status"],
            "value": fmt_value(f["factor"], f["value"]), "z": f["z"],
            "peer percentile": f["peer_percentile"], "contribution": f["contribution"]}
            for f in d["factors"]]), hide_index=True, use_container_width=True)

    st.subheader("Last eight quarters")
    if d["financials_8q"]:
        c1, c2 = st.columns(2)
        c1.altair_chart(charts.financials_chart(d["financials_8q"], th), use_container_width=True)
        if charts.has_margin(d["financials_8q"]):
            c2.altair_chart(charts.margin_chart(d["financials_8q"], th), use_container_width=True)
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
                        use_container_width=True)
        with st.expander("Table view", expanded=True):
            st.dataframe(display(pl.DataFrame(d["shareholding"])), hide_index=True)
    else:
        st.info("No shareholding filings as of this run's date.")

    if d["prices"]:
        st.altair_chart(charts.price_chart(d["prices"], th), use_container_width=True)

    st.subheader("Filings and announcements")
    st.dataframe(pl.DataFrame([{"filed (IST)": f"{f['filed_at'].astimezone(IST):%Y-%m-%d %H:%M}",
                                "kind": f["kind"], "title": f["title"],
                                "link": f["url"] or ""} for f in d["filings"]]),
                 hide_index=True, use_container_width=True,
                 column_config={"link": st.column_config.LinkColumn("link")})


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
        st.dataframe(pl.DataFrame(table), hide_index=True, use_container_width=True)
    else:
        st.info("Nothing on the watchlist yet.")
    with st.form("add_watch"):
        sym = st.text_input("NSE symbol")
        note = st.text_input("Note")
        if st.form_submit_button("Add") and sym:
            try:
                service.watchlist_add(conn(), sym, note)
                st.rerun()
            except service.NotFound as exc:
                st.error(str(exc))
    if items:
        rm = st.selectbox("Remove", [w["symbol"] for w in items], key="rm_watch")
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
                     use_container_width=True)
        st.download_button("Export CSV", service.rankings_csv(rows), f"{name}.csv", "text/csv",
                           key="dl_screen")
    if st.button("Delete screen", key="del_screen"):
        service.screen_delete(conn(), name)
        st.rerun()


def page_quality(run: dict) -> None:
    st.header("Run details and data quality")
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


def main() -> None:
    st.set_page_config(page_title="IndiaGrowthScreener", layout="wide")
    banner()
    page = st.sidebar.radio("Page", PAGES, key="page")
    run = pick_run()
    if run is None:
        return
    {"Rankings": page_rankings, "Stock": page_stock, "Watchlist": page_watchlist,
     "Saved screens": page_screens, "Data quality": page_quality}[page](run)
    st.sidebar.caption(DISCLAIMER)


main()
