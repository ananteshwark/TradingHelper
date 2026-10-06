-- Preserve the business-growth assessment with the exact scoring run.
-- Existing runs stay unassessed rather than being rewritten using today's data.
alter table score_result add column growth_profile jsonb;
