-- The momentum paper portfolios gain a fourth track: the rule's ten, weighted by the inverse
-- of each stock's recent volatility (docs/INSTITUTIONAL_ALGOS.md).
alter table momentum_rebalance drop constraint momentum_rebalance_track_check;
alter table momentum_rebalance add constraint momentum_rebalance_track_check
    check (track in ('rule', 'ai', 'results', 'risk'));
alter table momentum_period drop constraint momentum_period_track_check;
alter table momentum_period add constraint momentum_period_track_check
    check (track in ('rule', 'ai', 'results', 'risk'));
