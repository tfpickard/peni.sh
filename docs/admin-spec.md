# admin, votes, comments: build spec

Shared contract for the round that adds the full admin panel, public votes and
moderated comments. Voice rules from CLAUDE.md apply to every label and error.

## Data (migration 003_admin.sql, dbmate format)

- `settings(key text pk, value jsonb not null, updated_at timestamptz)`.
  Absent row means default. Keys:
  - `describe.model`, `describe.effort` (`low|medium|high|max` or null), `describe.prompt`
  - `haiku.model`, `haiku.prompt`, `grow.prompt`
- `images` gains `crop jsonb` (`{x,y,w,h}` in original pixels, null = uncropped)
  and `pub_sha text`. `pub_sha` is the content hash of the displayed pixels; derivative
  file names use it (`media/pub/{pub_sha}-{w}.{fmt}`). Backfill `pub_sha = sha256`.
  `sha256` stays the dedupe key of the uploaded original.
- `votes(image_id fk cascade, voter text, value smallint check in (-1,1), created_at,
  pk(image_id, voter))`. `voter` = hmac(server secret, anon cookie id) -- never raw.
- `comments(id bigserial, image_id fk cascade, name text null, body text not null,
  status text check in ('pending','approved','rejected') default 'pending',
  ip_hash text, created_at, decided_at)`. Index (status, created_at).
- `api_usage(id, at timestamptz default now(), purpose text, model text,
  input_tokens int, output_tokens int)`.
- `heartbeats(name text pk, at timestamptz, info jsonb)`; the worker upserts
  `worker` every 30 s.
- `corpus_stats_history(kind, model, n, temperature, at)`, appended by
  `refresh_corpus_stats`.

A server secret for hashing: `SECRET_KEY` env (required in prod; dev default).

## Runtime settings (penish/runtime.py)

- `async get(pool, key) -> Any` returns the db value or the default.
- `async put(pool, key, value)`, `async reset(pool, key)`.
- Defaults: models from Settings; prompts from `penish/prompts/*.md`.
- describe.py, haiku.py and scripts/grow_haikus.py read model, effort and prompt
  from here at call time.
- Every Anthropic call records `api_usage`.
- haiku.py takes its style examples from the active `haikus` table, never from the
  migrations folder.

## Worker additions

- An internal HTTP server on `:8001`, reachable only on the compose network:
  `POST /embed {text}` returns `{vector, model}` with the query instruction prefix.
  The api uses it for semantic search.
- New job types (handlers in `penish/worker/admin_jobs.py`, registered in __main__):
  - `regen {image_id, fields:[caption|description|tags|haiku|embedding]}`: re-run only
    those fields. It never overwrites owner text unless `force_owner: true` (used only
    when the admin clicked regenerate on that field, which explicitly converts it back to
    model-sourced). A description change re-embeds and refreshes corpus stats.
  - `crop {image_id, rect|null}`: rebuilds derivatives from the clean original with the
    rect (or uncropped), sets `pub_sha`, deletes old derivative files once the new ones
    exist, then regenerates model-sourced description, tags and caption, then embedding
    and stats.
  - `reembed_all`, `redescribe_all` (model-sourced fields only): enqueue per-image work.
  - `grow_haikus {n}` calls the grow script's core function.
- Delete is synchronous in the api: row delete (cascades), then remove
  `media/orig/{sha256}.*` and `media/pub/{pub_sha}-*`, then refresh corpus stats.
  The api needs `media/orig` mounted rw and `media/pub` rw for this.

## Public API (no auth)

- `GET /api/images/{slug}` gains `votes: {up, down, net, mine: -1|0|1}` and
  `comments: [{name, body, created_at}]` (approved only, oldest first).
- `POST /api/images/{slug}/vote {value: -1|0|1}`.
  - Anon id cookie `penish_v` (random 16 bytes, HttpOnly, Secure, SameSite=Lax, 1 year),
    set if missing.
  - 0 removes the vote.
  - Rate limit: 30 per minute per IP.
  - Returns the new `votes` object.
