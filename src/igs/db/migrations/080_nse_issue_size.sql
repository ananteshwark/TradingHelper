-- NSE's daily market capitalisation file (the mcap CSV in its PR archive, source
-- nse_market_cap): each listed or permitted security's issue size (shares), close and
-- market cap. The issue size is a fourth source of shares outstanding, for companies the
-- other three leave without a market cap (igs.factors.base.shares_outstanding).
create table nse_issue_size (
    trade_date      date    not null,
    symbol          text    not null,
    series          text    not null,
    security_name   text    not null,
    category        text    not null,          -- Listed, Permitted
    face_value      numeric,
    issue_size      bigint  not null check (issue_size > 0),
    close           numeric,                   -- as NSE prints it, rounded
    market_cap      numeric,                   -- NSE's own, from its unrounded close
    source_fetch_id text    not null references raw_payload (fetch_id),
    primary key (trade_date, symbol, series)
);
create index nse_issue_size_symbol_idx on nse_issue_size (symbol, trade_date desc);
