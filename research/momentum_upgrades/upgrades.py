"""Momentum upgrades from the published evidence against the Rankings order, after costs
and after Indian tax (PROTOCOL.md).

Usage: uv run --no-project --with polars --with numpy python -I upgrades.py \
         PRED UNIVERSE PRICES QUARTERS PERIOD OUT_PARQUET
PRED, UNIVERSE and QUARTERS as inputs.py writes them; PRICES research/promoter's
all-stock adjusted daily prices; PERIOD dev (2016-2020) or holdout (2021 - September 2026).
"""
import bisect
import datetime as dt
import math
import statistics
import sys

import numpy as np
import polars as pl

pred_path, uni_path, prices_path, q_path, period, out = sys.argv[1:7]
FIRST, LAST = {'dev': (dt.date(2016, 1, 1), dt.date(2020, 12, 31)),
               'holdout': (dt.date(2021, 1, 1), dt.date(2026, 9, 30))}[period]
START = 1_000_000.0
COST = 0.00195                  # one way, half of 0.39% a round trip
TOP, WIDE, KEEP_RANK = 20, 40, 60
CAP_T = 0.10
GAP = 5                         # sessions a close may be carried
STCG, LTCG, EXEMPT, CARRY_YEARS = 0.20, 0.125, 125_000.0, 8
CASH = 0.06 / 12
CLOSE = dt.time(15, 30)
STRATEGIES = ['M', 'R', 'R0', 'F', 'E', 'T']
T_STEPS = {'T1': {1}, 'T12': {1, 2}, 'T123': {1, 2, 3}, 'T': {1, 2, 3, 4}}

uni = pl.read_parquet(uni_path).select(pl.col('day').alias('me'), 'sid')
pred = pl.read_parquet(pred_path).select('sid', 'me', 'fwd_1m', 'p_momentum')
x = uni.join(pred, on=['sid', 'me'], how='left')
mes = sorted(x['me'].unique().to_list())
k_of = {me: k for k, me in enumerate(mes)}
months = [(a, b) for a, b in zip(mes[:-1], mes[1:], strict=True) if FIRST <= b <= LAST]
members = {me: g.sort('sid') for (me,), g in x.group_by('me')}

# Sessions and adjusted closes of every stock ever in the universe.
sess = pl.scan_parquet(prices_path).select('di', 'day').unique().collect().sort('di')
days = sess['day'].to_list()
pos_of_di = {d: i for i, d in enumerate(sess['di'].to_list())}
S = len(days)
sids = sorted(x['sid'].unique().to_list())
col = {s: j for j, s in enumerate(sids)}
px = (pl.scan_parquet(prices_path).filter(pl.col('sid').is_in(sids))
      .select('sid', 'di', 'aclose').collect().sort('sid', 'di'))
px = px.with_columns(pl.col('di').replace_strict(pos_of_di).alias('pos'))
C = np.full((S, len(sids)), np.nan)
C[px['pos'].to_numpy(), px['sid'].replace_strict(col).to_numpy()] = px['aclose'].to_numpy()
px = px.with_columns(
    (pl.col('aclose') / pl.col('aclose').shift(1).over('sid') - 1).alias('r'),
    (pl.col('pos') - pl.col('pos').shift(1).over('sid')).alias('gap'))
px = px.filter(pl.col('r').is_not_null() & (pl.col('gap') <= GAP))
RD = np.full((S, len(sids)), np.nan)          # daily returns
RD[px['pos'].to_numpy(), px['sid'].replace_strict(col).to_numpy()] = px['r'].to_numpy()
del px


def pos_at(day):
    """The last session on or before `day`."""
    return bisect.bisect_right(days, day) - 1


def closes_at(p, js=None):
    """Each stock's close at session p, or its last within GAP sessions before."""
    block = C[max(0, p - GAP):p + 1] if js is None else C[max(0, p - GAP):p + 1][:, js]
    outv = np.full(block.shape[1], np.nan)
    for row in block:
        ok = ~np.isnan(row)
        outv[ok] = row[ok]
    return outv


