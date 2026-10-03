alter table llm_call add column provider text not null default 'anthropic'
    check (provider in ('anthropic', 'openai', 'gemini', 'deepseek', 'openrouter'));
alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers',
                       'news_tone', 'verdicts', 'forward'));
