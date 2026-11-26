"""The AI calls' record by the model that made them (igs.assistant.calls.by_model), so a
change of model in Settings can be judged on its own record."""
import datetime as dt

import pytest
import test_ai_calls as A

from igs.assistant import calls as ai

T0 = dt.datetime(2026, 1, 5, 10, tzinfo=dt.UTC)


def _call(model, action, day, excess_1m=None, excess_now=None):
    def o(x):
        if x is None:
            return None
        right = None if action == "hold" else (x > 0 if action == "buy" else x < 0)
        return {"return_pct": x, "nifty500_pct": 0.0, "excess_pct": x, "right": right}
    return {"model": model, "action": action, "created_at": T0 + dt.timedelta(days=day),
            "outcome": {"horizons": {"1m": o(excess_1m)} if excess_1m is not None else {},
                        "so_far": o(excess_now)}}


def test_each_model_is_scored_on_its_own_calls():
    rows = [_call("old-model", "buy", 0, 4.0, 6.0), _call("old-model", "buy", 1, -2.0, -1.0),
            _call("old-model", "sell", 2, -3.0, -5.0), _call("old-model", "hold", 3, 9.0, 9.0),
            _call("new-model", "buy", 40, None, 1.5), _call("new-model", "sell", 41, None, 2.0)]
    out = {(r["model"], r["horizon"]): r for r in ai.by_model(rows)}
    # Oldest model first; horizons nobody has passed are left out, "so far" never is.
    assert list(out) == [("old-model", "1m"), ("old-model", "so far"), ("new-model", "so far")]
    old = out[("old-model", "1m")]
    assert (old["calls"], old["buys"], old["holds"], old["sells"]) == (4, 2, 1, 1)
    # Edge: a buy's excess, a sell's shortfall; the hold is not scored.
    assert old["scored"] == 3 and old["edge_pct"] == pytest.approx((4 - 2 + 3) / 3, abs=0.05)
    assert old["right_pct"] == pytest.approx(66.7, abs=0.1)
    assert old["t"] is not None and old["t"] > 0
    new = out[("new-model", "so far")]
    assert new["scored"] == 2 and new["edge_pct"] == pytest.approx((1.5 - 2.0) / 2, abs=0.05)
    assert new["right_pct"] == 50.0
    assert new["first"] == T0 + dt.timedelta(days=40) and new["last"] == T0 + dt.timedelta(41)
    lone = ai.by_model([_call("solo", "buy", 0, None, 3.0)])[0]
    assert (lone["scored"], lone["edge_pct"], lone["t"]) == (1, 3.0, None)
    assert ai.by_model([_call("quiet", "hold", 0, None, None)])[0]["scored"] == 0


scored = A.scored


@pytest.mark.db
def test_the_record_carries_the_by_model_table(scored):
    conn, run_id = scored
    start = dt.date(2022, 6, 1)
    A._insert(conn, run_id, 1, "GROW", "buy", start)
    A._insert(conn, run_id, 2, "CYCL", "sell", start)
    record = ai.track_record(conn, today=dt.date(2022, 9, 30))
    rows = {r["horizon"]: r for r in record["by_model"]}
    assert set(rows) == {"1m", "3m", "so far"} and {r["model"] for r in rows.values()} == {"m"}
    assert rows["1m"]["scored"] == 2
    grow = next(c for c in record["calls"] if c["symbol"] == "GROW")
    cycl = next(c for c in record["calls"] if c["symbol"] == "CYCL")
    edge = (grow["outcome"]["horizons"]["1m"]["excess_pct"]
            - cycl["outcome"]["horizons"]["1m"]["excess_pct"]) / 2
    assert rows["1m"]["edge_pct"] == pytest.approx(edge, abs=0.06)
