# How the largest fund managers and algo platforms trade

The owner asked what logic today's algorithmic trading platforms and the largest fund
managers use. This note combines the published evidence with tests on the app's own data
(`research/institutional/`). Firms keep their models secret, so what follows is what they,
regulators and researchers have made public.

## Who makes the money in India

Algorithms now place most orders on NSE:
- **Cash market:** more than half of turnover in FY25, for the first time since NSE's data
  begins in FY11 ([Business Standard](https://www.business-standard.com/markets/news/in-a-first-machines-overtake-humans-in-nse-s-cash-market-trades-125041500893_1.html)).
- **Futures and options:** about 70% of trading in FY25
  ([Outlook Business](https://www.outlookbusiness.com/amp/story/markets/algos-have-rewritten-the-script-for-trading-in-india-its-time-for-retail-investors-to-play-it-smart)).

SEBI's studies of equity derivatives show who wins:

| | FY24 | FY26 |
|---|---|---|
| Proprietary traders' gross profit | ₹33,000 crore, 96% from algorithms | ₹44,483 crore, 98% from options |
| Foreign investors' gross profit | ₹28,000 crore, 97% from algorithms | ₹13,896 crore, down 55% |
| Individuals | lost over ₹61,000 crore; 93% lost money over FY22–FY24 | 87.7% lost money; net losses ₹91,685 crore |

Sources: [FY24 study coverage](https://www.outlookbusiness.com/markets/fo-frenzy-over-93-retail-investors-lost-average-rs-2-lakh-in-three-years-says-sebi),
[FY26 study coverage](https://www.businesstoday.in/markets/story/rs91685-cr-lost-88-of-individual-traders-lost-money-in-fy26-options-drove-92-of-losses-550474-2026-08-21).

The profits come mostly from index options. They go to firms that quote prices and run
arbitrage at high speed, and individuals are usually the other side of their trades.

## The main kinds of algorithm

| Kind | Who | The logic | Can this app copy it? |
|---|---|---|---|
| Market making | Citadel Securities, Virtu, Jane Street, Optiver | Quote both a buy and a sell price and earn the gap; hedge each fill at once | No: needs co-location and microsecond speed |
| Arbitrage | Proprietary firms, mostly in index options | Keep futures, cash and options prices in line; trade volatility | No |
| Statistical arbitrage | Renaissance, D.E. Shaw, Two Sigma | Thousands of small, short-lived price patterns, balanced long and short, with leverage | Only the principle |
| Factor investing | AQR, BlackRock, Indian quant funds, NSE factor indices | Hold many stocks ranked on value, momentum, quality and low risk; rebalance monthly | Yes: the app's ranking and momentum tracker do this |
| Trend following | Man AHL, Winton, AQR | Go long what is rising and short what is falling, across 60+ futures markets | Partly: needs futures in many asset classes |
| Risk parity and macro | Bridgewater | Spread risk evenly across growth and inflation environments | It is asset allocation, not stock picking |
| Alternative data and machine learning | BlackRock, Two Sigma, Man, Bridgewater | Hundreds of signals from card spending, web traffic, news text and similar data | Partly: the ML intraday paper calls are a small version |
| Execution algorithms | Every large fund | Split large orders over time (VWAP, TWAP, implementation shortfall) to limit price impact | Yes, though it matters little at ₹1 lakh |

**Market makers win small, often.**
- Virtu disclosed one losing day in 1,485 trading days, yet only 49% of the positions it
  closed made money ([Quartz](https://qz.com/186209/the-high-frequency-trading-firm-thats-about-to-go-public-only-had-one-day-of-trading-losses-in-four-years),
  [FT via Euro2day](https://euro2day.gr/ftcom_en/article-ft-en/1321269/virtu-financial-seeks-26bn-from-listing.html)).
- Renaissance's Robert Mercer: "We're right 50.75 percent of the time... You can make
  billions that way"
  ([Zuckerman, *The Man Who Solved the Market*](https://quartr.com/insights/company-research/renaissance-technologies-and-the-medallion-fund)).
- The edge per trade is tiny. Millions of trades make it reliable.

**Factor investors diversify and combine.**
- AQR's core factors are value, momentum, carry and defensive (low risk, quality).
- Value and momentum returns are negatively correlated, so a mix of the two beats either
  alone ([Asness, Moskowitz & Pedersen 2013](https://pages.stern.nyu.edu/~lpederse/papers/ValMomEverywhere.pdf)).
- Over a century and six asset classes, these factors' returns were about 30% lower out
  of sample, mostly because of overfitting
  ([AQR](https://www.aqr.com/Insights/Research/Journal-Article/How-Do-Factor-Premia-Vary-Over-Time-A-Century-of-Evidence)).
- In 2025, Asness said quality, low risk and momentum had done well while value lagged
  ([Hedgeweek](https://www.hedgeweek.com/aqr-founder-says-rational-investing-is-back-as-quant-funds-surge/)).

**Trend followers protect against long bear markets, at a price.**
- From 1880 to 2016, trend following made money in 8 of the 10 worst drawdowns of a
  60/40 portfolio ([Hurst, Ooi & Pedersen](https://fairmodel.econ.yale.edu/ec439/hurst.pdf)).
- 2024 and the first half of 2025 were poor: by late May 2025 the SG CTA Index was down
  more than 8% for the year
  ([Alternatives Watch](https://www.alternativeswatch.com/2025/06/02/trend-following-hedge-funds-endure-more-pain-societe-generale-indices/)).
  Man AHL says trend systems must keep changing, and applies machine learning to them
  ([Man Group](https://www.man.com/insights/the-big-innovation-imperative)).

**Alternative data decays.**
- BlackRock's systematic equity team draws signals from sources such as foot traffic, web
  visits and local news text
  ([Citywire](https://citywire.com/americas/news/illuminating-lingo-3-weird-ways-blackrock-taps-data-to-gain-an-edge/a1392054)).
  The Financial Times reported that it had more than 1,000 signals, traded 100 to 200 of
  them at a time and changed about a fifth of its models each year, as few signals work
  for much longer than five years ([as quoted by Best Practice AI](https://www.bestpractice.ai/ai-case-study-best-practice/blackrock_chooses_investment_opportunities_to_add_to_its_portfolio_based_on_ai_recommendations)).
- In a study of machine learning on US stocks, trees and neural networks predicted best.
  All methods agreed on the main signals: momentum, liquidity and volatility
  ([Gu, Kelly & Xiu 2020](https://www.nber.org/papers/w25398)).
- In 2024, Bridgewater launched a fund of about $2 billion run mainly by machine learning,
  with humans controlling risk and a manual kill switch
  ([Fortune](https://fortune.com/2024/07/01/bridgewater-2-billion-fund-machine-learning-decision-making-openai-anthropic-perplexity)).

**Multi-manager funds cut losers mechanically.**
- Millennium reportedly halves a portfolio manager's capital after a 5% loss and usually
  dismisses the manager at 7.5%, though not always
  ([Wall Street Journal via eFinancialCareers](https://www.efinancialcareers.com/news/pratik-madhvani-glen-scheinberg)).

**Execution is a field of its own.**
- Price impact grows with roughly the square root of an order's size relative to daily
  volume ([Tóth et al.](https://www.arxiv.org/pdf/1105.1694)), so large funds split their
  orders.
- AQR's own $1.7 trillion of trades over 19 years cost an order of magnitude less than
  academic estimates ([Frazzini, Israel & Moskowitz](https://papers.ssrn.com/abstract=3229719)).

## Indian platforms and the rules

**Retail platforms** (Streak, Tradetron, AlgoTest and brokers' APIs) mostly offer
technical-indicator rules and options-selling templates with backtests.
- SEBI has barred brokers' algo services from citing past or expected returns since 2022.
- In October 2024, it sent notices to more than 120 brokers linked to Tradetron, whose
  strategies advertised monthly returns of 5% to 40%. Penalties of ₹16 lakh in total
  followed in March 2026 ([Moneylife](https://moneylife.in/article/sebi-cracks-down-on-tradetronlinked-brokers-slaps-16-lakh-penalties-over-assured-returns-claims/80053.html)).

**SEBI's retail algo framework** (circular of 4 February 2025, fully in force from 1 April
2026; [timeline](https://www.outlookbusiness.com/markets/sebi-extends-deadline-to-implement-retail-algo-trading-by-april-2026)):
- **Tagging:** every algo order placed through a broker's API carries an exchange-issued
  ID.
- **Providers:** algo providers must be empanelled with the exchanges. A provider of a
  "black box" algo, whose logic the user can't see, must register as a research analyst.
- **Brokers' APIs:** access only with a client-specific key from a whitelisted static IP,
  with OAuth and two-factor login.
- **Your own algos:** a retail investor's own algo needs exchange registration only above
  10 orders a second ([Zerodha](https://zerodha.com/z-connect/general/a-comprehensive-overview-of-nses-circular-on-the-new-retail-algo-trading-framework)).
- **This app:** its automatic Upstox orders are an algo the owner built for their own use.
  At a few orders a day they need no registration, but they need Upstox's API with the
  login and IP rules above.

**The Jane Street case** shows both an algorithmic edge and where SEBI draws the line. The
following are allegations in an interim order, not findings after a hearing
([Oxford Law blog](https://blogs.law.ox.ac.uk/oblb/blog-post/2025/07/jane-street-and-expiry-day-trap-unpacking-sebis-crackdown-algorithmic),
[Mondaq](https://www.mondaq.com/india/commoditiesderivativesstock-exchanges/1648134/sebis-interim-order-against-jane-street-allegations-of-index-manipulation-explained)).
- **The pattern:** on index-option expiry days, the group bought Bank Nifty stocks and
  futures in the morning, often more than 20% of the day's value in several stocks, while
  building bearish options positions. It then sold in the afternoon and pushed the index
  down.
- **One day:** on 17 January 2024 it bought ₹4,370 crore by 11:47 and made ₹735 crore that
  day.
- **The case:** SEBI put the group's India profits from January 2023 to March 2025 at
  ₹36,502 crore. It impounded ₹4,843.57 crore, which Jane Street deposited before
  resuming trading. Jane Street disputes the order. Its appeal is before the Securities
  Appellate Tribunal, which adjourned hearings in February 2026
  ([Business Today](https://www.businesstoday.in/amp/markets/story/jane-street-vs-sebi-sat-adjourns-hearing-in-market-manipulation-case-517925-2026-02-25))
  and again in April.
- **SEBI's response:** since 1 October 2025, each entity's intraday index options position
  is capped at ₹5,000 crore net and ₹10,000 crore gross, delta-adjusted
  ([Business Standard](https://www.business-standard.com/amp/markets/news/sebi-stricter-intraday-position-limits-options-market-regulation-125090200971_1.html)).
- **For individuals:** on expiry days, options traders can be up against firms with the
  scale to move the index.

## What the large managers have in common

1. **Many small edges, not one big call.** Grinold's "fundamental law" says a strategy's
   risk-adjusted return ≈ its skill × √(the number of independent bets)
   ([CFI](https://corporatefinanceinstitute.com/resources/capital-markets/fundamental-law-of-active-management/)).
   Renaissance and Virtu win barely more than half the time, many times over.
2. **Costs are measured, not assumed.** Execution algorithms and cost analysis are
   standard.
3. **Size by risk.** Common tools:
   - volatility targeting;
   - weights inversely proportional to risk;
   - fixed loss limits per manager.
4. **Combine signals that offset each other**, such as value and momentum.
5. **Keep researching, because edges fade.** Across 97 published predictors, returns fell
   26% out of sample and 58% after publication
   ([McLean & Pontiff 2016](https://Www.Gwern.net/doc/economics/2016-mclean.pdf)).
6. **Distrust backtests.** Of 452 published anomalies, 65% failed a careful replication
   ([Hou, Xue & Zhang](https://www.nber.org/papers/w23394)).
   A later study found most factors do replicate, and work in 93 countries
   ([Jensen, Kelly & Pedersen 2023](https://research-api.cbs.dk/ws/portalfiles/portal/95651880/theis_ingerslev_jensen_et_al_is_there_a_replication_crisis_in_finance_publishersversion.pdf)).
7. **Even professionals rarely beat the index.**
   - Over ten years to 2025, 76% of Indian large-cap funds lagged their benchmark
     ([SPIVA India](https://www.spglobal.com/spdji/en/spiva/article/spiva-india-year-end-2025/)).
   - Indian quant funds have mostly matched their benchmarks
     ([DSIJ](https://insights.dsij.in/dsijarticledetail/read-this-before-you-put-money-into-quant-funds-24036)).

## The Indian factor evidence

- **IIM Ahmedabad's factor data, October 1993 to December 2025**
  ([IIMA](https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/drawdown.php)):
  - momentum (winners minus losers) earned 10.96% a year, with 17.3% volatility and a worst
    fall of 62% in 2000;
  - value earned 6.86% a year;
  - small companies lagged large ones by 4.01% a year;
  - the market beat the risk-free rate by 5.63% a year.
- **NSE's Nifty200 Momentum 30** returned 19.3% a year from April 2005 to August 2025,
  against 14.3% for the Nifty 200 (total returns;
  [HDFC AMC](https://files.hdfcfund.com/s3fs-public/Others/2025-10/HDFC%20NIFTY200%20Momentum%2030%20Index%20Fund%20%20Leaflet%20September%202025.pdf)).

## Tested on the app's data

Five practices of large managers were tested on the app's momentum portfolio. The rules
were written down before any result was seen (`research/institutional/PROTOCOL.md`); the
choices between variants were made on 2013–2020 (`CHOICE.md`), and 2021–2026 was run once.

**Setup:**
- **Data:** the survivorship-free daily panel from NSE's files: the 250 most-traded stocks
  on each day, delisted ones included.
- **The base portfolio (M):** the ten stocks with the best 12-1 month momentum, equal
  weights, bought at the open after each month end.
- **Costs:** delivery costs, about 0.39% a round trip on each purchase.
- **Cash:** earns 6% a year.

| | 2013–2020: return a year, Sharpe, worst fall | 2021–Sep 2026: return a year, Sharpe, worst fall | Result |
|---|---|---|---|
| **M, the momentum top ten** | 18.2%, 0.53, −50% | 25.3%, 0.71, −47% | the base |
| Volatility targeting (cut exposure when M's recent volatility is high) | 14.9%, 0.44, −50% | not run | **failed** on 2013–2020 |
| Market trend filter (cash when NIFTYBEES is below its 200-day average) | 11.3%, 0.32, −45% | not run | **failed** on 2013–2020 |
| Momentum among the calmer half of stocks (factor combination) | 21.9%, 0.82, −37% | 22.6%, 0.84, −28% | **not passed**: the gain in Sharpe was not firm enough |
| M's ten, weighted by 1 / volatility (risk weighting) | 20.6%, 0.61, −47% | **30.6%, 0.87, −44%** | **passed** |
| The top 30 instead of ten (more, smaller bets) | 15.5%, 0.48, −52% | **29.6%, 0.92, −34%** | **passed** 2021–2026, but was worse in 2013–2020 |
| All 250 stocks equally (no costs) | 8.5%, 0.22, −55% | 17.9%, 0.69, −24% | for comparison |
| NIFTYBEES | 12.1%, 0.43, −36% | 10.1%, 0.36, −16% | for comparison |

- **Timing the market hurt.** Momentum's best years (2014, 2017, 2020) came straight after
  its most volatile months. Volatility targeting and the trend filter stepped aside then,
  and missed more gains than the losses they avoided. This matches US evidence that
  volatility management usually fails out of sample
  ([Cederburg et al. 2020](https://www.lehigh.edu/~xuy219/research/COWY.pdf)).
- **Risk weighting inside the portfolio helped in both periods.** It kept the same ten
  stocks but put less in the most volatile ones. In 2021–2026, it won in every one of
  2,000 resamples.
- **Momentum among calmer stocks gave similar returns with much shallower falls:** −37%
  instead of −50%, and −28% instead of −47%. Its higher Sharpe ratio held in only 64% of
  resamples in 2021–2026, short of the 80% required.
- **Thirty stocks did better than ten in 2021–2026 but worse in 2013–2020.** So breadth
  alone is not a reliable improvement here.
- **Momentum itself:** on this broader universe it beat the equal-weighted stocks in both
  periods. In the large-cap test ([MOMENTUM.md](MOMENTUM.md)) it lagged in 2013–2020. Its
  falls of about 50% are far deeper than the Nifty's.
- **Five practices were tested,** so one pass at these bars is evidence, not proof.

### Executing the monthly trades

Institutions trade rebalances over time rather than at one moment. For M's trades from
March 2022 in the 197 stocks with 5-minute data, 221 of its 584 trades, buying or selling
at the 09:15 open was compared with other schedules:

| Schedule | Average saving against the open | Average distance from the day's VWAP |
|---|---|---|
| The 09:15 open | — | 1.66% |
| VWAP over the first hour | −0.01% (standard error 0.10%) | 1.04% |
| VWAP over the whole session | +0.03% (0.16%) | — |
| The closing price (VWAP of 15:00–15:30) | +0.03% (0.24%) | 0.92% |

Over all 214,200 stock-days the pattern was the same: the open was 1.06% from the day's
VWAP on average, the first hour's VWAP 0.63%, and the close 0.53%.

- At ₹1 lakh a trade, the app's orders move no prices, so spreading them over time saves
  nothing on average.
- Spreading does make the price less random: a fill at the open can land well away from
  the day's average price.

## What this means here

1. **Don't compete with the fast firms.** Options and intraday speed are where
   proprietary algorithms take their profits, and where 88% of individuals lost money in
   FY26.
2. **What transfers is the portfolio discipline:**
   - many stocks ranked on factors with long evidence (the app's ranking and momentum
     tracker);
   - sized by risk;
   - costs counted;
   - judged over years, not months.
3. **A candidate:** weighting the momentum portfolio's ten stocks by 1 / volatility passed
   both periods. It could become a fourth paper track. Momentum among the calmer half is
   the lower-drawdown alternative.
4. **Don't time the market on top of momentum:** volatility targeting and a trend filter
   both failed here.
5. **For real orders,** a first-hour VWAP makes the fill less a matter of luck. It is not
   a source of profit.
6. **Use transparent algos only.** Avoid any platform that advertises returns: since 2022,
   brokers' algo services have been barred from doing so.
