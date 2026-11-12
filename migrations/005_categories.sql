-- migrate:up

-- The gallery's rooms. One per picture where possible, two at most; the list itself
-- lives in penish/categories.py so the api, worker and prompt cannot disagree.
create table image_categories (
  image_id bigint not null references images (id) on delete cascade,
  category text not null,
  source   text_source not null,
  model    text,
  primary key (image_id, category)
);
create index image_categories_category on image_categories (category);

-- migrate:down

drop table if exists image_categories;
