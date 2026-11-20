"""igs.risk_engine: the Aladdin-style factor risk model and optimizer behind the momentum
'engine' paper track (research/aladdin_engine)."""
import numpy as np
import pytest

from igs import risk_engine as re_


def _market(n=60, T=252, seed=3):
    """Daily returns driven by a market factor and two industries, each stock with its own
    noise; stock 0 is three times as noisy as the rest."""
    rng = np.random.default_rng(seed)
    industries = ["Banks" if i % 2 else "IT" for i in range(n)]
    market = rng.normal(0, 0.01, T)
    ind = {"Banks": rng.normal(0, 0.008, T), "IT": rng.normal(0, 0.008, T)}
    noise = rng.normal(0, 0.01, (T, n))
    noise[:, 0] *= 3
    returns = np.column_stack([market + ind[industries[i]] + noise[:, i] for i in range(n)])
    return returns, industries


def test_the_risk_model_finds_industry_risk_and_each_stocks_own():
    returns, industries = _market()
    X, names = re_.exposures(industries, [np.arange(60.0)])
    assert names == ["Banks", "IT", "style0"] and X.shape == (60, 3)
    F, D = re_.factor_model(returns, X, industries)
    assert np.all(np.linalg.eigvalsh(F) >= -1e-12)                  # a covariance
    # Each industry's monthly variance is about (1% + 0.8%)^2-ish x 21, the market included.
    assert F[0, 0] == pytest.approx((0.01 ** 2 + 0.008 ** 2) * 21, rel=0.35)
    # The noisy stock's own variance is about nine times the others'.
    assert D[0] / np.median(D[1:]) == pytest.approx(9, rel=0.35)
    sigma = np.sqrt(np.einsum("ij,jk,ik->i", X, F, X) + D)
    assert sigma[0] > 1.5 * np.median(sigma[1:])


def test_few_stocks_fall_back_to_a_market_factor():
    X, names = re_.exposures(["A", "B", "C", "D", "E"], [np.arange(5.0), np.ones(5)])
    assert names == ["market"] and X.shape == (5, 1)
    X, names = re_.exposures(["A"] * 12 + ["B"] * 12, [np.arange(24.0)])
    assert names == ["A", "B", "style0"]
    assert re_.zscore([1.0, np.nan, 3.0]).tolist() == [-1.0, 0.0, 1.0]
    assert re_.zscore([np.nan, np.nan]).tolist() == [0.0, 0.0]


def test_weights_follow_the_score_within_the_limits():
    returns, industries = _market()
    X, _ = re_.exposures(industries, [])
    F, D = re_.factor_model(returns, X, industries)
    sigma = np.sqrt(np.einsum("ij,jk,ik->i", X, F, X) + D)
    score = np.linspace(-2, 2, 60)                                 # stock 59 scores best
    alpha = re_.IC * sigma * re_.zscore(score)
    b = np.full(60, 1 / 60)
    w = re_.optimise(alpha, X, F, D, b, np.zeros(60), industries)
    assert w.sum() == pytest.approx(1) and (w >= 0).all() and w.max() <= re_.CAP + 1e-6
    assert w[-10:].sum() > w[:10].sum()                             # the best-scored get more
    assert ((w == 0) | (w >= re_.MIN_WEIGHT)).all()                 # no dust
    labels = np.array(industries)
    for g in ("Banks", "IT"):                                       # within 10 points of 50%
        assert abs(w[labels == g].sum() - 0.5) <= re_.IND_BAND + 1e-6
    # A higher risk penalty stays closer to the benchmark.
    tight = re_.optimise(alpha, X, F, D, b, np.zeros(60), industries, lam=200)
    assert re_.tracking_error(tight, b, X, F, D) < re_.tracking_error(w, b, X, F, D)


def test_trading_costs_keep_holdings_when_the_score_barely_moves():
    returns, industries = _market()
    X, _ = re_.exposures(industries, [])
    F, D = re_.factor_model(returns, X, industries)
    sigma = np.sqrt(np.einsum("ij,jk,ik->i", X, F, X) + D)
    b = np.full(60, 1 / 60)
    first = re_.optimise(re_.IC * sigma * re_.zscore(np.linspace(-2, 2, 60)), X, F, D, b,
                         np.zeros(60), industries)
    nudged = np.linspace(-2, 2, 60) + np.random.default_rng(1).normal(0, 0.05, 60)
    again = re_.optimise(re_.IC * sigma * re_.zscore(nudged), X, F, D, b, first, industries)
    assert np.abs(again - first).sum() / 2 < 0.05                   # under 5% traded
    # A short list still fills: the cap rises to 2/n.
    w = re_.optimise(np.array([0.01, 0.0, -0.01]), np.ones((3, 1)), np.array([[1e-3]]),
                     np.full(3, 1e-3), np.full(3, 1 / 3), np.zeros(3), ["A", "A", "A"])
    assert w.sum() == pytest.approx(1) and w.max() <= 2 / 3 + 1e-6 and w[0] > w[2]
