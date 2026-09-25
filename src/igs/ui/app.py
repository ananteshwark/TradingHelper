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

PAGES = ["Rankings", "Stock", "Ask", "Watchlist", "Saved screens", "Data quality", "Settings"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LOCAL_ADDRESSES = ("127.0.0.1", "localhost", "::1")
AI_NOTE = ("Written by the optional research assistant (Claude) from this run's stored data. "
           "It is not used in ranking and doesn't make recommendations; check the filings.")
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
    if "ic_status_generated_at" in run and not run["ic_status_generated_at"]:
        st.warning("Not yet validated: no backtest has measured these factors on real data. "
                   "The weights are starting assumptions taken from published Indian "
                   "evidence, so read the tiers as hypotheses to check, not findings.",
                   icon="🧪")


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
    _brief_panel(co["symbol"], run)

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
        st.caption("A factor with a z-score but no contribution is tracked at weight 0 "
                   "(no Indian evidence yet that it predicts returns); the backtest still "
                   "measures it.")
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
    _notes_table(co["company_id"], run)
    _insider_table(co["symbol"], run)


def _insider_table(symbol: str, run: dict) -> None:
    trades = service.insider_trades(conn(), symbol, run["as_of"])
    st.subheader("Insider trades (SEBI PIT), last 12 months")
    if not trades:
        st.caption("No insider-trading disclosures in the last 12 months, or none loaded yet "
                   "(`igs ingest range nse_insider_trading`).")
        return
    st.caption("Disclosed under SEBI's insider-trading rules and dated by the exchange "
               "broadcast. Open-market purchases of equity by promoters, directors and key "
               "managers feed the ownership pillar; other trades are shown for context.")
    st.dataframe(pl.DataFrame([{
        "broadcast (IST)": f"{t['filed_at'].astimezone(IST):%Y-%m-%d %H:%M}",
        "person": t["person_name"], "category": t["person_category"],
        "type": {"buy": "acquired", "sell": "disposed of"}.get(t["side"],
                                                               t["transaction_type"]),
        "mode": t["acquisition_mode"],
        "security": t["security_type"], "quantity": t["quantity"],
        "value (Rs cr)": None if t["value_inr"] is None else round(t["value_inr"] / 1e7, 2),
        "holding after %": t["holding_after_pct"],
        "counts": "yes" if (t["side"] == "buy" and t["open_market"]
                            and t["insider_role"] != "other"
                            and (t["security_type"] or "").lower().startswith("equity"))
        else ""} for t in trades]),
        hide_index=True, use_container_width=True)


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
        hide_index=True, use_container_width=True)


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
            hide_index=True, use_container_width=True)
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
    st.subheader("Research assistant (AI)")
    st.caption("Optional. It answers questions about a run, writes plain-language briefs and "
               "reads new announcements, using the Claude API (billed per use). It never "
               "affects rankings. Saved changes are kept in "
               f"`{settings_dir() / 'assistant.yaml'}` on top of `config/assistant.yaml`.")
    try:
        cfg = load_assistant()
    except (ValidationError, ValueError) as exc:
        st.error(f"The assistant settings are invalid: {exc}")
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
        budget = c2.number_input("Daily budget (USD)", min_value=0.0, max_value=1000.0,
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
        saved = st.form_submit_button("Save settings", disabled=not local)
    if saved and local:
        values = {"enabled": enabled, "model": model, "daily_budget_usd": float(budget),
                  "fallbacks": "default" if fallbacks else None,
                  "features": {
                      "ask": {"effort": ask_effort, "max_tool_rounds": int(ask_rounds)},
                      "brief": {"effort": brief_effort},
                      "announcements": {"effort": ann_effort, "days": int(ann_days),
                                        "max_per_run": int(ann_max), "scope": ann_scope}}}
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
    _usage_panel(cfg.daily_budget_usd)
    st.caption(DISCLAIMER)


def main() -> None:
    st.set_page_config(page_title="IndiaGrowthScreener", layout="wide")
    banner()
    page = st.sidebar.radio("Page", PAGES, key="page")
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
