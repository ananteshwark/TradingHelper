-- Local Strata inference is distinct from the DeepSeek cloud API in the audit log.
alter table llm_call drop constraint llm_call_provider_check;
alter table llm_call add constraint llm_call_provider_check
    check (provider in ('anthropic', 'openai', 'gemini', 'deepseek', 'openrouter', 'local'));
