"""Quarterly reports read from their figures; only positive reads reach Telegram
(igs.results_read, igs.alerts.operations.quarterly)."""
import datetime as dt

import pytest

from igs import results_read as rr
from igs.alerts import operations as ops

RULE = dict(rr.DEFAULTS)
Q = dt.date(2026, 6, 30)
CR = 1e7


def quarters(revenue: list[float], profit: list[float], concept: str = "pat") -> dict:
    """Figures for the quarters to Q, oldest first, in crore."""
    n = max(len(revenue), len(profit))
    out = {}
    for i in range(n):
        day = rr.quarter_back(Q, n - 1 - i)
        out[day] = {}
        if i >= n - len(revenue):
            out[day]["revenue"] = revenue[i - (n - len(revenue))] * CR
        if i >= n - len(profit):
            out[day][concept] = profit[i - (n - len(profit))] * CR
    return out


def pair(revenue: tuple[float, float], profit: tuple[float, float]) -> dict:
    """The quarter and the same quarter a year earlier, in crore, and nothing before."""
    then = rr.quarter_back(Q, 4)
    return {then: {"revenue": revenue[0] * CR, "pat": profit[0] * CR},
            Q: {"revenue": revenue[1] * CR, "pat": profit[1] * CR}}


# Twelve quarters of profit up about 9 crore on a year earlier each quarter.
STEADY = [100, 103, 104, 108, 109, 112, 114, 117, 118, 121, 124, 126]


@pytest.mark.parametrize("day, back, expected", [
    (dt.date(2026, 6, 30), 4, dt.date(2025, 6, 30)),
    (dt.date(2026, 3, 31), 1, dt.date(2025, 12, 31)),
    (dt.date(2025, 12, 31), 1, dt.date(2025, 9, 30)),
    (dt.date(2026, 6, 30), 12, dt.date(2023, 6, 30))])
def test_a_quarter_back(day, back, expected):
    assert rr.quarter_back(day, back) == expected


def test_a_strong_quarter_reads_positive():
    q = quarters([1000] * 12 + [1150], STEADY + [160])
    r = rr.judge(q, Q, RULE)
    assert r["verdict"] == "positive"
    assert r["revenue_yoy_pct"] == 15.0
    assert r["profit_yoy_pct"] == pytest.approx(100 * (160 / 118 - 1), abs=0.01)
    assert r["surprise"] > 5            # up 42 crore, against 9 or 10 crore before
    assert any("surprise" in reason for reason in r["reasons"])


def test_growth_the_profit_usually_shows_is_not_a_surprise():
    """Profit up 20% and revenue up 15%, but its year-on-year change swings by more than
    that: mixed."""
    jumpy = [100, 60, 140, 80, 150, 50, 170, 70, 150, 40, 190, 100]
    q = quarters([1000] * 12 + [1150], jumpy + [180])
    r = rr.judge(q, Q, RULE)
    assert r["profit_yoy_pct"] == pytest.approx(20.0)
    assert 0 < r["surprise"] < 1
    assert r["verdict"] == "mixed"


def test_without_enough_history_growth_alone_decides():
    r = rr.judge(pair((1000, 1100), (100, 130)), Q, RULE)
    assert r["surprise"] is None and r["verdict"] == "positive"
    assert any("fewer than 4" in reason for reason in r["reasons"])
    assert rr.judge(pair((1000, 1030), (100, 130)), Q, RULE)["verdict"] == "mixed"


def test_profit_growth_below_its_usual_change_is_a_negative_surprise():
    q = quarters([1000] * 12 + [1200], STEADY + [124])      # +5 crore against about +9
    r = rr.judge(q, Q, RULE)
    assert r["surprise"] < -1 and r["verdict"] == "negative"


@pytest.mark.parametrize("profit, verdict", [
    ([100, 90], "negative"),          # profit fell
    ([100, -5], "negative"),          # swung to a loss
    ([-20, -5], "mixed"),             # a loss that narrowed
    ([-20, -30], "negative"),         # a loss that widened
    ([-20, 15], "positive")])         # a loss turned into a profit
def test_falls_and_losses(profit, verdict):
    assert rr.judge(pair((1000, 1200), profit), Q, RULE)["verdict"] == verdict


