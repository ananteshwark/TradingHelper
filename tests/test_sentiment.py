"""Market sentiment in the scores: the whole market's mood tilts pillar weights, and each
stock's brokers' calls and news tone make a capped overlay. Both read only what was known
at the as-of time, and neither can lift a stock into High conviction on its own."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import polars as pl
import pytest

from igs import sentiment
from igs.config import (
    BrokerSentimentConfig,
    MarketMoodConfig,
    NewsFeed,
    NewsSentimentConfig,
    StockSentimentConfig,
    load_scoring,
)
from igs.guardrails import find_advice_language
from igs.pit.harness import check_no_lookahead
from igs.pit.view import PitDataset, PitView
from igs.score.normalize import ScoreResult
from igs.timeutil import IST

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
NOW = dt.datetime(2026, 9, 28, 23, 59, tzinfo=IST)
WEIGHTS = dict(load_scoring().pillar_weights)
MOOD = load_scoring().sentiment.market
STOCK = load_scoring().sentiment.stock


# --------------------------------------------------------------------------- market mood


def test_the_mood_tilts_pillar_weights_within_the_cap():
    fear = sentiment.tilted_weights(WEIGHTS, -1.0, MOOD)
    # momentum 20% x 0.75, quality and low volatility 20% x 1.25, rescaled to sum to 1
    assert fear["momentum"] == pytest.approx(0.15 / 1.05)
    assert fear["quality"] == pytest.approx(0.25 / 1.05) == fear["low_volatility"]
    assert fear["valuation"] == pytest.approx(0.20 / 1.05)
    assert sum(fear.values()) == pytest.approx(1.0)
    greed = sentiment.tilted_weights(WEIGHTS, 1.0, MOOD)
    assert greed["momentum"] > WEIGHTS["momentum"] > fear["momentum"]
    half = sentiment.tilted_weights(WEIGHTS, -0.5, MOOD)
    assert fear["momentum"] < half["momentum"] < WEIGHTS["momentum"]    # continuous
    for mood, cfg in ((0.0, MOOD), (None, MOOD), (-1.0, MOOD.model_copy(update={
            "enabled": False}))):
        assert sentiment.tilted_weights(WEIGHTS, mood, cfg) == WEIGHTS


@pytest.fixture(scope="module")
def market():
    import synthetic_market
    return synthetic_market.build()


def test_the_mood_is_read_from_breadth_trend_and_volatility(market):
    import synthetic_market
    rising = synthetic_market.GATE_DATES[2]            # every stock above its average
    m = sentiment.market_mood(PitView(market, rising), list(range(1, 7)), MOOD, WEIGHTS)
    assert set(m["readings"]) == set(MOOD.readings) and m["missing"] == []
    assert m["readings"]["breadth_200dma"]["detail"] == \
        "6 of 6 stocks above their 200-day average"
    assert (m["mood"], m["label"]) == (1.0, "greedy")
    assert m["weights"]["momentum"] > WEIGHTS["momentum"] and m["base_weights"] == WEIGHTS
    later = sentiment.market_mood(PitView(market, synthetic_market.GATE_DATES[3]),
                                  list(range(1, 7)), MOOD, WEIGHTS)
    assert -0.15 < later["mood"] < 0.15 and later["label"] == "neutral"
    # Without the index, two readings are missing; with too few left there is no tilt.
    no_index = PitDataset({k: v for k, v in market.tables.items() if k != "index_prices"})
    strict = MOOD.model_copy(update={"min_readings": 4})
    m = sentiment.market_mood(PitView(no_index, rising), list(range(1, 7)), strict, WEIGHTS)
    assert m["missing"] == ["index_vs_200dma", "volatility_rank_1y"]
    assert m["mood"] is None and m["weights"] == WEIGHTS
    off = sentiment.market_mood(PitView(market, rising), [1], MOOD.model_copy(
        update={"enabled": False}), WEIGHTS)
    assert off["enabled"] is False and off["weights"] == WEIGHTS


@pytest.mark.lookahead
def test_the_mood_is_point_in_time(market):
    import synthetic_market

    def mood(view: PitView) -> pl.DataFrame:
        m = sentiment.market_mood(view, list(range(1, 7)), MOOD, WEIGHTS)
        return pl.DataFrame([{"reading": k, "value": r["value"], "detail": r["detail"]}
                             for k, r in m["readings"].items()]
                            + [{"reading": "mood", "value": m["mood"], "detail": m["label"]}])
    check_no_lookahead(mood, market, synthetic_market.GATE_DATES, name="market mood")


# --------------------------------------------------------------------------- each stock


def call(cid, broker, stance, days_ago, created_days_ago=None, kind="research", n=0):
    day = NOW.date() - dt.timedelta(days=days_ago)
    created = NOW - dt.timedelta(days=days_ago if created_days_ago is None
                                 else created_days_ago)
    return {"broker_call_id": n or days_ago * 10 + cid, "company_id": cid,
            "broker_key": broker.lower(), "broker": broker, "stance": stance,
            "rating": stance.capitalize(), "kind": kind, "target_price": 100.0,
            "called_on": day, "source": "news", "url": f"https://example.com/{cid}/{n}",
            "created_at": created}


def tone(cid, value, confidence, days_ago, assessed_days_ago=None, n=1):
    published = NOW - dt.timedelta(days=days_ago)
    assessed = NOW - dt.timedelta(days=days_ago if assessed_days_ago is None
                                  else assessed_days_ago)
    return {"tone_id": n, "article_id": n, "company_id": cid, "tone": value,
            "confidence": confidence, "reason": "An order was won.", "quote": "won an order",
            "model": "test-model", "url": f"https://example.com/news/{n}",
            "title": f"Company {cid} wins an order", "feed_name": "test",
            "published_at": published, "received_at": published, "assessed_at": assessed}


CALLS = [
    call(1, "Alpha", "hold", 100, n=1), call(1, "Alpha", "buy", 5, n=2),    # upgrade
    call(1, "Beta", "sell", 40, n=3),                                      # too old
    call(1, "Gamma", "buy", 2, kind="trading", n=4),                       # not research
    call(2, "Alpha", "buy", 200, n=5), call(2, "Alpha", "hold", 10, n=6),  # downgrade
    call(3, "Delta", "buy", 3, created_days_ago=-1, n=7),                  # recorded later
    call(4, "Delta", "sell", 0, n=8), call(4, "Eta", "hold", 0, n=9),
]
TONES = [tone(1, 0.8, 0.9, 2, n=1),
         tone(3, 0.9, 0.9, 1, assessed_days_ago=-1, n=2),                  # read later
         tone(2, -0.9, 0.5, 1, n=3),                                       # low confidence
         tone(5, -0.6, 1.0, 20, n=4)]                                      # too old


def stock_overlay(cfg=STOCK, calls=CALLS, tones=TONES, as_of=NOW, base=None):
    ds = PitDataset.from_frames(broker_calls=pl.DataFrame(calls),
                                news_tone=pl.DataFrame(tones))
    res = ScoreResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame(
        {"company_id": [1, 2, 3, 4, 5], "composite": base or [0.5, 0.4, 0.3, 0.2, None],
         "coverage": [1.0] * 5}, schema_overrides={"composite": pl.Float64}))
    return sentiment.apply_stock_sentiment(res, PitView(ds, as_of), cfg).composite


def test_brokers_and_news_make_a_capped_overlay():
    rows = {r["company_id"]: r for r in stock_overlay().iter_rows(named=True)}
    b1 = 1 * 2 ** (-5 / 14) / 2                     # one upgrade, 5 days old, shrunk by 1/2
    n1 = 0.8 * 0.9 * 2 ** (-2 / 5) / 2              # one article, 2 days old
    one = rows[1]
    assert one["sentiment_adjustment"] == pytest.approx(0.10 * (0.6 * b1 + 0.4 * n1))
    assert one["composite"] == pytest.approx(0.5 + one["sentiment_adjustment"])
    assert one["base_composite"] == 0.5
    ev = json.loads(one["sentiment_evidence"])
    assert [(i["broker"], i["change"], i["previous"], i["value"])
            for i in ev["brokers"]["items"]] == [("Alpha", "upgrade", "Hold", 1)]
    assert [i["title"] for i in ev["news"]["items"]] == ["Company 1 wins an order"]
    text = sentiment.describe(ev)
    assert text == ("1 brokers' recent ratings (1 positive, 0 neutral, 0 negative) and the "
                    "tone of 1 news article (average +0.80), capped at 0.1")
    assert find_advice_language(text) == []
    # A downgrade to hold counts as -1; only brokers, so their signal is the whole of it.
    assert rows[2]["sentiment_adjustment"] == pytest.approx(
        -0.10 * 2 ** (-10 / 14) / 2)
    # Known only after the as-of time: nothing. Two calls today: (-1 + 0) / 2 x 2/3.
    assert rows[3]["sentiment_adjustment"] == 0 and rows[3]["sentiment_evidence"] == "{}"
    assert rows[4]["sentiment_adjustment"] == pytest.approx(-0.10 / 3)
    assert rows[5]["sentiment_adjustment"] == 0 and rows[5]["composite"] is None
    # Many strong calls stay within the cap.
    many = [call(1, f"B{i}", "buy", 0, n=100 + i) for i in range(50)]
    assert stock_overlay(calls=many, tones=[tone(9, 0.5, 0.9, 1)])[
        "sentiment_adjustment"][0] == pytest.approx(0.10 * 50 / 51)
    off = stock_overlay(StockSentimentConfig(enabled=False))
    assert off["sentiment_adjustment"].to_list() == [0.0] * 5


def test_the_weights_and_windows_come_from_config():
    only_news = STOCK.model_copy(update={"brokers": BrokerSentimentConfig(weight=0)})
    assert stock_overlay(only_news)["sentiment_adjustment"][0] == pytest.approx(
        0.10 * 0.8 * 0.9 * 2 ** (-2 / 5) / 2)
    wide = STOCK.model_copy(update={
        "brokers": BrokerSentimentConfig(window_days=60, revision_lookback_days=0),
        "news": NewsSentimentConfig(weight=0)})
    r = json.loads(stock_overlay(wide)["sentiment_evidence"][0])
    # Beta's sell is now inside the window; with no revision lookback Alpha counts as a buy.
    assert sorted((i["broker"], i["value"]) for i in r["brokers"]["items"]) == [
        ("Alpha", 1), ("Beta", -1)]


@pytest.mark.lookahead
def test_the_stock_overlay_is_point_in_time():
    ds = PitDataset.from_frames(broker_calls=pl.DataFrame(CALLS),
                                news_tone=pl.DataFrame(TONES))
    res = ScoreResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame(
        {"company_id": [1, 2, 3, 4], "composite": [0.5, 0.4, 0.3, 0.2],
         "coverage": [1.0] * 4}))
    dates = [NOW - dt.timedelta(days=d) for d in (30, 6, 3, 0)]
    check_no_lookahead(lambda v: sentiment.apply_stock_sentiment(res, v, STOCK).composite,
                       ds, dates, name="stock sentiment")


def test_a_positive_adjustment_alone_never_promotes_to_high_conviction():
    comp = pl.DataFrame({"company_id": [1, 2, 3, 4], "composite": [0.95, 0.90, 0.5, 0.2],
                         "sentiment_adjustment": [0.08, 0.0, 0.05, -0.05]})
    # Company 1 is first with its adjustment; without it (0.87) it falls to second of
    # four, out of the top 25%. Company 3's adjustment does not bring it into the band.
    got = sentiment.promotion_blockers(comp, [1, 2, 3, 4], 25.0)
    assert got["company_id"].to_list() == [1]
    assert got["reason"][0].startswith("in the top 25% only with the experimental sentiment")
    assert sentiment.promotion_blockers(comp, [1, 2, 3, 4], 50.0).height == 0
    assert sentiment.promotion_blockers(comp.drop("sentiment_adjustment"), [1], 25.0).height \
        == 0


def test_config_rejects_unknown_readings_and_pillars():
    with pytest.raises(ValueError, match="unknown mood readings"):
        MarketMoodConfig(readings={"vix": (30.0, 10.0)}, min_readings=1)
    with pytest.raises(ValueError, match="unknown pillars"):
        MarketMoodConfig(tilt={"size": 1}, readings=dict(MOOD.readings))
    with pytest.raises(ValueError, match="must differ"):
        MarketMoodConfig(readings={"breadth_200dma": (0.5, 0.5)}, min_readings=1)
    with pytest.raises(ValueError, match="min_readings"):
        MarketMoodConfig(readings={"breadth_200dma": (0.3, 0.7)}, min_readings=3)


# --------------------------------------------------------------------------- news tone


def _article(conn, n, title, body, hours_ago=5):
    return conn.execute("""insert into broker_article (url, title, body, published_at,
            feed_name, candidate) values (%s, %s, %s, now() - %s * interval '1 hour', 'test',
            false) returning article_id""",
        (f"https://example.com/{n}", title, f"{title}\n{body}", hours_ago)).fetchone()[0]


@pytest.mark.db
def test_the_ai_reads_each_articles_tone_and_quotes_it(db_conn):
    import db_market

    from igs import brokers
    from igs.assistant import news_tone
    db_market.load(db_conn)
    a1 = _article(db_conn, 1, "Example Bank wins a Rs 900 crore government contract",
                  "Keywords: Example Bank, order")
    _article(db_conn, 2, "Sensex ends flat; metal stocks drag", "Keywords: sensex, nifty")
    db_conn.commit()
    seen = []

    def structured(feature, *, system, prompt, schema):
        items = json.loads(prompt.split("\n", 1)[1].rsplit("\n", 1)[0])
        seen.append((feature, [i["title"] for i in items]))
        bank = next(i["article"] for i in items if "Example Bank" in i["title"])
        return {"items": [
            {"article": bank, "company": "Example Bank", "nse_symbol": "", "tone": 0.7,
             "confidence": 0.9, "reason": "A large government contract.",
             "quote": "wins a Rs 900 crore government CONTRACT"},
            {"article": bank, "company": "Nowhere Ltd", "nse_symbol": "", "tone": -0.5,
             "confidence": 0.8, "reason": "Invented.", "quote": "a fraud was found"},
            {"article": 7, "company": "Example Bank", "nse_symbol": "", "tone": 1,
             "confidence": 1, "reason": "No such article.", "quote": "wins"},
        ]}, SimpleNamespace(model="test-model")
    cfg = SimpleNamespace(features=SimpleNamespace(news_tone=SimpleNamespace(
        batch_size=15, max_per_run=150)))
    assistant = SimpleNamespace(conn=db_conn, cfg=cfg, structured=structured,
                                cost=lambda m: 0.002)
    got = news_tone.read_new(assistant)
    assert seen[0][0] == "news_tone" and len(seen[0][1]) == 2
    assert (got.read, got.stored, got.unmatched) == (2, 1, 0)
    assert any("quote is not in the article" in i for i in got.issues)
    assert any("matches no article" in i for i in got.issues)
    assert db_conn.execute("""select company_id, tone::float8, confidence::float8, model,
                                     prompt_version from stock_news_tone""").fetchall() == [
        (3, 0.7, 0.9, "test-model", "tone-v1")]
    assert news_tone.read_new(assistant).read == 0                      # read once
    # An article with a tone is kept when unused articles are deleted after 30 days.
    db_conn.execute("update broker_article set received_at = now() - interval '40 days'")
    db_conn.commit()
    cfg_b = brokers.load_broker_calls().model_copy(update={"feeds": [
        NewsFeed(name="Down", url="https://feeds.example/rss")]})
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as c:
        got = brokers.collect(db_conn, cfg_b, force=True, client=c)
    assert got.errors and got.articles == 0
    assert [r[0] for r in db_conn.execute("select article_id from broker_article")] == [a1]


# --------------------------------------------------------------------------- end to end


@pytest.fixture
def sentiment_run(db_conn, tmp_path, monkeypatch):
    """The synthetic market with a broker's upgrade and a news tone for GROW recorded
    before 2024-11-29, scored as of then."""
    import db_market
    import test_ai_calls

    from igs.config import load_universe
    from igs.score.pipeline import score_from_db
    conn, _ = test_ai_calls.scored.__wrapped__(db_conn, tmp_path, monkeypatch)
    as_of = db_market.AS_OF
    for stance, days in (("hold", 90), ("buy", 3)):
        day = as_of.date() - dt.timedelta(days=days)
        conn.execute("""insert into broker_call (company_id, stock_name, broker, stance,
                rating, kind, target_price, called_on, source, url, dedupe_key, created_at)
            values (1, 'Grow Industries', 'Kotak Securities', %s, %s, 'research', 150, %s,
                    'manual', 'https://example.com/call', %s, %s)""",
            (stance, stance.capitalize(), day, f"1|kotak securities|{day}|{stance}|150.00",
             as_of - dt.timedelta(days=days - 1)))
    article = conn.execute("""insert into broker_article (url, title, body, published_at,
            feed_name, candidate, received_at)
        values ('https://example.com/grow', 'Grow Industries wins a large export order',
                'Grow Industries wins a large export order', %s, 'test', false, %s)
        returning article_id""", (as_of - dt.timedelta(days=2),
                                  as_of - dt.timedelta(days=2))).fetchone()[0]
    conn.execute("""insert into stock_news_tone (article_id, company_id, company_text, tone,
            confidence, reason, quote, model, prompt_version, assessed_at)
        values (%s, 1, 'Grow Industries', 0.6, 0.9, 'A large export order.',
                'wins a large export order', 'test-model', 'tone-v1', %s)""",
        (article, as_of - dt.timedelta(days=1)))
    conn.commit()
    sc = load_scoring().model_copy(update={"peer_group": load_scoring().peer_group.model_copy(
        update={"min_peers": 2})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    run_id, _ = score_from_db(conn, as_of, None, sc=sc, uc=uc)
    conn.commit()
    return conn, run_id


@pytest.mark.db
def test_a_score_run_stores_the_mood_and_each_stocks_sentiment(sentiment_run, monkeypatch):
    from igs import service
    conn, run_id = sentiment_run
    mood = conn.execute("select market_sentiment from score_run where run_id = %s",
                        (run_id,)).fetchone()[0]
    assert mood["enabled"] and set(mood["readings"]) <= set(MOOD.readings)
    assert sum(mood["weights"].values()) == pytest.approx(1.0)
    row = conn.execute("""select sentiment_adjustment, sentiment_evidence, composite,
                                 base_composite, geopolitical_adjustment, explanation
                          from score_result where run_id = %s and company_id = 1""",
                       (run_id,)).fetchone()
    adj, ev, composite, base, geo, explanation = row
    assert adj > 0 and ev["brokers"]["items"][0]["change"] == "upgrade"
    assert ev["news"]["items"][0]["title"] == "Grow Industries wins a large export order"
    assert composite == pytest.approx(base + geo + adj)
    assert "Experimental sentiment adjustment" in explanation
    others = conn.execute("""select count(*) from score_result where run_id = %s
                             and company_id <> 1 and sentiment_adjustment <> 0""",
                          (run_id,)).fetchone()[0]
    assert others == 0
    detail = service.stock_detail(conn, "GROW", run_id)       # passes the guardrail
    assert detail["run"]["market_sentiment"]["label"] == mood["label"]
    # The AI's own calls see the mood and the stock's sentiment too.
    from igs.assistant.calls import gather
    _, _, data = gather(conn, service.resolve_run(conn, run_id), "GROW")
    assert data["sentiment"]["stock_adjustment"] == pytest.approx(adj)
    assert data["sentiment"]["market_mood"]["label"] == mood["label"]
    assert data["sentiment"]["news_tone"][0]["tone"] == 0.6

    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    assert any(i.value.startswith("Market mood: **") for i in at.info)
    at.session_state["page"] = "Stock"
    at.session_state["stock_sym"] = "GROW"
    at.run()
    assert not at.exception, at.exception
    assert any(t.value.startswith(f"Adjustment {adj:+.3f}") for t in at.text)
    firms = next(d.value for d in at.dataframe if "firm" in d.value.columns)
    assert firms["rating"].tolist() == ["Buy (from Hold)"]