# Monthly returns at the universe's month ends, and the universe's equal-weighted return.
ME_C = np.vstack([closes_at(pos_at(me)) for me in mes])
MR = np.full_like(ME_C, np.nan)
MR[1:] = ME_C[1:] / ME_C[:-1] - 1
MKT = np.full(len(mes), np.nan)
for k in range(1, len(mes)):
    js = [col[s] for s in members[mes[k - 1]]['sid'].to_list()]
    MKT[k] = np.nanmean(MR[k, js])


def residual_scores(k, js):
    """(scaled, unscaled) residual momentum at month end k for columns js."""
    window = range(max(1, k - 35), k + 1)
    rows = [i for i in window if not np.isnan(MKT[i])]
    m = MKT[rows]
    scaled = np.full(len(js), np.nan)
    plain = np.full(len(js), np.nan)
    score_rows = set(range(k - 11, k))       # the 11 months before the latest
    for n, j in enumerate(js):
        y = MR[rows, j]
        ok = ~np.isnan(y)
        if ok.sum() < 24:
            continue
        A = np.column_stack([np.ones(ok.sum()), m[ok]])
        coef, *_ = np.linalg.lstsq(A, y[ok], rcond=None)
        resid = y[ok] - A @ coef
        used = [e for e, i in zip(resid, np.array(rows)[ok], strict=True) if i in score_rows]
        if len(used) < 3:
            continue
        sd = np.std(used, ddof=1)
        plain[n] = float(np.sum(used))
        scaled[n] = float(np.sum(used)) / sd if sd > 0 else np.nan
    return scaled, plain


def discreteness(k, js):
    """Information discreteness over the sessions from 12 months to 1 month before."""
    a, b = pos_at(mes[k - 12]) + 1, pos_at(mes[k - 1]) + 1
    block = RD[a:b][:, js]
    ids = np.full(len(js), np.nan)
    for n in range(len(js)):
        r = block[:, n]
        r = r[~np.isnan(r)]
        if len(r) < 150:
            continue
        p = np.prod(1 + r) - 1
        ids[n] = np.sign(p) * ((r < 0).mean() - (r > 0).mean())
    return ids


q = (pl.read_parquet(q_path).select('sid', 'broadcast').drop_nulls().unique()
     .filter(pl.col('sid').is_in(sids)).sort('sid', 'broadcast'))
broadcasts = {s: g['broadcast'].to_list() for (s,), g in q.group_by('sid')}


def reactions(me, js, sid_list):
    """Each stock's reaction to its latest results, less the universe's, or NaN."""
    p_me = pos_at(me)
    uni_js = [col[s] for s in members[me]['sid'].to_list()]
    outv = np.full(len(js), np.nan)
    for n, (j, s) in enumerate(zip(js, sid_list, strict=True)):
        for b in reversed(broadcasts.get(s, [])):
            d = b.date()
            if d < me - dt.timedelta(days=100):
                break
            i = bisect.bisect_left(days, d)
            p0 = i if i < S and days[i] == d and b.time() < CLOSE else bisect.bisect_right(days, d)
            if p0 + 1 > p_me or p0 < 2:
                continue
            before, after = closes_at(p0 - 2, uni_js), closes_at(p0 + 1, uni_js)
            market = np.nanmean(after / before - 1)
            own = closes_at(p0 + 1, [j])[0] / closes_at(p0 - 2, [j])[0] - 1
            outv[n] = own - market
            break
    return outv


# --- Tax -------------------------------------------------------------------------------

def fy(day):
    return day.year if day.month >= 4 else day.year - 1


def anniversary(day):
    try:
        return day.replace(year=day.year + 1)
    except ValueError:                          # 29 February
        return day.replace(year=day.year + 1, day=28)


def long_term(bought, sold):
    return sold > anniversary(bought)


