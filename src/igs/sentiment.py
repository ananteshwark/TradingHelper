"""Market sentiment in the scores (scoring.yaml, `sentiment`). Experimental, capped, and
shown separately wherever a score is.

Both parts read only the point-in-time view; no model is called here (the news tone was
read and stored earlier, by igs.assistant.news_tone, and is known from when it was read).

- market_mood: the whole market's mood from -1 (fear) to +1 (greed), the average of up
  to five readings from prices the app already loads: breadth, the Nifty 500 against its
  200-day average, advances against declines, new 52-week highs against lows, and the
  Nifty 500's volatility against its past year. Every stock is in the same market, so the
  mood acts on the ranking only by tilting pillar weights (tilted_weights).
- apply_stock_sentiment: each stock's brokers' calls and news tone, a capped overlay on
  its composite, like the geopolitical one. No calls and no news: no adjustment.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math

import polars as pl

from igs.config import MarketMoodConfig, StockSentimentConfig
from igs.factors import base as b
from igs.pit.view import PitView
from igs.score.normalize import ScoreResult

INDEX = "Nifty 500"
FRESH_DAYS = 10        # a stock or the index without a close in this many days is left out
MIN_STOCKS = 5         # a breadth reading needs at least this many stocks
SESSIONS = 20          # advances/declines and recent highs/lows look back this many sessions
STANCE_LEVEL = {"buy": 1, "hold": 0, "sell": -1}
LABELS = ((-0.5, "fear"), (-0.15, "cautious"), (0.15, "neutral"), (0.5, "confident"))


# --------------------------------------------------------------------------- market mood


def _stocks(view: PitView, company_ids: list[int]) -> pl.DataFrame:
    """Adjusted closes of the universe's stocks that traded in the last FRESH_DAYS."""
    px = b.primary_prices(view).filter(pl.col("company_id").is_in(company_ids))
    fresh = (px.group_by("company_id").agg(pl.col("trade_date").max().alias("_last"))
               .filter(pl.col("_last") >= view.as_of_date - dt.timedelta(days=FRESH_DAYS)))
    return px.join(fresh.select("company_id"), on="company_id").sort("company_id",
                                                                        "trade_date")


def _breadth(px: pl.DataFrame) -> tuple[float, str] | None:
    g = (px.group_by("company_id")
           .agg(pl.len().alias("n"), pl.col("adj_close").last().alias("close"),
                pl.col("adj_close").tail(200).mean().alias("dma"))
           .filter(pl.col("n") >= 200))
    if g.height < MIN_STOCKS:
        return None
    above = int((g["close"] > g["dma"]).sum())
    return above / g.height, f"{above} of {g.height} stocks above their 200-day average"


def _advance_decline(px: pl.DataFrame) -> tuple[float, str] | None:
    dates = px["trade_date"].unique().sort().tail(SESSIONS).to_list()
    if len(dates) < SESSIONS:
        return None
    r = (px.with_columns((pl.col("adj_close") / pl.col("adj_close").shift(1).over("company_id")
                          - 1).alias("r"))
           .filter(pl.col("trade_date").is_in(dates) & pl.col("r").is_not_null()))
    adv, dec = int((r["r"] > 0).sum()), int((r["r"] < 0).sum())
    if r["company_id"].n_unique() < MIN_STOCKS or adv + dec == 0:
        return None
    return ((adv - dec) / (adv + dec),
            f"{adv} rises and {dec} falls over the last {SESSIONS} sessions")


def _highs_lows(px: pl.DataFrame) -> tuple[float, str] | None:
    g = (px.group_by("company_id")
           .agg(pl.len().alias("n"),
                pl.col("adj_close").tail(250).max().alias("hi"),
                pl.col("adj_close").tail(250).min().alias("lo"),
                pl.col("adj_close").tail(SESSIONS).max().alias("hi_recent"),
                pl.col("adj_close").tail(SESSIONS).min().alias("lo_recent"))
           .filter(pl.col("n") >= 250))
    if g.height < MIN_STOCKS:
        return None
    highs = int((g["hi_recent"] >= g["hi"]).sum())
    lows = int((g["lo_recent"] <= g["lo"]).sum())
    return ((highs - lows) / g.height,
            f"{highs} of {g.height} stocks at a 52-week closing high in the last {SESSIONS} "
            f"sessions, {lows} at a low")


