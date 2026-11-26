# Prominent institutions' deals on Telegram

When a prominent foreign (FII) or domestic (DII) institution buys or sells a stock in a
deal NSE discloses, Telegram gets a message as soon as the app collects NSE's file.

## What is covered

NSE publishes two kinds of named trades each trading day:
- **Bulk deals:** any client's trades in a stock adding up to more than 0.5% of the
  company's shares in a day. NSE's file appears after the close.
- **Block deals:** single large trades in the exchange's block windows.

The app collects both files every 15 minutes on weekdays, 08:00–20:45 IST
(`igs-intraday-deals.timer`, `uv run igs intraday-deals`).

The same files also show promoters' own deals: a client who is one of the company's known
promoters gets a message of its own in the same chat ([PROMOTER_TRADES.md](PROMOTER_TRADES.md)).

While the market is open, large trading in a stock is flagged within 5 minutes, before
any file names who: [TRADE_SURGES.md](TRADE_SURGES.md).

Trades smaller than these thresholds are not disclosed by name, so they can't be seen:
- ordinary buying and selling below 0.5% a day;
- a fund building a stake over many days.

Quarterly shareholding filings show those changes for holders above 1%, but months later;
they are not alerts.

## Who counts

`config/institutions.yaml` lists about 150 institutions, collected on 17 October 2026 from:
- AMFI's mutual fund houses;
- IRDAI's insurers;
- PFRDA's NPS pension fund managers;
- Prime Database's largest foreign portfolio investors;
- the names they trade under in NSE's 2013–2026 block deals.

| | Examples |
|---|---|
| DII: mutual funds | SBI, ICICI Prudential, HDFC, Nippon India, Kotak, Axis, UTI, Mirae, DSP; any other "mutual fund" |
| DII: insurers | LIC, SBI Life, HDFC Life, ICICI Prudential Life, New India Assurance, GIC Re |
| DII: pension funds | NPS Trust and its fund managers |
| FII: sovereign and public pension funds | GIC Singapore, Norges Bank, ADIA, Japan's GPIF, Canada's CPP and PSP |
| FII: fund managers | Capital Group, Vanguard, BlackRock (iShares), GQG, Fidelity |
| FII: hedge funds | Marshall Wace, Millennium |
| FII: foreign banks and brokers | Goldman Sachs, Morgan Stanley, Citigroup, Societe Generale, BNP Paribas, UBS, JP Morgan |

How names are matched:
- A client name matches when it contains one of an entry's name fragments, ignoring case,
  spaces and punctuation.
- More specific entries come first: HSBC Mutual Fund (DII) is tried before HSBC (FII).
- High-frequency and proprietary trading firms are left out.
- Ambiguous names are not guessed.

Against the 2013–2026 block deals, the list matched 780 client names.

**Foreign banks' desks** (often marked "ODI") frequently trade for clients, through
offshore derivative instruments, or for arbitrage, not for their own investment. Their
messages say so.

## The register: every FPI and mutual fund, refreshed every week

On top of the hand-written list, the app reads SEBI's own registers of intermediaries,
which SEBI updates daily at sebi.gov.in:
- **Foreign portfolio investors** (FPIs and deemed FPIs, the erstwhile FIIs): about 11,700
  in November 2026. They count as FIIs.
- **Mutual funds:** 62. They count as DIIs.

Insurers and pension fund managers change rarely and stay in the hand-written list.

**When:** `igs-institutions.timer` reads both every Sunday at 05:17 IST (`uv run igs
institutions refresh`; Settings > Schedules runs it now).
- It reads SEBI's pages as its own web page does, 2.5 seconds apart, about 20 minutes in
  all.
- Telegram (the deals chat) then gets what changed: the new registrations and the ones no
  longer listed.
- A register SEBI refuses, or serves short, is left as it was and said so.

**How a deal's client is matched:**
1. The hand-written list first: it names the institution and its group.
2. An FPI on the register: the same name, after normalisation. That means capitals, no
   punctuation, and no legal suffix such as "Pte Ltd", "Limited" or "- ODI".
3. A mutual fund on the register: its name appears in the client's, as in "ABAKKUS
   MUTUAL FUND A/C ABAKKUS FLEXI CAP FUND".

**Left out:**
- **Traders, not investors:** high-frequency, market-making and arbitrage traders
  registered as FPIs, such as Jane Street, Citadel Securities or Optiver (`exclude` in
  `config/institutions.yaml`).
- **Smaller deals:** deals by institutions found only in the registers, under ₹5 crore
  (`register_min_value_cr`).