- `POST /api/images/{slug}/comments {name?, body, turnstile?}`.
  - body is 1 to 2000 chars and name at most 40, both stripped.
  - If `TURNSTILE_SECRET` is set, verify the token with Cloudflare siteverify, failing
    closed. Otherwise, and in addition, apply a rate limit of 3 per 10 min per IP hash.
  - Stored as pending. Returns `{ok:true}`. The UI then says `held for review`.
- `GET /api/config` returns `{turnstile_site_key|null}`.
- All POSTs require the `X-Requested-With: penish` header.

## Admin API (cookie auth from penish/api/admin.py, all under /api/admin, no-store)

Pictures:
- `GET /pictures?q=&status=&sort=new|old|votes|lonely&cursor=` returns
  `{items:[{id,slug,status,thumb,caption,net,created_at}], next_cursor}`. With `q`,
  results are ranked by semantic similarity via the worker /embed.
- `GET /pictures/{id}`: full detail, including every text field with source and model,
  tags, crop, votes, comment counts, and the latest job errors.
- `PATCH /pictures/{id}` with any of `caption, description, prompt, tags[], slug`.
  Edits become `source='owner'`. A caption or slug change keeps the old slug as an alias.
  A description change enqueues an embedding regen.
- `POST /pictures/{id}/regen {fields[]}` enqueues `regen` with force_owner true.
- `POST /pictures/{id}/crop {rect|null}` enqueues `crop`.
- `DELETE /pictures/{id}`.
- `GET /pictures/{id}/original` streams the clean original (for the crop UI).

Comments:
- `GET /comments?status=pending|approved|rejected`
- `POST /comments/{id} {status}`
- `DELETE /comments/{id}`

Haikus:
- `GET /haikus?source=&active=`, `POST /haikus {lines}`
- `PATCH /haikus/{id} {lines?, active?}`, `DELETE /haikus/{id}`
- `POST /haikus/grow {n}`

Models:
- `GET /models` returns `{describe:{model,effort,prompt,default_prompt},
  haiku:{model,prompt,default_prompt}, grow:{prompt,default_prompt},
  known_models:[...]}`
- `PUT /models {key, value}` and `DELETE /models/{key}` (reset).
- `POST /models/reembed_all`, `POST /models/redescribe_all`.

Queue:
- `GET /queue` returns jobs that are not done (newest first, with last_error) plus the
  dirs in `failed/` with their `.err` text.
- `POST /queue/{job_id}/retry`, `DELETE /queue/{job_id}`.
- `POST /queue/failed/{name}/retry` moves the dir back to the inbox;
  `DELETE /queue/failed/{name}` removes it.
- Names are validated against path traversal.

Stats:
- `GET /stats` returns `{pictures, by_status, votes_total, comments_pending, top[10],
  bottom[10], temperature_history[], api_usage: {last_30d: [{day, model, in, out}]}}`.

System:
- `GET /system` returns `{deployed_sha, worker_heartbeat_at, disk:{total,used,free} for
  /data, last_backup_at|null, db_size, recent_errors:[last 20 job errors]}`.
- deployed_sha comes from the `GIT_SHA` env, which the image has set at build time;
  CI passes it as a build arg.

## Admin UI (web/src/routes/admin/**)

- A layout with a plain lowercase nav: `pictures comments haikus models queue stats
  system leave`. The comments item shows the pending count when it is above 0.
- The existing upload form moves to the top of `pictures`.
- Picture detail (`/admin/pictures/[id]`):
  - The image, plus editable fields showing the source (`owner` or the model name) in
    muted text.
  - `regenerate` per field.
  - A `crop` mode: drag a rectangle over the original with aspect free, then `apply` or
    `reset`.
  - `delete` asks for confirmation by typing the slug.
  - `view` link.
- Comments: a pending list with `approve` and `reject`, and tabs for approved and
  rejected.
- Models: model select or free text, effort select, prompt textarea with `save` and
  `reset`; `re-describe all` and `re-embed all` behind a confirm.
- Minimal tables, no cards, same tokens as app.css.

## Public UI (image page)

- Under the caption: `up` and `down` as text buttons, the net score as `+7`, and
  `12 up 5 down` small and muted. The current vote is highlighted in magenta.
- A comments section below the metrics: approved comments as plain paragraphs with the
  name or `anonymous` and the date muted, then a form with name (optional), body, and
  the Turnstile widget if configured, plus a `post` button. On success it shows
  `held for review`.
