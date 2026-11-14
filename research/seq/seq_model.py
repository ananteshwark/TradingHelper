"""Study C (PROTOCOL.md): walk-forward sequence models against Study B's LightGBM.

  predict FEATURES.parquet SEQ.npz PRED.parquet   quarterly retrain: seq, hybrid, gbm
  evaluate PRED.parquet dev                       eight candidates + baseline, 2023-2024
  evaluate PRED.parquet holdout MODEL K GATE      chosen candidate vs baseline, once

Trades, costs and statistics are Study B's own (research/ml/intraday_model.py).
"""
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml'))
import intraday_model as B  # noqa: E402

DEV = B.DEV
HOLDOUT = (dt.date(2025, 1, 1), dt.date(2026, 10, 8))
AFTER = (dt.date(2026, 10, 9), dt.date(2026, 10, 24))
SEEDS = (1, 2, 3)
EPOCHS, BATCH, LR, WD = 6, 1024, 1e-3, 1e-4


def net(n_static):
    import torch
    from torch import nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.stem = nn.Sequential(nn.Conv1d(12, 64, 5, padding=2), nn.GELU(),
                                      nn.Conv1d(64, 64, 5, padding=2), nn.GELU())
            self.gru = nn.GRU(64, 64, batch_first=True)
            self.head = nn.Sequential(nn.Linear(64 + n_static, 64), nn.GELU(),
                                      nn.Dropout(0.1), nn.Linear(64, 1))

        def forward(self, x, s=None):
            h = self.stem(x.transpose(1, 2)).transpose(1, 2)
            _, last = self.gru(h)
            z = last[-1]
            if s is not None:
                z = torch.cat([z, s], 1)
            return self.head(z).squeeze(1)
    return Net()


def train_predict(Xtr, Str, ytr, Xte, Ste, seed):
    import torch
    torch.manual_seed(seed)
    np.random.seed(seed)
    dev = 'cuda'
    model = net(0 if Str is None else Str.shape[1]).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WD)
    steps = EPOCHS * math.ceil(len(ytr) / BATCH)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    X = torch.from_numpy(Xtr).to(dev)
    S = None if Str is None else torch.from_numpy(Str).to(dev)
    y = torch.from_numpy(ytr).to(dev)
    gen = torch.Generator(device='cpu').manual_seed(seed)
    model.train()
    for _ in range(EPOCHS):
        for idx in torch.randperm(len(y), generator=gen).split(BATCH):
            idx = idx.to(dev)
            with torch.autocast('cuda', dtype=torch.bfloat16):
                p = model(X[idx].float(), None if S is None else S[idx])
            loss = torch.mean((p.float() - y[idx]) ** 2)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
    model.eval()
    out = []
    with torch.no_grad():
        Xt = torch.from_numpy(Xte).to(dev)
        St = None if Ste is None else torch.from_numpy(Ste).to(dev)
        for idx in torch.arange(len(Xt), device=dev).split(4096):
            with torch.autocast('cuda', dtype=torch.bfloat16):
                out.append(model(Xt[idx].float(), None if St is None else St[idx]).float())
    return torch.cat(out).cpu().numpy()


def standardise(train, test):
    """Study B's ridge treatment: training mean/sd, clipped at +/-5, missing as zero."""
    X, Xt = train.select(B.FEATURES).to_numpy(), test.select(B.FEATURES).to_numpy()
    mu, sd = np.nanmean(X, 0), np.nanstd(X, 0)
    sd[sd == 0] = 1
    z = lambda a: np.clip(np.nan_to_num((a - mu) / sd), -5, 5).astype(np.float32)  # noqa: E731
    return z(X), z(Xt)


def predict(features, seqs, out):
    df = B.prepare(features)
    npz = np.load(seqs)
    index = pl.DataFrame({'symbol': npz['symbol'],
                          'day': pl.Series(npz['day']).str.to_date(),
                          'row': np.arange(len(npz['symbol']))})
    df = df.join(index, on=['symbol', 'day'], how='inner').sort('day', 'symbol')
    X = npz['X']
    print('rows with both inputs', df.height, 'of', B.prepare(features).height, flush=True)
    parts = []
    for q, nxt in B.quarters(DEV[0], df['day'].max()):
        train = df.filter(pl.col('day') < q)
        test = df.filter((pl.col('day') >= q) & (pl.col('day') < nxt))
        if test.is_empty():
            continue
        Xtr, Xte = X[train['row'].to_numpy()], X[test['row'].to_numpy()]
        ytr = train['y'].to_numpy().astype(np.float32)
        Str, Ste = standardise(train, test)
        seq = [train_predict(Xtr, None, ytr, Xte, None, s) for s in SEEDS]
        hyb = [train_predict(Xtr, Str, ytr, Xte, Ste, s) for s in SEEDS]
        _, gbm = B.fit_predict(train, test)
        part = test.select('symbol', 'day', 'entry', 'exit', 'target').with_columns(
            pl.Series('seq', np.mean(seq, 0)), pl.Series('hybrid', np.mean(hyb, 0)),
            pl.Series('gbm', gbm),
            *[pl.Series(f'seq_s{s}', p) for s, p in zip(SEEDS, seq, strict=True)],
            *[pl.Series(f'hybrid_s{s}', p) for s, p in zip(SEEDS, hyb, strict=True)])
        parts.append(part)
        print(q, 'train', train.height, 'test', test.height, flush=True)
    pl.concat(parts).write_parquet(out)