def settle(g, carry, year):
    """(tax, carry left, short-term and long-term gains after set-off) for a year's
    realised [st gains, st losses, lt gains, lt losses]."""
    stg, stl, ltg, ltl = g
    st, lt = stg - stl, ltg - ltl
    if st < 0 < lt:
        use = min(-st, lt)
        st, lt = st + use, lt - use
    carry = sorted([list(c) for c in carry if year - c[0] <= CARRY_YEARS])
    for c in carry:                             # short-term losses brought forward
        if c[1] == 'st' and st > 0:
            use = min(c[2], st)
            st, c[2] = st - use, c[2] - use
    for kind in ('lt', 'st'):                   # long-term gains: long-term losses first
        for c in carry:
            if c[1] == kind and lt > 0:
                use = min(c[2], lt)
                lt, c[2] = lt - use, c[2] - use
    carry = [c for c in carry if c[2] > 1e-6]
    if st < 0:
        carry.append([year, 'st', -st])
    if lt < 0:
        carry.append([year, 'lt', -lt])
    tax = STCG * max(st, 0.0) + LTCG * max(lt - EXEMPT, 0.0)
    return tax, carry, st, lt


class Book:
    def __init__(self):
        self.lots = {}                          # sid -> [[bought, cost, value], ...] oldest first
        self.cash = START
        self.year = None
        self.g = [0.0, 0.0, 0.0, 0.0]
        self.carry = []
        self.owed = 0.0
        self.paid = 0.0
        self.costs = 0.0
        self.sold = 0.0
        self.held_days = 0.0
        self.gain_st = self.gain_lt = 0.0
        self.counts = {'kept': 0, 'losses': 0, 'exemption': 0}

    def value(self):
        return self.cash + sum(lot[2] for lots in self.lots.values() for lot in lots)

    def holding(self, s):
        return sum(lot[2] for lot in self.lots.get(s, []))

    def sell(self, s, amount, day):
        """Sell `amount` of s (None: all), oldest lots first."""
        lots = self.lots.get(s, [])
        left = self.holding(s) if amount is None else amount
        got = 0.0
        while lots and (left > 1e-6 or lots[0][2] <= 1e-6):
            bought, cost, value = lots[0]
            take = min(left, value)
            basis = cost * take / value if value > 1e-6 else cost
            gain = take - basis
            lt = long_term(bought, day)
            self.g[(2 if lt else 0) + (0 if gain >= 0 else 1)] += abs(gain)
            if gain > 0:
                if lt:
                    self.gain_lt += gain
                else:
                    self.gain_st += gain
            self.sold += take
            self.held_days += take * (day - bought).days
            lots[0][1] -= basis
            lots[0][2] -= take
            left -= take
            got += take
            if lots[0][2] <= 1e-6:
                lots.pop(0)
        if not lots:
            self.lots.pop(s, None)
        fee = COST * got
        self.cash += got - fee
        self.costs += fee

    def buy(self, s, amount, day):
        if amount <= 1e-6:
            return
        fee = COST * amount
        self.lots.setdefault(s, []).append([day, amount, amount])
        self.cash -= amount + fee
        self.costs += fee

    def roll(self, day):
        """Close the financial year when `day` is in a new one."""
        if self.year is None:
            self.year = fy(day)
        elif fy(day) != self.year:
            tax, self.carry, _, _ = settle(self.g, self.carry, self.year)
            self.owed += tax
            self.g = [0.0, 0.0, 0.0, 0.0]
            self.year = fy(day)

    def pay(self, day):
        """Pay the tax owed by selling every holding pro rata."""
        if self.owed <= 0:
            return 0.0
        owed, self.owed = self.owed, 0.0
        invested = self.value() - self.cash
        need = max(0.0, owed - self.cash) / (1 - COST)
        if invested > 0 and need > 0:
            f = min(1.0, need / invested)
            for s in list(self.lots):
                self.sell(s, f * self.holding(s), day)
        self.cash -= owed
        self.paid += owed
        return owed

    def liability(self, day):
        """Tax due if every lot were sold at this close, plus any owed."""
        g = list(self.g)
        for lots in self.lots.values():
            for bought, cost, value in lots:
                gain = value - cost
                g[(2 if long_term(bought, day) else 0) + (0 if gain >= 0 else 1)] += abs(gain)
        tax, *_ = settle(g, self.carry, self.year)
        return tax + self.owed

    def wealth(self, day):
        """After-tax wealth: the value less sale costs and the tax if sold now."""
        invested = self.value() - self.cash
        return self.value() - COST * invested - self.liability(day)


