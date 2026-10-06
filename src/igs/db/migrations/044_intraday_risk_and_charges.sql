-- Size each intraday trade by what the stop would lose, not only by order value, and
-- refuse one whose estimated charges leave too little reward (igs.intraday.trading).
alter table intraday_trading_settings
    add column max_risk_rupees numeric check (max_risk_rupees > 0),
    add column min_net_reward_risk numeric not null default 1.5
        check (min_net_reward_risk >= 0);
-- Start at 1% of the amount per trade: Rs 100 on the Rs 10,000 default.
update intraday_trading_settings
    set max_risk_rupees = greatest(round(max_trade_rupees / 100, 2), 0.01);
alter table intraday_trading_settings alter column max_risk_rupees set not null;

alter table intraday_trade
    add column risk_rupees numeric,       -- loss at the stop before charges
    add column est_charges numeric;       -- estimated round-trip charges to the target
