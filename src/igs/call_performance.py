"""Observed price performance from a recommendation date, not a trade simulation."""
from igs.assistant.calls import _price_rows, price_index
from igs.timeutil import IST, utc_now


def measure(prices, recommended_on, action, today):
    """Use the recommendation day's close, or the next available close within 7 days.

    Prices must be ordered ascending. Raw prices are displayed, while the exchange's
    adjusted previous-close chain removes split/bonus jumps from directional results.
    """
    result = {'entry price date': None, 'entry price (Rs)': None,
              'latest price date': None, 'latest price (Rs)': None,
              'price change (%)': None, 'adjusted change (%)': None,
              'directional return (%)': None, 'performance': 'Missing entry price',
              'price age (days)': None}
    eligible = [p for p in prices if recommended_on <= p[0] <= today]
    if recommended_on > today:
        result['performance'] = 'Awaiting recommendation date'
        return result
    if not eligible or (eligible[0][0] - recommended_on).days > 7:
        return result
    first, last = eligible[0], eligible[-1]
    result.update({'entry price date': first[0].isoformat(),
                   'entry price (Rs)': first[1], 'latest price date': last[0].isoformat(),
                   'latest price (Rs)': last[1], 'price age (days)': (today-last[0]).days})
    if first[1] <= 0 or any(p[1] <= 0 for p in eligible):
        result['performance'] = 'Invalid price'
        return result
    if first[0] == last[0]:
        result['performance'] = 'Awaiting later price'
        return result
    raw = 100 * (last[1] / first[1] - 1)
    adjusted = 100 * (price_index(eligible)[-1][1] - 1)
    directional = adjusted if action == 'buy' else -adjusted if action == 'sell' else None
    result.update({'price change (%)': round(raw, 2),
                   'adjusted change (%)': round(adjusted, 2),
                   'directional return (%)': round(directional, 2)
                       if directional is not None else None,
                   'performance': 'Hold — not scored' if directional is None else
                       'Unchanged' if abs(directional) < 0.005 else
                       'Right direction' if directional > 0 else 'Wrong direction'})
    if result['price age (days)'] > 7:
        result['performance'] = 'Stale prices — ' + result['performance']
    return result


def enrich(conn, items, recommendations, today=None):
    """Load once per company, sharing history across its broker and AI calls."""
    today = today or utc_now().astimezone(IST).date()
    starts = {}
    for company_id, day, _ in recommendations:
        if company_id is not None:
            starts[company_id] = min(day, starts.get(company_id, day))
    prices = {cid: _price_rows(conn, cid, start, today) for cid, start in starts.items()}
    for item, (cid, day, action) in zip(items, recommendations, strict=True):
        item.update(measure(prices.get(cid, []), day, action, today))
    return items