def _index(view: PitView) -> pl.DataFrame:
    idx = b.index_closes(view, INDEX)
    if idx.height and idx["trade_date"].max() < view.as_of_date - dt.timedelta(
            days=FRESH_DAYS):
        return idx.clear()
    return idx


def _index_trend(idx: pl.DataFrame) -> tuple[float, str] | None:
    if idx.height < 200:
        return None
    close, dma = idx["idx_close"][-1], idx["idx_close"].tail(200).mean()
    v = close / dma - 1
    return v, f"{INDEX} {abs(v):.1%} {'above' if v >= 0 else 'below'} its 200-day average"


def _volatility_rank(idx: pl.DataFrame) -> tuple[float, str] | None:
    if idx.height < 250 + SESSIONS:
        return None
    vol = (idx.select((pl.col("idx_close") / pl.col("idx_close").shift(1)).log().alias("r"))
              .select(pl.col("r").rolling_std(SESSIONS).alias("v"))["v"].drop_nulls()
              .tail(250))
    now = vol[-1]
    rank = float((vol <= now).mean())
    return (rank, f"{INDEX} {SESSIONS}-day volatility {now * math.sqrt(250):.1%} a year, "
                  f"higher than on {rank:.0%} of the past year's days")


def _score(value: float, fear: float, greed: float) -> float:
    return max(-1.0, min(1.0, 2 * (value - fear) / (greed - fear) - 1))


def label(mood: float | None) -> str:
    if mood is None:
        return "not measured"
    return next((name for top, name in LABELS if mood <= top), "greedy")


def tilted_weights(weights: dict[str, float], mood: float | None,
                   cfg: MarketMoodConfig) -> dict[str, float]:
    """Pillar weights moved by the mood, each by at most max_tilt of itself, rescaled to
    the same total."""
    if mood is None or not cfg.enabled or cfg.max_tilt == 0 or mood == 0:
        return dict(weights)
    raw = {p: w * (1 + cfg.tilt.get(p, 0) * mood * cfg.max_tilt) for p, w in weights.items()}
    scale = sum(weights.values()) / sum(raw.values())
    return {p: w * scale for p, w in raw.items()}


def market_mood(view: PitView, company_ids: list[int], cfg: MarketMoodConfig,
                weights: dict[str, float]) -> dict:
    """The mood at view.as_of, its readings, and the pillar weights it gives."""
    if not cfg.enabled:
        return {"enabled": False, "mood": None, "label": label(None), "readings": {},
                "missing": [], "weights": dict(weights), "base_weights": dict(weights)}
    px = _stocks(view, company_ids)
    idx = _index(view)
    compute = {"breadth_200dma": lambda: _breadth(px),
               "advance_decline_20d": lambda: _advance_decline(px),
               "highs_lows_52w": lambda: _highs_lows(px),
               "index_vs_200dma": lambda: _index_trend(idx),
               "volatility_rank_1y": lambda: _volatility_rank(idx)}
    readings, missing = {}, []
    for name, (fear, greed) in cfg.readings.items():
        got = compute[name]()
        if got is None:
            missing.append(name)
            continue
        value, detail = got
        readings[name] = {"value": value, "score": _score(value, fear, greed),
                          "detail": detail}
    mood = (sum(r["score"] for r in readings.values()) / len(readings)
            if len(readings) >= cfg.min_readings else None)
    return {"enabled": True, "mood": mood, "label": label(mood), "readings": readings,
            "missing": missing, "min_readings": cfg.min_readings,
            "weights": tilted_weights(weights, mood, cfg), "base_weights": dict(weights)}


# --------------------------------------------------------------------------- each stock


def _shrunk_mean(values: list[float]) -> float:
    """The mean, shrunk by n/(n+1): one item counts half as much as a strong consensus."""
    n = len(values)
    return sum(values) / n * n / (n + 1)


