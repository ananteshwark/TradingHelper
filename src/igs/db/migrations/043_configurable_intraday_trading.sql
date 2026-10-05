-- Administrator-selected caps replace the initial fixed application ceilings.
alter table intraday_trading_settings
    drop constraint intraday_trading_settings_max_trade_rupees_check,
    drop constraint intraday_trading_settings_max_daily_rupees_check,
    drop constraint intraday_trading_settings_max_daily_trades_check;
alter table intraday_trading_settings
    alter column max_trade_rupees type numeric,
    alter column max_daily_rupees type numeric,
    alter column max_daily_trades type bigint;
alter table intraday_trading_settings
    add constraint intraday_trading_settings_max_trade_rupees_check
        check (max_trade_rupees > 0),
    add constraint intraday_trading_settings_max_daily_rupees_check
        check (max_daily_rupees > 0),
    add constraint intraday_trading_settings_max_daily_trades_check
        check (max_daily_trades > 0);

alter table intraday_trade
    alter column quantity type bigint,
    alter column notional type numeric,
    add column telegram_update_id bigint unique;
-- A broker's definite refusal must not prevent a fresh approval after credentials
-- or exchange settings are corrected. Ambiguous submissions remain unique.
alter table intraday_trade drop constraint intraday_trade_company_id_trading_day_key;
create unique index intraday_trade_active_stock_day on intraday_trade(company_id,trading_day)
    where status <> 'rejected';
