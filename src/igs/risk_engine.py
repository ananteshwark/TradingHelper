"""An Aladdin-style portfolio engine: a score turned into weights by a linear factor risk
model and an optimizer with trading costs and limits (research/aladdin_engine).

BlackRock describes using Aladdin to weigh "alpha, risk, liquidity, transaction costs and
constraints". Its models are not public, so this follows the published concept:

- expected return  alpha = IC x sigma x z, with z the score's cross-sectional z-score
  (Grinold's rule);
- risk             Sigma = X F X' + D: industry and style exposures X, their covariance F
  and each stock's own variance D, from a year of daily returns;
- weights          maximize alpha'w - lambda/2 (w - b)'Sigma(w - b) - cost |w - w0|,
  long only, fully invested, each stock at most CAP, each industry within IND_BAND of the
  benchmark b.

The study's candidate (momentum score, lambda 10) did not beat the Rankings order in its
2021-2026 confirmation (24.4% a year against 37.0%, Sharpe 0.95 against 1.16); it runs
as a paper track at the owner's request (igs.momentum, track 'engine').
"""
from __future__ import annotations

import numpy as np

IC = 0.05
HALF_LIFE = 63                  # sessions, for the factor covariance and specific risk
WINDOW = 252
LAMBDA = 10
COST = 0.00195                  # one way: half of a 0.39% delivery round trip
CAP = 0.05
IND_BAND = 0.10
MIN_WEIGHT = 0.0025             # smaller weights are dropped and the rest scaled up
UNCLASSIFIED = "Unclassified"


def zscore(values) -> np.ndarray:
    """Cross-sectional z-score limited to +-3; missing values score 0."""
    v = np.asarray(values, float)
    ok = ~np.isnan(v)
    z = np.zeros_like(v)
    if ok.sum() > 1 and np.nanstd(v) > 0:
        z[ok] = np.clip((v[ok] - np.nanmean(v)) / np.nanstd(v), -3, 3)
    return z


def exposures(industries: list[str], styles: list[np.ndarray]) -> tuple[np.ndarray, list[str]]:
    """(X, factor names): one column per industry, then the z-scored styles. With too few
    stocks to estimate them (under three per factor), industries give way to one market
    factor, and with fewer still the styles go too."""
    n = len(industries)
    inds = sorted(set(industries))
    sty = [zscore(s) for s in styles]
    if n >= 3 * (len(inds) + len(sty)):
        cols = [np.array([i == k for i in industries], float) for k in inds] + sty
        return np.column_stack(cols), inds + [f"style{j}" for j in range(len(sty))]
    if n >= 3 * (1 + len(sty)):
        return np.column_stack([np.ones(n)] + sty), ["market"] + [f"style{j}"
                                                               for j in range(len(sty))]
    return np.ones((n, 1)), ["market"]


def factor_model(returns: np.ndarray, X: np.ndarray, industries: list[str]
                 ) -> tuple[np.ndarray, np.ndarray]:
    """(F, D), monthly: the covariance of the factors' daily returns and each stock's
    specific variance, exponentially weighted. `returns` is sessions x stocks (NaN where a
    stock has no return); each session's factor returns are a least-squares fit."""
    T, n = returns.shape
    k = X.shape[1]
    f = np.full((T, k), np.nan)
    u = np.full((T, n), np.nan)
    for t in range(T):
        ok = ~np.isnan(returns[t])
        if ok.sum() < 3 * k:
            continue
        coef, *_ = np.linalg.lstsq(X[ok], returns[t, ok], rcond=None)
        f[t] = coef
        u[t, ok] = returns[t, ok] - X[ok] @ coef
    weight = 0.5 ** ((T - 1 - np.arange(T)) / HALF_LIFE)
    good = ~np.isnan(f).any(axis=1)
    if good.sum() > 1:
        fw, ww = f[good], weight[good] / weight[good].sum()
        mean = ww @ fw
        F = ((fw - mean).T * ww) @ (fw - mean) * 21
    else:
        F = np.zeros((k, k))
    seen = ~np.isnan(u)
    w2 = np.where(seen, weight[:, None], 0.0)
    D = np.where(seen.sum(0) >= HALF_LIFE,
                 (np.nan_to_num(u) ** 2 * w2).sum(0) / np.maximum(w2.sum(0), 1e-12) * 21,
                 np.nan)
    if np.isnan(D).all():
        # No stock has enough residuals: each one's own variance over what it has.
        D = np.array([np.nanvar(returns[:, j]) * 21 if (~np.isnan(returns[:, j])).sum() > 1
                      else np.nan for j in range(n)])
    labels = np.array(industries)
    for g in set(industries):
        m = labels == g
        fill = np.nanmedian(D[m]) if np.isfinite(D[m]).any() else np.nanmedian(D)
        D[m & np.isnan(D)] = fill
    D = np.nan_to_num(D, nan=0.1 ** 2)             # no history at all: 10% a month
    return F, np.maximum(D, max(np.percentile(D, 5), 1e-6))


def optimise(alpha: np.ndarray, X: np.ndarray, F: np.ndarray, D: np.ndarray, b: np.ndarray,
             w0: np.ndarray, industries: list[str], lam: float = LAMBDA) -> np.ndarray | None:
    """The weights, or None when no solver finds them. `w0` is the current holding of each
    stock, after the month's moves; holdings outside the list are sold whatever the
    weights. Each stock is capped at CAP, or 2/n in a list too short for CAP to fill."""
    import cvxpy as cp
    n = len(alpha)
    cap = max(CAP, 2.0 / n)
    vals, vecs = np.linalg.eigh((F + F.T) / 2)
    L = vecs * np.sqrt(np.clip(vals, 0, None))
    w = cp.Variable(n)
    active = w - b
    risk = (cp.sum_squares(L.T @ (X.T @ active))
            + cp.sum_squares(cp.multiply(np.sqrt(D), active)))
    cons = [w >= 0, cp.sum(w) == 1, w <= cap]
    labels = np.array(industries)
    for g in sorted(set(industries)):
        m = (labels == g).astype(float)
        bg = float(m @ b)
        cons += [m @ w <= bg + IND_BAND, m @ w >= max(0.0, bg - IND_BAND)]
    prob = cp.Problem(cp.Maximize(alpha @ w - lam / 2 * risk - COST * cp.norm1(w - w0)), cons)
    for solver in ("CLARABEL", "OSQP", "SCS"):
        try:
            prob.solve(solver=solver)
        except (cp.error.SolverError, ValueError):
            continue
        if prob.status in ("optimal", "optimal_inaccurate") and w.value is not None:
            v = np.clip(np.asarray(w.value).ravel(), 0, None)
            v[v < MIN_WEIGHT] = 0.0
            return _rescale(v, cap) if v.sum() > 0 else None
    return None


def _rescale(v: np.ndarray, cap: float) -> np.ndarray:
    """Weights summing to 1 after dust was dropped, without lifting any above the cap."""
    v = v / v.sum()
    for _ in range(50):
        over = v > cap
        if not over.any():
            break
        spare = (v[over] - cap).sum()
        v[over] = cap
        free = (v > 0) & (v < cap)
        if not free.any():
            break
        v[free] += spare * v[free] / v[free].sum()
    return v


def tracking_error(w: np.ndarray, b: np.ndarray, X: np.ndarray, F: np.ndarray,
                   D: np.ndarray) -> float:
    """Predicted tracking error a year, as a fraction."""
    a = w - b
    y = X.T @ a
    return float(np.sqrt(max(0.0, y @ F @ y + (D * a * a).sum()) * 12))
