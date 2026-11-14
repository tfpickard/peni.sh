-- migrate:up

-- Four short bodies per picture and recipe, one per facet. `split` is cut from the
-- private analysis (or, when that has no labels, from one extract call); `extract`
-- comes from the tailored vision call. The graph reads one recipe at a time, set in
-- mycelium_state.facet_recipe. There is no owner source yet, so no source column.
create table if not exists image_facets (
  image_id   bigint not null references images (id) on delete cascade,
  facet      text not null check (facet in ('subject', 'setting', 'style', 'mood')),
  recipe     text not null check (recipe in ('split', 'extract')),
  body       text not null,
  model      text,
  created_at timestamptz not null default now(),
  primary key (image_id, facet, recipe)
);
create index if not exists image_facets_recipe_idx on image_facets (recipe, facet);

-- The whole raw extract response, so offers and needs (Increment 5) are read from
-- here rather than paid for twice.
create table if not exists image_extracts (
  image_id   bigint primary key references images (id) on delete cascade,
  model      text not null,
  body       jsonb not null,
  created_at timestamptz not null default now()
);

-- Facet vectors sit beside the description vector, one kind per facet and recipe,
-- so corpus_stats keeps a mean per kind and each recipe is centered on its own.
alter table embeddings drop constraint if exists embeddings_kind_check;
alter table embeddings add constraint embeddings_kind_check check (
  kind in ('description', 'image')
  or kind ~ '^facet:(subject|setting|style|mood)/(split|extract)$'
);

-- Which recipe the four facet layers grow on. Split for everyone until every live
-- picture has the extract recipe too; then the owner may switch.
alter table mycelium_state add column if not exists facet_recipe text not null default 'split'
  check (facet_recipe in ('split', 'extract'));

-- migrate:down

alter table mycelium_state drop column if exists facet_recipe;
delete from embeddings where kind like 'facet:%';
delete from corpus_stats where kind like 'facet:%';
delete from corpus_stats_history where kind like 'facet:%';
alter table embeddings drop constraint if exists embeddings_kind_check;
alter table embeddings add constraint embeddings_kind_check check (
  kind in ('description', 'image')
);
drop table if exists image_extracts;
drop table if exists image_facets;
