-- migrate:up

-- When each book's anchor vector was written, so the admin can show how old it is. Set
-- with the vector by the worker and cleared with it on an anchor edit. Rows embedded before
-- this column existed keep null: a vector with no time is embedded, time unknown. 023
-- cleared its vectors before the column existed, so it has nothing to clear here.
alter table collection_books add column anchor_embedded_at timestamptz;

-- migrate:down

alter table collection_books drop column if exists anchor_embedded_at;
