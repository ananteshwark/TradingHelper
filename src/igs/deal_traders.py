"""The traders whose same-day bets in NSE's bulk deal files keep winning
(docs/DEAL_TRADERS.md).

NSE's bulk deal file names every client whose trades in a stock add up to more than 0.5%
of its shares in a day, with what it bought and sold and at what average prices. A client
that both bought and sold the same stock that day made an **intraday bet** the file shows
whole:
- **its result** is the quantity bought and sold, times the sell price less the buy price,
  before charges;
- **what it carried** to the close (bought less sold) is marked at the day's close, and
  followed into the next session against the Nifty 500.

Each client's record keeps every bet since the app began collecting the files. One with
many bets, nearly all won (by default 20 or more, 80% won, and fewer than 1 in 100 by
chance), is flagged once on Telegram (the deals chat), and listed on the Winning traders
page.

What this can't do: the file appears after the close, so a bet is known only once it is
over. Copying it the same day isn't possible. What can be measured is whether what such a
trader carries overnight moves their way the next session; the page keeps that record.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from decimal import Decimal

from psycopg.rows import dict_row

from igs.timeutil import IST, utc_now

DEFAULTS = {'enabled': True, 'min_bets': 20, 'min_win_rate': 0.8, 'max_p': 0.01}
FOLLOW_DAYS = 60                     # bets this recent get their close and next session


def rule(cfg=None) -> dict:
    """rules.winning_traders in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get('winning_traders') or {})}


def p_value(wins: int, n: int) -> float:
    """The chance of `wins` or more out of `n` coin tosses: how often luck alone wins as
    often."""
    if n <= 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n


def bets(body: str, day: dt.date) -> list[dict]:
    """Each client's buys and sells per stock in one bulk deal file."""
    from igs.promoter_trades import deal_rows
    agg = defaultdict(lambda: {'buy': [Decimal(0), Decimal(0)],
                               'sell': [Decimal(0), Decimal(0)]})
    for r in deal_rows(body, day):
        a = agg[(r['day'], r['symbol'], r['client'].strip().upper())]
        a[r['side']][0] += r['quantity']
        a[r['side']][1] += r['quantity'] * r['price']
    out = []
    for (d, symbol, client), a in agg.items():
        bq, bv = a['buy']
        sq, sv = a['sell']
        buy = bv / bq if bq else None
        sell = sv / sq if sq else None
        matched = min(bq, sq)
        out.append({'trade_date': d, 'symbol': symbol, 'client': client, 'buy_qty': bq,
                    'buy_price': buy, 'sell_qty': sq, 'sell_price': sell,
                    'matched_qty': matched,
                    'pnl_inr': matched * (sell - buy) if matched else None,
                    'ret_pct': (sell / buy - 1) * 100 if matched else None,
                    'net_qty': bq - sq})
    return out


def record(conn, now: dt.datetime | None = None) -> int:
    """The bets in every collected bulk deal file not read yet, oldest first; a later copy
    of a day's file replaces that day's figures. Returns how many files were read."""
    from igs.institutions import _company
    now = now or utc_now()
    files = conn.execute("""select content_hash, fetched_at, body from intraday_deal_fetch f
        where source_url ilike '%%bulk%%' and not exists (
            select 1 from deal_trader_scan s where s.content_hash = f.content_hash)
        order by fetched_at""").fetchall()
    for digest, fetched, body in files:
        try:
            rows = bets(body, fetched.astimezone(IST).date())
        except (ValueError, KeyError, ArithmeticError):
            from igs.alerts.operations import record_issue
            record_issue('deal-traders', 'unreadable bulk deal file')
            rows = []
        for b in rows:
            conn.execute("""insert into deal_trader_bet (trade_date, symbol, client,
                    company_id, buy_qty, buy_price, sell_qty, sell_price, matched_qty,
                    pnl_inr, ret_pct, net_qty, source_hash, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (trade_date, symbol, client) do update set
                    buy_qty = excluded.buy_qty, buy_price = excluded.buy_price,
                    sell_qty = excluded.sell_qty, sell_price = excluded.sell_price,
                    matched_qty = excluded.matched_qty, pnl_inr = excluded.pnl_inr,
                    ret_pct = excluded.ret_pct, net_qty = excluded.net_qty,
                    close = null, net_pnl_inr = null, next_vs_index_pct = null,
                    source_hash = excluded.source_hash, updated_at = excluded.updated_at""",
                         (b['trade_date'], b['symbol'], b['client'],
                          _company(conn, b['symbol'], b['trade_date']), b['buy_qty'],
                          b['buy_price'], b['sell_qty'], b['sell_price'], b['matched_qty'],
                          b['pnl_inr'], b['ret_pct'], b['net_qty'], digest, now))
        conn.execute('insert into deal_trader_scan (content_hash, scanned_at, bets) '
                     'values (%s, %s, %s) on conflict do nothing', (digest, now, len(rows)))
        conn.commit()
    return len(files)


