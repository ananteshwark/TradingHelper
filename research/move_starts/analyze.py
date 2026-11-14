"""Part 1 (anatomy) and Part 2 (prediction) of research/move_starts/PROTOCOL.md.

Usage (with scikit-learn: uv run --with scikit-learn python -I analyze.py ...):
  analyze.py NPZ anatomy
  analyze.py NPZ dev
  analyze.py NPZ confirm BUY=<candidate> SELL=<candidate|none>
Candidates are named EVENT:H (an event held H sessions) or SCORE. Each run prints only its
period.
"""
import datetime as dt
import math
import sys

import numpy as np

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct      # noqa: E402

Z = np.load(sys.argv[1])
mode = sys.argv[2]
args = dict(a.split('=') for a in sys.argv[3:])
days = [dt.date.fromordinal(int(x)) for x in Z['days']]
ND = len(days)
OK, AO, AC, RO, LC, MED, FUT = (Z[k] for k in ('OK', 'AO', 'AC', 'RO', 'LC', 'MED', 'FUT'))
UP, DN = Z['UPSTART'], Z['DNSTART']
NAMES = [str(n) for n in Z['names']]
NS = OK.shape[0]
year = np.array([d.year for d in days])
DEV = (year >= 2013) & (year <= 2020)
CONF = year >= 2021
FUT_COST = 0.119


def window_any(E, back, upto=0):
    """Any event in sessions t-back .. t-upto."""
    cs = np.concatenate([np.zeros((E.shape[0], 1), np.int32), np.cumsum(E, axis=1, dtype=np.int32)],
                        axis=1)
    hi = np.arange(ND) - upto + 1
    lo = np.arange(ND) - back
    hi, lo = np.clip(hi, 0, ND), np.clip(lo, 0, ND)
    return (cs[:, hi] - cs[:, lo]) > 0


def anatomy():
    rows = []
    base = OK & DEV[None, :]
    up, dn = UP & base, DN & base
    up_next = window_any(UP, 0, -10)            # an up-move start within the next 10 sessions
    dn_next = window_any(DN, 0, -10)
    p_up10, p_dn10 = up_next[base].mean(), dn_next[base].mean()
    for k in NAMES:
        E = Z[f'E_{k}']
        w5, wb, wa = window_any(E, 4), window_any(E, 19, 5), window_any(E, -1, -5)
        a, u, d = w5[base].mean(), w5[up].mean(), w5[dn].mean()
        on = E & base
        rows.append((k, int(on.sum()), a, u, d, u / a if a else np.nan, d / a if a else np.nan,
                     wb[up].mean() / wb[base].mean() if wb[base].mean() else np.nan,
                     wb[dn].mean() / wb[base].mean() if wb[base].mean() else np.nan,
                     wa[up].mean() / wa[base].mean() if wa[base].mean() else np.nan,
                     wa[dn].mean() / wa[base].mean() if wa[base].mean() else np.nan,
                     up_next[on].mean() / p_up10 if on.any() else np.nan,
                     dn_next[on].mean() / p_dn10 if on.any() else np.nan))
    print(f'Development 2013-2020: {int(base.sum()):,} investable stock-days, '
          f'{int(up.sum()):,} up-move starts, {int(dn.sum()):,} down-move starts')
    print(f'Base rates: an up-move start within 10 sessions {p_up10:.2%}, a down-move start '
          f'{p_dn10:.2%}')
    print(f"{'event':14s} {'events':>8s} {'any day':>8s} {'up-start':>9s} {'dn-start':>9s} "
          f"{'lift up':>8s} {'lift dn':>8s} {'up t-19..t-5':>12s} {'dn t-19..t-5':>12s} "
          f"{'up t+1..t+5':>11s} {'dn t+1..t+5':>11s} {'P(up in 10) x':>13s} {'P(dn in 10) x':>13s}")
    for r in sorted(rows, key=lambda r: -r[5]):
        print(f'{r[0]:14s} {r[1]:8,d} {r[2]:8.2%} {r[3]:9.2%} {r[4]:9.2%} {r[5]:8.2f} {r[6]:8.2f} '
              f'{r[7]:12.2f} {r[8]:12.2f} {r[9]:11.2f} {r[10]:11.2f} {r[11]:13.2f} {r[12]:13.2f}')
    return {r[0]: ('buy' if r[5] >= r[6] else 'sell') for r in rows}


