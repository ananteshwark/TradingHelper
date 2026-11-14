-- Order levels and how long an unfilled entry stays open become trading settings
-- (igs.intraday.trading.order_levels). The defaults keep the levels orders had: the entry
-- 1% better than the call's price and the stop 1% beyond it. Zero means the call's own
-- price, or its own stop.
alter table intraday_trading_settings
    add column entry_offset_pct numeric not null default 1
        check (entry_offset_pct >= 0 and entry_offset_pct < 5),
    add column stop_pct numeric not null default 1
        check (stop_pct >= 0 and stop_pct <= 5),
    -- Counted from when the order is placed, not from the call's candle; 15:15 IST at most.
    add column order_minutes integer not null default 10
        check (order_minutes between 1 and 360);
