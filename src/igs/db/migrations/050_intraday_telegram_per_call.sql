-- One Telegram message per call, identified by its candle, instead of one per stock and
-- direction a day: a stock called again later in the day is alerted again, so that call
-- can be approved by replying to its own message.
alter table intraday_telegram add column candle_end timestamptz;
update intraday_telegram set candle_end = (result->>'candle_end')::timestamptz;
alter table intraday_telegram alter column candle_end set not null;
alter table intraday_telegram drop constraint intraday_telegram_pkey;
alter table intraday_telegram add primary key (company_id, trading_day, action, candle_end);
