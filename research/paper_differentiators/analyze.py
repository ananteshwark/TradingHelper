"""Descriptive winner/loser comparisons; never changes model or trading settings.

Run against the frozen numerical export described in docs/PAPER_CALL_DIFFERENTIATORS.md.
Days, not randomly selected stock rows, define the chronological partitions.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
from pathlib import Path

PREVIOUS = {'prev_r1', 'prev_clv', 'prev_last_hour', 'prev_intraday', 'last30',
            'rvol_last30', 'close_vwap', 'rel1', 'f_prev_ret'}
MARKET = {'f_nifty_day', 'f_nifty_30', 'f_crowd', 'f_crowd_side'}
EXCLUDE = {'trade_side', 'f_side', 'log_value20', 'prediction_strength_pct',
           'predicted_net_pct', 'chosen_target_pct'}


def category(feature):
    if feature.startswith('n_') or feature in MARKET:
        return 'market'
    return 'previous_session' if feature in PREVIOUS else 'stock'


def valid(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def describe(rows, feature):
    groups = [[r['features'][feature] for r in rows
               if (r['net_inr'] > 0) == win and valid(r['features'].get(feature))]
              for win in (True, False)]
    w, loss = groups
    wm, lm = (st.median(g) if g else None for g in groups)
    effect = None
    if len(w) >= 3 and len(loss) >= 3:
        sd = math.sqrt(((len(w)-1)*st.variance(w)+(len(loss)-1)*st.variance(loss))
                       / (len(w)+len(loss)-2))
        effect = (st.mean(w)-st.mean(loss))/sd if sd else None
    return {'winners': len(w), 'losers': len(loss), 'winner_median': wm,
            'loser_median': lm, 'median_gap': wm-lm if wm is not None and lm is not None
            else None, 'standardized_mean_gap': effect}


def ranks(values):
    ordered = sorted(values)
    return [st.mean([i for i, x in enumerate(ordered) if x == v]) for v in values]


def daily_association(rows, feature):
    """One observation per date; descriptive rank correlation, not a p-value."""
    points = []
    for day in sorted({r['date'] for r in rows}):
        dayrows = [r for r in rows if r['date'] == day
                   and valid(r['features'].get(feature))]
        if dayrows:
            points.append((st.mean(r['features'][feature] for r in dayrows),
                           st.mean(r['net_inr'] for r in dayrows)))
    x, y = ([p[i] for p in points] for i in (0, 1))
    rho = st.correlation(ranks(x), ranks(y)) if (len(x) >= 3 and len(set(x)) > 1
                                               and len(set(y)) > 1) else None
    return {'days': len(points), 'spearman_mean_daily_pnl': rho}


def within_day(rows, feature):
    """Equal weight for each date having both winners and losers on this side."""
    gaps = []
    for day in sorted({r['date'] for r in rows}):
        groups = [[r['features'][feature] for r in rows if r['date'] == day
                   and (r['net_inr'] > 0) == win and valid(r['features'].get(feature))]
                  for win in (True, False)]
        if all(groups):
            gaps.append(st.mean(groups[0])-st.mean(groups[1]))
    nonzero = [v for v in gaps if abs(v) > 1e-12]
    return {'mixed_outcome_days': len(gaps), 'nonzero_gap_days': len(nonzero),
            'mean_daily_gap': st.mean(gaps) if gaps else None,
            'days_winners_higher': sum(v > 1e-12 for v in gaps),
            'days_winners_lower': sum(v < -1e-12 for v in gaps)}


def analyze(data):
    out = {}
    for strategy, item in data['strategies'].items():
        dates = sorted(set(item['days']))
        cutoff = dates[len(dates)*2//3]
        groups = {}
        for side in (0, 1, -1):
            rows = [r for r in item['rows'] if side == 0 or r['side'] == side]
            early = [r for r in rows if r['date'] < cutoff]
            later = [r for r in rows if r['date'] >= cutoff]
            features = {}
            for f in sorted({f for r in rows for f in r['features']}):
                if f in EXCLUDE or f.startswith('dow_'):
                    continue
                a, b = describe(early, f), describe(later, f)
                sufficient = all(s['winners'] >= 3 and s['losers'] >= 3 for s in (a, b))
                same = (sufficient and all(s['standardized_mean_gap'] is not None
                                          for s in (a, b))
                        and a['median_gap']*b['median_gap'] > 0
                        and a['standardized_mean_gap']*b['standardized_mean_gap'] > 0
                        and a['median_gap']*a['standardized_mean_gap'] > 0
                        and b['median_gap']*b['standardized_mean_gap'] > 0)
                features[f] = {'category': category(f), 'all': describe(rows, f),
                               'earlier': a, 'later': b,
                               'same_direction_in_both_blocks': bool(same),
                               'adequate_group_counts_for_comparison': sufficient,
                               'within_day': within_day(rows, f),
                               'daily': daily_association(rows, f),
                               'daily_earlier': daily_association(early, f),
                               'daily_later': daily_association(later, f)}
            groups['all' if side == 0 else 'buy' if side == 1 else 'short'] = {
                'trades': len(rows), 'wins': sum(r['net_inr'] > 0 for r in rows),
                'days': len({r['date'] for r in rows}),
                'earlier_trades': len(early), 'later_trades': len(later),
                'features': features}
        out[strategy] = {'later_starts': cutoff, 'groups': groups}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = analyze(json.loads(args.snapshot.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False))
    for strategy, item in result.items():
        for side, group in item['groups'].items():
            print(strategy, side, {k: v for k, v in group.items() if k != 'features'})
            for cat in ('stock', 'previous_session', 'market'):
                features = [(f, v) for f, v in group['features'].items()
                            if v['category'] == cat and v['same_direction_in_both_blocks']]
                features.sort(key=lambda pair: -abs(pair[1]['earlier']['standardized_mean_gap']))
                for f, v in features[:4]:
                    print(cat, f, json.dumps({k: v[k] for k in ('all', 'earlier', 'later',
                                                              'within_day', 'daily')}))


if __name__ == '__main__':
    main()
