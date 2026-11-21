-- migrate:up

-- How a leaf's book was decided, and what the filer's scores said at that moment, so the
-- admin can say why a leaf went where it did after the books around it have filled.
-- `score` is the filer's own choice, `owner` a book the owner gave, `judge` a model's
-- choice (the filer's judge, 025). `scores` holds every open book's terms (cos,
-- tag, fill, total), the best, the runner-up, the margin and the tuning they were made
-- under. Both are null on rows from before, and on moves that decided no book.
alter table collection_leaf_history
  add column chosen_by text check (chosen_by in ('score', 'owner', 'judge')),
  add column scores jsonb;

-- migrate:down

alter table collection_leaf_history
  drop column if exists scores,
  drop column if exists chosen_by;
