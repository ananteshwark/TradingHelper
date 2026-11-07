"""No buy call with a target under the minimum a year (features.call.min_buy_target_pct_a_year):
the AI's own buys (igs.assistant.calls.buy_rule) and brokers' buys it agrees with
(igs.call_list.given_buy)."""
import datetime as dt

import pytest
import test_ai_calls as A

from igs import call_list
from igs.alerts import call_message
from igs.assistant import calls as ai
from igs.assistant.llm import Assistant


@pytest.mark.parametrize("target, price, months, pct", [
    (115, 100, 12, 15.0), (110, 100, 6, 21.0), (130, 100, 24, 14.02), (90, 100, 12, -10.0),
    (None, 100, 12, None), (115, None, 12, None), (0, 100, 12, None)])
def test_a_target_as_a_rate_a_year(target, price, months, pct):
    assert ai.pct_a_year(target, price, months) == pct


@pytest.mark.parametrize("action, target, months, recorded", [
    ("buy", 115, 12, "buy"),                # exactly 15% a year
    ("buy", 114.9, 12, "hold"),
    ("buy", 107.5, 6, "buy"),               # 15.6% a year over 6 months
    ("buy", 130, 24, "hold"),               # 30% over two years is 14% a year
    ("buy", None, 12, "hold"),              # no target
    ("sell", None, 12, "sell"), ("hold", 101, 12, "hold")])
def test_a_buy_under_the_minimum_is_recorded_as_hold(action, target, months, recorded):
    assert ai.buy_rule(action, target, 100, months, 15)[0] == recorded


def test_the_ai_is_told_the_minimum():
    text = ai.system(15)
    assert "{target_rule}" not in text
    assert "at least 15% a year above the last close" in text
    assert "over 6 months 7.2% above it, over 12 months 15.0%, over 24 months 32.2%" in text
    assert "the call is hold, not buy" in text
    assert "at least 20% a year" in ai.system(20)
    assert "target_price" in ai.output_schema()["required"]


def test_the_note_and_the_target_text():
    held = {"action": "hold", "ai_action": "buy", "target_price": 105.0,
            "target_pct_a_year": 5.0, "horizon_months": 12}
    assert ai.rule_note(held, 15) == ("The AI said buy, but its target of Rs 105.00 comes to "
                                      "+5.0% a year from the entry, under the 15% a year "
                                      "minimum, so it is recorded as hold.")
    assert "without a target" in ai.rule_note({**held, "target_pct_a_year": None})
    assert ai.rule_note({**held, "ai_action": None}) is None
    assert ai.target_text(held) is None
    assert ai.target_text({**held, "action": "buy", "target_pct_a_year": 18.24}) == \
        "Rs 105.00 in 12 months, +18.2% a year from the entry"


@pytest.mark.parametrize("call, target, price, kind, shown", [
    ("Buy", 120.0, 100.0, "research", "Buy"),
    ("Buy", 110.0, 100.0, "research", "Not given: target +10.0% a year"),
    # A trading idea over one month: 1.2% comes to 15.4% a year, 1% to 12.7%.
    ("Buy", 101.2, 100.0, "trading", "Buy"),
    ("Buy", 101.0, 100.0, "trading", "Not given: target +12.7% a year"),
    ("Buy", None, 100.0, "trading", "Not given: no target"),
    ("Buy", 120.0, None, "research", "Not given: no price to measure the target from"),
    ("Sell", 80.0, 100.0, "research", "Sell"),
    ("Not confirmed", 105.0, 100.0, "research", "Not confirmed")])
def test_a_brokers_buy_is_given_only_with_a_target_over_the_minimum(call, target, price,
                                                                   kind, shown):
    item = {"call": call, "target (Rs)": target}
    call_list.given_buy(item, 15, price, call_list.HORIZON_MONTHS[kind])
    assert item["call"] == shown


# --------------------------------------------------------------------------- database

scored = A.scored


@pytest.mark.db
def test_the_ais_buy_under_the_minimum_is_stored_as_hold(scored):
    conn, run_id = scored
    close = conn.execute("""select close::float8 from price_eod where symbol = 'GROW'
                            and trade_date = %s""", (dt.date(2024, 11, 29),)).fetchone()[0]
    client = A.reply(target_price=round(close * 1.05, 2))
    c = ai.make_call(Assistant.open(conn, A._cfg(), client), "GROW", run_id,
                     quotes=lambda keys: {})
    assert "at least 15% a year above the last close" in str(client.requests[0]["system"])
    assert c["action"] == "hold" and c["ai_action"] == "buy"
    assert c["target_pct_a_year"] == pytest.approx(5.0, abs=0.01)
    row = conn.execute("""select action, ai_action, target_price, target_pct_a_year
                          from ai_call where call_id = %s""", (c["call_id"],)).fetchone()
    assert row[:2] == ("hold", "buy") and row[2] == pytest.approx(close * 1.05, abs=0.01)
    assert "under the 15% a year minimum" in ai.rule_note(ai.calls(conn, "GROW")[0], 15)

    good = A.reply(target_price=round(close * 1.30, 2))
    c = ai.make_call(Assistant.open(conn, A._cfg(), good), "GROW", run_id,
                     quotes=lambda keys: {})
    assert c["action"] == "buy" and c["ai_action"] is None
    assert c["target_pct_a_year"] == pytest.approx(30.0, abs=0.01)
    brief = call_message.brief_message(call_message.call(conn, c["call_id"]))
    assert "Target Rs" in brief and "+30.0% a year from the entry" in brief
