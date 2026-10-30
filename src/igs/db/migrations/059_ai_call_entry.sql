-- An AI call's entry: the price when it was made, which its record is measured from
-- (igs.assistant.calls.entry_now). 'live': Upstox's last traded price during market
-- hours, with the Nifty 500's at the same moment. 'close': the close of entry_date, the
-- last session that had ended when the call was made, read from the day's prices once
-- they are loaded (so entry_price stays empty). price_date and price_close stay what they
-- were: the last close the model saw, at its score run's date.
alter table ai_call
    add column entry_source text check (entry_source in ('live', 'close')),
    add column entry_date date,
    add column entry_price double precision check (entry_price > 0),
    add column entry_benchmark double precision check (entry_benchmark > 0),
    add column entry_at timestamptz,
    add constraint ai_call_entry_check check (
        entry_source is null
        or (entry_source = 'live' and entry_price is not null and entry_date is not null)
        or (entry_source = 'close' and entry_price is null and entry_date is not null));

-- Calls made before: the last close at the time, i.e. the session that had ended (15:30
-- IST on a weekday) before the call; a holiday falls back to the session before it when
-- the record is measured. A call is otherwise never changed.
alter table ai_call disable trigger ai_call_append_only;
update ai_call set entry_source = 'close', entry_at = created_at, entry_date = (
    select case extract(isodow from d) when 6 then d - 1 when 7 then d - 2 else d end
    from (select ((created_at at time zone 'Asia/Kolkata')
                  - interval '15 hours 30 minutes')::date as d) s);
alter table ai_call enable trigger ai_call_append_only;
