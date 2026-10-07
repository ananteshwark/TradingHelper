-- Automatic orders (igs.intraday.trading, source 'auto') have their own minimum
-- reward-to-risk after charges; approved calls keep min_net_reward_risk.
alter table intraday_trading_settings
    add column auto_min_net_reward_risk numeric not null default 2.5
        check (auto_min_net_reward_risk >= 0);
