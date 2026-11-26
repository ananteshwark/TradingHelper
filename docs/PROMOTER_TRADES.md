# Promoters buying and selling, on Telegram

When a company's promoter, or a member of its promoter group, buys or sells the company's
shares through the market, Telegram gets a message within 15 to 30 minutes of the app
seeing it. This covers every listed company, not only the watchlist.

## Why it was missing before

Before 18 November 2026 a promoter's trade reached Telegram only:
- **On the watchlist:** when the company was on the watchlist (`watchlist_insider_trades`);
- **Late:** at the next daily job, up to four hours after its disclosure was loaded;
- **From one source:** from the insider-trading disclosure, never from NSE's deal files or
  from takeover-code filings.

So a promoter's sale or purchase in a company like Azad Engineering or Greenlam
Industries, off the watchlist, sent nothing.

## Where it comes from

| Source | When the app sees it | What it says |
|---|---|---|
| **Insider-trading disclosure** (SEBI PIT, Regulation 7(2)) | the data check after NSE broadcasts it, every 30 minutes; usually 1–2 trading days after the trade | who, buy or sell, shares, value, the mode, holding before and after |
| **NSE bulk or block deal file**, when the client is a known promoter | the deals collection, every 15 minutes, 08:00–20:45 IST; the deal day itself | who, buy or sell, shares, price; % of the company's shares |
| **Takeover-code disclosure** (SEBI SAST Regulation 29) naming a known promoter | the data check, from NSE's announcements | the announcement's text and its PDF, where the figures are |

**Promoters only:**
- **Counted:** the disclosed category says Promoter or Promoter Group, including "Member
  of the Promoter Group".
- **Not counted:** directors and key managers who aren't promoters. They stay with the
  watchlist alert.

**Through the market only:**
- **Counted:** market purchases and sales.
- **Left out:** transfers between promoters (inter-se), gifts, pledges, ESOPs, allotments,
  bonuses and rights. These don't change what the market holds.
- **Kept:** a mode the app doesn't know, such as "Block Deal", with its name as
  disclosed.

**A known promoter** is a name in the company's earlier insider-trading disclosures as a
promoter or promoter group. Names are matched exactly after their spelling is normalised:
- capitals;
- no punctuation;
- no "Mr", "Smt", "Pvt Ltd" or "Limited".

A promoter who has never filed such a disclosure since 2012 isn't known. Their deal shows
only when the disclosure arrives.

**Takeover-code filings:**
- **Included:** holdings of 5% or more, or changes of 2% or more (Regulation 29).
- **Left out:** pledges and other encumbrances (Regulation 31).
- **Only promoters:** a filing counts when it names a known promoter or says "promoter".
  Funds crossing 5% are left out.

## The message

For example:

```
PROMOTER BOUGHT: Greenlam Industries Ltd (GREENLAM)
Blue Diamond Properties Pvt Ltd (Promoter Group) bought 10,22,000 shares through the market (Market Purchase) for ₹21.40 crore (₹209.39 a share) on 16 Nov 2026.
Their holding: 14.89% → 15.30%.
Disclosed under SEBI's insider-trading rules on 18 Nov 2026 18:00 IST.
On 16 Nov the stock closed +1.2% at ₹212.30.
Earlier here: sold on 02 Sep 2026 (disclosure).
https://nsearchives.nseindia.com/...
```

A deal says "PROMOTER SOLD (NSE block deal)" and names the client as the deal file does.

**The same trade can arrive up to three times:**
1. first from the deal file, the same day;
2. then from the disclosure, with the holding before and after;
3. sometimes from a takeover-code filing.

Each says where it came from.

## Settings

Settings > Alerts, `promoter_trades` (`config/alerts.yaml`):

| Setting | Default | What |
|---|---|---|
| `enabled` | true | |
| `scope` | all | `all`: every listed company; `watchlist`: watchlist companies only |
| `min_value_cr` | 0.5 | outside the watchlist, trades worth less than this many crore are not sent; on the watchlist, every one is |
| `include_buys`, `include_sells` | true, true | |
| `sast` | true | takeover-code filings |

**Where they go:** the institutional deals chat (Settings > Telegram), or the main chat
when that has none.

**When a message can't go:**
- **Telegram unavailable:** the trade waits and is sent on the next run.
- **Too old:** a trade first seen more than three days ago is no longer sent.

**The watchlist alert** (`watchlist_insider_trades`) now leaves promoters to this alert
while it is on, so a watchlist promoter's trade isn't sent twice.

## The record

The **Promoter trades** page lists every trade found, newest first. Each shows the
stock's move against the Nifty 500 from the trade day's close to the next session and
to the 21st.

**What the history says** (research/promoter): promoters' open-market purchases of ₹1
crore or more beat the market by 1.3% over 63 sessions in 2015–2020. They didn't confirm
in 2021–2026: +0.2% after costs, and the median trade trailed the market by 3%. So
these messages are news, not a signal to trade; the page's record shows whether that
changes.

## Commands

```bash
uv run igs sync             # the data check: loads disclosures, then sends promoter trades
uv run igs intraday-deals   # the deals collection: deal files, then promoter deals
```

Migration 083 adds `promoter_trade` and `promoter_deal_scan`.
