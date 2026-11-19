-- migrate:up

-- Hand-picked sets of pictures that the machine arranges. Membership is only ever the
-- owner's: no model job writes to these tables except to place what the owner chose.
-- General so a second collection is a row, but only the Pansophicon exists.
create table if not exists collections (
  id              serial primary key,
  slug            text not null unique,
  title           text not null,
  abbrev          text not null,
  abbrev_latin    text not null,
  -- Owner text, shown as written and never generated.
  preamble        text not null default '',
  book_count      int not null check (book_count between 1 and 99),
  leaves_per_book int not null check (leaves_per_book between 1 and 999),
  -- Built and filled out of sight until this is set.
  published       boolean not null default false,
  -- When the last full re-sort ran, so the weekly one knows whether it is due.
  resorted_at     timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table if not exists collection_books (
  id            serial primary key,
  collection_id int not null references collections (id) on delete cascade,
  number        int not null check (number >= 1),
  -- Owner text; null shows only the numeral.
  title         text,
  -- What the filer matches pictures against before the book has leaves of its own.
  -- Internal: never served publicly, nor its vector, nor the hints.
  anchor_text   text not null default '',
  -- Untyped like embeddings.vec, with the model it came from, so a model swap shows up
  -- as a stale anchor to re-embed rather than a width mismatch mid-score.
  anchor_vec    vector,
  anchor_model  text,
  tag_hints     text[] not null default '{}',
  updated_at    timestamptz not null default now(),
  unique (collection_id, number)
);

-- A filed picture. book_id and position are both set: a picture marked but not yet
-- placed lives in collection_requests instead, so nothing here is ever half-filed.
create table if not exists collection_leaves (
  image_id        bigint not null references images (id) on delete cascade,
  collection_id   int not null references collections (id) on delete cascade,
  book_id         int not null references collection_books (id) on delete cascade,
  position        int not null check (position >= 1),
  -- The owner chose the book; the filer never moves the leaf to another one.
  pinned_book     boolean not null default false,
  -- The owner chose the leaf too; the filer never moves it at all.
  pinned_position boolean not null default false,
  -- Room for the conservator's note; nothing writes it yet.
  note            text,
  filed_at        timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  primary key (image_id, collection_id),
  check (pinned_book or not pinned_position),
  -- Deferred so a re-sort can permute a whole book in one transaction without parking
  -- every leaf on a temporary slot first.
  constraint collection_leaves_slot unique (book_id, position) deferrable initially deferred
);
create index if not exists collection_leaves_book_idx on collection_leaves (book_id);

-- Every move, appended. Book and leaf by number, not by id, so the record reads the way
-- the page does and survives a book row being rebuilt. A null `to` is an unfiling.
create table if not exists collection_leaf_history (
  id            bigserial primary key,
  image_id      bigint not null references images (id) on delete cascade,
  collection_id int not null references collections (id) on delete cascade,
  from_book     int,
  from_position int,
  to_book       int,
  to_position   int,
  reason        text not null check (reason in ('auto', 'resort', 'owner')),
  at            timestamptz not null default now()
);
create index if not exists collection_leaf_history_image_idx
  on collection_leaf_history (image_id, collection_id, at desc, id desc);
create index if not exists collection_leaf_history_at_idx
  on collection_leaf_history (collection_id, at desc, id desc);

-- Marked for a collection, not yet filed: waiting for publish, for the job, or failed
-- with `error`. Book and position are the owner's pins, either may be null.
create table if not exists collection_requests (
  image_id      bigint not null references images (id) on delete cascade,
  collection_id int not null references collections (id) on delete cascade,
  book          int check (book >= 1),
  position      int check (position >= 1),
  error         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (image_id, collection_id),
  check (book is not null or position is null)
);

insert into collections (slug, title, abbrev, abbrev_latin, preamble, book_count, leaves_per_book)
values (
  'pansophicon',
  'Πανσοφικὸν τῶν πάντων',
  'ΠΤΠ',
  'PTP',
  'Acquired incomplete. Believed to comprise seven books of seventy-seven leaves. No book has '
  'been found complete. The books are presented in the order in which they were found, which '
  'is believed to be their original order.',
  7,
  77
)
on conflict (slug) do nothing;

-- Anchors are written in the analysis register, since that is the text the pictures are
-- embedded from: plain visual description, not titles.
insert into collection_books (collection_id, number, anchor_text, tag_hints)
select c.id, b.number, b.anchor, b.hints
from collections c
cross join (values
  (1,
   'Engraved or painted plates of single animals and composite creatures shown in profile '
   'against a plain ground, sometimes with small figures beside them for scale. Bestiary and '
   'natural-history illustration, hand-coloured woodcut, marginal drolleries, beasts drawn with '
   'careful anatomy and impossible parts.',
   array['creature', 'animal', 'beast', 'bestiary', 'woodcut', 'drollery']),
  (2,
   'Botanical plates showing a whole plant from root to flower, seeds and cross-sections, gems '
   'and minerals drawn in rows, pseudo-script labels, herbal and lapidary pages with faded washes.',
   array['plant', 'botanical', 'flower', 'herbal', 'gem', 'mineral']),
  (3,
   'Engraved diagrams of surgical, mechanical or ritual apparatus in exploded view with lettered '
   'parts, anatomical figures with flaps and cutaways, patients and practitioners, bathhouses and '
   'infirmaries, procedures in progress.',
   array['diagram', 'apparatus', 'anatomy', 'surgery', 'machine', 'patient']),
  (4,
   'Celestial spheres, nested rings and orbits, volvelles, astrolabe plates, star charts, maps of '
   'seas and coasts, cosmological diagrams with figures at the margins, gold and lapis on '
   'parchment.',
   array['celestial', 'stars', 'astrolabe', 'map', 'cosmology', 'orbit']),
  (5,
   'Alchemical emblems, flasks and furnaces with scenes inside them, crowned figures, serpents, '
   'suns and moons, sequences of small panels, laboratory interiors.',
   array['alchemy', 'flask', 'furnace', 'serpent', 'emblem', 'laboratory']),
  (6,
   'Icons with gold grounds, saints and figures in registers, relic caskets, liturgical objects, '
   'processions, heterodox or unfamiliar sacred scenes, mosaic and fresco fragments.',
   array['icon', 'saint', 'gold', 'relic', 'procession', 'fresco']),
  (7,
   'Dream images paired in grids, emblems and their meanings, divination tables, figures in '
   'symbolic scenes, a sleeping person with a vision above, alphabets of signs.',
   array['dream', 'sign', 'symbol', 'divination', 'vision', 'sleep'])
) as b (number, anchor, hints)
where c.slug = 'pansophicon'
on conflict (collection_id, number) do nothing;

-- migrate:down

drop table if exists collection_requests;
drop table if exists collection_leaf_history;
drop table if exists collection_leaves;
drop table if exists collection_books;
drop table if exists collections;
