"""Study A (PROTOCOL.md): yearly walk-forward predictions and the portfolio candidates.

  predict DIR                       writes DIR/pred.parquet (signal dates only)
  evaluate DIR dev                  the eight candidates and the momentum baseline, 2016-2020
  evaluate DIR holdout MODEL FREQ N the chosen candidate and the baseline, 2021 onward

Implementation details fixed before any result: the ridge model's features are standardised
on its training rows and clipped at +/-5 standard deviations; a holding that did not trade
on a rebalance day is valued at its last close; a stock is bought only if it trades on the
entry day.
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, '/home/user/TradingHelper/src')
from igs.momentum import cost_pct      # noqa: E402

STOCK = ['r1', 'r5', 'r21', 'r63', 'r126', 'mom', 'vol21', 'vol63', 'max1', 'hi52', 'lo52',
         'sma50', 'sma200', 'ovn21', 'intra21', 'gap1', 'clv1', 'clv5', 'logtv', 'tvr', 'amihud',
         'logpx', 'dlv21', 'dlv_chg', 'dlv_val', 'beta63', 'idio63', 'age']
FEATURES = ['x_' + f for f in STOCK] + ['m_r5', 'm_r21', 'm_r63', 'm_vol21', 'm_breadth', 'm_disp']
FIRST_MODEL_YEAR, HOLDOUT_YEAR = 2016, 2021
H_OF = {'weekly': 5, 'monthly': 21}


def signal_days(days, freq):
    """The last trading day of each month or ISO week, as day indexes."""
    key = (lambda d: (d.year, d.month)) if freq == 'monthly' else (lambda d: d.isocalendar()[:2])
    out = {}
    for i, d in enumerate(days):
        out[key(d)] = i
    return sorted(out.values())[:-1]                 # the last group may be incomplete


def fit_predict(train, test, H):
    import lightgbm as lgb
    from sklearn.linear_model import Ridge
    X, y = train.select(FEATURES).to_numpy(), train[f'y{H}'].to_numpy()
    Xt = test.select(FEATURES).to_numpy()
    mu, sd = np.nanmean(X, 0), np.nanstd(X, 0)
    sd[sd == 0] = 1
    z = lambda a: np.clip(np.nan_to_num((a - mu) / sd), -5, 5)
    ridge = Ridge(alpha=1.0).fit(z(X), y)
    gbm = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=15,
                            min_child_samples=1000, subsample=0.7, subsample_freq=1,
                            colsample_bytree=0.7, reg_lambda=5, random_state=7, verbose=-1)
    gbm.fit(np.nan_to_num(X), y)
    return ridge.predict(z(Xt)), gbm.predict(np.nan_to_num(Xt))


def predict(base):
    panel = pl.read_parquet(base / 'panel.parquet')
    days = pl.read_parquet(base / 'days.parquet')['day'].to_list()
    parts = []
    for freq, H in H_OF.items():
        sig = set(signal_days(days, freq))
        for year in range(FIRST_MODEL_YEAR, days[-1].year + 1):
            cut = next(i for i, d in enumerate(days) if d.year >= year)   # first day of the year
            train = panel.filter((pl.col('di') + 1 + H < cut) & pl.col(f'y{H}').is_not_null())
            test = panel.filter(pl.col('day').dt.year() == year)
            test = test.filter(pl.col('di').is_in(list(sig)))
            if test.is_empty():
                continue
            p_ridge, p_gbm = fit_predict(train, test, H)
            parts.append(test.select('sid', 'symbol', 'day', 'di', 'mom', 'entry_adj', 'entry_raw')
                         .with_columns(pl.lit(freq).alias('freq'), pl.Series('ridge', p_ridge),
                                       pl.Series('gbm', p_gbm)))
            print(freq, year, 'train', train.height, 'test', test.height, flush=True)
    pl.concat(parts).write_parquet(base / 'pred.parquet')


class Prices:
    def __init__(self, base):
        px = pl.read_parquet(base / 'prices.parquet').sort('sid', 'di')
        self.by = {}
        for (sid,), g in px.group_by(['sid'], maintain_order=True):
            self.by[sid] = (g['di'].to_numpy(), g['aopen'].to_numpy(), g['aclose'].to_numpy(),
                            g['open'].to_numpy())

    def value(self, sid, d):
        """Adjusted open on day d, or the last adjusted close before it."""
        di, ao, ac, _ = self.by[sid]
        k = np.searchsorted(di, d)
        if k < len(di) and di[k] == d:
            return ao[k]
        return ac[k - 1] if k > 0 else np.nan

    def raw_open(self, sid, d):
        di, _, _, ro = self.by[sid]
        k = np.searchsorted(di, d)
        return ro[k] if k < len(di) and di[k] == d else None


def simulate(pred, prices, score, N, lo, hi, universe_ret):
    """Monthly series of (net, universe, excess) for a top-N strategy with a 2N buffer."""
    sigs = sorted(pred['di'].unique())
    holdings, rows = [], []
    for k, s in enumerate(sigs[:-1]):
        entry, exit_ = s + 1, sigs[k + 1] + 1
        g = pred.filter((pl.col('di') == s) & pl.col('entry_adj').is_not_null()).sort(score, descending=True, nulls_last=True)
        ranked = g['sid'].to_list()
        top2n = set(ranked[:2 * N])
        keep = [h for h in holdings if h in top2n]
        new = keep + [x for x in ranked if x not in keep][:N - len(keep)]
        bought, sold = set(new) - set(holdings), set(holdings) - set(new)
        cost = 0.0
        for sid in bought | sold:
            raw = prices.raw_open(sid, entry)
            cost += 0.5 * cost_pct(raw) if raw else 0.5 * 0.39
        rets = [prices.value(sid, exit_) / prices.value(sid, entry) - 1 for sid in new]
        gross = float(np.nanmean(rets)) * 100
        net = gross - cost / N
        day = g['day'][0]
        if lo <= day.year <= hi:
            rows.append({'day': day, 'gross': gross, 'net': net, 'cost': cost / N,
                         'universe': universe_ret[(s, exit_)], 'turnover': len(bought) / N})
        holdings = new
    df = pl.DataFrame(rows).with_columns((pl.col('net') - pl.col('universe')).alias('excess'))
    return df


def universe_returns(pred, prices):
    out = {}
    sigs = sorted(pred['di'].unique())
    for k, s in enumerate(sigs[:-1]):
        entry, exit_ = s + 1, sigs[k + 1] + 1
        g = pred.filter((pl.col('di') == s) & pl.col('entry_adj').is_not_null())
        rets = [prices.value(sid, exit_) / prices.value(sid, entry) - 1 for sid in g['sid']]
        out[(s, exit_)] = float(np.nanmean(rets)) * 100
    return out


def summarise(df, label):
    m = df.group_by(pl.col('day').dt.strftime('%Y-%m').alias('m')).agg(
        pl.col('net').sum(), pl.col('universe').sum(), pl.col('excess').sum(), pl.col('cost').sum()).sort('m')
    ex = m['excess']
    years = m.with_columns(pl.col('m').str.slice(0, 4).alias('y')).group_by('y').agg(pl.col('excess').sum()).sort('y')
    return {'candidate': label, 'months': m.height,
            'net_%_month': round(m['net'].mean(), 2), 'universe_%_month': round(m['universe'].mean(), 2),
            'excess_%_month': round(ex.mean(), 2), 'IR': round(ex.mean() / ex.std() * math.sqrt(12), 2),
            't': round(ex.mean() / ex.std() * math.sqrt(m.height), 2),
            'cost_%_month': round(m['cost'].mean(), 2), 'turnover': round(df['turnover'].mean(), 2),
            'years_positive': f"{(years['excess'] > 0).sum()}/{years.height}",
            'by_year': ' '.join(f"{y}:{v:+.1f}" for y, v in years.rows())}


def evaluate(base, period, only=None):
    pred_all = pl.read_parquet(base / 'pred.parquet')
    prices = Prices(base)
    lo, hi = (2016, 2020) if period == 'dev' else (2021, 2026)
    rows = []
    for freq in ('weekly', 'monthly'):
        pred = pred_all.filter(pl.col('freq') == freq)
        uret = universe_returns(pred, prices)
        combos = [(m, N) for m in ('ridge', 'gbm') for N in (10, 20)]
        if only:
            combos = [(only[0], only[2])] if only[1] == freq else []
        for model, N in combos:
            rows.append(summarise(simulate(pred, prices, model, N, lo, hi, uret), f'{model} {freq} N={N}'))
        for N in ((10, 20) if not only else ((only[2],) if only[1] == freq else ())):
            rows.append(summarise(simulate(pred, prices, 'mom', N, lo, hi, uret), f'momentum {freq} N={N}'))
    with pl.Config(tbl_rows=30, tbl_cols=20, tbl_width_chars=260, fmt_str_lengths=80):
        print(pl.DataFrame(rows))


if __name__ == '__main__':
    base = Path(sys.argv[2])
    if sys.argv[1] == 'predict':
        predict(base)
    elif sys.argv[3] == 'dev':
        evaluate(base, 'dev')
    else:
        evaluate(base, 'holdout', (sys.argv[4], sys.argv[5], int(sys.argv[6])))
