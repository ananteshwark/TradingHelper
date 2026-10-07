-- The price file's own name for a security (UDiFF FinInstrmNm, abbreviated): the instrument
-- master names a new listing with it until NSE's equity list has the company. Rows loaded
-- before this migration have none; `igs rebuild` fills them from the raw files.
alter table price_eod add column security_name text;

-- Brokers' calls pasted from a page read in the owner's own browser (Moneycontrol's
-- recommendations page is behind bot protection, so the app never fetches it).
alter table broker_call drop constraint broker_call_source_check;
alter table broker_call add constraint broker_call_source_check
    check (source in ('news', 'manual', 'pasted'));
create index if not exists nse_equity_list_snapshot_idx on nse_equity_list (snapshot_date);