def _broker_signals(view: PitView, cfg) -> dict[int, dict]:
    """Each broker's latest research call in the window, once per broker: +1/0/-1 for a
    buy/hold/sell, or +1/-1 for an upgrade/downgrade from its previous call."""
    since = view.as_of_date - dt.timedelta(days=cfg.window_days + cfg.revision_lookback_days)
    calls = (view.table("broker_calls")
                 .filter((pl.col("kind") == "research") & pl.col("company_id").is_not_null()
                         & (pl.col("called_on") >= since)
                         & (pl.col("called_on") <= view.as_of_date))
                 .sort("company_id", "broker_key", "called_on", "broker_call_id"))
    items: dict[int, list[dict]] = {}
    for (cid, _), g in calls.group_by(["company_id", "broker_key"], maintain_order=True):
        rows = g.sort("called_on", "broker_call_id").rows(named=True)
        last = rows[-1]
        age = (view.as_of_date - last["called_on"]).days
        if age > cfg.window_days:
            continue
        prev = rows[-2] if len(rows) > 1 else None
        if prev and (last["called_on"] - prev["called_on"]).days > cfg.revision_lookback_days:
            prev = None
        change = None
        value = STANCE_LEVEL[last["stance"]]
        if prev and prev["stance"] != last["stance"]:
            change = ("upgrade" if value > STANCE_LEVEL[prev["stance"]] else "downgrade")
            value = 1 if change == "upgrade" else -1
        weight = 2 ** (-age / cfg.half_life_days)
        items.setdefault(cid, []).append({
            "broker": last["broker"], "rating": last["rating"], "stance": last["stance"],
            "called_on": last["called_on"].isoformat(), "change": change,
            "previous": prev["rating"] if change else None, "value": value,
            "weight": weight, "url": last["url"], "source": last["source"]})
    return {cid: {"signal": _shrunk_mean([i["value"] * i["weight"] for i in its]),
                  "items": sorted(its, key=lambda i: (i["called_on"], i["broker"]),
                                  reverse=True)}
            for cid, its in items.items()}


def _news_signals(view: PitView, cfg) -> dict[int, dict]:
    """Tone x confidence x age decay of each article about the company in the window."""
    rows = (view.table("news_tone")
                .with_columns(pl.col("published_at").dt.convert_time_zone("UTC"))
                .filter(pl.col("company_id").is_not_null()
                        & (pl.col("confidence") >= cfg.min_confidence)
                        & (pl.col("published_at") >= view.as_of
                           - dt.timedelta(days=cfg.window_days))
                        & (pl.col("published_at") <= view.as_of))
                .sort("company_id", "published_at", "tone_id"))
    items: dict[int, list[dict]] = {}
    seen: set[tuple] = set()
    for r in rows.iter_rows(named=True):
        # Exact syndicated evidence counts once per company/day, not once per publisher.
        # Preserve distinct numerical updates and avoid fuzzy grouping unrelated stories.
        import re
        quote = re.sub(r'\W+', ' ', (r.get('quote') or '').casefold()).strip()
        title = re.sub(r'\W+', ' ', r['title'].casefold()).strip()
        fingerprint = quote if len(quote) >= 40 else title
        key = (r['company_id'], r['published_at'].date(), fingerprint)
        if len(fingerprint) >= 25 and key in seen:
            continue
        seen.add(key)
        age = (view.as_of - r["published_at"]).total_seconds() / 86400
        weight = r["confidence"] * 2 ** (-age / cfg.half_life_days)
        items.setdefault(r["company_id"], []).append({
            "title": r["title"], "url": r["url"], "published_at": r["published_at"].isoformat(),
            "tone": r["tone"], "confidence": r["confidence"], "reason": r["reason"],
            "weight": weight})
    return {cid: {"signal": _shrunk_mean([i["tone"] * i["weight"] for i in its]),
                  "items": its[::-1]}
            for cid, its in items.items()}


