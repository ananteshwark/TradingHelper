-- The ranking scores momentum alone (config/scoring.yaml, research/ml_rating). The other
-- pillars are shown, and one far below its peers is a warning on the stock: 'weak quality
-- (-1.4)'. Earlier runs have none.
alter table score_result add column warnings text[] not null default '{}';

-- The momentum paper portfolios gain a sixth track: the ranking's own top ten, so the
-- order the owner chose on 2026-10-17 builds a forward record.
alter table momentum_rebalance drop constraint momentum_rebalance_track_check;
alter table momentum_rebalance add constraint momentum_rebalance_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged', 'ranking'));
alter table momentum_period drop constraint momentum_period_track_check;
alter table momentum_period add constraint momentum_period_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged', 'ranking'));
