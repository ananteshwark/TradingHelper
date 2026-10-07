-- Key numbers per company and run (igs.score.key_numbers): price, 52-week range, P/E, EPS,
-- book value, P/B, debt/equity, dividend yield, sales and profit (TTM), promoter holding.
-- Display only; runs before this migration have none.
alter table score_result add column key_numbers jsonb;