Their messages say "on SEBI's register".

## Takeover-code filings

An FII or DII must also disclose under SEBI's takeover code when its holding in a company
reaches 5%, or changes by 2% or more (Regulation 29). Such an announcement on NSE that
names an institution goes to the same chat, after the 30-minute data check that loads it.
The filing's figures are in its PDF, so the message carries the link. Pledges and other
encumbrances (Regulation 31) are not trades and are left out.

## Settings

Settings > Alerts, `institution_deals` (`config/alerts.yaml`):

| Setting | Default | What |
|---|---|---|
| `types` | FII, DII | |
| `min_value_cr` | 0 | leave out deals worth less than this |
| `register` | true | match SEBI's registers too |
| `register_min_value_cr` | 5 | for institutions found only in the registers |
| `filings` | true | takeover-code filings |

The **Institutional deals** page shows the registers' sizes and last refresh, and the
filings.

This list is separate from **Settings > Investors**, which the intraday scan uses. There,
only exact names an administrator has checked change a call's direction. The alerts
change no call, ranking or order.

## The message

```
FII BOUGHT: Test Industries Ltd (TEST)
Goldman Sachs (Foreign bank or broker: its desks often trade for clients or arbitrage)
NSE block deal on 16 Oct 2026: bought 1,20,000 shares at ₹1,066.67 = ₹12.8 crore
1.20% of the company's shares; 2.1× its average daily trading (20 sessions)
On the day the stock closed +3.1% at ₹1,092.40.
Earlier here since collection began: bought 0.40% of shares on 2 Sep 2026.
Possible impact (history, not a forecast): after 128 block deals in which FIIs bought
1–5 days' trading, 2013–2026, the stock beat the market by +2.0% on the deal day on
average (already in the price). After it: +0.1% the next session and +1.0% over 21
sessions (median -0.6%, t 0.4), so there was no reliable further move.
Client name: GOLDMAN SACHS (SINGAPORE) PTE - ODI
```

- **One message per institution, stock, day and file.** Its client names are added up,
  buys minus sells, at the volume-weighted price of the net side. Equal buys and sells
  send nothing.
- **% of shares:** the net quantity over the total shares in the company's latest
  shareholding filing. It is left out when there is no filing.
- **× daily trading:** the deal's value over the stock's average daily traded value in the
  20 sessions before it.
- **On the day:** the deal day's close, if that day's prices are already loaded.
- **Earlier here:** this institution's previous deals in the stock since collection began.
- **Possible impact:** what happened after past block deals of the same kind (FII or DII,
  buy or sell) and size (`research/institutional_deals`, `config/institution_impact.json`).
  - A group with fewer than 30 past deals falls back to all sizes.
  - With fewer than 30 in all, it says there are too few to say. Domestic institutions
    almost never sell in block deals, so DII sells say this.
  - Bulk deals have no history at NSE, so they are described by block deals of the same
    size.

## What history says the impact is

In 2013–2026 block deals:
- **On the deal day,** stocks bought by these institutions beat the market: by 1.5% for
  FIIs and 2.2% for DIIs, and about 5% for deals of five or more days' trading. The deal
  isn't public until after it.
- **After the deal day,** there was no reliable further move on average.
- **Exception:** the largest FII purchases did better afterwards, +6% over a month, but in
  only 37 cases.

So a message says who is buying or selling. It is not a forecast that the price will keep
moving. The **Institutional deals** page lists every deal with the stock's move against
the Nifty 500 from the deal day's close, next session and 21 sessions on, which shows
from here whether these deals lead anywhere.

## Settings

`rules.institution_deals` in `config/alerts.yaml` (Settings > Alerts):

| Setting | As shipped | Meaning |
|---|---|---|
| `enabled` | true | send the messages |
| `types` | [FII, DII] | which institutions |
| `min_value_cr` | 0 | leave out deals worth less than this many crore |

How sending works:
- Messages need Telegram set up (`IGS_TELEGRAM_TOKEN`, `IGS_TELEGRAM_CHAT_ID`). They go to
  the deals chat if one is set (`IGS_TELEGRAM_DEALS_CHAT_ID`, docs/DEPLOY.md).
- A deal that can't be sent waits for the next collection.
- A deal first seen more than three days ago is no longer sent.

To add an institution, add an entry to `config/institutions.yaml`.

## Deploying

Migration 067 adds `institution_deal`. Rerun `scripts/install-schedules.sh` as the
application user to move the collection from hourly to every 15 minutes.