def apply_stock_sentiment(res: ScoreResult, view: PitView,
                          cfg: StockSentimentConfig) -> ScoreResult:
    """composite += max_adjustment x (weighted mean of the available brokers' and news
    signals), clipped to +/- max_adjustment; the evidence is kept per company."""
    comp = res.composite
    if "base_composite" not in comp.columns:
        comp = comp.with_columns(pl.col("composite").alias("base_composite"))
    parts: dict[str, tuple[float, dict[int, dict]]] = {}
    if cfg.enabled and cfg.brokers.weight > 0 and view.has("broker_calls"):
        parts["brokers"] = (cfg.brokers.weight, _broker_signals(view, cfg.brokers))
    if cfg.enabled and cfg.news.weight > 0 and view.has("news_tone"):
        parts["news"] = (cfg.news.weight, _news_signals(view, cfg.news))
    offsets, evidence = [], []
    for cid, composite in zip(comp["company_id"], comp["composite"], strict=True):
        used = {k: (w, sig[cid]) for k, (w, sig) in parts.items() if cid in sig}
        if composite is None or not used:
            offsets.append(0.0)
            evidence.append("{}")
            continue
        signal = (sum(w * s["signal"] for w, s in used.values())
                  / sum(w for w, _ in used.values()))
        offsets.append(cfg.max_adjustment * max(-1.0, min(1.0, signal)))
        evidence.append(json.dumps({"signal": signal, "cap": cfg.max_adjustment,
                                    **{k: s for k, (_, s) in used.items()}}, default=str))
    comp = comp.with_columns(pl.Series("sentiment_adjustment", offsets, dtype=pl.Float64),
                             pl.Series("sentiment_evidence", evidence, dtype=pl.Utf8))
    comp = comp.with_columns((pl.col("composite") + pl.col("sentiment_adjustment"))
                             .alias("composite"))
    return dataclasses.replace(res, composite=comp)


def promotion_blockers(comp: pl.DataFrame, eligible: list[int],
                       top_pct: float) -> pl.DataFrame:
    """Stocks in the High conviction band only because of a positive sentiment
    adjustment: it is not validated, so it may not promote a stock that far."""
    empty = pl.DataFrame(schema={"company_id": pl.Int64, "reason": pl.Utf8})
    if "sentiment_adjustment" not in comp.columns:
        return empty
    e = comp.filter(pl.col("company_id").is_in(eligible) & pl.col("composite").is_not_null())
    if e.height == 0:
        return empty
    without = pl.col("composite") - pl.col("sentiment_adjustment")
    e = e.with_columns(
        (pl.col("composite").rank("ordinal", descending=True) / pl.len()).alias("_with"),
        (without.rank("ordinal", descending=True) / pl.len()).alias("_without"))
    band = top_pct / 100
    return e.filter((pl.col("sentiment_adjustment") > 0) & (pl.col("_with") <= band)
                    & (pl.col("_without") > band)).select("company_id", pl.format(
                        "in the top {} only with the experimental sentiment adjustment (+{}); "
                        "not yet validated", pl.lit(f"{top_pct:g}%"),
                        pl.col("sentiment_adjustment").round(3)).alias("reason"))


def describe(evidence: dict) -> str:
    """One sentence on what a stock's sentiment adjustment rests on, in screen language
    (no ratings words: the screen's output is never a call)."""
    parts = []
    if "brokers" in evidence:
        its = evidence["brokers"]["items"]
        pos = sum(i["value"] > 0 for i in its)
        neg = sum(i["value"] < 0 for i in its)
        parts.append(f"{len(its)} brokers' recent ratings ({pos} positive, "
                     f"{len(its) - pos - neg} neutral, {neg} negative)")
    if "news" in evidence:
        its = evidence["news"]["items"]
        tone = sum(i["tone"] for i in its) / len(its)
        parts.append(f"the tone of {len(its)} news article{'s' if len(its) > 1 else ''} "
                     f"(average {tone:+.2f})")
    cap = f", capped at {evidence['cap']:g}" if "cap" in evidence else ""
    return " and ".join(parts) + cap
