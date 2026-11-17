-- migrate:up

-- The owner can overrule the detector. An owner row is never replaced by a detector run
-- unless the owner asks for one on that picture; the detector's model and `seen` note are
-- kept beside it, so the picture page still shows what the detector thought.
alter table image_nudity add column if not exists source text_source not null default 'model';
alter table image_nudity alter column model drop not null;

-- migrate:down

delete from image_nudity where model is null;
alter table image_nudity alter column model set not null;
alter table image_nudity drop column if exists source;