def follow(conn, now: dt.datetime | None = None) -> int:
    """The day's close and the next session against the Nifty 500 for recent bets that
    carried something overnight, once the prices are in."""
    from igs.institutions import outcomes
    now = now or utc_now()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select trade_date, symbol, client, company_id, net_qty, buy_price,
                sell_price from deal_trader_bet
            where company_id is not null and (close is null or (net_qty <> 0
                  and next_vs_index_pct is null)) and trade_date >= %s""",
                    (now.astimezone(IST).date() - dt.timedelta(days=FOLLOW_DAYS),))
        rows = cur.fetchall()
    done = 0
    for r in rows:
        close = conn.execute("""select close from price_eod where exchange = 'NSE'
            and trade_date = %s and symbol = %s order by series = 'EQ' desc limit 1""",
                             (r['trade_date'], r['symbol'])).fetchone()
        if not close:
            continue
        close = Decimal(str(close[0]))
        net = r['net_qty']
        side_price = r['buy_price'] if net > 0 else r['sell_price']
        net_pnl = net * (close - side_price) if net and side_price else Decimal(0)
        nxt = None
        if net:
            moved = outcomes(conn, r).get('next session')
            nxt = moved * (1 if net > 0 else -1) if moved is not None else None
        conn.execute("""update deal_trader_bet set close = %s, net_pnl_inr = %s,
                next_vs_index_pct = %s
            where trade_date = %s and symbol = %s and client = %s""",
                     (close, net_pnl, nxt, r['trade_date'], r['symbol'], r['client']))
        done += 1
    conn.commit()
    return done


def what(client: str, extra=None) -> str:
    """Who a client is, as far as the app knows."""
    from igs.institution_register import _excluded
    from igs.institutions import identify
    flat = ''.join(ch for ch in client.upper() if ch.isalnum())
    if any(x and x in flat for x in _excluded()):
        return 'high-frequency or arbitrage trader'
    inst = identify(client)
    if inst is None and extra is not None:
        inst = extra(client)
    if inst is not None:
        return f'{inst.type}: {inst.group}'
    return 'other: a broker, proprietary trader or individual'


def traders(conn, min_bets: int = 1) -> list[dict]:
    """Every client's record of same-day bets, the most consistent winners first."""
    rows = conn.execute("""
        select client, count(*) as bets, count(*) filter (where pnl_inr > 0) as wins,
               sum(pnl_inr)::float8 as pnl, avg(ret_pct)::float8 as avg_ret,
               count(distinct symbol) as stocks, min(trade_date), max(trade_date),
               count(next_vs_index_pct), avg(next_vs_index_pct)::float8
        from deal_trader_bet where matched_qty > 0
        group by client having count(*) >= %s""", (min_bets,)).fetchall()
    from igs.institution_register import lookup
    extra = lookup(conn)
    out = []
    for client, n, wins, pnl, avg_ret, stocks, first, last, carried, nxt in rows:
        out.append({'client': client, 'bets': n, 'wins': wins, 'win_rate': wins / n,
                    'p_value': p_value(wins, n), 'pnl_inr': pnl, 'avg_ret_pct': avg_ret,
                    'stocks': stocks, 'first': first, 'last': last,
                    'carried': carried, 'carried_next_pct': nxt,
                    'what': what(client, extra)})
    out.sort(key=lambda t: (t['p_value'], -t['win_rate'], -t['bets'], -(t['pnl_inr'] or 0),
                            t['client']))
    return out