def rebalance_equal(book, targets, day):
    """Hold `targets` in equal weights; sell everything else."""
    V = book.value()
    held = dict((s, book.holding(s)) for s in book.lots)
    n = len(targets)
    Vp = V
    for _ in range(30):
        t = Vp / n
        turnover = sum(abs(t - held.get(s, 0.0)) for s in targets) + \
            sum(v for s, v in held.items() if s not in targets)
        new = V - COST * turnover
        if abs(new - Vp) < 0.01:
            break
        Vp = new
    t = Vp / n
    for s, v in held.items():
        if s not in targets:
            book.sell(s, None, day)
        elif v > t:
            book.sell(s, v - t, day)
    for s in targets:
        v = held.get(s, 0.0)
        if v < t:
            book.buy(s, min(t - v, book.cash / (1 + COST)), day)


def manage_tax(book, top, rank, universe, day, steps):
    """T: the top 20 managed for tax (PROTOCOL.md, steps 1-4)."""
    V = book.value()
    for s in list(book.lots):
        if s not in universe:
            book.sell(s, None, day)
            continue
        if s in top:
            v = book.holding(s)
            if v > CAP_T * V:
                book.sell(s, v - CAP_T * V, day)
            continue
        if 2 in steps and rank.get(s, 10 ** 6) <= KEEP_RANK and any(
                value >= 1.2 * cost and day <= anniversary(bought)
                < day + dt.timedelta(days=62) for bought, cost, value in book.lots[s]):
            book.counts['kept'] += 1
            continue
        book.sell(s, None, day)
    entrants = [s for s in top if s not in book.lots]
    if entrants:
        each = book.cash / (len(entrants) * (1 + COST))
        for s in entrants:
            book.buy(s, each, day)
    elif book.cash > 1:
        room = [s for s in top if s in book.lots]
        each = book.cash / (len(room) * (1 + COST))
        for s in room:
            book.buy(s, each, day)
    if 3 in steps:
        for s in list(book.lots):
            lots = book.lots[s]
            last = None
            for i, (bought, cost, value) in enumerate(lots):
                if value < cost and day <= anniversary(bought) < day + dt.timedelta(days=35):
                    last = i
            if last is not None:
                amount = sum(lot[2] for lot in lots[:last + 1])
                book.sell(s, amount, day)
                book.buy(s, min(amount, book.cash / (1 + COST)), day)
                book.counts['losses'] += 1
    if 4 in steps and day.month == 3:
        for _ in range(10):
            _, _, _, lt = settle(book.g, book.carry, book.year)
            need = EXEMPT - lt
            if need < 1000:
                break
            gains = []
            for s, lots in book.lots.items():
                g = 0.0
                for bought, cost, value in lots:
                    if not long_term(bought, day) or value <= cost:
                        break
                    g += value - cost
                if g > 0:
                    gains.append((g, s))
            if not gains:
                break
            for _, s in sorted(gains, reverse=True):
                if need < 1000:
                    break
                amount = 0.0
                for bought, cost, value in book.lots[s]:
                    if not long_term(bought, day) or value <= cost or need < 1000:
                        break
                    ratio = (value - cost) / value
                    take = min(value, need / ratio)
                    amount += take
                    need -= take * ratio
                book.sell(s, amount, day)
                book.buy(s, min(amount, book.cash / (1 + COST)), day)
                book.counts['exemption'] += 1


# --- The run ----------------------------------------------------------------------------

