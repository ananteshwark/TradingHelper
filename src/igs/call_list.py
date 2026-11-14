"""One display model for broker-originated and independent AI calls."""
import datetime as dt

from igs import brokers
from igs.call_performance import after_close
from igs.timeutil import IST

# The horizon a broker's target is spread over, by the kind of call: a research call's
# target is for a year; a short-term trading idea is taken as one month.
HORIZON_MONTHS = {'research': 12, 'trading': 1}


def confirmed_action(stance, verdict):
    return stance if verdict == 'agree' and stance in ('buy', 'sell', 'hold') else None


def rows(conn, ai_calls, days=30, minimum=None):
    """Broker calls of the last `days` days and `ai_calls`, newest first, measured from the
    call's date. A broker's buy the AI agrees with is given as Buy only when its target
    clears `minimum` % a year (features.call.min_buy_target_pct_a_year; given_buy)."""
    if minimum is None:
        from igs.config import load_assistant
        minimum = load_assistant().features.call.min_buy_target_pct_a_year
    result = []
    recommendations = []
    months = []                     # a broker call's horizon (HORIZON_MONTHS); None for AI
    for c in brokers.recent(conn, days):
        # Broker calls carry a date, not a time: measured from that day's close.
        recommendations.append((c['company_id'], c['called_on'], c['stance']))
        months.append(HORIZON_MONTHS[c['kind']])
        confirmed = confirmed_action(c['stance'], c['ai_verdict'])
        result.append({'id': f"Broker {c['broker_call_id']}",
            'date': c['called_on'].isoformat(), 'source': 'Broker',
            'stock': c['symbol'] or c['stock_name'], 'broker': c['broker'],
            'original call': c['rating'],
            'call': confirmed.capitalize() if confirmed else
                    'Pending review' if c['ai_verdict'] is None else 'Not confirmed',
            "AI's verdict": c['ai_verdict'] or 'Pending',
            'AI reviewed (IST)': c['ai_verdict_at'].astimezone(IST).strftime('%Y-%m-%d %H:%M')
                if c['ai_verdict_at'] else '',
            'why': c['ai_reason'] or '', 'target (Rs)': c['target_price'],
            'target a year (%)': None,
            'confidence': c['ai_confidence'],
            'confidence level': confidence_level(c['ai_confidence']), 'link': c['url']})
    for c in ai_calls:
        months.append(None)
        recommendations.append((c.get('company_id'), c['created_at'].astimezone(IST).date(),
                                c['action'], after_close(c['created_at'])))
        result.append({'id': f"AI {c['call_id']}",
            'date': c['created_at'].astimezone(IST).date().isoformat(), 'source': 'AI',
            'stock': c['symbol'], 'broker': '', 'original call': c['action'].capitalize(),
            'call': c['action'].capitalize(), "AI's verdict": 'Independent call',
            'AI reviewed (IST)': c['created_at'].astimezone(IST).strftime('%Y-%m-%d %H:%M'),
            'why': c['summary'], 'target (Rs)': c.get('target_price'),
            'target a year (%)': c.get('target_pct_a_year'),
            'confidence': c['confidence'],
            'confidence level': confidence_level(c['confidence']), 'link': None})
    from igs.call_performance import enrich
    for item, (company_id, day, *_), horizon in zip(
            enrich(conn, result, recommendations), recommendations, months, strict=True):
        if item['source'] == 'Broker':
            price = item.get('entry price (Rs)')
            if price is None and item['call'] == 'Buy' and company_id is not None:
                near = close_near(conn, company_id, day)
                price = near[1] if near else None
            given_buy(item, minimum, price, horizon)
    return sorted(result, key=lambda r: (r['date'], r['id']), reverse=True)


def close_near(conn, company_id, day):
    """(date, close) a broker's target is measured from: the close on the call's date or
    the next within 7 days, else the last close before it."""
    from igs.assistant.calls import _price_rows
    rows = _price_rows(conn, company_id, day, day + dt.timedelta(days=7))
    if rows:
        return rows[0][0], rows[0][1]
    return conn.execute('''
        select p.trade_date, p.close::float8 from price_eod p
        join security_identifier si on si.id_type = 'ISIN' and si.id_value = p.isin
         and p.trade_date >= si.valid_from
         and (si.valid_to is null or p.trade_date < si.valid_to)
        join security s on s.security_id = si.security_id
        where s.company_id = %s and p.exchange = 'NSE' and p.series in ('EQ', 'BE')
          and p.trade_date < %s and p.close is not null
        order by p.trade_date desc limit 1''', (company_id, day)).fetchone()


def given_buy(item, minimum, price, months):
    """A broker's buy the AI agrees with is given as Buy only when its target is at least
    `minimum` % a year above `price`, the close on the call's date (close_near), spread over
    `months` (HORIZON_MONTHS: a year for a research call, a month for a trading idea)."""
    from igs.assistant.calls import pct_a_year
    pct = pct_a_year(item['target (Rs)'], price, months)
    item['target a year (%)'] = pct
    if item['call'] != 'Buy' or (pct is not None and pct >= minimum):
        return
    item['call'] = ('Not given: no target' if not item['target (Rs)'] else
                    'Not given: no price to measure the target from' if pct is None else
                    f'Not given: target {pct:+.1f}% a year')


def agreement_target(conn, dedupe_key, minimum=None):
    """For a broker + AI agreement alert ('broker_agreement:<broker_call_id>'): whether it
    may be sent, and a line on its target. A sell goes as it is; a buy only when its target
    clears the minimum (given_buy), from the close on the call's date (close_near)."""
    if minimum is None:
        from igs.config import load_assistant
        minimum = load_assistant().features.call.min_buy_target_pct_a_year
    row = conn.execute('''select company_id, stance, target_price::float8, called_on, kind
                          from broker_call where broker_call_id = %s''',
                       (int(dedupe_key.split(':', 1)[1]),)).fetchone()
    if row is None:
        return False, 'Not given: the broker call was deleted'
    company_id, stance, target, day, kind = row
    if stance != 'buy':
        return True, None
    on = close_near(conn, company_id, day)
    item = {'call': 'Buy', 'target (Rs)': target}
    months = HORIZON_MONTHS[kind]
    given_buy(item, minimum, on[1] if on else None, months)
    if item['call'] != 'Buy':
        return False, item['call']
    over = '12 months' if months == 12 else 'one month (a trading idea)'
    return True, (f"Target comes to {item['target a year (%)']:+.1f}% a year over {over} "
                  f"from Rs {on[1]:,.2f}, the close of {on[0]:%d %b %Y}")


def confidence_level(value):
    if value is None:
        return 'Not assessed'
    return 'High' if value >= 0.75 else 'Medium' if value >= 0.5 else 'Low'


def frame(items):
    """Infer across all calls, including older rows with the first available return."""
    import polars as pl
    return pl.DataFrame(items, infer_schema_length=None, schema_overrides={
        name: pl.Float64 for name in ('target (Rs)', 'target a year (%)', 'confidence',
            'entry price (Rs)',
            'latest price (Rs)', 'price change (%)', 'adjusted change (%)',
            'directional return (%)', 'Nifty 500 change (%)', 'excess vs Nifty 500 (%)')
        if any(name in row for row in items)})
