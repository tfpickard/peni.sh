-- migrate:up

-- Derivative ladders a picture's page served while it was live, for the purge list an
-- unpublished picture shows. Cloudflare keeps /media/pub/ for a year as immutable, and
-- a crop or a heal changes pub_sha, after which nothing else names the old URLs. A row
-- is written when a live picture goes down and when a live picture's pub_sha is
-- replaced; rows outlive a republish, since the cache does too.
create table if not exists served_media (
  image_id   bigint not null references images (id) on delete cascade,
  pub_sha    text not null,
  width      int not null,
  created_at timestamptz not null default now(),
  primary key (image_id, pub_sha)
);

-- Pictures already down served their current ladder.
insert into served_media (image_id, pub_sha, width)
select id, pub_sha, width from images
 where status = 'unpublished' and pub_sha is not null and width is not null
on conflict do nothing;

-- migrate:down

drop table if exists served_media;
