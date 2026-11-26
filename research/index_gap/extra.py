# ruff: noqa: E402, F821 - runs gap.py's setup with exec, so its names are defined at run time
"""Two checks beyond the protocol, written after its results were seen (README): what the
universes made held in equal weights with no selection, and M bought at the next session's
close rather than at the signal's own close.

Usage: the same arguments as gap.py.
"""

MARK = '# ' + '-' * 67 + ' the index'          # where gap.py's setup ends
src = open(__file__.replace('extra.py', 'gap.py')).read()
assert MARK in src
exec(compile(src.split(MARK)[0], 'gap.py', 'exec'))
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

# Equal weights, every stock in the universe, rebalanced monthly at the signal's close.
for name, universe in (('the 200 largest', cap200), ('the 500 most traded', most_traded)):
    def every(me, prev, universe=universe):
        ids = universe(me)['sid'].to_list()
        return {s: 1 / len(ids) for s in ids}
    r, _ = simulate(every, False)
    print(f"All of {name}, equal weights, monthly: {ann(r):+.1%}")

# M bought at the close of the session after each month end, held to the close of the
# session after the next month end.
sessions = px.select('day').unique().sort('day')['day'].to_list()
after = {me: sessions[sessions.index(me) + 1] for me in MES[:-1]}
closes = px.select('sid', 'day', 'aclose').sort('day')


def lagged_fwd(me, nxt):
    a = closes.filter(pl.col('day') <= after[me]).group_by('sid').agg(
        pl.col('aclose').last().alias('a'), pl.col('day').last().alias('da'))
    b = closes.filter(pl.col('day') <= after.get(nxt, nxt)).group_by('sid').agg(
        pl.col('aclose').last().alias('b'))
    j = a.filter(pl.col('da') >= me).join(b, on='sid')
    return dict(zip(j['sid'].to_list(), (j['b'] / j['a'] - 1).to_list(), strict=True))


target = strategy(most_traded, m_score, 20, 'equal', False)
rets, gross = [], []
w_prev = {}
for me, nxt in MONTHS:
    w = target(me, None)
    f = lagged_fwd(me, nxt)
    port = sum(w[s] * (f.get(s) or 0.0) for s in w)
    turnover = sum(abs(w.get(s, 0) - w_prev.get(s, 0)) for s in set(w) | set(w_prev))
    gross.append(port)
    rets.append((1 - COST * turnover) * (1 + port) - 1)
    grown = {s: w[s] * (1 + (f.get(s) or 0.0)) for s in w}
    tot = sum(grown.values())
    w_prev = {s: v / tot for s, v in grown.items()}
print(f"M bought at the next session's close: {ann(np.array(gross)):+.1%} before costs, "
      f"{ann(np.array(rets)):+.1%} after")
print(f"(months {MONTHS[0][0]} to {MONTHS[-1][1]})")
