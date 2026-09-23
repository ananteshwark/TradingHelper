from __future__ import annotations

import datetime as dt
import json

import db_market
import httpx
import pytest

from igs import service
from igs.alerts import delivery
from igs.alerts.rules import Alert, evaluate, record_new
from igs.config import AlertsConfig, load_alerts, load_scoring, load_universe
from igs.pit import gate
from igs.score.pipeline import score_from_db
from igs.timeutil import IST

pytestmark = pytest.mark.db
EARLIER = dt.datetime(2024, 8, 30, 23, 59, tzinfo=IST)


@pytest.fixture
def two_runs(db_conn, tmp_path, monkeypatch):
    db_market.load(db_conn)
    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "gate.json"))
    (tmp_path / "gate.json").write_text(json.dumps(
        {"fingerprint": gate.code_fingerprint(), "passed_at": "t", "summary": ""}))
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    first, _ = score_from_db(db_conn, EARLIER, None, sc=sc, uc=uc)
    service.watchlist_add(db_conn, "GROW")
    service.watchlist_add(db_conn, "LATE")
    second, _ = score_from_db(db_conn, db_market.AS_OF, None, sc=sc, uc=uc)
    return db_conn, first, second


def _cfg() -> AlertsConfig:
    base = load_alerts()
    rules = {k: dict(v) for k, v in base.rules.items()}
    rules["top_decile_entrants"]["top_pct"] = 34      # tiny synthetic universe
    rules["pledge_changes"]["min_change_pp"] = 1.0   # GROW's pledge rises 1.5 pp a quarter
    return base.model_copy(update={"rules": rules})


def test_rules_fire_on_the_right_events(two_runs):
    conn, first, second = two_runs
    alerts = evaluate(conn, _cfg(), second, first, EARLIER, db_market.AS_OF)
    kinds = {}
    for a in alerts:
        kinds.setdefault(a.kind, []).append(a)
    # LATE's CFO resigned on 2024-10-15: newly tripped between the two runs.
    assert [a.message for a in kinds["watchlist_red_flag"]][0].startswith("Watchlist: LATE")
    # LATE filed September-quarter results on 2024-11-24 (55 days after quarter end).
    assert any("Late Foods" in a.message and "2024-09-30" in a.message
               for a in kinds["watchlist_results"])
    # GROW's promoter pledge moved by 1.5 pp in the latest shareholding filing.
    assert any("Grow Industries" in a.message for a in kinds["pledge_change"])
    assert all(not a.message.lower().startswith(("buy", "sell")) for a in alerts)


def test_alerts_are_delivered_once(two_runs):
    conn, first, second = two_runs
    alerts = evaluate(conn, _cfg(), second, first, EARLIER, db_market.AS_OF)
    assert record_new(conn, alerts, second)
    assert record_new(conn, alerts, second) == []          # dedupe


def test_digest_and_channels(tmp_path, monkeypatch):
    alerts = [Alert("pledge_change", 1, "X: promoter pledge 10.0% -> 13.0%.", "k1"),
              Alert("watchlist_results", 2, "Watchlist: Y filed results.", "k2")]
    sent = []

    class FakeSMTP:
        def __init__(self, host, port):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(msg)

    posts = []

    def handler(req: httpx.Request) -> httpx.Response:
        posts.append(req)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("IGS_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("IGS_ALERT_TO", "me@example.invalid")
    monkeypatch.setenv("IGS_TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("IGS_TELEGRAM_CHAT_ID", "42")
    res = delivery.deliver(alerts, 7, db_market.AS_OF, load_alerts(), tmp_path,
                           smtp_factory=FakeSMTP,
                           telegram_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert res["email"] is True and res["telegram"] is True
    body = sent[0].get_content()
    assert "Promoter pledge changes (1)" in body and "Personal research tool" in body
    assert posts[0].url.path == "/bot123:abc/sendMessage"
    assert (tmp_path / "alerts_run7.txt").read_text() == delivery.digest(alerts, 7,
                                                                           db_market.AS_OF)


def test_no_channel_configured_writes_file_only(tmp_path, monkeypatch):
    for var in ("IGS_SMTP_HOST", "IGS_TELEGRAM_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    res = delivery.deliver([Alert("pledge_change", 1, "X changed.", "k")], 1, db_market.AS_OF,
                           load_alerts(), tmp_path)
    assert res["email"] is False and res["telegram"] is False
    assert (tmp_path / "alerts_run1.txt").exists()


def test_daily_job_reports_every_failed_step(db_conn, tmp_path, monkeypatch):
    """With no verified sources and no gate, every ingest step fails loudly, the score step
    fails on the gate, and the job says so instead of pretending to have run."""
    from igs.config import load_sources
    from igs.daily import run_daily
    from igs.ingest.http import Fetcher
    from igs.ingest.jobs import Context
    from igs.ingest.raw_store import RawStore

    monkeypatch.setenv("IGS_GATE_PATH", str(tmp_path / "no_gate.json"))
    store = RawStore(tmp_path / "raw")
    ctx = Context(conn=db_conn, store=store, sources=load_sources(), fetcher=Fetcher(store))
    rep = run_daily(ctx, dt.date(2024, 11, 29), None, tmp_path / "reports")
    failed = dict((n, s) for n, st, s in rep.steps if st == "failed")
    for step in ("equity list", "prices, delivery, index closes", "corporate actions",
                 "results listing"):
        assert "SourceNotVerified" in failed[step], (step, failed.get(step))
    assert "GateError" in failed["score"]
    assert "alerts" in failed and rep.run_id is None
