"""Illustrative, not pre-registered: the Plan's variants before and after Indian capital-gains
tax, from Rs 10 lakh, on the survivorship-free panel of research/ (docs/PLAN.md). D is the
rule built into igs.plan; B was the first proposal. No results or red-flag exclusions.

Usage: uv run python -I variants.py BUILD_DIR > variants.txt

Equity: STCG 20% (held < 12 months), LTCG 12.5% above Rs 1.25 lakh a year; losses set off
within the financial year (short-term losses against both, long-term only against
long-term), unused losses not carried forward (conservative). Gold ETF: 12.5% if held
over 12 months, else 30% (slab). Tax paid from cash in the July after each financial year.
The 2024 rates are applied to all years."""
import contextlib, io, math, runpy, sys
import numpy as np
sys.argv = ['portfolios.py', sys.argv[1], 'dev']
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path('/home/user/TradingHelper/research/institutional/portfolios.py')
days, AO, AC, LC, RO, sids, sym, entries, universe = (g[k] for k in (
    'days', 'AO', 'AC', 'LC', 'RO', 'sids', 'symbol_of', 'entries', 'universe'))
sim_days = g['sim_days']
from igs.momentum import cost_pct
gold = next(i for i, s in enumerate(sids) if sym[s] == 'GOLDBEES')
CASH = 1.06 ** (1 / 250) - 1
first, last = entries[0], entries[-1]
estart = set(entries)
CAP0 = 10e5

def targets(sig, held, top, buffer, cap):
    u = universe(sig).sort('mom', descending=True)
    r = u['r'].to_list()
    rank = {x: i + 1 for i, x in enumerate(r)}
    keep = [h for h in held if rank.get(h, 999) <= buffer and h != gold]
    names = keep + [x for x in r if x not in keep][:max(0, top - len(keep))]
    v63 = dict(zip(r, u['vol63'].to_list())); v252 = dict(zip(r, u['vol252'].to_list()))
    inv = np.array([1 / (v63[x] if v63.get(x) == v63.get(x) and v63.get(x) else v252[x]) for x in names])
    w = inv / inv.sum()
    for _ in range(20):
        over = w > cap
        if not over.any(): break
        extra = (w[over] - cap).sum(); w[over] = cap
        w[~over] += extra * w[~over] / w[~over].sum()
    return dict(zip(names, w.tolist()))

def fy(d):  # financial year of a date
    return d.year if d.month >= 4 else d.year - 1

