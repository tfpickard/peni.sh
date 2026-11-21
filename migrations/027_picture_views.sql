-- migrate:up

-- Counted views per picture per day, beside the site-wide view_days. Counts only: no
-- address, no agent, no cookie. Written in the same transaction as the view itself, so
-- it inherits the one-minute rule and sums to view_days.
create table if not exists picture_views (
  image_id bigint not null references images (id) on delete cascade,
  day      date   not null,
  n        integer not null default 0,
  primary key (image_id, day)
);

-- Where a counted view came from, as a host and nothing more: never a path, a query or
-- anything that tells one visitor from another.
create table if not exists view_referrers (
  day  date not null,
  host text not null,
  n    integer not null default 0,
  primary key (day, host)
);

-- migrate:down

drop table if exists view_referrers;
drop table if exists picture_views;
