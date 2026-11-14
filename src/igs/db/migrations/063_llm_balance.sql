-- A provider's credit balance as the owner read it on the provider's billing page
-- (igs.assistant.balances). Anthropic has no API for the balance, so the app estimates what
-- is left by subtracting the spend it logged since then (llm_call). The latest entry counts.
create table llm_balance (
    balance_id  bigint generated always as identity primary key,
    provider    text           not null,
    balance_usd numeric(12, 2) not null check (balance_usd >= 0),
    as_of       timestamptz    not null default now()
);
create index llm_balance_provider_idx on llm_balance (provider, as_of desc);
