-- migrate:up

-- Log lines from both processes, for the admin logs page. Written in batches by a
-- background thread (penish/logs.py), so `at` is when the line was logged, not when the
-- batch landed; `id` is insert order and is what the page pages by. The worker prunes it
-- by age (log.keep_days) and to a hard row cap, so it never grows without bound.
create table if not exists app_log (
  id      bigserial primary key,
  at      timestamptz not null default now(),
  source  text not null check (source in ('api', 'worker')),
  -- Python's level number, so "warning and up" is a plain comparison.
  level   smallint not null,
  logger  text not null,
  message text not null,
  -- The formatted traceback, when the line carried one.
  exc     text,
  -- job_id, job_type, attempt, image_id, purpose, model, provider when they were set.
  context jsonb
);

create index if not exists app_log_at_idx on app_log (at desc);
create index if not exists app_log_level_at_idx on app_log (level, at desc);
-- The system page links a failed job here, and the picture editor a picture.
create index if not exists app_log_job_idx on app_log ((context->>'job_id'));
create index if not exists app_log_image_idx on app_log ((context->>'image_id'));
-- Every listing sums the drop reports; without this that is a scan of the whole table
-- every few seconds while the page follows. The literal must match the query's.
create index if not exists app_log_drops_idx on app_log (id) where logger = 'penish.logs';

-- migrate:down

drop table if exists app_log;
