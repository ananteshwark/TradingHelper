-- The momentum paper portfolios gain a seventh track: an Aladdin-style engine
-- (igs.risk_engine), whose 2016-2026 test did not beat the Rankings order
-- (research/aladdin_engine) and which the owner asked to follow anyway.
alter table momentum_rebalance drop constraint momentum_rebalance_track_check;
alter table momentum_rebalance add constraint momentum_rebalance_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged', 'ranking', 'engine'));
alter table momentum_period drop constraint momentum_period_track_check;
alter table momentum_period add constraint momentum_period_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged', 'ranking', 'engine'));
