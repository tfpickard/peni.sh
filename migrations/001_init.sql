-- migrate:up

create extension if not exists vector;

create type image_status as enum ('queued', 'processing', 'pending_enrichment', 'live', 'failed');
create type text_source  as enum ('owner', 'model');
create type haiku_source as enum ('seed', 'model', 'owner');
create type job_status   as enum ('queued', 'running', 'done', 'failed');

-- sha256 is the identity: a re-dropped file with the same bytes is an update, not a new image.
-- slug stays null until publish so a half-enriched image never claims a public URL.
create table images (
  id           bigserial primary key,
  sha256       text not null unique,
  slug         text unique,
  stem         text,
  width        int,
  height       int,
  bytes        bigint,
  mime         text,
  blurhash     text,
  palette      jsonb,
  status       image_status not null default 'queued',
  -- Frozen at ingest against the corpus as it was then; later uploads must not rewrite history.
  surprise     real,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  published_at timestamptz
);

-- Serves the public feed: live images newest first.
create index images_status_published_idx on images (status, published_at desc);

-- Owner-wins invariant (enforced by the upsert in code, not by a trigger):
-- model output never overwrites a row whose source = 'owner'. The upsert is
--   insert ... on conflict (image_id, field) do update set ...
--   where image_texts.source <> 'owner' or excluded.source = 'owner';
-- so an owner sidecar always lands, and a re-describe only refreshes model text.
create table image_texts (
  image_id   bigint not null references images (id) on delete cascade,
  field      text not null check (field in ('caption', 'description', 'prompt')),
  body       text not null,
  source     text_source not null,
  model      text,
  updated_at timestamptz not null default now(),
  primary key (image_id, field)
);

-- Old slugs keep resolving after a caption change renames the image.
create table slug_aliases (
  slug     text primary key,
  image_id bigint not null references images (id) on delete cascade
);
create index slug_aliases_image_idx on slug_aliases (image_id);

create table tags (
  image_id bigint not null references images (id) on delete cascade,
  tag      text not null,
  source   text_source not null,
  model    text,
  primary key (image_id, tag)
);
create index tags_tag_idx on tags (tag);

-- vec is deliberately untyped so swapping embedding models needs no migration; dim
-- records what the model produced and the check stops mixed-width rows under one label.
-- Similarity is an exact scan for now (corpus is small). When it is not, add a partial
-- expression index per model width, e.g.
--   create index on embeddings using hnsw ((vec::vector(1024)) vector_cosine_ops)
--   where kind = 'description';
create table embeddings (
  image_id   bigint not null references images (id) on delete cascade,
  kind       text not null check (kind in ('description', 'image')),
  vec        vector not null,
  model      text not null,
  dim        int not null,
  created_at timestamptz not null default now(),
  primary key (image_id, kind),
  check (vector_dims(vec) = dim)
);
-- Queries always compare within one (kind, model) space.
create index embeddings_kind_model_idx on embeddings (kind, model);

-- Running mean per embedding space, used to center vectors before cosine so
-- "nearest" is not dominated by what every image has in common.
create table corpus_stats (
  kind        text not null,
  model       text not null,
  mean        vector,
  n           int not null default 0,
  temperature real,
  updated_at  timestamptz not null default now(),
  primary key (kind, model)
);

create table jobs (
  id         bigserial primary key,
  type       text not null,
  payload    jsonb not null default '{}'::jsonb,
  status     job_status not null default 'queued',
  attempts   int not null default 0,
  run_after  timestamptz not null default now(),
  locked_at  timestamptz,
  last_error text,
  -- Lets the watcher enqueue idempotently: a second drop of the same stem while the
  -- first is still pending collapses into it, but finished jobs do not block reruns.
  dedupe_key text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
-- Keeps the "for update skip locked ... order by run_after" claim off a full scan
-- once done/failed rows pile up.
create index jobs_claim_idx on jobs (run_after) where status = 'queued';
-- Stale-lock recovery scans only running jobs.
create index jobs_running_locked_idx on jobs (locked_at) where status = 'running';
create unique index jobs_dedupe_idx on jobs (dedupe_key)
  where status in ('queued', 'running') and dedupe_key is not null;

create table haikus (
  id         bigserial primary key,
  l1         text not null,
  l2         text not null,
  l3         text not null,
  source     haiku_source not null,
  -- Set null, not cascade: a haiku written for an image is still a haiku after the image goes.
  image_id   bigint references images (id) on delete set null,
  active     boolean not null default true,
  created_at timestamptz not null default now()
);
-- Case-insensitive dedupe so grow_haikus.py can insert blindly with on conflict do nothing.
create unique index haikus_text_uniq on haikus (lower(l1 || E'\n' || l2 || E'\n' || l3));
create index haikus_image_idx on haikus (image_id) where image_id is not null;

-- migrate:down

drop table if exists haikus;
drop table if exists jobs;
drop table if exists corpus_stats;
drop table if exists embeddings;
drop table if exists tags;
drop table if exists slug_aliases;
drop table if exists image_texts;
drop table if exists images;

drop type if exists job_status;
drop type if exists haiku_source;
drop type if exists text_source;
drop type if exists image_status;

drop extension if exists vector;
