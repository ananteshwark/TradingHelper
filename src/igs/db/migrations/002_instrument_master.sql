-- Instrument master.
--
-- company   : the issuer; fundamentals attach here.
-- security  : a tradable equity line of a company; prices attach here.
-- security_identifier : ISIN / NSE symbol / BSE code / broker tokens with
--   validity ranges. An ISIN can change (e.g. on a face-value split) and a
--   symbol can be renamed or, rarely, reused by a different issuer, so every
--   mapping is dated. valid_to is exclusive; null means still valid.

create extension if not exists btree_gist;

create table company (
    company_id  bigserial primary key,
    name        text        not null,
    cin         text unique,
    created_at  timestamptz not null default now()
);

create table security (
    security_id    bigserial primary key,
    company_id     bigint not null references company,
    security_type  text   not null default 'equity'
                   check (security_type in ('equity', 'dvr', 'partly_paid')),
    created_at     timestamptz not null default now()
);
create index security_company_idx on security (company_id);

create table security_identifier (
    security_id      bigint not null references security,
    id_type          text   not null check (id_type in (
                         'ISIN', 'NSE_SYMBOL', 'BSE_CODE',
                         'ANGEL_TOKEN_NSE', 'ANGEL_TOKEN_BSE', 'BREEZE_CODE')),
    id_value         text   not null,
    valid_from       date   not null,
    valid_to         date,
    evidence         text   not null,
    source_fetch_id  text references raw_payload (fetch_id),
    check (valid_to is null or valid_to > valid_from),
    -- One identifier value points at one security at any moment ...
    constraint security_identifier_value_unique_in_time exclude using gist (
        id_type with =, id_value with =,
        daterange(valid_from, valid_to, '[)') with &&),
    -- ... and a security has at most one identifier of each type at a moment.
    constraint security_identifier_type_unique_in_time exclude using gist (
        security_id with =, id_type with =,
        daterange(valid_from, valid_to, '[)') with &&)
);
create index security_identifier_security_idx on security_identifier (security_id);

create table security_listing (
    security_id      bigint not null references security,
    exchange         text   not null check (exchange in ('NSE', 'BSE')),
    listed_on        date,
    delisted_on      date,
    status           text   not null check (status in ('active', 'suspended', 'delisted')),
    source_fetch_id  text references raw_payload (fetch_id),
    primary key (security_id, exchange)
);
