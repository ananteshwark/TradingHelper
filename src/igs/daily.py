"""The daily job: ingest -> instrument master -> score -> alerts.

Every step runs even if an earlier one failed (a stale-but-consistent score is
more useful than none), and every failure is reported in the job summary and
in the alert digest. Scheduling is left to cron (see scripts/crontab.example).
"""

from __future__ import annotations

import datetime as dt
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from igs.alerts.delivery import configured_channels, deliver, deliver_pending
from igs.alerts.rules import evaluate, record_new
from igs.config import load_alerts, load_sync
from igs.ingest import jobs
from igs.score.pipeline import score_from_db
from igs.sync import _summary, run_sync
from igs.timeutil import end_of_day_ist


@dataclass
class DailyReport:
    day: dt.date
    steps: list[tuple[str, str, str]] = field(default_factory=list)
    run_id: int | None = None
    alerts_sent: int = 0

    @property
    def failed(self) -> list[str]:
        return [name for name, status, _ in self.steps if status == "failed"]


def _step(report: DailyReport, ctx: jobs.Context, name: str, fn: Callable[[], object]) -> object:
    try:
        result = fn()
        ctx.conn.commit()
        report.steps.append((name, "ok", _summary(result)))
        return result
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none is swallowed
        ctx.conn.rollback()
        report.steps.append((name, "failed", f"{type(exc).__name__}: {exc}"))
        ctx.dq.emit("error", "daily_step_failed", f"{name}: {exc}",
                    details={"traceback": traceback.format_exc()[-2000:]})
        return None


def run_daily(ctx: jobs.Context, day: dt.date, ic_status_path: Path | None,
              reports_dir: Path) -> DailyReport:
    rep = DailyReport(day=day)
    s = lambda name, fn: _step(rep, ctx, name, fn)  # noqa: E731
    # The ingest half is a check like `igs sync`: it waits for a running check instead of
    # skipping, ignores the minimum interval, and asks for prices up to `day`.
    checked = run_sync(ctx, "daily", load_sync(), force=True, wait=True, day=day,
                       prices_to=day)
    rep.steps.extend(checked.steps)

    def score() -> str:
        run_id, run = score_from_db(ctx.conn, end_of_day_ist(day), ic_status_path)
        rep.run_id = run_id
        return f"run {run_id}"
    s("score", score)

    def notes() -> str:
        from igs.config import load_assistant
        if not load_assistant().enabled:
            return "assistant off"
        from igs.assistant.announcements import read_new
        from igs.assistant.llm import Assistant
        r = read_new(Assistant.open(ctx.conn))
        return (f"read {r.read} announcements, stored {r.stored} notes, ~${r.cost_usd:.3f}"
                + (f"; {len(r.issues)} issues: {'; '.join(r.issues[:3])}" if r.issues else ""))
    s("announcement notes (assistant)", notes)

    def alerts() -> str:
        if rep.run_id is None:
            raise RuntimeError("no score run today; alerts not evaluated")
        return send_alerts(ctx.conn, rep.run_id, reports_dir, rep)
    s("alerts", alerts)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    return rep


def send_alerts(conn, run_id: int, reports_dir: Path, rep: DailyReport | None = None) -> str:
    cfg = load_alerts()
    with conn.cursor() as cur:
        cur.execute("select run_id, as_of from score_run where run_id <= %s "
                    "order by run_id desc limit 2", (run_id,))
        rows = cur.fetchall()
    as_of = rows[0][1]
    prev_id, since = (rows[1][0], rows[1][1]) if len(rows) > 1 else \
        (None, as_of - dt.timedelta(days=1))
    alerts = evaluate(conn, cfg, run_id, prev_id, since, as_of)
    if rep is not None and rep.failed:
        from igs.alerts.rules import Alert
        alerts.append(Alert("daily_failures", None,
                           f"Daily job steps failed: {', '.join(rep.failed)}; scores may be "
                           "based on stale data.", f"daily_failures:{run_id}"))
    channels = configured_channels(cfg)
    fresh = record_new(conn, alerts, run_id, channels)
    result = deliver(fresh, run_id, as_of, cfg.model_copy(update={"channels": {}}),
                     reports_dir / "alerts")
    result.update(deliver_pending(conn, cfg))
    if rep is not None:
        rep.alerts_sent = len(fresh)
    return f"{len(fresh)} new alerts; " + ", ".join(f"{k}={v}" for k, v in result.items())
