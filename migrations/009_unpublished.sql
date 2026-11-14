-- migrate:up

-- A published picture taken back down, reversibly: its derivatives wait in a private
-- holding dir and every public route treats it as not here. Alone in this file because
-- a new enum value cannot be used in the transaction that adds it.
alter type image_status add value if not exists 'unpublished';

-- migrate:down

-- Postgres cannot drop an enum value, so the type is rebuilt without it. A picture that
-- was unpublished stays off the site as failed rather than coming back live with its
-- files still held.
update images set status = 'failed' where status = 'unpublished';
alter type image_status rename to image_status_old;
create type image_status as enum ('queued', 'processing', 'pending_enrichment', 'live', 'failed');
alter table images alter column status drop default;
alter table images alter column status type image_status using status::text::image_status;
alter table images alter column status set default 'queued';
drop type image_status_old;
