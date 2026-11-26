-- A buy call's target (igs.assistant.calls.buy_rule): the price the AI expects by the end of
-- its horizon, and the yearly rate that comes to from the call's entry (its live price, else
-- the last close). A buy whose target comes to less than
-- features.call.min_buy_target_pct_a_year (config/assistant.yaml), or that has none, is
-- recorded as hold; ai_action then keeps the action the AI gave. Earlier calls have none.
alter table ai_call
    add column target_price double precision check (target_price > 0),
    add column target_pct_a_year double precision,
    add column ai_action text check (ai_action in ('buy', 'hold', 'sell'));
