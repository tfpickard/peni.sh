-- migrate:up

-- Which model wrote a haiku, provider prefix and all (`openrouter:...`), so a haiku a
-- fallback provider wrote can be told from one the configured model wrote. Null for
-- owner and seed rows, and for model rows written before anyone asked.
alter table haikus add column model text;

-- migrate:down

alter table haikus drop column if exists model;