names = STRATEGIES[:-1] + list(T_STEPS)
books = {n: Book() for n in names}
rows = []
for me, nxt in months:
    g = members[me]
    k = k_of[me]
    sid_list = g['sid'].to_list()
    js = [col[s] for s in sid_list]
    universe = set(sid_list)
    mom = g['p_momentum'].to_numpy()
    order = [sid_list[i] for i in sorted(range(len(sid_list)),
                                         key=lambda i: (-mom[i] if not np.isnan(mom[i])
                                                        else math.inf, sid_list[i]))
             if not np.isnan(mom[i])]
    rank = {s: r + 1 for r, s in enumerate(order)}
    top = order[:TOP]
    wide = order[:WIDE]
    scaled, plain = residual_scores(k, js)

    def best(score, ids=sid_list, n=TOP):
        idx = [i for i in range(len(ids)) if not np.isnan(score[i])]
        idx.sort(key=lambda i: (-score[i], ids[i]))
        return [ids[i] for i in idx[:n]]

    wide_js = [col[s] for s in wide]
    ids = discreteness(k, wide_js)
    f_pick = [s for _, _, s in sorted(
        (0 if not np.isnan(v) else 1, v if not np.isnan(v) else r, s)
        for r, (v, s) in enumerate(zip(ids, wide, strict=True)))][:TOP]
    reac = reactions(me, wide_js, wide)
    e_pick = [s for _, _, s in sorted(
        (0 if not np.isnan(v) else 1, -v if not np.isnan(v) else r, s)
        for r, (v, s) in enumerate(zip(reac, wide, strict=True)))][:TOP]
    targets = {'M': top, 'R': best(scaled), 'R0': best(plain), 'F': f_pick, 'E': e_pick}

    fwd = dict(zip(sid_list, g['fwd_1m'].fill_null(0.0).to_list(), strict=True))
    for name in names:
        book = books[name]
        book.roll(me)
        w_before = book.wealth(me)
        v_before = book.value()
        if name in targets:
            rebalance_equal(book, set(targets[name]), me)
        else:
            manage_tax(book, set(top), rank, universe, me, T_STEPS[name])
        paid = book.pay(me)
        held = len(book.lots)
        # The month: every lot moves with its stock to the next month end.
        for s, lots in book.lots.items():
            for lot in lots:
                lot[2] *= 1 + fwd.get(s, 0.0)
        rows.append({'strategy': name, 'me': me, 'held_to': nxt, 'v_before': v_before,
                     'w_before': w_before, 'paid': paid, 'holdings': held})
    print(me, 'done', flush=True)

# The period's end: value and after-tax wealth if everything were sold at its last close.
end = months[-1][1]
for name in names:
    book = books[name]
    book.roll(end)
    rows.append({'strategy': name, 'me': end, 'held_to': None, 'v_before': book.value(),
                 'w_before': book.wealth(end), 'paid': 0.0, 'holdings': len(book.lots)})
res = pl.DataFrame(rows)
res.write_parquet(out)

# --- Summary ----------------------------------------------------------------------------


def series(name):
    s = res.filter(pl.col('strategy') == name).sort('me')
    v, w, paid = s['v_before'].to_numpy(), s['w_before'].to_numpy(), s['paid'].to_numpy()
    pre = v[1:] / (v[:-1] - paid[:-1]) - 1
    post = w[1:] / w[:-1] - 1
    return pre, post, s


def fall(r):
    wealth = np.cumprod(1 + r)
    return (wealth / np.maximum.accumulate(wealth) - 1).min()


def sharpe(r):
    ex = r - CASH
    return ex.mean() / ex.std(ddof=1)


years = len(months) / 12
pre_m, post_m, _ = series('M')
print(f"\n{period}: {months[0][1]:%b %Y} - {months[-1][1]:%b %Y}, {len(months)} months, "
      f"from Rs {START / 1e5:.0f} lakh\n")
print(f"{'':5s} {'pre-tax':>8s} {'after':>7s} {'vol':>6s} {'Sharpe':>6s} {'worst':>6s} "
      f"{'worst at':>8s} {'turn':>5s} {'held':>5s} {'LT %':>5s} {'tax L':>6s} "
      f"{'vs M at':>8s} {'t at':>5s} {'t pre':>6s} {'names':>5s}")
