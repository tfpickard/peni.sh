-- migrate:up

-- The members a gallery held at its largest, kept for the tombstone. A list of
-- {"id", "slug"} rather than flags on colony_members, since one picture can have
-- several stints and the peak is a moment, not a stint. No foreign key: the slug
-- still names a picture that was deleted. Written by the tick when a living
-- gallery ends a tick larger than any end of tick before.
alter table colonies add column if not exists peak_members jsonb;
alter table colonies add column if not exists peak_tick bigint;

-- Summed strength of the latched edges inside, beside the mean, for the gallery
-- sparkline. Null on rows written before this column existed: not recorded, not zero.
alter table colony_ticks add column if not exists total_strength double precision;

-- Children are found by parent; the gallery page asks for them by id.
create index if not exists colonies_parent_idx on colonies (parent_id);

-- migrate:down

drop index if exists colonies_parent_idx;
alter table colony_ticks drop column if exists total_strength;
alter table colonies drop column if exists peak_tick;
alter table colonies drop column if exists peak_members;
