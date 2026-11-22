-- migrate:up

-- Leaves of the Pansophicon that belong to none of its books are kept as book 0, incertae
-- sedis: in the collection, placed in no book. 0 rather than 8, so a book inferred later
-- can take viii. The row has no anchor, no vector and no hints: the filer never scores it.
-- Its title is the owner's from here on, as every book title is; this only writes it once.
-- Safe to run again: the tests put the collection back by re-running the seeds and this.
alter table collection_books drop constraint if exists collection_books_number_check;
alter table collection_books add constraint collection_books_number_check check (number >= 0);

insert into collection_books (collection_id, number, title)
select c.id, 0, 'incertae sedis'
  from collections c
 where c.slug = 'pansophicon'
on conflict do nothing;

-- migrate:down

-- The old check cannot hold while a leaf is in book 0. Membership is the owner's and
-- outlives the place: each such leaf is marked again, unfiled, for the filer to place.
update collection_requests set book = null, position = null, updated_at = now() where book = 0;
-- An owner's note rides on the request, as file_request expects; a model note is not kept.
insert into collection_requests (image_id, collection_id, note)
select image_id, collection_id, case when note_source = 'owner' then note end
  from collection_leaves where book = 0
on conflict do nothing;
-- The older code knows no book 0: a judgement of it is dropped, and history forgets the
-- place rather than name a book that no longer exists.
delete from collection_judgements where book = 0 or runner_up = 0;
update collection_leaf_history set from_book = null, from_position = null where from_book = 0;
update collection_leaf_history set to_book = null, to_position = null where to_book = 0;
delete from collection_leaves where book = 0;
delete from collection_books where number = 0;
alter table collection_books drop constraint if exists collection_books_number_check;
alter table collection_books add constraint collection_books_number_check check (number >= 1);
