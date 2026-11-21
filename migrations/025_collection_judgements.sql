-- migrate:up

-- What a model said about which book a picture belongs in, kept by what it was asked, so
-- the same question is never paid for twice. `input_hash` covers the model, the prompt,
-- the picture's caption, description and analysis, and every book of the collection, full
-- or not, with its number, title and anchor text; change any of them and the question is a
-- new one.
-- A row is a judgement (book, runner_up, reason) or a failure (`error`): a refusal is
-- kept so it is not asked again, and the others are kept so a dry run can say why a leaf
-- was not judged, then asked again by the next judging.
create table collection_judgements (
  id            bigserial primary key,
  collection_id bigint not null references collections (id) on delete cascade,
  image_id      bigint not null references images (id) on delete cascade,
  input_hash    text not null,
  model         text not null,
  book          int,
  runner_up     int,
  reason        text,
  error         text check (error in ('refused', 'invalid', 'timeout', 'error')),
  created_at    timestamptz not null default now(),
  unique (collection_id, image_id, input_hash),
  check (
    (error is null and book is not null and runner_up is not null and reason is not null
     and book <> runner_up)
    or (error is not null and book is null and runner_up is null and reason is null)
  )
);

-- migrate:down

drop table if exists collection_judgements;
