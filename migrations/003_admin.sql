-- migrate:up

-- Runtime overrides for model, effort and prompts. An absent row means the code default,
-- so a reset is a delete and a fresh deploy needs no seed rows.
create table settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

-- crop is in original pixels, null = uncropped. pub_sha names the derivative files: it is the
-- hash of what is displayed, so a crop gets fresh immutable URLs while sha256 stays the
-- dedupe key of the uploaded bytes.
alter table images add column crop jsonb, add column pub_sha text;
-- Every live file on disk is already named by sha256, so this backfill needs no renames.
update images set pub_sha = sha256 where pub_sha is null;

-- A column default cannot reference another column, and an image row that starts life
-- without a pub_sha would publish files nobody can find. The trigger keeps raw inserts
-- (tests, psql) honest; ingest sets it explicitly anyway.
create function images_default_pub_sha() returns trigger language plpgsql as $$
begin
  if new.pub_sha is null then
    new.pub_sha := new.sha256;
  end if;
  return new;
end
$$;
create trigger images_default_pub_sha before insert on images
  for each row execute function images_default_pub_sha();

alter table images alter column pub_sha set not null;
-- Not unique: two originals that crop to identical pixels would legitimately share files.
-- Delete looks up other users of a pub_sha before removing them.
create index images_pub_sha_idx on images (pub_sha);

-- voter is an hmac of the anon cookie id, never the raw id.
create table votes (
  image_id   bigint not null references images (id) on delete cascade,
  voter      text not null,
  value      smallint not null check (value in (-1, 1)),
  created_at timestamptz not null default now(),
  primary key (image_id, voter)
);

create table comments (
  id         bigserial primary key,
  image_id   bigint not null references images (id) on delete cascade,
  name       text,
  body       text not null,
  status     text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  ip_hash    text,
  created_at timestamptz not null default now(),
  decided_at timestamptz
);
-- Serves both the moderation queue and the per-status admin tabs.
create index comments_status_created_idx on comments (status, created_at);
-- The public image page reads approved comments for one image.
create index comments_image_idx on comments (image_id, created_at);

create table api_usage (
  id            bigserial primary key,
  at            timestamptz not null default now(),
  purpose       text not null,
  model         text not null,
  input_tokens  int not null default 0,
  output_tokens int not null default 0
);
create index api_usage_at_idx on api_usage (at);

create table heartbeats (
  name text primary key,
  at   timestamptz not null default now(),
  info jsonb not null default '{}'::jsonb
);

create table corpus_stats_history (
  kind        text not null,
  model       text not null,
  n           int not null,
  temperature real,
  at          timestamptz not null default now()
);
create index corpus_stats_history_idx on corpus_stats_history (kind, model, at);

-- migrate:down

drop table if exists corpus_stats_history;
drop table if exists heartbeats;
drop table if exists api_usage;
drop table if exists comments;
drop table if exists votes;
drop index if exists images_pub_sha_idx;
drop trigger if exists images_default_pub_sha on images;
drop function if exists images_default_pub_sha();
alter table images drop column if exists pub_sha, drop column if exists crop;
drop table if exists settings;
