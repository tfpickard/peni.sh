# logs

Both processes keep their log lines in Postgres as well as on stdout. `/admin/logs`
reads them. `docker compose logs` works as before and is unaffected by anything here.

## what is stored

Table `app_log` (migration 014):

| column | |
|---|---|
| `id` | bigserial, insert order; the page pages by it |
| `at` | when the line was logged (not when the batch landed) |
| `source` | `api` or `worker` |
| `level` | Python level number: 10 debug, 20 info, 30 warning, 40 error, 50 critical |
| `logger` | logger name, e.g. `penish.worker.describe` |
| `message` | the formatted message, up to 32000 chars |
| `exc` | the traceback, when there was one |
| `context` | jsonb, see below |

`context` holds whatever was set when the line was logged:

- `job_id`, `job_type`, `attempt`, `image_id` for anything inside a job (set in
  `jobs._run_one`; `image_id` only when the payload has one),
- `purpose`, `model`, `provider` inside a model call (`logs.model_context`),
- any `extra={...}` passed to the log call, such as `stop_reason`.

Before a line is queued, every configured secret (api keys, admin password, secret key,
database password) is replaced with `<secret>`, as are `Authorization` / `x-api-key`
values and anything shaped like an api key. `data:` urls and base64 runs of 200 chars or
more become `<image mime n chars>` / `<base64 n chars>`. Debug lines from the http and
model sdk libraries are never kept, since they dump request bodies.

The uvicorn access log is not kept, so no client address is stored.

## levels

Runtime key `log.level`: `debug`, `info` (default), `warning`, `error`. It sets what the
table keeps, not what stdout prints (stdout stays at `LOG_LEVEL`, info in the worker,
warning in the api). Both processes re-read it about every 30 s. Set it from
`/admin/logs` or `PUT /api/admin/models {"key": "log.level", "value": "debug"}`.

At `debug` every model call is logged: purpose, model, provider, stop reason, block
types, usage, duration and the raw reply (8000 chars). At any level a reply that fails
validation is logged at warning with the raw text, and a refusal is logged at warning
with whatever partial text came back. A refusal partway through a haiku grow ends the run
there: the haikus already accepted are inserted, and the job then fails with the summary
(`accepted n, ..., stopped: model refused (model)`) so it shows in recent errors.

Anthropic calls ask for the server-side fallback (`fallbacks: "default"`), so a request a
safety classifier declines is rerun on Anthropic's recommended model for that category;
an `info` line names both models, and usage is booked to the one that answered. A refusal
that survives that names its category in the error (`model refused (model, category)`).
A haiku refused as `reasoning_extraction` is asked once more without the drafts field.

## retention

Runtime key `log.keep_days`, 1 to 365, default 14. The worker prunes hourly (and at
startup): lines older than that, then everything but the newest 500000.

## where to look

- A failed job: the system page's recent errors link to `/admin/logs?job=<id>`.
- A picture: the editor links to `/admin/logs?image=<id>`.
- A model that keeps failing: filter `level=warning`, search the purpose or `refus`.
- Nothing there: check `dropped` (below) and `docker compose logs worker`.

## how it is written

`penish/logs.py`. `install()` adds a `DbHandler` to the root logger in each process (the
api in its lifespan, the worker at the top of `main`). `emit` formats the line in the
caller's thread, so the context is the caller's, and puts it on a bounded queue
(10000); it never blocks and never raises. A daemon thread with its own sync connection
writes batches about once a second in one transaction.

If the database is unreachable the thread keeps what it has (at most twice the queue),
drops the rest, and retries with backoff up to 30 s. Dropped lines are counted, and the
next batch that lands carries a warning row from `penish.logs`, `dropped n log lines`,
with `context.dropped = n`. A row the table refuses (a NaN in an `extra`, say) is dropped
and counted on its own, without the rest of its batch: the batch is retried one row at a
time under savepoints in one transaction, so a connection lost partway through writes none
of it and the retry neither duplicates lines nor counts a drop twice. Text that cannot be
encoded as UTF-8 (a non-UTF-8 file name decodes to lone surrogates) is stored with
backslash escapes, `IMG_\udcff.jpg`.

The handler stays on the root logger until `uninstall()`, and `close()` does not stop the
writer while it is installed, since `logging.config.dictConfig` closes every handler it
finds. The worker's embed server builds its uvicorn config with `log_config=None` so it
never runs dictConfig in the first place.

At `debug` the root logger is opened to debug, the stream handlers stay at their old
level, and the noisy libraries (`anthropic`, `openai`, `httpx`, `httpcore`, `psycopg`,
`watchfiles`, and so on) are held at info.

## admin api

All under `/api/admin/logs`, behind the admin gate and login like the rest of admin.

`GET /api/admin/logs`, newest first. Query:

| param | |
|---|---|
| `level` | minimum: `debug`, `info`, `warning`, `error`, `critical` |
| `source` | `api` or `worker` |
| `logger` | name prefix (literal; `%` and `_` are not wildcards) |
| `q` | case-insensitive substring of message or traceback |
| `job` | `context.job_id` |
| `image` | `context.image_id` |
| `before` | id; only lines older than it (paging) |
| `after` | id; only lines newer than it (follow polls with the top id) |
| `limit` | 1 to 500, default 100 |

```json
{
  "items": [
    {"id": 812, "at": "2026-11-15T09:12:03.120+00:00", "source": "worker",
     "level": "warning", "logger": "penish.worker.describe",
     "message": "invalid haiku output ...", "has_exc": false,
     "context": {"job_id": 77, "job_type": "haiku", "attempt": 1, "purpose": "haiku"}}
  ],
  "next": 712,
  "dropped": 0,
  "level": "info",
  "keep_days": 14,
  "levels": ["debug", "info", "warning", "error"]
}
```

`level` on an item is the lowercase level name (`level 25` for a number Python has no name
for). `next` is the id to pass as `before` for the following page, null on the last.
`dropped` is the sum of drop reports still in the table (served by the partial index
`app_log_drops_idx`) plus what this api process has dropped and not reported yet. `level` and `keep_days` are the current runtime values.

The page's `follow` polls every 5 s with `after` set to its top line, and keeps the newest
500 lines, moving `next` up so `more` still pages back without a gap. A poll that returns a
full page (`next` set) starts the list over from the newest.

`GET /api/admin/logs/{id}`: the same fields with `exc` (the full traceback) in place of
`has_exc`. 404 `not here` when it is gone.
