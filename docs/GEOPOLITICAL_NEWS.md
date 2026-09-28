# Geopolitical news and ratings

The News page imports reporting and documented company exposures, then uses the
existing AI assistant to assess potential price pressure. It is also available by CLI:

```bash
uv run igs db migrate
uv run igs news import articles.json
uv run igs news assess --limit 10
uv run igs gate run
uv run igs score
```

Enable the assistant and configure its API key in Settings first. Assessment uses
the existing model, usage log and soft daily spending threshold. Import and scoring
do not call the model. The daily job assesses up to 10 pending articles before scoring;
AI failure is reported and does not prevent scoring with existing eligible assessments.

Initial intake is user-supplied JSON, also accepted through the local News page.
There is no automatic news feed or web scraping. Source URLs are retained, not fetched
or independently verified. Only submit text you are entitled to send to your AI provider.
Each item must include a timezone-aware publication timestamp and at least one current
NSE symbol with a sourced description of its business exposure. All company links are
explicit; a model cannot add companies from memory. For example (fictitious text and
placeholder sources; replace before importing):

```json
[
  {
    "url": "https://example.com/news/shipping-event",
    "title": "Shipping disruption increases freight costs on a key trade route",
    "body": "Shipping services on the route were suspended, increasing freight costs. The duration of the disruption remains uncertain.",
    "published_at": "2026-09-28T09:00:00+05:30",
    "companies": [
      {
        "symbol": "REPLACE_WITH_NSE_SYMBOL",
        "description": "The company reports dependence on this route for imported raw materials; cite the actual disclosure here.",
        "source_url": "https://example.com/company/annual-report"
      }
    ]
  }
]
```

An import is atomic, rejects unknown/ambiguous symbols and future timestamps, and
deduplicates source URLs and identical normalized article bodies. Imported articles and
company links are immutable through this workflow. A repeated URL does not replace an
earlier article; corrected reporting should be imported with its own source URL/text.

The AI supplies direction/strength (-1 to +1), confidence (0 to 1), a transmission
channel, rationale including uncertainty and horizon, and an exact quote from the article.
Invalid outputs, invented quotes or extra/missing companies are rejected and remain
pending. Whole articles are stored atomically. Model/version and assessment time are
recorded; retrying completed articles does not call the AI again.

## Rating calculation

For eligible assessments, each contribution is:

`impact × confidence × 2^(-article_age_days / half_life_days)`

The mean contribution multiplied by `max_adjustment` is added to the base composite.
Defaults in `config/scoring.yaml`: cap ±0.15 composite z-score units, confidence at least
0.60, seven-day half-life, expiry after 21 days. This is a modeling prior, not an empirically
calibrated weight, percent price change, return prediction, or probability of profit.
Different articles can still cover the same event; averaging and the cap limit their
influence but do not establish independent corroboration.

Ranks and tiers use the adjusted composite. Fundamental coverage, rejection flags,
robustness and health checks remain in force. Any nonzero AI adjustment additionally
withholds High conviction until predictive value is validated; it can still change
Watchlist versus Not shortlisted. Missing, stale, low-confidence or disabled news produces
no adjustment. Missing base scores remain missing. Per-factor contributions explain the
base score; the separate news adjustment reconciles it to the final composite.

The stock page and stock-detail API expose base score, adjustment and the exact evidence
snapshot used in that run. Assessments are eligible only after publication, local receipt
AND AI assessment time. Assessing old news today cannot alter a past run or create a
historical AI signal in backtests. Evidence accumulates prospectively; any earlier
backtest without contemporaneous assessments tests only the base model. No predictive
performance claim is made for this feature.

Set `geopolitical.enabled: false` to disable the overlay and daily assessment. The main
assistant switch controls new model calls; it does not erase already stored assessments.