stats = {}
for name in names:
    pre, post, s = series(name)
    book = books[name]
    a_pre = np.prod(1 + pre) ** (1 / years) - 1
    a_post = np.prod(1 + post) ** (1 / years) - 1
    d_post, d_pre = post - post_m, pre - pre_m
    t_post = d_post.mean() / d_post.std(ddof=1) * math.sqrt(len(d_post)) \
        if d_post.std() > 0 else float('nan')
    t_pre = d_pre.mean() / d_pre.std(ddof=1) * math.sqrt(len(d_pre)) \
        if d_pre.std() > 0 else float('nan')
    avg_v = s['v_before'].mean()
    turn = book.sold / avg_v / years
    held = book.held_days / book.sold / 30.44 if book.sold else float('nan')
    lt_share = book.gain_lt / (book.gain_lt + book.gain_st) \
        if book.gain_lt + book.gain_st else float('nan')
    final_tax = book.liability(end)
    tax = (book.paid + final_tax) / 1e5
    stats[name] = {'post': post, 'a_post': a_post, 'mean_diff': d_post.mean(), 't': t_post}
    print(f"{name:5s} {a_pre:+8.1%} {a_post:+7.1%} {pre.std(ddof=1) * math.sqrt(12):6.1%} "
          f"{sharpe(pre) * math.sqrt(12):6.2f} {fall(pre):6.0%} {fall(post):8.0%} "
          f"{turn:5.1f} {held:5.1f} {lt_share:5.0%} {tax:6.1f} "
          f"{d_post.mean() * 12:+8.1%} {t_post:5.2f} {t_pre:6.2f} "
          f"{s['holdings'].mean():5.1f}")
print("\npre-tax and after: return a year after costs, before and after tax (sold at the "
      "end); vol, Sharpe (over 6%) and worst: before tax; worst at: the worst fall after "
      "tax; turn: one-way turnover a year; held: average months a sold rupee was held; "
      "LT %: share of realised gains that were long-term; tax L: tax paid and due at the "
      "end, Rs lakh; vs M at, t at: mean after-tax monthly difference from M a year and "
      "its paired t; t pre: the same before tax.")

print("\nT's steps:", {n: books[n].counts for n in T_STEPS})

# M in a fund wrapper: no tax inside, 0.3% a year of expenses, 12.5% once at the end.
fund = START * np.prod(1 + pre_m - 0.003 / 12)
fund_after = fund - LTCG * max(0.0, fund - START - EXEMPT)
print(f"\nM in a fund wrapper (illustration): {(fund / START) ** (1 / years) - 1:+.1%} a year "
      f"before tax, {(fund_after / START) ** (1 / years) - 1:+.1%} after")

# Calendar years after tax.
by_year = {}
for name in names:
    _, post, s = series(name)
    for d, r in zip(s['me'].to_list()[1:], post, strict=True):
        by_year.setdefault((name, d.year), []).append(r)
yrs = sorted({y for _, y in by_year})
print('\nAfter-tax return by calendar year (months ending in the year):')
print('      ' + ' '.join(f'{y:>7d}' for y in yrs))
for name in names:
    print(f'{name:5s} ' + ' '.join(f"{np.prod(1 + np.array(by_year[(name, y)])) - 1:+7.1%}"
                                   for y in yrs))

# Deflated Sharpe ratio of the best of the six strategies (after tax).
srs = {n: sharpe(stats[n]['post']) for n in STRATEGIES}
best_name = max(srs, key=srs.get)
r = stats[best_name]['post'] - CASH
T = len(r)
skew = float(((r - r.mean()) ** 3).mean() / r.std() ** 3)
kurt = float(((r - r.mean()) ** 4).mean() / r.std() ** 4)
nd = statistics.NormalDist()
var_sr = float(np.var(list(srs.values()), ddof=1))
gamma = 0.5772156649
N = len(STRATEGIES)
sr0 = math.sqrt(var_sr) * ((1 - gamma) * nd.inv_cdf(1 - 1 / N)
                           + gamma * nd.inv_cdf(1 - 1 / (N * math.e)))
sr = srs[best_name]
dsr = nd.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
print(f"\nDeflated Sharpe ratio of the best after tax ({best_name}, monthly Sharpe {sr:.3f}, "
      f"{N} strategies): {dsr:.2f}")

print('\nPass rule (after-tax monthly difference from M):')
for name in STRATEGIES[1:]:
    st = stats[name]
    print(f"  {name}: mean {st['mean_diff'] * 12:+.1%} a year, t {st['t']:.2f}; after tax "
          f"{st['a_post']:+.1%} vs M {stats['M']['a_post']:+.1%}")
