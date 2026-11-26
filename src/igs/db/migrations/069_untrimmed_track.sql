-- The momentum paper portfolios gain an eighth track: the Rankings order's ten without the
-- monthly reset to equal weights (igs.momentum.untrimmed_picks). Not trimming came closest
-- of the refinements tested in research/momentum_upgrades but did not pass; the owner
-- asked to follow it.
alter table momentum_rebalance drop constraint momentum_rebalance_track_check;
alter table momentum_rebalance add constraint momentum_rebalance_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged', 'ranking', 'engine',
                     'untrimmed'));
alter table momentum_period drop constraint momentum_period_track_check;
alter table momentum_period add constraint momentum_period_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged', 'ranking', 'engine',
                     'untrimmed'));
