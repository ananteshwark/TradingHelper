# Geopolitical news and ratings

The News page automatically collects Indian-context reporting, then uses the existing
AI assistant to assess potential price pressure on NSE-listed companies. Default feeds
are The Economic Times' Indian economy, defence and international sections, verified
against its [published RSS directory](https://economictimes.indiatimes.com/rss.cms), plus
Moneycontrol's economy and international-market feeds. On September 29, 2026,
Moneycontrol returned HTTP 200 but April 2024 entries; these are skipped by the
freshness filter. The feeds remain scheduled so fresh entries can be collected when available.
Publisher excerpts and links are retained for personal research; full articles are not
scraped. International events are assessed for their transmission to Indian industries,
not treated as automatically relevant to every Indian stock.

No `articles.json` is needed. To collect and assess immediately:

```bash
uv run igs db migrate
uv run igs news collect
uv run igs news assess --limit 10
uv run igs gate run
uv run igs score
```

Enable the assistant and configure its API key in Settings first. Assessment uses
the existing model, usage log and soft daily spending threshold. Import and scoring
do not call the model. Collection runs as part of `igs sync`: on app startup and periodic checks (normally
once every two hours while `igs ui` runs), and on the existing sync/daily schedule.
Each feed is polled at most once per hour; `igs news collect --force` bypasses that
interval. The [systemd schedules](SCHEDULES.md) run independently of the UI: hourly
`igs news process` collects and assesses up to 10 pending articles, and two-hourly
sync collects exchange data. The daily job also assesses up to 10 articles before scoring. Regular sync only
collects; it does not call AI or change stored ratings. Collection works with AI off.

The News page shows feed errors, newly collected articles, company matches and AI retry
status. `config/news.yaml` controls sources, limits and topic-to-industry candidate rules.
An import failure for one feed does not discard another feed's successful results.
Raw feed responses and fetch metadata are stored in `geopolitical_feed_fetch` for audit.
Invalid dates, future/stale items, unsafe links and summaries too thin to use are skipped.

Automatic company context uses observed NSE names and industry labels. Direct name
matches are preferred, then watchlist companies and ranking order, capped at 10 candidates
per article. An industry match is a hypothesis, not verified company revenue, supplier or
country exposure. The model must explain an India-specific economic mechanism and abstain
when evidence is insufficient. RSS confidence is capped in code at 0.65. As a result,
weak news can legitimately have no rating effect. Unmatched articles are retained and
can be matched after the instrument master/industry data becomes available.

Articles no rating used are deleted 30 days after publication, at each collection: feed
articles with no AI assessment (no company matched, or the assessment never ran or failed).
Articles are assessed only in their first 21 days (`max_age_days`), so by then they can
never affect a rating. Old feed responses that no remaining article came from are deleted
with them. Assessed articles are kept, including those assessed at zero impact, because
past ratings were computed from them; so are articles you imported yourself. The period is
`delete_unassessed_after_days` in `config/news.yaml` and must be longer than the
assessment window.

Failed automatic assessments are retried after 15 minutes, at most three times; one bad
article does not block later articles. Inspect failures in News before explicitly resetting
attempts in the database. Budget/authentication failures stop model calls and are reported;
existing eligible assessments remain usable. Manual imports keep their explicit retry
behavior.

## Optional manual input

You may still run `uv run igs news import articles.json` for your own sourced material,
or upload JSON on the News page. This is optional; the file must exist before importing. Source URLs are retained, not fetched
or independently verified. Only submit text you are entitled to send to your AI provider.
Each item must include a timezone-aware publication timestamp and at least one current
NSE symbol with a sourced description of its business exposure. Manual company links are explicit; a model cannot add companies from memory. For example (fictitious text and
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
Invalid outputs, invented quotes or extra/missing companies are rejected and recorded
for retry. Whole articles are stored atomically. Model/version and assessment time are
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
