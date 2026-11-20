"""igs.results_drift: results reactions on paper, the buys after +5% and the flags after -5%."""
import datetime as dt

import polars as pl
import pytest

from igs import momentum, results_drift
from igs.timeutil import IST

D = dt.date


def at(day: D, hour: int = 20, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), IST)


def test_next_sessions_use_known_days_then_weekdays_without_holidays():
    known = [D(2024, 10, 21), D(2024, 10, 22), D(2024, 10, 23)]
    assert results_drift.next_sessions(D(2024, 10, 21), 2, known, set()) == [
        D(2024, 10, 22), D(2024, 10, 23)]
    # Past the known days: weekdays, skipping the weekend and a listed holiday.
    assert results_drift.next_sessions(D(2024, 10, 22), 4, known, {D(2024, 10, 28)}) == [
        D(2024, 10, 23), D(2024, 10, 24), D(2024, 10, 25), D(2024, 10, 29)]


START = D(2024, 10, 1)


def _px(rows: dict[int, list[tuple[float, float]]]) -> pl.DataFrame:
    """Company -> [(close, turnover)] on consecutive days from 1 October 2024."""
    out = []
    for cid, series in rows.items():
        for i, (close, turnover) in enumerate(series):
            out.append({"company_id": cid, "symbol": f"S{cid}",
                        "trade_date": START + dt.timedelta(days=i), "adj_open": close,
                        "adj_close": close, "turnover": turnover})
    return pl.DataFrame(out)


def test_liquid_needs_the_average_and_a_trade_that_day():
    px = _px({1: [(100, 60e7)] * 3, 2: [(100, 40e7)] * 3, 3: [(100, 90e7)] * 2})
    assert results_drift.liquid(px, D(2024, 10, 3)) == [1]          # 3 didn't trade on the 3rd
    assert sorted(results_drift.liquid(px, D(2024, 10, 2))) == [1, 3]


def test_the_reaction_is_against_the_universe_average():
    px = _px({1: [(100, 1), (100, 1), (120, 1)], 2: [(50, 1), (50, 1), (51, 1)],
              3: [(10, 1), (10, 1), (9.9, 1)]})
    prices = results_drift.Prices(px)
    own, avg = results_drift.reaction(prices, 1, D(2024, 10, 1), D(2024, 10, 3), [1, 2, 3])
    assert own == pytest.approx(20.0) and avg == pytest.approx((20 + 2 - 1) / 3)
    assert results_drift.reaction(prices, 1, D(2024, 9, 30), D(2024, 10, 3), [1]) is None
    # The open on a traded day; otherwise the last close before it.
    assert prices.value(1, D(2024, 10, 3)) == 120
    assert prices.value(1, D(2024, 10, 9), use_open=True) == 120
    assert prices.value(1, D(2024, 9, 1)) is None


def _jump(conn, symbol: str, day: str, factor: float) -> None:
    """A real move: prices from `day` on scaled, the day's previous close left as it was."""
    conn.execute("""update price_eod set open = open * %s, high = high * %s, low = low * %s,
                        close = close * %s,
                        prev_close = case when trade_date > %s then prev_close * %s
                                          else prev_close end
                    where symbol = %s and trade_date >= %s""",
                 (factor, factor, factor, factor, day, factor, symbol, day))


def _file(conn, symbol: str, period_end: str, filed_at: str, n: int) -> None:
    import db_market
    conn.execute("""insert into filing_ref (exchange, filing_system, filing_type, symbol,
                        period_end, filed_at, filed_at_precise, document_url, source_fetch_id)
                    values ('NSE', 'test', 'financial_results', %s, %s, %s, true, %s, %s)""",
                 (symbol, period_end, filed_at, f"https://example.invalid/{n}.xml",
                  db_market.FETCH))


def _setup(conn):
    import db_market
    db_market.load(conn)
    conn.execute("delete from price_eod where trade_date > '2024-11-29'")
    _jump(conn, "GROW", "2024-10-22", 1.2)       # results on the 21st, after the close
    _jump(conn, "CYCL", "2024-10-22", 0.8)
    _jump(conn, "NBFC", "2024-11-27", 1.2)       # results on the 26th
    _file(conn, "GROW", "2024-09-30", "2024-10-21 18:00+05:30", 1)
    _file(conn, "GROW", "2024-09-30", "2024-10-25 18:00+05:30", 2)   # a later revision
    _file(conn, "CYCL", "2024-09-30", "2024-10-21 18:30+05:30", 3)
    _file(conn, "BANK", "2024-09-30", "2024-10-21 16:00+05:30", 4)
    _file(conn, "BANK", "2024-03-31", "2024-10-21 16:00+05:30", 5)   # an old period: ignored
    _file(conn, "NBFC", "2024-09-30", "2024-11-26 18:00+05:30", 6)
    # A holiday that turns out to have traded: the exit is worked out again from the prices.
    conn.execute("""insert into trading_holiday (exchange, holiday_date, description,
                        source_fetch_id) values ('NSE', '2024-11-01', 'Diwali', %s)""",
                 (db_market.FETCH,))
    conn.execute("create temp table later as select * from price_eod "
                 "where trade_date > '2024-10-22'")
    conn.execute("delete from price_eod where trade_date > '2024-10-22'")
    conn.commit()


