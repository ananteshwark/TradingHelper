# Where brokers' calls come from

Brokers' buy / hold / sell calls are read from news feeds (`config/broker_calls.yaml`,
`igs.brokers`). The data check reads every feed every 30 minutes, at most once in 25 minutes
a feed. Each article is handled in one of three ways:
- **a headline that states the whole call** (Moneycontrol's "Buy X; target of Rs N: Broker")
  is recorded as it stands;
- **an article that mentions a rating, a target or a brokerage** is read by the AI, which
  records each explicit call;
- **anything else** is left out.

The same call reported by two sites is kept once. A call counts as the same when it has the
same stock, broker, rating and target within a few days (`igs.brokers.add_call`).

## The feeds

Until 24 November 2026 there were three: two Economic Times feeds and Moneycontrol's news
sitemap. Six were added that day.

To find them:
1. Each site's own list of feeds was read, and other sites were looked up on the web.
2. Every feed found was read with the app's own reader (`igs.news.parse_feed`).
3. The articles naming a broker's call were counted, over the days each feed covers.

| Feed | Kind | Days covered | Articles | Read by the AI | Naming a broker's call |
|---|---|---|---|---|---|
| Financial Express, market news | news sitemap, `/market/` | 2 | 53 | 16 | 10 |
| CNBC-TV18, markets | RSS | 5 | 200 | 38 | 17 |
| Business Standard, markets | RSS | 2 | 35 | 5 | 4 |
| Business Today | RSS | 6 | 101 | 14 | 6 |
| Mint, markets | RSS | 1 | 35 | 9 | 1 |
| BusinessLine, stock markets | RSS | 6 | 52 | 4 | 1 |
| *Moneycontrol (already read)* | news sitemap | 2 | 68 | 9 | 6, 4 of them whole calls in the headline |
| *Economic Times, stocks news (already read)* | RSS | 2 | 50 | 6 | 1 |

**Tried and left out:**
- the Economic Times "stock recos" feeds, which are empty;
- the Economic Times podcast feed;
- NDTV Profit and Zee Business, whose feeds refuse automated readers;
- Whalesbook, whose news sitemap was empty;
- Trendlyne's research-report pages, which aren't a feed.

Each feed's robots.txt allows the path read. The Financial Express sitemap is the one it
lists in robots.txt.

**Tried from here, all nine together** (the app's collection, `igs.brokers.collect`,
24 November 2026):
- 576 articles were read, with no feed errors;
- 4 calls came straight from Moneycontrol's headlines;
- 99 articles were queued for the AI.

From then on, each day adds a few dozen articles for the AI. That is two or three requests of
15 articles at low effort, well inside the daily budget (`config/assistant.yaml`).

## News tone

The AI also reads each article's tone for the stock sentiment adjustment
(`igs.assistant.news_tone`). It does that only for the feeds in `tone_feeds`: the original
three, about 10 requests a day. Reading the six new feeds' articles too would take about 10
more requests a day, a third of the default daily budget, for an adjustment that is capped.
Delete `tone_feeds` to read every feed's.

## Adding or removing a feed

Edit `feeds` in `config/broker_calls.yaml`, or use Settings > Data checks.
- An RSS feed needs a `name` and a `url`.
- A news sitemap also takes `sections`: the URL paths to keep.
- A feed whose newest article is older than `stale_after_days` is reported as stopped.

A setting changed on the Settings page is kept in `data/settings/broker_calls.yaml`, over this
file. If `feeds` was changed there, the new feeds above have to be added there too.