def test_unknown_without_the_year_earlier_quarter():
    q = {Q: {"revenue": 1e10, "pat": 1e9}}
    r = rr.judge(q, Q, RULE)
    assert r["verdict"] == "unknown" and r["revenue"] == 1e10
    assert rr.judge({}, Q, RULE)["verdict"] == "unknown"


def test_owners_profit_is_used_only_when_both_years_have_it():
    q = pair((1000, 1200), (100, 130))
    q[Q]["pat_owners"] = 300 * CR             # this year only: not compared with pat
    assert rr.judge(q, Q, RULE)["profit"] == 130 * CR
    q[rr.quarter_back(Q, 4)]["pat_owners"] = 280 * CR
    assert rr.judge(q, Q, RULE)["profit"] == 300 * CR


def test_the_thresholds_are_settings():
    q = pair((1000, 1100), (100, 130))
    assert rr.judge(q, Q, {**RULE, "min_revenue_growth_pct": 12})["verdict"] == "mixed"
    assert rr.judge(q, Q, {**RULE, "min_profit_growth_pct": 35})["verdict"] == "mixed"


def test_the_message_gives_the_figures_and_the_record():
    q = quarters([1000] * 12 + [1150], STEADY + [160])
    result = {**rr.judge(q, Q, RULE), "basis": "consolidated"}
    payload = {"company": "Acme Ltd", "company_id": 1, "period_end": "2026-06-30",
               "basis": "consolidated", "exchange": "NSE",
               "filed_at": "2026-08-10T12:30:00+00:00"}
    text = rr.message(payload, result, "ACME", {"n": 4, "up": 3, "mean": 1.25})
    assert text.startswith("RESULTS READ POSITIVE\nAcme Ltd (ACME)")
    assert "Quarter ended 30 Jun 2026, consolidated" in text
    assert "Revenue Rs 1,150.0 crore, +15.0% on the same quarter last year" in text
    assert "Profit Rs 160.0 crore, +35.6%" in text
    assert "Filed on NSE at 18:00 IST, 10 Aug 2026" in text
    assert "of the last 4 positive reads" in text and "+1.2% against it" in text
    assert "No record yet" in rr.message(payload, result, None, {"n": 0})
    assert "below its one-year high" not in text
    fallen = rr.message(payload, result, "ACME", None, below_high=-40.0)
    assert "The stock is 40% below its one-year high: if the business is turning, this " \
        "may be near a bottom." in fallen
    assert "below its one-year high" not in rr.message(payload, result, "ACME", None,
                                                       below_high=-20.0)


# --------------------------------------------------------------------------- database


def _fetch(conn) -> str:
    conn.execute("""insert into raw_payload (fetch_id, source_id, fetched_at, content_sha256,
                        size_bytes, blob_path, origin)
                    values ('f', 's', now(), repeat('a', 64), 1, 'blobs/aa/a', 'http')
                    on conflict do nothing""")
    return "f"


def _company(conn, name: str, symbol: str, isin: str) -> int:
    cid = conn.execute("insert into company (name) values (%s) returning company_id",
                       (name,)).fetchone()[0]
    sid = conn.execute("insert into security (company_id) values (%s) returning security_id",
                       (cid,)).fetchone()[0]
    for kind, value in (("NSE_SYMBOL", symbol), ("ISIN", isin)):
        conn.execute("""insert into security_identifier (security_id, id_type, id_value,
                            valid_from, evidence) values (%s, %s, %s, '2020-01-01', 'test')""",
                     (sid, kind, value))
    return cid


def _filing(conn, cid: int, basis: str, filed_at: str, period_end, figures: dict,
            exchange: str = "NSE", sha: str = "b") -> None:
    """A results filing for `period_end` (None: history only, which notifies nothing) with
    `figures` {day: {concept: value}}."""
    fid = conn.execute("""insert into filing (company_id, exchange, filing_system, filing_type,
                              period_end, statement_basis, filed_at, ingested_at,
                              content_sha256, source_fetch_id)
                          values (%s, %s, 'test', 'financial_results', %s, %s, %s, now(),
                                  repeat(%s, 64), %s) returning filing_id""",
                       (cid, exchange, period_end, basis, filed_at, sha, _fetch(conn))
                       ).fetchone()[0]
    for day, values in figures.items():
        for concept, value in values.items():
            conn.execute("""insert into fundamental_fact (filing_id, company_id, statement_basis,
                                period_end, period_type, concept, source_element, value, unit,
                                filed_at, ingested_at)
                            values (%s, %s, %s, %s, 'Q', %s, 'x', %s, 'INR', %s, now())""",
                         (fid, cid, basis, day, concept, value, filed_at))


