-- Why a call was made: the new data that prompted an automatic call ("new results filed",
-- "tier changed", ...), or "asked" for one made on request.
alter table ai_call add column reason text;