def run(top=10, buffer=10, cap=1.0, core=0.60, goldw=0.25, band=0.05, quarterly=True,
        band_rel=0.25, tax=True):
    lots = {}          # r -> list of [units, cost per unit, buy day index]
    cash = CAP0
    rets, prev = [], CAP0
    pending = False
    k = 0
    gains = {}         # fy -> [st, lt] equity; gold [st, lt]
    ggains = {}
    paid = set()
    def units(r): return sum(l[0] for l in lots.get(r, []))
    def sell(r, n, p, d):
        nonlocal cash
        out = 0.0
        while n > 1e-12 and lots.get(r):
            l = lots[r][0]
            q = min(n, l[0])
            gain = q * (p - l[1])
            lt = (days[d] - days[l[2]]).days > 365
            book = ggains if r == gold else gains
            book.setdefault(fy(days[d]), [0.0, 0.0])[1 if lt else 0] += gain
            l[0] -= q; n -= q; out += q * p
            if l[0] <= 1e-12: lots[r].pop(0)
        if not lots.get(r): lots.pop(r, None)
        return out
    def buy(r, v, p, d):
        lots.setdefault(r, []).append([v / p, p, d])
    for d in range(first, last):
        price = lambda r: AO[r, d] if not np.isnan(AO[r, d]) else LC[r, d - 1]
        # Taxes for the year just ended, paid on the first session of July (approx).
        y = fy(days[d]) - 1
        if tax and days[d].month >= 7 and fy(days[d]) > fy(days[first]) and y not in paid:
            paid.add(y)
            st, lt = gains.get(y, [0.0, 0.0])
            if st < 0: lt += st; st = 0.0
            t = 0.20 * max(st, 0) + 0.125 * max(lt - 1.25e5, 0)
            gst, glt = ggains.get(y, [0.0, 0.0])
            t += 0.30 * max(gst, 0) + 0.125 * max(glt, 0)
            cash -= t
        is_month = d in estart
        if is_month or pending:
            val = {r: units(r) * price(r) for r in list(lots) if r != gold}
            gv = units(gold) * price(gold)
            nav = sum(val.values()) + gv + cash
            full = (is_month and (not quarterly or days[d].month in (1, 4, 7, 10))) or pending or k == 0
            corev = nav * core if full else sum(val.values())
            gtarget = nav * goldw if full else gv
            tw = targets(d - 1, list(val), top, buffer, cap) if is_month else (
                {r: v / sum(val.values()) for r, v in val.items()} if val else {})
            want = {}
            for r, w in tw.items():
                if np.isnan(AO[r, d]): continue
                tgt, cur = corev * w, val.get(r, 0.0)
                want[r] = cur if (cur and abs(tgt - cur) <= band_rel * tgt and not full) else tgt
            cost = 0.0
            for r in list(val):
                if r not in want:
                    cash += sell(r, units(r), price(r), d)
                elif want[r] < val[r]:
                    cash += sell(r, (val[r] - want[r]) / price(r), price(r), d)
            for r, v in want.items():
                b = v - val.get(r, 0.0)
                if b > 0:
                    cost += b * cost_pct(RO[r, d]) / 100
                    cash -= b; buy(r, b, price(r), d)
            gb = gtarget - gv
            if gb > 0: cash -= gb; buy(gold, gb, price(gold), d)
            elif gb < 0: cash += sell(gold, -gb / price(gold), price(gold), d)
            cost += abs(gb) * 0.0005
            cash -= cost
            pending = False
            if is_month: k += 1
        cash *= 1 + CASH
        close = sum(units(r) * LC[r, d] for r in lots) + cash
        rets.append(close / prev - 1); prev = close
        gs, cs = units(gold) * LC[gold, d] / close, cash / close
        if abs(gs - goldw) > band or abs(cs - (1 - core - goldw)) > band:
            pending = True
    return np.array(rets)

def stats(r, label):
    nav = np.cumprod(1 + r); dd = (nav / np.maximum.accumulate(nav) - 1).min()
    yr = []
    for y in sorted({d.year for d in sim_days}):
        m = np.array([d.year == y for d in sim_days]); yr.append(np.prod(1 + r[m]) - 1)
    a = np.array([d.year <= 2020 for d in sim_days])
    c = lambda x: (np.prod(1 + x) ** (250 / len(x)) - 1) * 100
    print(f'{label:52s} {c(r):5.1f}%/yr (2013-20 {c(r[a]):5.1f}, 2021-26 {c(r[~a]):5.1f})  worst fall {dd*100:6.1f}%  worst year {min(yr)*100:+4.0f}%  losing {sum(x<0 for x in yr)}/{len(yr)}  Rs 10L -> {nav[-1]*10:,.0f}L')

print(f'{sim_days[0]} to {sim_days[-1]}; illustrative; from Rs 10 lakh')
V = [(dict(), 'A: top 10 risk-weighted, monthly; 60/25/15 quarterly'),
     (dict(top=12, buffer=25, cap=0.15), 'B: top 12, cap 15%, keep to rank 25 (proposed)'),
     (dict(top=10, buffer=15, cap=0.15), 'C: top 10, cap 15%, keep to rank 15'),
     (dict(top=10, buffer=10, cap=0.15), 'D: top 10, cap 15%, monthly')]
for kw, label in V:
    stats(run(tax=False, **kw), label + ', before tax')
    stats(run(tax=True, **kw), label + ', after tax')
