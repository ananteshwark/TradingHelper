-- Automatic entries require an explicit switch; a new installation starts disabled.
alter table intraday_trading_settings
    add column auto_high_volume_enabled boolean not null default false;

alter table intraday_trade drop constraint intraday_trade_approved_by_check;
alter table intraday_trade add constraint intraday_trade_approved_by_check
    check (approved_by in ('telegram', 'admin', 'auto'));