bench_cache = {}


def bench(e, H):
    if (e, H) not in bench_cache:
        rows = np.flatnonzero(OK[:, e - 1] & ~np.isnan(AO[:, e]))
        x = e + H
        b = np.where(np.isnan(AO[rows, x]), LC[rows, x - 1], AO[rows, x])
        bench_cache[(e, H)] = float(np.nanmean(b / AO[rows, e] - 1))
    return bench_cache[(e, H)]


def slip(med):
    return 0.3 if med < 1e8 else 0.1 if med < 5e8 else 0.0


def trades(E, H, side, period):
    """Net returns (%) of trades after each event: [(entry session, net)]."""
    out, last = [], {}
    rs, ts = np.nonzero(E & OK & period[None, :])
    for r, t in sorted(zip(rs.tolist(), ts.tolist(), strict=True), key=lambda x: x[1]):
        e = t + 1
        if e + H >= ND or np.isnan(AO[r, e]):
            continue
        if side == 'sell' and not FUT[r, t]:
            continue
        if r in last and e < last[r] + H:
            continue
        last[r] = e
        x = e + H
        px = AO[r, x] if not np.isnan(AO[r, x]) else LC[r, x - 1]
        ab = (float(px / AO[r, e]) - 1 - bench(e, H)) * 100
        if not math.isfinite(ab):
            continue
        net = (ab - cost_pct(float(RO[r, e])) - slip(float(MED[r, t])) if side == 'buy'
               else -ab - FUT_COST)
        out.append((e, net))
    return out


def stats(tr):
    if len(tr) < 2:
        return None
    by = {}
    for e, net in tr:
        by.setdefault((days[e].year, days[e].month), []).append(net)
    m = np.array([np.mean(v) for v in by.values()])
    yrs = {}
    for e, net in tr:
        yrs.setdefault(days[e].year, []).append(net)
    return {'trades': len(tr), 'per_trade': np.mean([n for _, n in tr]),
            'median': float(np.median([n for _, n in tr])),
            'months': len(m), 'monthly': m.mean(),
            't': m.mean() / m.std(ddof=1) * math.sqrt(len(m)) if len(m) > 1 and m.std() else 0.0,
            'years': {y: round(float(np.mean(v)), 2) for y, v in sorted(yrs.items())}}


def show(label, s):
    if s is None:
        print(f'{label:24s} too few trades')
        return
    print(f"{label:24s} trades {s['trades']:6d}  per trade {s['per_trade']:+6.2f}%  median "
          f"{s['median']:+6.2f}%  months {s['months']:3d}  monthly {s['monthly']:+6.2f}%  "
          f"t {s['t']:+5.2f}")


# ------------------------------------------------------------------ the combined score
def features():
    """Feature columns for every stock-day: event flags over 5 and 20 sessions, 12-1 month
    return, distance from the 250-session high, log median traded value."""
    cols = []
    for k in NAMES:
        E = Z[f'E_{k}']
        cols += [window_any(E, 4), window_any(E, 19)]
    lag = lambda n: np.concatenate([np.full((NS, n), np.nan, np.float32), LC[:, :-n]], axis=1)  # noqa: E731
    mom = lag(21) / lag(252) - 1
    from numpy.lib.stride_tricks import sliding_window_view
    pad = np.concatenate([np.full((NS, 249), np.nan, np.float32), LC], axis=1)
    hi = np.nanmax(sliding_window_view(pad, 250, axis=1), axis=2)
    dist = LC / hi - 1
    cols += [np.nan_to_num(mom, nan=0.0).clip(-1, 5), np.nan_to_num(dist, nan=0.0),
             np.log(np.nan_to_num(MED, nan=2e7).clip(1e6, None))]
    return cols


