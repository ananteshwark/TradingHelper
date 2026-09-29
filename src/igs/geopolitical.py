"""Evidence-backed news intake and point-in-time geopolitical score adjustment.

No model calls happen during scoring. Import timestamps and assessment timestamps
are assigned by the database, never taken from the article or model.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import polars as pl
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from igs.config import GeopoliticalConfig
from igs.score.normalize import ScoreResult
from igs.timeutil import utc_now


class Exposure(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=40)
    # User-supplied company exposure, supported by a filing or company source.
    description: str = Field(min_length=20, max_length=2000)
    source_url: HttpUrl


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: HttpUrl
    title: str = Field(min_length=10, max_length=500)
    body: str = Field(min_length=40, max_length=20000)
    published_at: AwareDatetime
    companies: list[Exposure] = Field(min_length=1, max_length=25)


def import_articles(conn, data: list[dict]) -> int:
    """Validate the entire batch before an atomic, idempotent import. No URLs fetched."""
    if not isinstance(data, list) or not 1 <= len(data) <= 100:
        raise ValueError("provide a JSON list of 1 to 100 articles")
    articles = [Article.model_validate(a) for a in data]
    now = utc_now()
    count = 0
    with conn.transaction():
        for article in articles:
            if article.published_at > now:
                raise ValueError("news publication time cannot be in the future")
            companies = []
            seen = set()
            for exposure in article.companies:
                rows = conn.execute("""select distinct s.company_id from security_identifier i
                    join security s using (security_id) where i.id_type = 'NSE_SYMBOL'
                    and i.id_value = %s and i.valid_from <= %s
                    and (i.valid_to is null or i.valid_to > %s)""",
                    (exposure.symbol.upper(), now.date(), now.date())).fetchall()
                if len(rows) != 1:
                    raise ValueError(f"unknown or ambiguous current symbol: {exposure.symbol}")
                cid = rows[0][0]
                if cid in seen:
                    raise ValueError(f"duplicate company: {exposure.symbol}")
                seen.add(cid)
                companies.append({**exposure.model_dump(mode="json"), "company_id": cid})
            digest = hashlib.sha256(" ".join(article.body.split()).encode()).hexdigest()
            cur = conn.execute("""insert into geopolitical_news
                (url, title, body, published_at, content_hash, companies)
                values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
                (str(article.url), article.title, article.body, article.published_at,
                 digest, json.dumps(companies)))
            count += cur.rowcount
    return count


def apply_overlay(res: ScoreResult, view, cfg: GeopoliticalConfig) -> ScoreResult:
    """Mean signed impact × confidence × age decay, capped in composite z-score units.

    Averaging prevents duplicate coverage from multiplying the score. Only evidence
    available at this as-of time participates; missing evidence is not a neutral opinion.
    """
    values = {}
    if cfg.enabled and view.has("geopolitical"):
        cutoff = view.as_of - dt.timedelta(days=cfg.max_age_days)
        rows = view.table("geopolitical").filter(
            (pl.col("published_at") >= cutoff) & (pl.col("published_at") <= view.as_of)
            & (pl.col("confidence") >= cfg.min_confidence))
        for row in rows.sort("assessment_id").iter_rows(named=True):
            age = (view.as_of - row["published_at"]).total_seconds() / 86400
            weight = row["impact"] * row["confidence"] * 2 ** (-age / cfg.half_life_days)
            values.setdefault(row["company_id"], []).append((weight, row))
    offsets, evidence = [], []
    for cid in res.composite["company_id"]:
        items = values.get(cid, [])
        delta = cfg.max_adjustment * sum(v for v, _ in items) / len(items) if items else 0.0
        offsets.append(max(-cfg.max_adjustment, min(cfg.max_adjustment, delta)))
        evidence.append(json.dumps([{
            k: r[k] for k in ("assessment_id", "news_id", "url", "title", "impact",
                              "confidence", "rationale", "evidence", "channel", "model",
                              "published_at", "assessed_at", "exposure", "exposure_url")
        } for _, r in items], default=str))
    comp = res.composite.with_columns(
        pl.col("composite").alias("base_composite"),
        pl.Series("geopolitical_adjustment", offsets, dtype=pl.Float64),
        pl.Series("geopolitical_evidence", evidence, dtype=pl.Utf8))
    comp = comp.with_columns(
        pl.when(pl.col("base_composite").is_null()).then(0.0)
        .otherwise(pl.col("geopolitical_adjustment")).alias("geopolitical_adjustment"))
    comp = comp.with_columns((pl.col("base_composite") + pl.col("geopolitical_adjustment"))
                              .alias("composite"))
    return ScoreResult(res.factors, res.pillars, comp)
