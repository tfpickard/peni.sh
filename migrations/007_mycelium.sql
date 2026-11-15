-- migrate:up

-- The organism's clock and memory, one row. The tick locks it `for update`, so a
-- tick, a pause and a dirty mark from a publish or a delete never interleave.
create table if not exists mycelium_state (
  id                integer primary key check (id = 1),
  running           boolean not null default false,
  -- Every random draw descends from this; a replay with the same seed is the same run.
  seed              bigint not null check (seed >= 0),
  -- The scheduled time of the next tick. A tick's time is this, never the wall clock.
  next_due          timestamptz,
  last_tick         bigint,
  graph_version     integer not null default 0,
  -- Set by go-live, delete and description-embed; the next tick rebuilds the candidates.
  graph_dirty       boolean not null default true,
  tunables_version  integer,
  -- colonies.ColoniesState.to_dict(), so a restore is exact; the colony tables below
  -- are a projection of it for reading.
  colonies          jsonb,
  -- Set once a gallery address is public; genesis and a full reset refuse after it.
  public_since      timestamptz,
  -- The last tick genesis simulated, so the admin can tell replay from life.
  simulated_through bigint,
  updated_at        timestamptz not null default now()
);

-- Ships stopped; Tom presses start.
insert into mycelium_state (id, seed)
values (1, floor(random() * 4294967296)::bigint)
on conflict (id) do nothing;

-- Who each picture may grow toward, per layer. Rebuilt whole when the graph is dirty.
create table if not exists candidate_edges (
  layer    text not null,
  src      bigint not null references images (id) on delete cascade,
  dst      bigint not null references images (id) on delete cascade,
  raw      double precision not null,
  affinity double precision not null,
  primary key (layer, src, dst)
);
-- The click check asks about a pair in any layer; the cascades need each end indexed.
create index if not exists candidate_edges_pair_idx on candidate_edges (src, dst);
create index if not exists candidate_edges_dst_idx on candidate_edges (dst);

-- engine.EdgeState, field for field. double precision round trips float64 exactly,
-- which is what keeps a restored state's hash equal to the one it was saved with.
create table if not exists edge_state (
  layer    text not null,
  src      bigint not null references images (id) on delete cascade,
  dst      bigint not null references images (id) on delete cascade,
  strength double precision not null,
  latched  boolean not null,
  fatigue  double precision not null,
  fused    boolean not null,
  idle     integer not null,
  primary key (layer, src, dst)
);
create index if not exists edge_state_src_idx on edge_state (src);
create index if not exists edge_state_dst_idx on edge_state (dst);

-- Clicks from one picture to a candidate neighbor, waiting for the next tick. Nothing
-- about who clicked: no address, no hash, no cookie.
create table if not exists mycelium_traffic (
  id         bigserial primary key,
  src        bigint not null references images (id) on delete cascade,
  dst        bigint not null references images (id) on delete cascade,
  created_at timestamptz not null default now()
);
create index if not exists mycelium_traffic_created_idx on mycelium_traffic (created_at);
create index if not exists mycelium_traffic_src_idx on mycelium_traffic (src);
create index if not exists mycelium_traffic_dst_idx on mycelium_traffic (dst);

-- One row per tick. The primary key is what makes a second run of the same tick fail
-- instead of doubling it.
create table if not exists mycelium_ticks (
  tick             bigint primary key,
  scheduled_at     timestamptz not null,
  ran_at           timestamptz not null default now(),
  simulated        boolean not null,
  state_hash       text not null,
  colonies_hash    text not null,
  stats            jsonb not null,
  tunables_version integer,
  graph_version    integer
);

-- Append only; the newest version is current. "values" holds only what differs from
-- the defaults in code, so a changed default reaches every version that left it alone.
-- Quoted because values is a reserved word.
create table if not exists mycelium_tunables (
  version    serial primary key,
  "values"   jsonb not null,
  created_at timestamptz not null default now(),
  note       text
);

-- Colonies and galleries, written only by the tick (and genesis, and reset). Rows are
-- never deleted outside a full wipe: a dead gallery keeps its serial and its page.
create table if not exists colonies (
  id             bigint primary key,
  layer          text not null,
  state          text not null check (state in ('forming', 'alive', 'fading', 'dead', 'failed')),
  serial         integer unique,
  title          text,
  title_source   text check (title_source in ('model', 'owner')),
  born_tick      bigint not null,
  fruited_tick   bigint,
  faded_tick     bigint,
  died_tick      bigint,
  cause          text check (
    cause in ('depopulated', 'absorbed', 'schism', 'drifted', 'starved', 'reset')
  ),
  parent_id      bigint,
  absorbed_by    bigint,
  -- Set null, not cascade: a gallery outlives a deleted cover.
  cover_image_id bigint references images (id) on delete set null,
  peak           integer not null default 0,
  simulated      boolean not null default false,
  updated_at     timestamptz not null default now()
);
create index if not exists colonies_state_idx on colonies (state);

-- One row per stint: leaving closes it, coming back opens another. The slug is kept
-- so a tombstone can still name a picture that was deleted.
create table if not exists colony_members (
  id          bigserial primary key,
  colony_id   bigint not null references colonies (id) on delete cascade,
  image_id    bigint references images (id) on delete set null,
  slug        text not null,
  founder     boolean not null default false,
  joined_tick bigint not null,
  left_tick   bigint
);
create index if not exists colony_members_colony_idx on colony_members (colony_id);
create index if not exists colony_members_image_idx on colony_members (image_id);

create table if not exists colony_events (
  id              bigserial primary key,
  tick            bigint not null,
  scheduled_at    timestamptz not null,
  kind            text not null check (kind in (
    'formed', 'fruited', 'joined', 'left', 'faded', 'revived', 'failed',
    'absorbed', 'schism', 'starved', 'drifted', 'depopulated', 'reset'
  )),
  colony_id       bigint not null references colonies (id) on delete cascade,
  layer           text not null,
  image_id        bigint references images (id) on delete set null,
  other_colony_id bigint,
  simulated       boolean not null default false
);
create index if not exists colony_events_tick_idx on colony_events (tick);
create index if not exists colony_events_colony_idx on colony_events (colony_id);
create index if not exists colony_events_image_idx on colony_events (image_id);

-- Size and strength per tick, for a gallery's sparkline.
create table if not exists colony_ticks (
  colony_id     bigint not null references colonies (id) on delete cascade,
  tick          bigint not null,
  size          integer not null,
  mean_strength double precision not null,
  primary key (colony_id, tick)
);

-- migrate:down

drop table if exists colony_ticks;
drop table if exists colony_events;
drop table if exists colony_members;
drop table if exists colonies;
drop table if exists mycelium_tunables;
drop table if exists mycelium_ticks;
drop table if exists mycelium_traffic;
drop table if exists edge_state;
drop table if exists candidate_edges;
drop table if exists mycelium_state;