@pytest.mark.db
def test_reactions_open_paper_buys_and_flags_and_close_after_21_sessions(db_conn):
    _setup(db_conn)
    sent = []
    out = results_drift.run(db_conn, now=at(D(2024, 10, 22)), min_turnover=0,
                            notify=sent.append)
    assert out == ("3 new reaction(s), 1 paper buy(s), 1 flag(s); "
                   "0 paper buy(s) priced to 22 Oct 2024")
    rows = {r[0]: r[1:] for r in db_conn.execute(
        """select symbol, before_date, after_date, abnormal_pct::float8, flag_until
           from results_reaction""").fetchall()}
    assert set(rows) == {"GROW", "CYCL", "BANK"}
    assert rows["GROW"][:2] == (D(2024, 10, 18), D(2024, 10, 22))
    assert rows["GROW"][2] > 10 and rows["GROW"][3] is None
    assert rows["CYCL"][2] < -10
    assert rows["CYCL"][3] == D(2024, 11, 21)    # 21 sessions on, skipping the listed holiday
    assert abs(rows["BANK"][2]) < 5 and rows["BANK"][3] is None
    trade = db_conn.execute("select status, entry_date, exit_due from results_trade").fetchall()
    assert trade == [("open", D(2024, 10, 23), D(2024, 11, 22))]
    assert len(sent) == 1 and "Paper buy GROW" in sent[0] and "Flag CYCL" in sent[0]

    # Known only from the evening it was recorded, to the end of its 21 sessions.
    assert results_drift.flagged(db_conn, at(D(2024, 10, 22), 15)) == {}
    assert set(results_drift.flagged(db_conn, at(D(2024, 10, 25)))) == {2}
    assert results_drift.flagged(db_conn, at(D(2024, 11, 22))) == {}

    assert results_drift.run(db_conn, now=at(D(2024, 10, 22), 21), min_turnover=0,
                             notify=sent.append).startswith("0 new reaction(s)")
    assert len(sent) == 1                                               # nothing new

    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    out = results_drift.run(db_conn, now=at(D(2024, 11, 29)), min_turnover=0,
                            notify=sent.append)
    assert out == ("1 new reaction(s), 0 paper buy(s), 0 flag(s); "
                   "1 paper buy(s) priced to 29 Nov 2024")
    trades = {r[0]: r[1:] for r in db_conn.execute(
        """select symbol, status, entry_date, exit_due, exit_date, entry::float8, exit::float8,
                  return_pct::float8, universe_pct::float8, cost_pct::float8,
                  net_excess_pct::float8 from results_trade""").fetchall()}
    # NBFC's entry, the 28th's open, had passed when its reaction was seen: missed.
    assert trades["NBFC"][0] == "missed" and trades["NBFC"][4] is None
    status, entry_date, exit_due, exit_date, entry, exit_, ret, uni, cost, net = trades["GROW"]
    assert (status, entry_date) == ("closed", D(2024, 10, 23))
    assert exit_due == exit_date == D(2024, 11, 21)        # 1 November traded after all
    opens = dict(db_conn.execute("""select trade_date, open::float8 from price_eod
                                    where symbol = 'GROW' and trade_date in (%s, %s)""",
                                 (entry_date, exit_date)).fetchall())
    assert entry == pytest.approx(opens[entry_date]) and exit_ == pytest.approx(opens[exit_date])
    assert ret == pytest.approx((exit_ / entry - 1) * 100, abs=0.001)
    assert cost == pytest.approx(momentum.cost_pct(entry), abs=0.001)
    assert net == pytest.approx(ret - uni - cost, abs=0.002)
    assert len(sent) == 1                       # a missed buy is recorded, not announced

    rec = results_drift.record(db_conn)
    assert rec["closed"]["n"] == 1 and rec["closed"]["mean_net_excess_pct"] == pytest.approx(net)
    assert rec["closed"]["t"] is None and [t["symbol"] for t in rec["trades"]] == ["NBFC", "GROW"]