def rank_ic(pred, col):
    ics = [g.select(pl.corr(col, 'target', method='spearman')).item()
           for _, g in pred.group_by('day') if g.height >= 10]
    ics = [x for x in ics if x is not None and not math.isnan(x)]
    m, sd = float(np.mean(ics)), float(np.std(ics, ddof=1))
    return round(m, 4), round(m / (sd / math.sqrt(len(ics))), 1)


def daily(t, days):
    return (pl.DataFrame({'day': days}).join(t.group_by('day').agg(pl.col('pnl').sum()),
                                             on='day', how='left').fill_null(0)['pnl']
            .to_numpy())


def row_for(pred, model, k, gate, days):
    t = B.trades(pred, model, k, gate)
    row = {'model': model, 'k': k, 'gate': gate, **B.stats(t, days)}
    for y in sorted({d.year for d in days}):
        yd = [d for d in days if d.year == y]
        row[f'{y}_daily_Rs'] = B.stats(t.filter(pl.col('day').dt.year() == y), yd)['daily_Rs']
    for side, name in ((1, 'long'), (-1, 'short')):
        s = t.filter(pl.col('side') == side)
        row[f'{name}_net_%'] = round(s['net_pct'].mean(), 3) if s.height else None
    return row, t


def show(rows):
    with pl.Config(tbl_cols=30, tbl_width_chars=250):
        print(pl.DataFrame(rows))


def evaluate(path, period, only=None):
    lo, hi = DEV if period == 'dev' else HOLDOUT
    pred = pl.read_parquet(path).filter(pl.col('day').is_between(lo, hi))
    days = sorted(pred['day'].unique())
    print(period, lo, hi, len(days), 'days')
    for col in ('seq', 'hybrid', 'gbm'):
        print('rank IC', col, rank_ic(pred, col))
    if period == 'dev':
        rows = [row_for(pred, m, k, g, days)[0] for m in ('seq', 'hybrid') for k in (3, 10)
                for g in (False, True)]
        rows.append(row_for(pred, 'gbm', 3, True, days)[0])
        show(rows)
        return
    model, k, gate = only
    chosen, tc = row_for(pred, model, k, gate, days)
    base, tb = row_for(pred, 'gbm', 3, True, days)
    show([chosen, base])
    diff = daily(tc, days) - daily(tb, days)
    t = diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff)))
    print(f'daily difference (chosen - gbm): Rs {diff.mean():.0f} a day, t {t:.2f}')
    bar = (chosen['t_daily'] or 0) >= 2 and chosen['daily_Rs'] > 0 and \
        (chosen['PF'] or 0) >= 1.1 and chosen['trades'] >= 100 and \
        chosen.get('2025_daily_Rs', 0) > 0 and chosen.get('2026_daily_Rs', 0) > 0
    print('pass 1 (Study B bar):', bar, '| pass 2 (better than gbm, t >= 2):',
          diff.mean() > 0 and t >= 2)
    for s in SEEDS:
        print(f'seed {s}:', row_for(pred, f'{model}_s{s}', k, gate, days)[0]['daily_Rs'],
              'Rs a day')
    after = pl.read_parquet(path).filter(pl.col('day').is_between(*AFTER))
    if not after.is_empty():
        ad = sorted(after['day'].unique())
        print('after the holdout (reported, too few to judge):', len(ad), 'days')
        show([row_for(after, model, k, gate, ad)[0], row_for(after, 'gbm', 3, True, ad)[0]])


if __name__ == '__main__':
    if sys.argv[1] == 'predict':
        predict(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[3] == 'dev':
        evaluate(sys.argv[2], 'dev')
    else:
        evaluate(sys.argv[2], 'holdout', (sys.argv[4], int(sys.argv[5]), sys.argv[6] == 'gate'))
