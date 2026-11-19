-- migrate:up

-- A collection is a fixed shape of books and leaves that pictures are filed into by hand.
-- The owner decides membership; the worker decides placement unless the owner pinned it.
-- tuning holds the filer's constants, so they change from the admin without a deploy;
-- an absent key means the default in penish/collection/plan.py.
create table collections (
  id              bigserial primary key,
  slug            text not null unique check (slug ~ '^[a-z0-9-]{1,40}$'),
  title           text not null,
  abbrev          text not null,
  abbrev_latin    text not null,
  -- Owner text, shown as written. Nothing generates it.
  preamble        text not null default '',
  book_count      int not null check (book_count between 1 and 99),
  leaves_per_book int not null check (leaves_per_book between 1 and 999),
  -- Off until the owner says so: a collection can be filled before anyone can see it.
  published       boolean not null default false,
  tuning          jsonb not null default '{}'::jsonb,
  last_resort_at  timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- anchor_text is private filing guidance written in the analysis register, so its vector
-- lands in the same space as the pictures'. Untyped vector with model and dim beside it,
-- as on embeddings: a model swap makes the row stale rather than wrong-width.
create table collection_books (
  collection_id    bigint not null references collections (id) on delete cascade,
  number           int not null check (number >= 1),
  title            text,
  anchor_text      text not null default '',
  anchor_embedding vector,
  anchor_model     text,
  anchor_dim       int,
  tag_hints        text[] not null default '{}',
  updated_at       timestamptz not null default now(),
  primary key (collection_id, number),
  check (anchor_embedding is null or vector_dims(anchor_embedding) = anchor_dim)
);

-- One row per filed picture. A picture is in at most one place per collection.
-- book_pinned keeps it in its book; pinned also keeps its leaf, and implies book_pinned.
-- Unfiled requests live in collection_requests, so book and position here are never null.
create table collection_leaves (
  image_id      bigint not null references images (id) on delete cascade,
  collection_id bigint not null references collections (id) on delete cascade,
  book          int not null,
  position      int not null check (position >= 1),
  book_pinned   boolean not null default false,
  pinned        boolean not null default false,
  -- Reserved for the conservator's note. Nothing writes it yet.
  note          text,
  filed_at      timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (image_id, collection_id),
  foreign key (collection_id, book) references collection_books (collection_id, number),
  check (book_pinned or not pinned),
  -- Deferred: a re-sort permutes positions inside one transaction, and an immediate
  -- check would refuse the intermediate states of a swap.
  constraint collection_leaves_slot unique (collection_id, book, position)
    deferrable initially deferred
);
create index collection_leaves_book_idx on collection_leaves (collection_id, book);

-- Marked by the owner, not filed yet: the job is pending, waiting for publish, or failed.
-- book and position are what the owner asked for, null where the filer chooses.
create table collection_requests (
  image_id      bigint not null references images (id) on delete cascade,
  collection_id bigint not null references collections (id) on delete cascade,
  book          int,
  position      int check (position >= 1),
  error         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (image_id, collection_id),
  check (position is null or book is not null)
);

-- Appended whenever a leaf is filed, moves or is taken out. from is null for a filing,
-- to is null for an unfiling. The public page shows only the latest earlier position.
create table collection_leaf_history (
  id            bigserial primary key,
  image_id      bigint not null references images (id) on delete cascade,
  collection_id bigint not null references collections (id) on delete cascade,
  from_book     int,
  from_position int,
  to_book       int,
  to_position   int,
  reason        text not null check (reason in ('auto', 'resort', 'owner')),
  at            timestamptz not null default now()
);
create index collection_leaf_history_image_idx
  on collection_leaf_history (image_id, collection_id, id desc);
create index collection_leaf_history_at_idx on collection_leaf_history (collection_id, id desc);

-- migrate:down

drop table if exists collection_leaf_history;
drop table if exists collection_requests;
drop table if exists collection_leaves;
drop table if exists collection_books;
drop table if exists collections;