@pytest.mark.db
def test_the_momentum_track_skips_flagged_stocks(db_conn, monkeypatch):
    _setup(db_conn)
    results_drift.run(db_conn, now=at(D(2024, 10, 22)), min_turnover=0)
    db_conn.execute("insert into price_eod select * from later")
    db_conn.execute("""insert into index_price (index_name, trade_date, close, is_total_return,
                           source_fetch_id)
                       select 'Nifty 50', trade_date, close, is_total_return, source_fetch_id
                       from index_price where index_name = 'Nifty 500'""")
    for cid in range(1, 6):
        db_conn.execute("insert into index_member (index_name, as_of, company_id) values "
                        "('NIFTY 200', '2024-01-01', %s)", (cid,))
    db_conn.commit()
    ranking = [{"company_id": c, "symbol": s, "score_pct": 40.0 - i, "rank": i + 1}
               for i, (c, s) in enumerate([(2, "CYCL"), (1, "GROW"), (3, "BANK"), (4, "NBFC")])]
    monkeypatch.setattr(momentum, "rank", lambda *a, **k: ranking)
    sent = []
    momentum.run(db_conn, top=2, min_turnover=0, notify=sent.append)
    held = {t: ([h["symbol"] for h in h_], note) for t, h_, note in db_conn.execute(
        "select track, holdings, note from momentum_rebalance where signal_date = '2024-10-31'"
    ).fetchall()}
    assert held["rule"][0] == ["CYCL", "GROW"]
    assert held["results"][0] == ["GROW", "BANK"]
    assert held["results"][1].startswith("Skipped after bad results: CYCL (-")
    assert "Rule, skipping bad results: holds GROW, BANK." in sent[0]
    assert "skipping bad results" in sent[0].split("Since the start: rule")[1]


@pytest.mark.db
def test_ai_calls_on_a_flagged_stock_carry_a_note_and_are_compared(db_conn):
    from igs.alerts import call_message
    _setup(db_conn)
    results_drift.run(db_conn, now=at(D(2024, 10, 22)), min_turnover=0)
    run_id = db_conn.execute("""insert into score_run (as_of, gate_fingerprint, config)
                                values ('2024-10-22 20:00+05:30', 'x', '{}')
                                returning run_id""").fetchone()[0]
    ids = {}
    for cid, sym in ((2, "CYCL"), (1, "GROW")):
        ids[sym] = db_conn.execute(
            """insert into ai_call (company_id, symbol, run_id, action, confidence,
                   horizon_months, summary, reasons, risks, buy_when, sell_when, data_gaps,
                   price_date, price_close, inputs, model, prompt_version, trigger,
                   created_at, reason)
               values (%s, %s, %s, 'buy', 0.6, 12, 'Summary', '["A reason"]', '[]', '[]',
                       '[]', '[]', '2024-10-22', 100, '{}', 'm', 'v', 'scheduled',
                       '2024-10-25 12:00+05:30', 'test') returning call_id""",
            (cid, sym, run_id)).fetchone()[0]
    db_conn.commit()
    cycl = call_message.call(db_conn, ids["CYCL"])
    assert cycl["bad_results"]["flag_until"] == "2024-11-21"
    assert "Note: results moved this stock -" in call_message.brief_message(cycl)
    assert "(session of 22 Oct)" in call_message.brief_message(cycl)
    grow = call_message.call(db_conn, ids["GROW"])
    assert grow["bad_results"] is None and "Note: results" not in call_message.brief_message(grow)

    made = at(D(2024, 10, 25), 12)
    calls = [{"company_id": 2, "action": "buy", "created_at": made,
              "outcome": {"horizons": {"1 month": {"excess_pct": -3.0}}}},
             {"company_id": 1, "action": "buy", "created_at": made,
              "outcome": {"horizons": {"1 month": {"excess_pct": 2.0}, "3 months": None}}},
             {"company_id": 2, "action": "sell", "created_at": made,
              "outcome": {"horizons": {"1 month": {"excess_pct": 5.0}}}},
             {"company_id": 2, "action": "buy", "created_at": at(D(2024, 12, 2)),
              "outcome": None}]
    assert results_drift.ai_call_comparison(db_conn, calls) == [
        {"horizon": "1 month", "group": "after bad results", "calls": 1, "mean_excess_pct": -3.0},
        {"horizon": "1 month", "group": "other buy calls", "calls": 1, "mean_excess_pct": 2.0}]


@pytest.mark.db
def test_the_page_shows_the_buys_and_the_flags(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    _setup(db_conn)
    results_drift.run(db_conn, now=at(D(2024, 10, 22)), min_turnover=0)
    db_conn.execute("insert into price_eod select * from later")
    db_conn.commit()
    results_drift.run(db_conn, now=at(D(2024, 11, 29)), min_turnover=0)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at_ = AppTest.from_string('''from igs.db import connect
from igs.ui.results_days import page
page(connect(autocommit=True))''', default_timeout=30).run()
    assert not at_.exception, at_.exception
    assert [m.label for m in at_.metric] == ["Paper buys closed",
                                            "Average vs market, after costs",
                                            "Beat the market", "t-statistic"]
    assert at_.metric[0].value == "1"
    trades = at_.dataframe[0].value
    assert "stock" in trades.columns
    assert list(trades["status"]) == ["missed (seen late)", "closed"]
    # Nothing is flagged on today's date: the synthetic flags ended in 2024.
    assert any("No stock is flagged now" in c.value for c in at_.caption)
