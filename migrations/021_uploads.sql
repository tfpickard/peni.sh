-- migrate:up

-- Every arrival at the door, written before the picture is decoded, so an upload that
-- never becomes an image still says what it was and where it was meant to go. No key to
-- images: this row exists exactly when that one may not. Where an upload has got to is
-- read from the images row, the leaves and the data dirs, never stored here.
create table uploads (
  id              bigserial primary key,
  stem            text not null,
  sha256          text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  -- As the sender named it. An rsync drop only ever had its inbox name.
  filename        text,
  bytes           bigint,
  -- The `_collection.txt` line as it arrived, judged later.
  collection_line text,
  source          text not null check (source in ('form', 'rsync')),
  -- Why the worker gave up on it; cleared when the picture goes live.
  error           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (stem, sha256)
);
create index uploads_sha256_idx on uploads (sha256);

-- migrate:down

drop table if exists uploads;
