"""Loopback-only local analysis and model training console."""

from __future__ import annotations

import datetime as dt
import json
import statistics
import subprocess

import polars as pl
import streamlit as st

from igs.local_research import jobs
from igs.local_research.sync import DB, STATE, connection

SYNC_UNIT = "tradinghelper-research-sync.service"


@st.cache_data(ttl=300, max_entries=2)
def paper_rows() -> list[dict]:
    queries = (
        ("Morning ML", "ml_intraday_pick", "session_date"),
        ("Evening ML", "evening_pick", "signal_date"),
        ("Spike ML", "intraday_spike_ml", "trade_date"),
    )
    rows = []
    with connection(DB) as conn:
        for track, table, date in queries:
            if table == "intraday_spike_ml":
                q = (
                    f"select {date}, symbol, case when side=-1 then 1 else -1 end, "
                    f"net_inr::float8, features from {table} where status='closed' "
                    "and takes is true and net_inr is not null"
                )
            else:
                q = (
                    f"select {date}, symbol, side, net_inr::float8, features "
                    f"from {table} where status='closed' and net_inr is not null"
                )
            for day, symbol, side, net, features in conn.execute(q):
                rows.append(
                    {
                        "track": track,
                        "date": str(day),
                        "symbol": symbol,
                        "side": "Buy" if side == 1 else "Short",
                        "net_inr": net,
                        "features": features or {},
                    }
                )
    return rows


def differences(rows: list[dict], track: str, side: str) -> list[dict]:
    selected = [r for r in rows if r["track"] == track and r["side"] == side]
    names = sorted(
        {
            name
            for row in selected
            for name, value in row["features"].items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    )
    out = []
    for name in names:
        wins = [
            float(r["features"][name])
            for r in selected
            if r["net_inr"] > 0 and isinstance(r["features"].get(name), (int, float))
        ]
        losses = [
            float(r["features"][name])
            for r in selected
            if r["net_inr"] <= 0 and isinstance(r["features"].get(name), (int, float))
        ]
        if len(wins) < 3 or len(losses) < 3:
            continue
        out.append(
            {
                "Parameter": name,
                "Winning calls": len(wins),
                "Losing calls": len(losses),
                "Winner median": statistics.median(wins),
                "Loser median": statistics.median(losses),
                "Difference": statistics.median(wins) - statistics.median(losses),
            }
        )
    return sorted(out, key=lambda r: r["Parameter"])


def _status() -> dict:
    path = STATE / "status.json"
    return json.loads(path.read_text()) if path.is_file() else {"state": "not synced"}


def sync_service_state() -> str:
    """The one local transfer service owns both scheduled and manual refreshes."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", SYNC_UNIT, "--property=ActiveState", "--value"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def start_sync() -> None:
    """Queue a sync through systemd so manual clicks share its lock and logs."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", "--no-block", SYNC_UNIT],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not start the sync service: {exc}") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not start the sync service")


def _jobs() -> list[dict]:
    return (
        [
            json.loads(path.read_text())
            for path in sorted(jobs.JOBS.glob("*.json"), reverse=True)[:12]
        ]
        if jobs.JOBS.exists()
        else []
    )


@st.cache_data(ttl=300, max_entries=8)
def dataset_coverage(name: str) -> dict:
    dataset = jobs.artifact("features", name)
    manifest = json.loads((dataset / "manifest.json").read_text())
    labels = pl.read_parquet(dataset / "labels.parquet", columns=["as_of", "target"])
    matured = labels.filter(pl.col("target").is_not_null())["as_of"]
    return {
        "rows": manifest["rows"],
        "labelled_rows": manifest["labelled_rows"],
        "labelled_dates": matured.n_unique(),
        "first_labelled_date": str(matured.min()) if len(matured) else None,
        "last_labelled_date": str(matured.max()) if len(matured) else None,
        "benchmark_warnings": manifest.get("benchmark_warnings", []),
    }


