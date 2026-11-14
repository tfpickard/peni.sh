-- migrate:up

-- A gallery's haiku lives in the same table as the deck, filed against its colony.
-- Set null, not cascade, as image_id is: colony rows are only ever deleted by a full
-- wipe before galleries are public, and that wipe deletes the colony's haikus first
-- (store.wipe_network), so a null here never means a gallery haiku that lost its room.
-- Every reader of the deck (home, examples for new haikus, the growth job) asks for
-- colony_id is null, so a gallery haiku is only ever shown on its gallery.
alter table haikus add column if not exists colony_id bigint
  references colonies (id) on delete set null;
create index if not exists haikus_colony_idx on haikus (colony_id) where colony_id is not null;

-- The deck dedupes on its text; a gallery haiku dedupes only within its gallery, so a
-- regenerate that writes the same three lines again is a no-op and a gallery haiku
-- never collides with the deck (where it is not shown anyway).
drop index if exists haikus_text_uniq;
create unique index if not exists haikus_text_uniq
  on haikus (lower(l1 || E'\n' || l2 || E'\n' || l3)) where colony_id is null;
create unique index if not exists haikus_colony_text_uniq
  on haikus (colony_id, lower(l1 || E'\n' || l2 || E'\n' || l3)) where colony_id is not null;

-- The switch for the clerk. Off until Tom has looked at the serial-named galleries;
-- every naming call, automatic or asked for, checks it.
alter table mycelium_state add column if not exists naming boolean not null default false;

-- A title written by the naming job, so the per-day rate can be counted from the log.
-- Not a public event: the gallery page and the public log list their own kinds.
alter table colony_events drop constraint if exists colony_events_kind_check;
alter table colony_events add constraint colony_events_kind_check check (kind in (
  'formed', 'fruited', 'joined', 'left', 'faded', 'revived', 'failed',
  'absorbed', 'schism', 'starved', 'drifted', 'depopulated', 'reset', 'named'
));
-- The rate counts today's names.
create index if not exists colony_events_named_idx on colony_events (scheduled_at)
  where kind = 'named';

-- migrate:down

drop index if exists colony_events_named_idx;
delete from colony_events where kind = 'named';
alter table colony_events drop constraint if exists colony_events_kind_check;
alter table colony_events add constraint colony_events_kind_check check (kind in (
  'formed', 'fruited', 'joined', 'left', 'faded', 'revived', 'failed',
  'absorbed', 'schism', 'starved', 'drifted', 'depopulated', 'reset'
));
alter table mycelium_state drop column if exists naming;
-- Without the column a gallery haiku would read as part of the deck.
delete from haikus where colony_id is not null;
drop index if exists haikus_colony_text_uniq;
drop index if exists haikus_text_uniq;
create unique index if not exists haikus_text_uniq
  on haikus (lower(l1 || E'\n' || l2 || E'\n' || l3));
drop index if exists haikus_colony_idx;
alter table haikus drop column if exists colony_id;