def consistent(t: dict, r: dict) -> bool:
    return (t['bets'] >= r['min_bets'] and t['win_rate'] >= r['min_win_rate']
            and t['p_value'] <= r['max_p'])


def describe(t: dict) -> str:
    luck = ('less than 1 in 1,000' if t['p_value'] < 0.001
            else f"1 in {1 / t['p_value']:,.0f}")
    lines = [f"{t['client']} ({t['what']})",
             f"Since {t['first']:%d %b %Y}: {t['bets']} same-day round trips in "
             f"{t['stocks']} stocks, {t['wins']} won ({t['win_rate']:.0%}), "
             f"₹{t['pnl_inr'] / 1e7:,.2f} crore before charges, "
             f"{t['avg_ret_pct']:+.2f}% a round trip on average. Luck alone wins as often "
             f"{luck} times."]
    if t['carried']:
        lines.append(f"What they carried overnight ({t['carried']} times) did "
                     f"{t['carried_next_pct']:+.2f}% the next session against the Nifty 500, "
                     "on average.")
    return '\n'.join(lines)


def alert(conn, notify=None, cfg=None, now: dt.datetime | None = None) -> int:
    """One Telegram message naming the clients that newly qualify as consistent winners;
    each is named once. Returns how many."""
    r = rule(cfg)
    if not r['enabled']:
        return 0
    flagged = {c for (c,) in conn.execute('select client from deal_trader_flag').fetchall()}
    new = [t for t in traders(conn, r['min_bets']) if consistent(t, r)
           and t['client'] not in flagged]
    if not new:
        return 0
    head = ['WINNING TRADERS in NSE\'s bulk deal files',
            f"{len(new)} newly meet the bar: {r['min_bets']} or more same-day round trips, "
            f"{r['min_win_rate']:.0%} or more won, and less than a {r['max_p']:.0%} chance "
            'of that by luck.']
    body = [describe(t) for t in new[:10]]
    more = [f'And {len(new) - 10} more on the Winning traders page.'] if len(new) > 10 else []
    tail = ["Their bets are known only after the close, so they can't be copied the same "
            'day. The page keeps the record of what they carry overnight.']
    text = '\n\n'.join(['\n'.join(head), *body, *more, *tail])
    if notify is None:
        from igs.alerts.delivery import send_deals as notify
    if not notify(text):
        return 0
    now = now or utc_now()
    for t in new:
        conn.execute("""insert into deal_trader_flag (client, flagged_at, bets, wins, alert_text)
            values (%s, %s, %s, %s, %s) on conflict do nothing""",
                     (t['client'], now, t['bets'], t['wins'], text))
    conn.commit()
    return len(new)


def step(conn, notify=None, now: dt.datetime | None = None) -> str:
    """The deals job's part: read new bulk deal files, follow recent bets, flag winners."""
    from igs.alerts.delivery import TelegramError
    now = now or utc_now()              # one time for the run: what is read, and who is named
    files = record(conn, now)
    followed = follow(conn, now)
    try:
        flagged = alert(conn, notify=notify, now=now)
    except TelegramError as exc:
        conn.rollback()
        return f'Deal traders: {files} files read, {followed} followed; alert waits: {exc}'
    return f'Deal traders: {files} files read, {followed} followed, {flagged} newly flagged'
