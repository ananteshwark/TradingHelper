"""AI calls aimed at the app's purpose: own improving businesses before the price catches
up, and book the profit near the top (igs.assistant.calls)."""
import datetime as dt
import json

import pytest
import test_ai_calls as A

from igs.alerts import call_message
from igs.assistant import calls as ai
from igs.assistant.llm import Assistant


def test_the_ai_is_told_the_purpose_and_the_evidence():
    text = ai.system(15)
    assert "before the price has caught up" in text and "book the profit near the top" in text
    assert "buying them lost money" in text                   # docs/MOVE_STARTS.md
    assert "don't buy right after such a spike" in text
    assert "say why that growth isn't enough" in text
    schema = ai.output_schema()
    assert {"stage", "vs_growth"} <= set(schema["required"])
    assert schema["properties"]["stage"]["enum"] == [
        "before the move", "early in the move", "late in the move", "near a top", "falling",
        "no setup"]


def test_the_growth_label_the_ai_reads():
    label = {"version": 1, "profile": "Early growth", "reasons": [], "growth_strength": 0.03,
             "evidence": {"revenue_quarter_yoy": {"value": 0.05, "ids": [1]},
                          "pat_quarter_yoy": {"value": 0.03}}}
    assert ai.growth_inputs(json.dumps(label)) == {
        "profile": "Early growth", "reasons": [],
        "evidence": {"revenue_quarter_yoy": 0.05, "pat_quarter_yoy": 0.03}}
    assert ai.growth_inputs(None) == {"profile": "Not assessed"}


def _series(closes: list[float], start=dt.date(2024, 1, 1)) -> list[tuple]:
    days = [start + dt.timedelta(days=i) for i in range(len(closes))]
    return list(zip(days, closes, strict=True))


def test_the_last_10_sessions_show_a_spike():
    """A +8% day against a flat market, on 4x the usual traded value, to a 52-week high."""
    closes = [100.0] * 290 + [100, 100, 100, 108, 109, 109, 110, 110, 111, 111]
    idx = _series([c / 100 for c in closes])
    bench = _series([1000.0] * 300)
    rows = [(d, c, None, 1e7 * (4 if i == 293 else 1))
            for i, (d, c) in enumerate(_series(closes))]
    out = ai.recent_moves(idx, bench, rows)
    assert out["best_day_vs_nifty500_pct"] == 8.0 and out["worst_day_vs_nifty500_pct"] == 0.0
    assert out["busiest_day_vs_normal_turnover"] == 4.0
    assert out["made_52w_high"] is True
    calm = ai.recent_moves(_series([1.0] * 300), bench,
                           [(d, 1.0, None, 1e7) for d, _ in _series([1.0] * 300)])
    assert calm["best_day_vs_nifty500_pct"] == 0.0


scored = A.scored


@pytest.mark.db
def test_a_call_names_its_stage_and_answers_the_growth_label(scored):
    conn, run_id = scored
    first = ai.make_call(Assistant.open(conn, A._cfg(), A.reply()), "GROW", run_id,
                         quotes=lambda keys: {})
    row = conn.execute("select stage, vs_growth from ai_call where call_id = %s",
                       (first["call_id"],)).fetchone()
    assert row == ("early in the move", A.answer()["vs_growth"])
    assert "early in the move" in call_message.brief_message(
        call_message.call(conn, first["call_id"]))

    client = A.reply(action="sell", stage="near a top",
                     vs_growth="Early growth, but +3% is already in a price 40% above its "
                               "200-day average.")
    second = ai.make_call(Assistant.open(conn, A._cfg(), client), "GROW", run_id,
                          quotes=lambda keys: {})
    prompt = client.requests[0]["messages"][0]["content"]
    assert "<growth_profile>" in prompt and "<last_buy>" in prompt
    assert "last_10_sessions" in prompt
    shown = ai.last_buy(conn, first["company_id"])
    assert shown["target_price"] == 1_000_000.0 and shown["stage"] == "early in the move"
    assert second["action"] == "sell" and second["stage"] == "near a top"
    text = call_message.text_message(call_message.call(conn, second["call_id"]),
                                     {"summary": []}, bold=False)
    assert "near a top" in text and "Growth: Early growth, but +3%" in text
