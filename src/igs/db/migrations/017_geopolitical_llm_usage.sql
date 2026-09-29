-- Geopolitical assessment uses the same metered assistant as the original features.
-- Widen the constraint without rewriting historical migrations or usage records.
alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical'));
