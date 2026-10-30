# Event definitions

Written after listing the announcement categories and block-deal counterparties with their
counts and checking samples of each group, before any return or move statistic was computed.
The rules are code: `events_rules.py`.

## Amendment 1: bulk deals dropped

NSE's historical bulk- and block-deal listing returns at most 70 rows a request, with no
paging, and a single busy day of bulk deals exceeds that (for example 15 June 2021 and
5 March 2024). Bulk deals therefore can't be collected whole and are left out. Block deals
stay under the limit once a month is split into weeks or days; any day that still hits the
limit is reported.

## Announcement groups

Each announcement goes to the first group whose rule it matches, by its category and, where
marked, its text:

| Group | Rule |
|---|---|
| DISTRESS | category: default, insolvency, winding up, strike, lockout, disturbance |
| QUERY | category: price movement, spurt in volume, news verification, clarification (the exchange asking about a move) |
| PLEDGE | category or text: pledge, encumbrance |
| ORDER | category or text: an order or contract bagged, received, awarded, won or secured; not court, tribunal, SEBI, tax or penalty orders |
| BUYBACK | category or text: buyback |
| BONUS_SPLIT | category or text: bonus, sub-division, split |
| FUNDRAISE | category or text: allotment or issue of securities, preferential issue, QIP, rights issue, fund raising; not employee stock options |
| ACQ | category: acquisition, amalgamation, merger, scheme of arrangement, diversification, disinvestment, MoU |
| RATING_UP / RATING_DN / RATING | category: credit rating; text says upgraded or revised upward or outlook positive / downgraded, revised downward, outlook negative or default / neither |
| EXIT | category: resignation, cessation, change in management or auditors, of a CFO, CEO, managing director, whole-time director, chairman, auditor or independent director |
| INVESTOR | category: analyst or institutional investor meet, investor presentation |
| SAST | category: takeover-code disclosures (substantial acquisitions) |
| PRESS | category: press release |
| OTHER | category "Updates" not matched above |

Routine filings (trading window, record dates, dividends, meetings, results filings already
covered by the results days, compliance officers, newspaper copies and the like) are not
events.

## Block deals

Each counterparty's quantity is netted for the day; a counterparty that bought and sold the
same quantity is a trader and is left out. A net buyer or seller is an **institution** if
the name is a mutual fund, insurer, pension or provident fund, asset manager, a named
global bank or investor (Goldman Sachs, Morgan Stanley, Government of Singapore and the
like) or a foreign fund (Fund, PLC, Pte, Mauritius, S.A., GmbH, LLC, LP), and not a broker
or trading house (securities, broking, shares, trading). Events: an institution buying or
selling, and anyone buying or selling.
