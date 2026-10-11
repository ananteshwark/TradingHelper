-- NSE's F&O lot sizes (source nse_fo_lots): which stocks have futures and what one lot
-- is, by contract month. A snapshot is kept whenever the list changes.
create table fno_lot (
    as_of           date    not null,
    symbol          text    not null,
    kind            text    not null check (kind in ('index', 'stock')),
    month           date    not null,           -- the contract month's first day
    lot             integer not null check (lot > 0),
    source_fetch_id text    not null references raw_payload (fetch_id),
    primary key (as_of, symbol, month)
);

-- The momentum paper portfolios gain a fifth track: the rule's ten among stocks with
-- futures, hedged by selling Nifty futures (docs/FUTURES_OPTIONS.md).
alter table momentum_rebalance drop constraint momentum_rebalance_track_check;
alter table momentum_rebalance add constraint momentum_rebalance_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged'));
alter table momentum_period drop constraint momentum_period_track_check;
alter table momentum_period add constraint momentum_period_track_check
    check (track in ('rule', 'ai', 'results', 'risk', 'hedged'));