def target(H=63):
    """Abnormal return from the next open to the open H sessions later, per stock-day."""
    y = np.full((NS, ND), np.nan, np.float32)
    for t in range(ND - H - 1):
        e, x = t + 1, t + 1 + H
        rows = np.flatnonzero(OK[:, t] & ~np.isnan(AO[:, e]))
        px = np.where(np.isnan(AO[rows, x]), LC[rows, x - 1], AO[rows, x])
        y[rows, t] = px / AO[rows, e] - 1 - bench(e, H)
    return y


def score_calls(side):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    cols = features()
    y = target()
    fit = OK & (year >= 2013)[None, :] & (year <= 2018)[None, :] & ~np.isnan(y)
    val = OK & ((year >= 2019) & (year <= 2020))[None, :]
    X = np.stack([c[fit].astype(np.float32) for c in cols], axis=1)
    lab = (y[fit] >= 0.30) if side == 'buy' else (y[fit] <= -0.25)
    sc = StandardScaler().fit(X)
    model = LogisticRegression(max_iter=300, C=1.0).fit(sc.transform(X), lab)
    del X

    def scores(mask):
        Xm = np.stack([c[mask].astype(np.float32) for c in cols], axis=1)
        out = np.zeros((NS, ND), np.float32)
        out[mask] = model.predict_proba(sc.transform(Xm))[:, 1]
        return out
    sv = scores(val)
    thr = np.quantile(sv[val], 0.995)
    names = [f'{k}{w}' for k in NAMES for w in ('_5', '_20')] + ['mom', 'dist_high', 'log_value']
    top = sorted(zip(model.coef_[0], names, strict=True), key=lambda x: -abs(x[0]))[:12]
    print(f'  {side} score: fitted on {int(fit.sum()):,} stock-days ({lab.mean():.2%} positive); '
          f'threshold {thr:.4f} (top 0.5% of 2019-2020); largest weights: '
          + ', '.join(f'{n} {c:+.2f}' for c, n in top))
    return sv >= thr, scores, thr


def run_dev():
    direction = anatomy()
    print('\nPart 2, development: single events (2013-2020), at least 300 trades')
    cands = {'buy': [], 'sell': []}
    for k in NAMES:
        side = direction[k]
        for H in (21, 63):
            s = stats(trades(Z[f'E_{k}'], H, side, DEV))
            if s and s['trades'] >= 300:
                cands[side].append((f'{k}:{H}', s))
    for side in ('buy', 'sell'):
        print(f'{side.upper()} candidates:')
        for name, s in sorted(cands[side], key=lambda x: -x[1]['t']):
            show(name, s)
    print('\nThe combined score (fitted 2013-2018, judged on 2019-2020):')
    val = (year >= 2019) & (year <= 2020)
    for side in ('buy', 'sell'):
        calls, _, _ = score_calls(side)
        s = stats(trades(calls, 63, side, val))
        show(f'SCORE ({side})', s)
        if s:
            cands[side].append(('SCORE', s))
    for side in ('buy', 'sell'):
        best = max(cands[side], key=lambda x: x[1]['t'], default=None)
        if best and best[1]['monthly'] > 0:
            print(f'CHOSEN {side}: {best[0]} (t {best[1]["t"]:.2f}, {best[1]["monthly"]:+.2f}% a month)')
        else:
            print(f'CHOSEN {side}: none has a positive mean; not confirmed')


def run_confirm():
    for side in ('buy', 'sell'):
        name = args.get(side.upper(), 'none')
        if name == 'none':
            continue
        if name == 'SCORE':
            _, scores, thr = score_calls(side)
            calls = scores(OK & CONF[None, :]) >= thr
            tr = trades(calls, 63, side, CONF)
        else:
            k, H = name.split(':')
            tr = trades(Z[f'E_{k}'], int(H), side, CONF)
        s = stats(tr)
        print(f'Confirmation 2021 to {days[max(e for e, _ in tr)]}, {side}:')
        show(name, s)
        print('  by year:', s['years'])
        pos_years = sum(v > 0 for v in s['years'].values())
        print(f"  PASS: {s['monthly'] > 0 and s['t'] >= 2 and pos_years >= 4}"
              f" (mean > 0, t >= 2, positive in {pos_years} of {len(s['years'])} years)")


if mode == 'anatomy':
    anatomy()
elif mode == 'dev':
    run_dev()
elif mode == 'confirm':
    run_confirm()
