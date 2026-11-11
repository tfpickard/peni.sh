-- migrate:up

-- 'analysis' is the faithful structured text that gets embedded; 'description' is the
-- public one and is free to look elsewhere.
alter table image_texts drop constraint image_texts_field_check;
alter table image_texts add constraint image_texts_field_check
  check (field in ('caption', 'description', 'prompt', 'analysis'));

-- Every description written so far was the faithful four-paragraph text, so it is already
-- the analysis and the vectors made from it stay valid without a re-embed.
insert into image_texts (image_id, field, body, source, model, updated_at)
select image_id, 'analysis', body, source, model, updated_at
  from image_texts
 where field = 'description'
on conflict (image_id, field) do nothing;

-- migrate:down

delete from image_texts where field = 'analysis';
alter table image_texts drop constraint image_texts_field_check;
alter table image_texts add constraint image_texts_field_check
  check (field in ('caption', 'description', 'prompt'));
