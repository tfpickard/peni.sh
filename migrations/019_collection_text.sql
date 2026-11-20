-- migrate:up

-- Owner text a collection states about itself. Nothing generates any of it.
-- gloss sits under the title; record is the catalogue record as ordered [label, value]
-- pairs (a value may hold several lines); vendor_text is the dealer's own lot
-- description, quoted as written.
alter table collections
  add column gloss text not null default '',
  add column record jsonb not null default '[]'::jsonb,
  add column vendor_text text,
  add constraint collections_record_is_array check (jsonb_typeof(record) = 'array');

-- The conservator's note on a leaf, with where it came from, as on image_texts: model
-- output never replaces an owner note.
alter table collection_leaves
  add column note_source text check (note_source in ('owner', 'model')),
  add column note_model text,
  add constraint collection_leaves_note_has_source check (note is null or note_source is not null);

-- An owner note that arrived with the request (upload form or `_note.txt`), held until
-- the picture is filed and then written to the leaf.
alter table collection_requests add column note text;

-- migrate:down

alter table collection_requests drop column if exists note;
alter table collection_leaves
  drop constraint if exists collection_leaves_note_has_source,
  drop column if exists note_model,
  drop column if exists note_source;
alter table collections
  drop constraint if exists collections_record_is_array,
  drop column if exists vendor_text,
  drop column if exists record,
  drop column if exists gloss;