def main() -> None:
    st.set_page_config(page_title="TradingHelper research", layout="wide")
    st.title("Local research")
    st.caption(
        "Server data is copied here for analysis. Training runs locally; "
        "new models are candidates until independent after-cost evidence supports a change."
    )
    state = _status()
    service_state = sync_service_state()
    running = service_state in {"active", "activating"}
    with st.container(horizontal=True):
        st.metric("Mirror", state.get("state", "unknown"), border=True)
        st.metric("Last sync (UTC)", state.get("synced_at", "—")[:19], border=True)
        st.metric("Database tables", state.get("tables", "—"), border=True)
    with st.container(horizontal=True):
        if st.button("Sync from server now", key="manual_sync", disabled=running):
            try:
                start_sync()
            except RuntimeError as exc:
                st.error(str(exc))
            else:
                st.success("Sync started. Refresh the status here to see when it finishes.")
        st.button("Refresh sync status", key="refresh_sync_status")
    if running:
        st.info("Server data sync is running. The previous mirror remains available until "
                "it finishes.")
    if state.get("last_sync_error"):
        st.warning(f"The previous refresh failed: {state['last_sync_error']}")
    if state.get("state") != "ready":
        st.warning("The local database is not ready. Check the research sync service log.")
        return
    if st.session_state.get("mirror_synced_at") != state.get("synced_at"):
        paper_rows.clear()
        st.session_state["mirror_synced_at"] = state.get("synced_at")
    st.subheader("Paper-call outcomes")
    try:
        rows = paper_rows()
    except Exception as exc:
        st.error(f"Could not read local mirror: {exc}")
        return
    summary = []
    for track in ("Morning ML", "Evening ML", "Spike ML"):
        for side in ("Buy", "Short"):
            part = [r for r in rows if r["track"] == track and r["side"] == side]
            if part:
                summary.append(
                    {
                        "Track": track,
                        "Side": side,
                        "Closed": len(part),
                        "Winners": sum(r["net_inr"] > 0 for r in part),
                        "Net ₹": round(sum(r["net_inr"] for r in part), 2),
                        "Days": len({r["date"] for r in part}),
                    }
                )
    st.dataframe(summary, hide_index=True)
    track = st.selectbox("Track", ("Morning ML", "Evening ML", "Spike ML"))
    side = st.selectbox("Side", ("Buy", "Short"))
    st.dataframe(differences(rows, track, side), hide_index=True)
    st.caption(
        "These are descriptive medians across completed calls. Check each date, "
        "missing features and later outcomes before proposing a filter."
    )

    st.subheader("Train a research model")
    st.caption(
        "The existing point-in-time feature exporter, look-ahead gate and chronological "
        "model trainer run as background jobs. Results stay in reports/local-research."
    )
    if st.button("Run look-ahead gate"):
        st.success(f"Started gate job {jobs.launch('gate')}")
    today = dt.datetime.now(dt.UTC).date()
    with st.form("features"):
        start = st.date_input("First signal date", today - dt.timedelta(days=730))
        end = st.date_input("Last signal date", today - dt.timedelta(days=30))
        horizon = st.number_input("Future label horizon (sessions)", 1, 756, 21)
        frequency = st.selectbox("Signal frequency", ("monthly", "weekly", "daily"))
        if st.form_submit_button("Export dated feature dataset"):
            job_id = jobs.launch(
                "features", start=start, end=end, horizon=horizon, frequency=frequency
            )
            st.success(f"Started export job {job_id}")
    datasets = sorted(
        path.name
        for path in (jobs.ARTIFACTS / "features").glob("*")
        if (path / "manifest.json").is_file()
    )
    if datasets:
        with st.form("training"):
            dataset = st.selectbox("Feature dataset", datasets)
            coverage = dataset_coverage(dataset)
            st.caption(
                f"{coverage['labelled_rows']:,} labelled rows on "
                f"{coverage['labelled_dates']} signal dates "
                f"({coverage['first_labelled_date']} to {coverage['last_labelled_date']})."
            )
            if coverage["labelled_dates"] < 16:
                st.warning(
                    "Training needs at least 12 earlier dates and two dates in each "
                    "evaluation block. This dataset has too few labelled dates."
                )
            if coverage["benchmark_warnings"]:
                st.warning("Benchmark coverage: " + "; ".join(coverage["benchmark_warnings"]))
            validation = st.date_input("Validation starts", today - dt.timedelta(days=365))
            holdout = st.date_input("Holdout starts", today - dt.timedelta(days=180))
            algorithm = st.selectbox("Algorithm", ("ridge", "lightgbm"))
            if st.form_submit_button(
                "Train candidate model", disabled=coverage["labelled_dates"] < 16
            ):
                job_id = jobs.launch(
                    "train",
                    dataset=dataset,
                    validation=validation,
                    holdout=holdout,
                    algorithm=algorithm,
                )
                st.success(f"Started training job {job_id}")
    else:
        st.info("Export a feature dataset before training.")
    models = sorted(
        path.name
        for path in (jobs.ARTIFACTS / "train").glob("*")
        if (path / "manifest.json").is_file()
    )
    if models and datasets:
        with st.form("predictions"):
            model = st.selectbox("Candidate model", models)
            later_dataset = st.selectbox("Later feature dataset", datasets)
            if st.form_submit_button("Score later paper calls"):
                job_id = jobs.launch("predict", model=model, dataset=later_dataset)
                st.success(f"Started prediction job {job_id}")
    predictions = sorted(
        path.name
        for path in (jobs.ARTIFACTS / "predict").glob("*")
        if (path / "manifest.json").is_file()
    )
    if predictions and datasets:
        with st.form("portfolio"):
            prediction = st.selectbox("Prediction set", predictions)
            portfolio_dataset = st.selectbox("Matching feature dataset", datasets)
            portfolio_manifest = json.loads(
                (jobs.artifact("features", portfolio_dataset) / "manifest.json").read_text()
            )
            if portfolio_manifest["frequency"] != "monthly":
                st.warning("The costed portfolio test currently supports monthly signals only.")
            if st.form_submit_button(
                "Test after costs", disabled=portfolio_manifest["frequency"] != "monthly"
            ):
                job_id = jobs.launch("portfolio", predictions=prediction, dataset=portfolio_dataset)
                st.success(f"Started costed portfolio job {job_id}")
    st.subheader("Recent jobs")
    for item in _jobs():
        with st.expander(f"{item['action']} · {item['job_id']} · {item['state']}"):
            st.json(item)
            log = jobs.JOBS / f"{item['job_id']}.log"
            if log.is_file():
                st.code(log.read_text()[-10000:])
            result = jobs.ARTIFACTS / item["action"] / item["job_id"]
            for filename in ("metrics.json", "drift.json", "summary.json"):
                report = result / filename
                if report.is_file():
                    st.caption(filename)
                    st.json(json.loads(report.read_text()))


if __name__ == "__main__":
    main()