FILED = "2026-08-10 18:00+05:30"


def _report(conn, cid: int, basis: str, figures: dict, **kw) -> None:
    """History in one filing, then the quarter's results with last year's quarter."""
    then = rr.quarter_back(Q, 4)
    _filing(conn, cid, basis, "2026-05-01 18:00+05:30", None,
            {d: v for d, v in figures.items() if d not in (Q, then)}, sha=kw.pop("hsha", "c"))
    _filing(conn, cid, basis, FILED, Q, {d: figures[d] for d in (Q, then)}, **kw)


def _sessions(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


@pytest.mark.db
def test_only_positive_results_reach_telegram_once_a_quarter(db_conn, monkeypatch):
    c = db_conn
    acme = _company(c, "Acme Ltd", "ACME", "INE000A01011")
    dull = _company(c, "Dull Ltd", "DULL", "INE000A01029")
    strong = quarters([1000] * 12 + [1150], STEADY + [160])
    _report(c, acme, "consolidated", strong, sha="d")
    _report(c, acme, "standalone", strong, sha="e", hsha="f")            # the same quarter
    _report(c, dull, "consolidated", quarters([1000] * 12 + [1010], STEADY + [128]),
            sha="g", hsha="h")                      # revenue +1%, profit +8%: mixed
    c.commit()
    assert c.execute("select count(*) from operational_notification "
                     "where kind = 'quarterly_report'").fetchone()[0] == 3
    sent = []
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert len(sent) == 1 and sent[0].startswith("RESULTS READ POSITIVE\nAcme Ltd (ACME)")
    assert "No record yet" in sent[0]
    reads = c.execute("""select company_id, basis, verdict, sent from results_read
                         order by notification_id""").fetchall()
    assert reads == [(acme, "consolidated", "positive", True),
                     (acme, "standalone", "positive", False),
                     (dull, "consolidated", "mixed", False)]
    assert c.execute("""select skipped from operational_notification
                        where kind = 'quarterly_report' order by notification_id""").fetchall() \
        == [(None,), ("already sent for this quarter",), ("results read mixed",)]
    assert c.execute("""select count(*) from operational_notification
                        where kind = 'quarterly_report' and sent_at is null
                          and skipped is null""").fetchone()[0] == 0
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 0      # nothing again

    # The record: Acme's close the session before (Mon 10 Aug, filed after the close) to
    # the next session and 21 on, against the Nifty 500.
    days = _sessions(dt.date(2026, 8, 7), 24)
    for i, day in enumerate(days):
        price = 100.0 if i < 2 else 105.0 + i
        c.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series, close,
                         prev_close, source_fetch_id)
                     values ('NSE', %s, 'INE000A01011', 'ACME', 'EQ', %s, %s, 'f')""",
                  (day, price, None))
        c.execute("""insert into index_price (index_name, trade_date, close, is_total_return,
                         source_fetch_id) values ('Nifty 500', %s, %s, false, 'f')""",
                  (day, 1000.0 if i < 2 else 1010.0))
    c.commit()
    history = rr.history(c)
    acme_read = next(r for r in history if r["company_id"] == acme)
    assert acme_read["sent"] and acme_read["symbol"] == "ACME"
    assert acme_read["moves"]["next_session"] == pytest.approx((7.0, 6.0))
    assert acme_read["moves"]["sessions_21"][0] == pytest.approx(100 * (105 + 22) / 100 - 100)
    assert rr.positive_so_far(c) == {"n": 1, "up": 1, "mean": pytest.approx(6.0)}
    summary = rr.summary(history)
    assert summary["mixed"]["n"] == 0              # Dull has no prices

    # With only_positive off, every report goes, with its read.
    monkeypatch.setattr(rr, "rule", lambda cfg=None: {**RULE, "only_positive": False})
    _report(c, dull, "standalone", quarters([1000] * 12 + [1010], STEADY + [128]),
            sha="i", hsha="j")
    c.commit()
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert sent[-1].startswith("NEW QUARTERLY REPORT\nDull Ltd (DULL)")
    assert "Read mixed by rule" in sent[-1]
    assert "of the last 1 positive reads" in sent[-1]


@pytest.mark.db
def test_a_failed_send_reads_the_report_again(db_conn):
    c = db_conn
    acme = _company(c, "Acme Ltd", "ACME", "INE000A01011")
    _report(c, acme, "consolidated", quarters([1000] * 12 + [1150], STEADY + [160]))
    c.commit()
    assert ops.deliver_database(c, lambda t: False) == 0
    assert c.execute("select count(*) from results_read").fetchone()[0] == 0
    assert c.execute("""select last_error, sent_at, skipped from operational_notification
                        where kind = 'quarterly_report'""").fetchone() == \
        ("RuntimeError", None, None)
    c.execute("update operational_notification set next_attempt_at = now()")
    c.commit()
    sent = []
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert c.execute("select verdict, sent from results_read").fetchone() == ("positive", True)


@pytest.mark.db
def test_the_read_uses_only_figures_filed_by_then(db_conn):
    """A later filing's restated figures don't change how a report read when it was filed."""
    c = db_conn
    acme = _company(c, "Acme Ltd", "ACME", "INE000A01011")
    _report(c, acme, "consolidated", quarters([1000] * 12 + [1150], STEADY + [160]))
    _filing(c, acme, "consolidated", "2026-11-10 18:00+05:30", dt.date(2026, 9, 30),
            {Q: {"pat": 50 * CR}}, sha="k")
    filed = dt.datetime.fromisoformat("2026-08-10T18:00+05:30")
    assert rr.read(c, acme, Q, "consolidated", filed, RULE)["verdict"] == "positive"
    assert rr.read(c, acme, Q, "consolidated", None, RULE)["verdict"] == "negative"
    assert rr.read(c, acme, Q, None, filed, RULE)["basis"] == "consolidated"


@pytest.mark.db
def test_the_results_days_page_shows_the_reads(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    c = db_conn
    acme = _company(c, "Acme Ltd", "ACME", "INE000A01011")
    _report(c, acme, "consolidated", quarters([1000] * 12 + [1150], STEADY + [160]))
    c.commit()
    ops.deliver_database(c, lambda t: True)
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.results_days import page
page(connect(autocommit=True))''', default_timeout=30).run()
    assert not at.exception, at.exception
    assert "Read positive: vs Nifty 500, next session" in [m.label for m in at.metric]
    reads = at.dataframe[-1].value
    assert list(reads["stock"]) == ["ACME"] and list(reads["read"]) == ["positive"]
    assert list(reads["sent"]) == [True]


@pytest.mark.db
def test_positive_results_on_a_fallen_stock_say_it_may_be_a_bottom(db_conn):
    """Acme closed at 200 for 100 sessions, then at 120: 40% below its one-year high when
    its strong quarter is filed."""
    c = db_conn
    acme = _company(c, "Acme Ltd", "ACME", "INE000A01011")
    _report(c, acme, "consolidated", quarters([1000] * 12 + [1150], STEADY + [160]))
    for i, day in enumerate(_sessions(dt.date(2025, 8, 4), 260)):
        if day > dt.date(2026, 8, 10):
            break
        c.execute("""insert into price_eod (exchange, trade_date, isin, symbol, series, close,
                         prev_close, source_fetch_id)
                     values ('NSE', %s, 'INE000A01011', 'ACME', 'EQ', %s, null, 'f')""",
                  (day, 200.0 if i < 100 else 120.0))
    c.commit()
    filed = dt.datetime.fromisoformat("2026-08-10T18:00+05:30")
    assert rr.below_high(c, acme, filed) == -40.0
    sent = []
    assert ops.deliver_database(c, lambda t: sent.append(t) or True) == 1
    assert "The stock is 40% below its one-year high" in sent[0]
    rows = rr.history(c)
    assert rows[0]["below_high_pct"] == -40.0 and rr.fallen(rows) == rows
