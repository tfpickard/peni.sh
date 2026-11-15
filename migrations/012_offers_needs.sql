-- migrate:up

-- What a picture offers and what it needs, one short phrase per row, read from its
-- extract (image_extracts.body) and embedded by the worker. Private: phrases appear
-- on the admin page and nowhere else. A need's satisfaction belongs to the need layer
-- of the organism and only the tick (and a wipe or reset under the state row) writes
-- it; an offer's stays 0. Any other write here takes the mycelium_state row first, as
-- every embedding write does, so a tick or a genesis never waits on it the other way.
create table if not exists offers_needs (
  id           bigserial primary key,
  image_id     bigint not null references images (id) on delete cascade,
  role         text not null check (role in ('offer', 'need')),
  phrase       text not null,
  vec          vector not null,
  model        text not null,
  dim          int not null,
  recipe       text not null default 'extract' check (recipe in ('split', 'extract')),
  -- 0 is unsatisfied, 1 satisfied.
  satisfaction double precision not null default 0
    check (satisfaction >= 0 and satisfaction <= 1),
  created_at   timestamptz not null default now(),
  unique (image_id, role, phrase, recipe),
  check (vector_dims(vec) = dim),
  check (role = 'need' or satisfaction = 0)
);
-- The tick reads one (role, model, dim) space at a time.
create index if not exists offers_needs_space_idx on offers_needs (role, model, dim);

-- A need edge carries the need it serves and the offer that answers it, so damping
-- can be applied at step time from the need's current satisfaction without a rebuild,
-- and the admin can say which phrases met. Null on every other layer. A phrase that
-- goes takes its edges with it; the writer marks the graph dirty, so the next tick
-- rebuilds the layer anyway.
alter table candidate_edges add column if not exists need_id bigint
  references offers_needs (id) on delete cascade;
alter table candidate_edges add column if not exists offer_id bigint
  references offers_needs (id) on delete cascade;
create index if not exists candidate_edges_need_idx on candidate_edges (need_id)
  where need_id is not null;
create index if not exists candidate_edges_offer_idx on candidate_edges (offer_id)
  where offer_id is not null;

-- migrate:down

drop index if exists candidate_edges_offer_idx;
drop index if exists candidate_edges_need_idx;
alter table candidate_edges drop column if exists offer_id;
alter table candidate_edges drop column if exists need_id;
delete from candidate_edges where layer = 'need';
delete from edge_state where layer = 'need';
drop table if exists offers_needs;
