-- NSE puts its older, single-level industry label (smIndustry, e.g. "Pharmaceuticals",
-- "Finance - Housing") and the ISIN on each corporate announcement. They are kept as
-- published. Scoring uses the label only for companies whose four-level NSE
-- classification (industry_classification) is not loaded, and records which of the two
-- a result's industry came from.

alter table announcement
    add column industry_label text,
    add column isin           text;

alter table score_result
    add column industry_source text;
