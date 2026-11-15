-- migrate:up

-- How long each picture has gone unseen. Nothing about who looked: no address, no
-- cookie, no visitor id. Overgrowth itself is never stored; it is computed at read time
-- from last_viewed_at, so a view clears it at once and the tick cannot undo a view.
create table if not exists picture_attention (
  image_id       bigint primary key references images (id) on delete cascade,
  views          integer not null default 0,
  last_viewed_at timestamptz not null,
  -- Written by the tick from Increment 2 on; until then every picture spreads nothing.
  neighbor_level double precision not null default 0
);

-- Counted views per day, for the pace that sets the overgrowth time constant.
create table if not exists view_days (
  day date primary key,
  n   integer not null default 0
);

-- Every picture starts as just seen, so the site does not open fully overgrown. One
-- still waiting to go live gets its clock reset again by ingest._go_live.
insert into picture_attention (image_id, last_viewed_at)
select id, now() from images
on conflict (image_id) do nothing;

-- migrate:down

drop table if exists view_days;
drop table if exists picture_attention;
