-- migrate:up

-- Rows the nudity detector adds: the kinds it found as tags, and erotica as a category.
-- Their own source so a later run that finds nothing takes back exactly what an earlier
-- one added, and so a redescribe or recategorize, which replace 'model' rows, leaves them.
-- Not used in this file: a value added in a transaction is unusable until it commits.
alter type text_source add value if not exists 'detect';

-- One row per picture the detector has looked at; no row means never checked. An empty
-- kinds array is a clean result. `seen` is the detector's one-line account of what it
-- saw, kept for the admin to judge a false positive by, and never public.
create table if not exists image_nudity (
  image_id   bigint primary key references images (id) on delete cascade,
  kinds      text[] not null default '{}',
  seen       text not null default '',
  model      text not null,
  checked_at timestamptz not null default now()
);

-- migrate:down

drop table if exists image_nudity;
-- Postgres cannot drop an enum value, so the rows that use it go back to plain model rows.
update tags set source = 'model' where source::text = 'detect';
update image_categories set source = 'model' where source::text = 'detect';
