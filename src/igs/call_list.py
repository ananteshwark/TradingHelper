"""One display model for broker-originated and independent AI calls."""
from igs import brokers
from igs.timeutil import IST


def confirmed_action(stance, verdict):
    return stance if verdict == 'agree' and stance in ('buy', 'sell', 'hold') else None


def rows(conn, ai_calls, days=30):
    result = []
    recommendations = []
    for c in brokers.recent(conn, days):
        recommendations.append((c['company_id'], c['called_on'], c['stance']))
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
            'confidence': c['ai_confidence'],
            'confidence level': confidence_level(c['ai_confidence']), 'link': c['url']})
    for c in ai_calls:
        recommendations.append((c.get('company_id'), c['created_at'].astimezone(IST).date(),
                                c['action']))
        result.append({'id': f"AI {c['call_id']}",
            'date': c['created_at'].astimezone(IST).date().isoformat(), 'source': 'AI',
            'stock': c['symbol'], 'broker': '', 'original call': c['action'].capitalize(),
            'call': c['action'].capitalize(), "AI's verdict": 'Independent call',
            'AI reviewed (IST)': c['created_at'].astimezone(IST).strftime('%Y-%m-%d %H:%M'),
            'why': c['summary'], 'target (Rs)': None,
            'confidence': c['confidence'],
            'confidence level': confidence_level(c['confidence']), 'link': None})
    from igs.call_performance import enrich
    return sorted(enrich(conn, result, recommendations),
                  key=lambda r: (r['date'], r['id']), reverse=True)


def confidence_level(value):
    if value is None:
        return 'Not assessed'
    return 'High' if value >= 0.75 else 'Medium' if value >= 0.5 else 'Low'


def frame(items):
    """Infer across all calls, including older rows with the first available return."""
    import polars as pl
    return pl.DataFrame(items, infer_schema_length=None, schema_overrides={
        name: pl.Float64 for name in ('target (Rs)', 'confidence', 'entry price (Rs)',
            'latest price (Rs)', 'price change (%)', 'adjusted change (%)',
            'directional return (%)') if any(name in row for row in items)})
