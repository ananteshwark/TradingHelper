from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from synthetic import ca_frame, price_frame, standard_dataset

from igs.normalize.adjust import (
    adjusted_prices,
    bonus_factor,
    cumulative_factors,
    dividend_factor,
    event_factors,
    exchange_implied_factors,
    rights_factor,
    split_factor,
)


def test_split_and_consolidation():
    assert split_factor(10, 2) == pytest.approx(0.2)
    assert split_factor(10, 1) == pytest.approx(0.1)
    assert split_factor(1, 10) == pytest.approx(10.0)


def test_bonus():
    assert bonus_factor(1, 1) == pytest.approx(0.5)
    assert bonus_factor(3, 2) == pytest.approx(0.4)   # 3 new for every 2 held: 2/5


def test_rights():
    # 1 for 4 at 80 with cum price 100: TERP = (4*100 + 80) / 5 = 96
    assert rights_factor(1, 4, 80, 100) == pytest.approx(0.96)
    assert rights_factor(1, 4, 120, 100) == 1.0      # priced above market: no dilution


def test_dividend():
    assert dividend_factor(5, 100) == pytest.approx(0.95)


@pytest.mark.parametrize("fn,args", [
    (split_factor, (0, 2)), (bonus_factor, (1, 0)), (rights_factor, (1, 4, 80, 0)),
    (dividend_factor, (100, 100)),
])
def test_invalid_inputs_raise(fn, args):
    with pytest.raises(ValueError):
        fn(*args)


def _days(*ds):
    return [dt.date(2024, 1, d) for d in ds]


def test_cumulative_factor_excludes_ex_date_itself():
    px = price_frame(1, dict(zip(_days(1, 2, 3, 4, 5), [100, 100, 50, 50, 10], strict=True)))
    factors = pl.DataFrame({"security_id": [1, 1], "ex_date": [dt.date(2024, 1, 3),
                                                               dt.date(2024, 1, 5)],
                            "factor": [0.5, 0.2]})
    cum = cumulative_factors(px, factors).to_list()
    assert cum == pytest.approx([0.1, 0.1, 0.2, 0.2, 1.0])
    adj = adjusted_prices(px, factors.with_columns(pl.lit(1).alias("ca_id")), dt.date(2024, 1, 5))
    assert adj["adj_close"].to_list() == pytest.approx([10, 10, 10, 10, 10])
    assert adj["adj_volume"].to_list() == pytest.approx([10000, 10000, 5000, 5000, 1000])


def test_adjustment_ignores_actions_after_as_of():
    px = price_frame(1, dict(zip(_days(1, 2, 3), [100, 100, 50], strict=True)))
    factors = pl.DataFrame({"security_id": [1], "ex_date": [dt.date(2024, 1, 3)],
                            "factor": [0.5]})
    adj = adjusted_prices(px, factors, dt.date(2024, 1, 2))
    assert adj.height == 2
    assert adj["adj_close"].to_list() == [100, 100]


def test_factors_are_per_security():
    px = pl.concat([price_frame(1, dict(zip(_days(1, 2), [100, 50], strict=True))),
                    price_frame(2, dict(zip(_days(1, 2), [70, 70], strict=True)))])
    factors = pl.DataFrame({"security_id": [1], "ex_date": [dt.date(2024, 1, 2)],
                            "factor": [0.5]})
    adj = adjusted_prices(px, factors, dt.date(2024, 1, 2)).sort("security_id", "trade_date")
    assert adj["adj_close"].to_list() == [50, 50, 70, 70]


def test_event_factors_from_corporate_actions():
    px = price_frame(1, dict(zip(_days(1, 2, 3), [100, 100, 96], strict=True)))
    cas = ca_frame([
        {"ca_id": 1, "security_id": 1, "action_type": "rights", "ex_date": dt.date(2024, 1, 3),
         "ratio_a": 1, "ratio_b": 4, "issue_price": 80},
        {"ca_id": 2, "security_id": 1, "action_type": "dividend", "ex_date": dt.date(2024, 1, 3),
         "cash_per_share": 5},
        {"ca_id": 3, "security_id": 1, "action_type": "demerger", "ex_date": dt.date(2024, 1, 2)},
    ])
    f = event_factors(cas, px).sort("ca_id")
    assert f["ca_id"].to_list() == [1, 3]            # dividends excluded by default
    assert f["factor"][0] == pytest.approx(0.96)
    assert f["status"].to_list() == ["ok", "needs_review"]
    with_div = event_factors(cas, px, include_dividends=True).sort("ca_id")
    assert with_div.filter(pl.col("ca_id") == 2)["factor"][0] == pytest.approx(0.95)


def test_standard_dataset_adjusted_series_is_continuous():
    ds = standard_dataset()
    px = ds.tables["prices"].filter(pl.col("security_id") == 2)
    factors = event_factors(ds.tables["corporate_actions"], px)
    adj = adjusted_prices(px, factors, dt.date(2024, 9, 30)).sort("trade_date")
    rets = (adj["adj_close"] / adj["adj_close"].shift(1) - 1).drop_nulls()
    assert rets.abs().max() < 0.01                     # no fake 50% / 80% drops


def test_exchange_implied_factor_on_ex_date():
    closes = dict(zip(_days(1, 2, 3), [100.0, 100.0, 50.0], strict=True))
    px = price_frame(1, closes, prev_close_override={dt.date(2024, 1, 3): 50.0})
    implied = exchange_implied_factors(px)
    row = implied.filter(pl.col("trade_date") == dt.date(2024, 1, 3))
    assert row["implied_factor"][0] == pytest.approx(0.5)
