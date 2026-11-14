# mycelium: build spec

Shared contract for the mycelium galleries work. Pictures grow hyphae along
the similarity graph; reinforced edges form colonies; colonies fruit into
galleries that can die. Voice and house rules from CLAUDE.md apply to every
label, error, prompt and comment. No em dashes.

Ids below are `images.id`. A tick is ten minutes. Every duration in the
organism is counted in ticks, so a replay and a live run are the same
computation.

## Baseline found in the repo (2026-11-13)

| # | Finding | Consequence |
|---|---|---|
| B1 | The describer still writes a private `analysis` with four labelled paragraphs (Subject, Setting, Style, Mood; `describe.ANALYSIS_LABELS`, `check_analysis`). The one `description` embedding is made from it. Owner-written analyses are not validated | Existing pictures get facets by splitting `analysis`; the splitter needs a fallback for unlabelled text |
| B3 | No view tracking; no `from` state on the client | New table, endpoint, beacon |
| B4 | `social.RateLimit` is in-memory, keyed by `ip_hash`; `social._gate` requires `x-requested-with: penish`. Only hashes are stored | Reuse both. The once-per-minute rule lives in SQL so it survives restarts |
| B5 | List items come from `grid_items()`, strips from `strip()` (`penish/api/routes_images.py`). No response models | One helper feeds both builders |
| B6 | No scheduler. `heartbeat.run_heartbeat` is the only interval loop | A second loop of the same shape, not a job: jobs back off for hours, fail after 12 attempts and can be deleted from the admin queue |
| B7 | Neighbors are computed per request (`penish/similarity.py`); nothing stored | New `candidate_edges` |
| B8 | `index.catch_all` returns 200 for unknown paths; `/i/<slug>` returns the shell with 404 and og tags via `render_index` | `/g/<n>` follows `_image_page` |
| B9 | No unpublish; hard delete only. `/media/pub/` is served by filename with no database lookup and a one-year immutable header | A status alone hides nothing; files must move |
| B11 | Password login, HMAC cookie, `Depends(_gate), Depends(require_admin)`. `_gate` checks the header on POST only | Every state-changing admin route is a POST |
| B12 | `api_usage` logs tokens; `runtime.PURPOSES` is closed; no cap | New purposes and a daily call cap |

No local snapshot of production exists in the repo. The harness runs against
any DSN (a restored nightly dump) or a seeded synthetic corpus.

## Layers

```
whole  facet:subject  facet:setting  facet:style  facet:mood  need
```

Each layer is an independent organism over the same pictures: its own
candidate edges, walkers, budget, random stream and colonies. Adding a layer
never changes the draws or dynamics of another. A picture can be in one living
gallery per layer, so in up to six at once. Membership tables are
many-to-many, so overlap inside one layer can replace this later without a
schema change. The one-per-layer rule is enforced in code, not by a constraint.

Layer order for display and iteration is the order above. Code that needs a
number for a layer derives it from the name (`sha256(name)`), never from its
position, so inserting a layer later changes nothing.

## Tunables

All in `penish/mycelium/tunables.py` with default, bounds and meaning. Values
apply to every layer unless noted. Starting values; the harness sets the final
defaults.

| Name | Default | Meaning |
|---|---|---|
| `candidates` | 8 | Nearest neighbors per picture per layer |
| `affinity_floor` | 0.05 | Lowest affinity after rescale |
| `walkers` | 1 | Walkers released per picture per layer per tick |
| `steps` | 3 | Steps per walker |
| `alpha` | 1.0 | Weight on existing strength |
| `beta` | 2.0 | Weight on affinity |
| `epsilon` | 0.05 | Exploration floor |
| `rho` | 0.01 | Strength learning rate per tick (half-life about 11.5 h) |
| `budget` | 1.0 | Cap on a picture's total outgoing strength in one layer |
| `wither_min` | 0.02 | Strength below which an idle edge is dropped |
| `wither_idle` | 50 | Ticks without flow before a weak edge is dropped |
| `hypha_visible` | 0.15 | Drawn in public above this |
| `colony_enter` | 0.30 | An edge latches when strength reaches this |
| `colony_leave` | 0.24 | A latched edge unlatches below this |
| `fatigue_gamma` | 0.8 | How much a fully fatigued edge's deposit is cut |
| `fatigue_ticks` | 720 | Ticks latched to reach full fatigue (5 days); recovers at the same rate |
| `traffic_per_click` | 0.5 | Flow added per recorded click |
| `traffic_edge_cap` | 1.0 | Most traffic flow one edge takes per tick |
| `traffic_share` | 0.25 | Traffic flow in a layer is scaled down to at most this share of that tick's walker crossings |
| `fruit_ticks` | 144 | Consecutive ticks a forming colony must hold to fruit (24 h) |
| `grace_ticks` | 288 | Ticks a fading gallery has to reconnect (48 h) |
| `fruit_cap` | 3 | Fruitings across all layers per rolling 144 ticks |
| `min_size` | 3 | Smallest colony |
| `max_size` | 12 | Largest gallery |
| `remerge_size` | 10 | A full gallery admits again at or below this |
| `admit_ticks` | 6 | Consecutive ticks a join or leave must persist before it applies |
| `match_jaccard` | 0.5 | Overlap needed to keep identity |
| `drift_keep` | 0.3333 | Below this share of founders a gallery has drifted |
| `duplicate_jaccard` | 0.7 | A candidate this close to a living gallery in another layer does not fruit |
| `overgrowth_tau_min_h` | 72 | Floor of the overgrowth time constant, hours |
| `overgrowth_sigma` | 0.5 | Spread from hypha neighbors |
| `overgrowth_cap` | 0.85 | Drawn coverage at full overgrowth |

`TICK_SECONDS = 600` is a constant, not a tunable.

Tunables are versioned append-only. A tick reads one version at its start and
records its number.

## Candidate graph (`graph.py`)

Input per layer: picture ids in ascending order, one vector each, the corpus
mean for that layer (or none). Output: directed edges `(layer, src, dst,
raw, affinity)` sorted by `(src, dst)`.

- Score is centered cosine, `cos(v_i - m, v_j - m)`, falling back to raw
  cosine when there is no mean or a centered vector has zero norm (same rule
  as `similarity.py`).
- Each picture points to its `candidates` nearest others. Ties break by lower id.
- Affinity is the empirical quantile of `raw` among all edges of the layer
  (rank / (n - 1), ties averaged), then `max(affinity_floor, q)`.
- Fewer than `min_size` pictures in a layer: no edges.
- The facet bonus and need edges arrive in later increments; the function
  signature leaves room for them.

## One tick (`engine.py`)

Pure function. Inputs: tick number, seed, tunables, candidate edges per layer,
edge state, live picture ids, consumed traffic `{(src, dst): clicks}`, base
overgrowth per picture. Output: new edge state, per-edge flow, per-picture
neighbor level, summary figures.

Per layer, in this order:

1. **Random stream.** `numpy.random.Generator(PCG64(SeedSequence([seed, tick,
   layer_key])))` where `layer_key` is the first 8 bytes of `sha256(layer)` as
   an unsigned int. Only `.random()` is drawn; sampling is done by hand
   (cumulative sum, first index above the draw) so numpy's sampling
   internals never affect results.
2. **Walkers.** For each picture in ascending id order, `walkers` walkers of up
   to `steps` steps. From node `i`, the options are its out-edges excluding the
   node it just came from, weighted `(epsilon + s_ij) ** alpha * a_ij ** beta`.
   No options: the walker stops. Each crossing adds 1 to that edge's flow.
3. **Traffic.** Each consumed click on a candidate edge of this layer adds
   `traffic_per_click`, capped at `traffic_edge_cap` per edge. If total traffic
   flow exceeds `traffic_share` times this tick's walker crossings, all traffic
   flow in the layer is scaled down by one factor.
4. **Strength.** `g = f / (1 + f)` scaled by `(1 - fatigue_gamma * fatigue)`.
   `s = (1 - r) * s + r * g` with `r = rho`, or `rho / 2` for a fused edge
   (half decay and half deposit together).
5. **Budget.** If a picture's outgoing strengths in the layer sum above
   `budget`, scale them all by one factor.
6. **Latch.** Latched when `s >= colony_enter`; unlatched when `s <
   colony_leave`; otherwise unchanged.
7. **Fatigue.** Latched: `fatigue += 1 / fatigue_ticks`; else `fatigue -= 1 /
   fatigue_ticks`; clamp 0..1.
8. **Fused.** An edge is fused when it and its reverse are both latched.
9. **Wither.** Drop the state of an edge with `s < wither_min` and no flow for
   `wither_idle` ticks. Missing state means zero.
10. **Edges whose endpoints are not both live** are dropped before step 2.

After all layers: neighbor level per picture is the strength-weighted mean of
the base overgrowth of every picture it shares a visible hypha with (`s >=
hypha_visible`, either direction, any layer); 0 with none.

Determinism: iterate in sorted order everywhere; strengths are float64 and
stored as `double precision`; time is the tick's scheduled time, never the
wall clock. `state_hash` is sha256 over the sorted edge state with floats
written by `float.hex`.

Strength stays in 0..1. A picture's outgoing strength in one layer never
exceeds `budget` (within float tolerance).

## Overgrowth (`overgrowth.py`)

- `tau_hours = max(overgrowth_tau_min_h, 2 * live_pictures / views_per_day * 24)`
  where `views_per_day` is the mean over the last 7 days, floored at 1.
- `base = 1 - exp(-hours_unseen / tau_hours)`.
- `level = min(1, base * (1 + overgrowth_sigma * neighbor_level))`.
- Base is computed at read time from `last_viewed_at`, so a view clears it at
  once. The tick stores only `neighbor_level`.

## Colonies (`colonies.py`)

States: `forming`, `alive`, `fading`, `dead`, `failed`. `alive` and `fading`
are living and public. `forming` and `failed` are admin-only. A serial is
assigned at fruiting from one global counter; forming colonies have none.

**Groups.** Per layer, the undirected graph of latched edges (latched in
either direction) among live pictures. Connected components of `min_size` or
more. A component larger than `max_size` is cut by single linkage with a size
cap: edges in order `(strength desc, src, dst)`, union two sets only if the
result has at most `max_size` members; the parts of `min_size` or more are
groups.

**Each tick, per layer, in this order:**

0. Remove members that are not live from every living colony and log `left`.
   A living colony now under `min_size` dies `depopulated`.
1. Match living colonies (alive and fading) to groups. Candidate pairs with
   Jaccard >= `match_jaccard`, ordered by Jaccard desc, intersection desc,
   colony id asc, lowest picture id in the group asc. Accept a pair when both
   are free. A matched fading colony is alive again (`revived`).
2. Merge. For each group, K is the living colonies with a strict majority of
   their members inside it. If K has two or more, the survivor is the colony
   matched to the group in step 1, else the one with the highest Jaccard, which
   takes the group even below the threshold. The others die `absorbed`, with
   `absorbed_by` set to the survivor.
3. Engulfed. A living colony that is still unmatched and has a strict majority
   of its members inside a group nobody holds takes that group.
4. An unmatched living colony that is alive becomes `fading` (`faded`), with
   its members frozen. A fading colony stays fading.
5. Forming colonies match the leftover groups by the step 1 rule. Leftover
   groups become new forming colonies (`formed`); parent is the fading colony
   holding the plurality of their members, if any. An unmatched forming colony
   becomes `failed`.
6. A fading colony dies when `grace_ticks` have passed since it faded, or on
   the tick a forming colony sharing a member with it fruits. Cause `schism` if
   two or more current groups each hold at least two of its frozen members and
   none holds half or more; otherwise `starved`.
7. Drift (alive only): fewer than `drift_keep` of the founders (members at
   fruiting) remain. It dies `drifted`; its current group becomes a forming
   colony with it as parent, the same tick.
8. One cause per death, first in this order: depopulated, absorbed, schism,
   drifted, starved.

**Membership of a matched alive colony.** Pictures in the group but not in
the colony join, and members not in the group leave, only after the
difference has held for `admit_ticks` consecutive ticks. A colony with
`max_size` members admits nobody until it is at or below `remerge_size`.
Forming colonies take their group as is. A revived colony takes its group as
is.

**Fruiting.** A forming colony that has been matched for `fruit_ticks`
consecutive ticks is a candidate. Candidates fruit oldest first by (formed
tick, lowest member id), at most `fruit_cap` across all layers per rolling 144
ticks. A candidate whose members have Jaccard >= `duplicate_jaccard` with a
living gallery in another layer does not fruit and waits. At fruiting: serial,
founders, `fruited` event. Cover and title come later from the store.

**Events.** `formed, fruited, joined, left, faded, revived, failed, absorbed,
schism, starved, drifted, depopulated`. Each carries tick, colony id, layer,
and where relevant image id or other colony id.

Peak size is the most members a colony has held while living.

## Driving it

- **Harness** `python -m penish.mycelium.sim`. Reads pictures, vectors and the
  corpus mean from `--dsn`, or builds a seeded clustered corpus with
  `--synthetic N`. Writes nothing. `--ticks`, `--seed`, `--set name=value`,
  `--from-publish-dates`. Prints: publish-date spread, share of live pictures
  in a living gallery (end and mean over the run), lifespans with censoring
  (died, still alive, median of the dead, oldest alive), largest gallery,
  deaths by cause, membership changes per day, edges above each threshold.
- **Live tick** a worker loop (Increment 2).
- **Genesis** replay from publish dates (Increment 2).

## Increments

0 verify and simulate, 1 attention and overgrowth, 2 the network on the whole
layer, 3 providers and facets, 4 fruiting and death, 5 needs and offers,
6 naming and the map. Detail per increment lives with each change.

## Increment 1

Attention and overgrowth. Useful alone: neglected pictures grow over, a view
clears them. `neighbor_level` is 0 everywhere until the tick writes it in
Increment 2.

### Tables (`migrations/006_attention.sql`)

| Table | Columns | Notes |
|---|---|---|
| `picture_attention` | `image_id bigint` primary key, references `images` on delete cascade; `views integer` default 0; `last_viewed_at timestamptz not null`; `neighbor_level double precision` default 0 | One row per picture. Backfilled for every existing image with `last_viewed_at = now()` at migration time, and upserted in the `ingest._go_live` transaction (`on conflict` resets `last_viewed_at = now()`, keeps `views`), so nothing starts overgrown, including a picture that waited in the queue with a row from an earlier stint |
| `view_days` | `day date` primary key; `n integer` default 0 | Counted views per day; the pace behind tau |

Neither table has a column about the visitor: no address, no hash, no cookie,
no user agent. A test introspects `information_schema` to keep it that way.
Both join the truncate list in `tests/conftest.py`.

### `POST /api/images/{slug}/view`

- Body optional: `{"from": slug | null}`. `from` is a string of `[a-z0-9-]`,
  1 to 100 characters, or null; anything else is 422. In this increment it is
  validated and dropped; Increment 2 turns it into traffic on a candidate edge.
- Guards in order: `social._gate` (`x-requested-with: penish`, else 403
  `no`); `attention.view_limit`, a `social.RateLimit` of 30 per 60 s keyed by
  `ip_hash` in memory only (429 `wait`), checked before the lookup so a flood
  never reaches the database; `resolve_live` (current slug or alias of a live
  picture, else 404 `not here`).
- `store.record_view` in one transaction: an upsert into
  `picture_attention` that sets `views + 1, last_viewed_at = now()` only
  `where last_viewed_at < now() - interval '1 minute'`. The minute rule lives
  in the row, so it holds across restarts and processes. Only when a row came
  back does `view_days(current_date)` gain one.
- Never touches `images.updated_at`, which versions the sort snapshots.
- Returns 204 with no body.

### Overgrowth on tiles

`store.overgrowth_by_id(conn, ids, tunables=DEFAULTS) -> dict[int, float]`,
one query per batch:

- `live` = count of live published pictures; `views` = `sum(view_days.n)`
  where `day > current_date - 7` (seven days, today included).
- `tau = overgrowth.tau_hours(live, views_per_day(views), tunables)`, so at
  least `overgrowth_tau_min_h` (72 h) with no views or no live pictures.
- `hours` = database `now()` minus `last_viewed_at`, or `published_at` when a
  picture has no attention row; 0 overgrowth when it has neither.
- `min(1, base * (1 + overgrowth_sigma * neighbor_level))`, clamped to 0..1,
  rounded to 3 places.
- Tunables are `tunables.DEFAULTS` until the versioned table arrives in
  Increment 2.

Payloads gain `overgrowth: float` on:

- every item of `GET /api/images` (newest and computed sorts),
  `GET /api/search` and each category `cover` of `GET /api/categories` (its
  query now selects `i.id`); all through `grid_items(rows, overgrowth)`;
- every entry of `similar` and `dissimilar` in `GET /api/images/{slug}`.

The detail payload itself carries no overgrowth for the picture being shown.

### Client

`ImageItem.overgrowth` and `Neighbor.overgrowth` in `web/src/lib/api.ts`.
The image page sends the view two seconds after it is shown (`fetch` with
`keepalive`, cancelled when the reader leaves first), `from` being the
previous image slug from `afterNavigate`, or null when that slug does not
match `^[a-z0-9-]{1,100}$` (a mistyped URL would otherwise make the server
422 the whole view). Slugs viewed this session render at
0 everywhere, including grids restored from a snapshot. `lib/hyphae.ts` draws
a fixed dot order seeded from the slug, so only extent changes with level,
capped at `overgrowth_cap`, once, near the viewport, no animation. Every slug
gets exactly 220 dots (a tip that would leave the picture turns back off the
edge rather than dying), so a level covers the same share of every picture;
`tests/test_hyphae.py` runs the shipped source under node to hold that.

### Deferred

- The `most overgrown` sort.

## Increment 2

The network on the whole layer: persistence, the live tick, traffic, genesis,
admin, and the public `reaching` strip. Colonies are detected and stored from
here on, admin-only until Increment 4.

### Tables (`migrations/007_mycelium.sql`)

| Table | Columns | Notes |
|---|---|---|
| `mycelium_state` | `id` (check 1), `running`, `seed bigint >= 0`, `next_due`, `last_tick`, `graph_version`, `graph_dirty`, `tunables_version`, `colonies jsonb`, `public_since`, `simulated_through`, `updated_at` | One row, inserted by the migration with a random seed. Ships stopped. `colonies` is `ColoniesState.to_dict()`, so a restore is exact. Lock order: anyone who touches a row the tick writes or key-share locks (images, network tables, attention) takes this row before it |
| `candidate_edges` | `layer, src, dst, raw, affinity`, pk `(layer, src, dst)` | Image FKs cascade. Indexed on `(src, dst)` (the click check) and `dst` |
| `edge_state` | `layer, src, dst, strength, latched, fatigue, fused, idle`, pk `(layer, src, dst)` | `engine.EdgeState` field for field; `double precision` round trips float64 exactly, so a restored state hashes as it was saved. `engine.state_hash` skips a layer with no edges, since a reload cannot name an empty layer |
| `mycelium_traffic` | `id, src, dst, created_at` | Clicks waiting for a tick. Nothing about the visitor, of any kind |
| `mycelium_ticks` | `tick` pk, `scheduled_at, ran_at, simulated, state_hash, colonies_hash, stats jsonb, tunables_version, graph_version` | The pk is what makes a second commit of one tick fail. `stats` per layer: `live, in_gallery, edges, visible, latched, fused, mean_strength, crossings, traffic, clicks` |
| `mycelium_tunables` | `version serial, "values" jsonb, created_at, note` | Append only, newest is current. Only values that differ from the defaults are stored. `"values"` is quoted (reserved word); every read goes through `store` |
| `colonies` | `id` (= colonies-module id), `layer, state, serial unique, title, title_source (model, owner), born_tick, fruited_tick, faded_tick, died_tick, cause, parent_id, absorbed_by, cover_image_id, peak, simulated, updated_at` | Never deleted outside a full wipe. `cause` adds `reset` to the module's causes |
| `colony_members` | `id, colony_id, image_id (set null), slug, founder, joined_tick, left_tick` | One row per stint; the slug stays for a tombstone of a deleted picture |
| `colony_events` | `id, tick, scheduled_at, kind, colony_id, layer, image_id (set null), other_colony_id, simulated` | `kind` adds `reset` |
| `colony_ticks` | `colony_id, tick, size, mean_strength`, pk both | Forming and living colonies each tick; mean of latched strengths inside, else 0 |

All of them are in the truncate list of `tests/conftest.py`, which then
reinserts the state row with seed 1.

### The tick (`penish/mycelium/tick.py`, SQL in `store.py`)

A loop beside the heartbeat wakes every 30 s and runs what is due, catching
and logging every exception. One tick is one transaction: lock the state row,
return if stopped or not due, rebuild the whole-layer candidates when
`graph_dirty` is set (or a stored edge has an endpoint that is no longer live),
take the clicks with `delete ... where created_at < scheduled_at + 10 min
returning`, step the engine and the colonies on the persisted seed, write edge
state, the colony projection, `neighbor_level` and the tick row, and move
`next_due` on by one tick. Base overgrowth is computed at the tick's scheduled
time. Catch-up runs at most 144 ticks per wake, re-checking `running` under
the lock each time; a backlog left after that moves `next_due` to now. Start
sets `next_due = now + 10 min`, keeping a `next_due` already inside that
window (one genesis left).

The graph is centered with `corpus_stats.mean` for (`description`, embed
model) once the live corpus has `centered_min_images` vectors, the rule
`similarity.py` uses for its auto mode. The dirty mark is the first statement
of the go-live, admin delete, admin analysis edit and `ingest._ensure_embedding`
transactions, since those take locks on `images` (or rows the tick's foreign
keys key-share lock) that a tick holding the state row would wait on. The other
description-embedding writers (`ingest._write_text`, `admin_jobs._apply_described`,
`admin_jobs._embed`) mark after their write: they lock only texts and
embeddings, which the tick never locks. `refresh_corpus_stats` for
`description` marks once more at the end of its own transaction, because the
writer's mark commits before the new mean does and a tick in between would
rebuild on the old mean and clear the mark; it marks last so the state row is
not held across the pairwise average, which is safe because the tick only
reads `corpus_stats`.

### Traffic

`POST /api/images/{slug}/view` with `from`: when the organism is running,
`from` resolves (slug or old alias) to a live picture other than this one and
`(from, this)` is a candidate edge in any layer, one row goes into
`mycelium_traffic`. A second `RateLimit`, 6 per 10 minutes per `ip_hash`, in
memory, is spent only on clicks that would count. Views count as before
whether or not the click does; the response is still 204. The view commits
before the click check, so its attention row lock never waits on network
tables.

### Genesis (`penish/mycelium/genesis.py`)

Worker job `mycelium_genesis` (dedupe key the same), registered in
`admin_jobs.HANDLERS`. Never retried: any failure is a `HardError`.

- Refuses (`running`, `public`, `no publish dates`, `fewer than N pictures`,
  `nothing to replay`)
  before touching anything.
- Tick 0 is the publish time of the `centered_min_images`-th live picture with
  a vector (ties by id). Every live picture enters at
  `max(0, floor((published_at - tick 0) / 10 min))`; one with no publish time
  enters at 0. A picture without a vector is live and has no edges.
- The graph is rebuilt, with today's mean, whenever a picture with a vector
  arrives. No clicks. Base overgrowth only for the last tick, since it feeds
  nothing but the stored neighbor level.
- Each tick writes its colony projection and its tick row with the tick's
  `scheduled_at` and `simulated = true`. The fruiting cap is the colonies
  module's own rolling 144 ticks.
- Ends at the last tick scheduled at or before now: `last_tick` and
  `simulated_through` set to it, `next_due` the tick after, graph clean, still
  stopped.
- Before committing it rebuilds the graph from a fresh read of the live corpus
  and raises `GenesisMismatch` if that differs from the replay's last graph.
- One transaction from the refusal checks through the wipe to the commit, so a
  failure leaves the network as it was. Publishes, deletes and embedding writes
  wait on the state row meanwhile. About 8 ms of CPU per tick at 300 pictures,
  so a year of history takes some minutes.
- Readers never wait on it. The wipe (`store.wipe_network`, shared with the
  full reset) deletes rather than truncates: truncate's access exclusive lock
  would hold every image page (`hyphae_for` reads `edge_state`) and every click
  check (`candidate_edges`) for the whole replay and drain the api pool. Until
  the commit other transactions see the old network. Neighbor levels are not
  touched until the final statements, so a view of a picture with a level does
  not wait on the replay either.

### Admin API (`penish/api/admin_mycelium.py`)

Prefix `/api/admin/mycelium`, `Depends(_gate)` and `Depends(require_admin)`.
Every route returns the payload below.

`GET ""`:

```
{running, tick: int|null, next_due: iso|null, behind: int, graph_version: int,
 graph_dirty, public, simulated_through: int|null,
 genesis: {status: none|queued|running|failed|done, error: str|null},
 tunables: [{name, value, default, lo, hi, meaning, int}],
 tunables_version: int|null,
 history: [{version, created_at, values, note}],          newest first, 20
 series: [{tick, scheduled_at, simulated, live, in_gallery, visible, latched,
           mean_strength, crossings, traffic}],          whole layer, 1008, oldest first
 colonies: {forming: [colony], living: [colony]},
 events: [{tick, scheduled_at, kind, colony_id, layer, slug|null,
           other_colony_id|null, simulated}]}             newest first, 50
colony = {id, layer, state, serial|null, title|null, size, born_tick,
          fruited_tick|null, members: [{slug, founder}]}  live open members only
```

`tunables` is in field order; `lo` and `hi` are ints for an integer tunable.
`genesis` is the newest `mycelium_genesis` job; `error` is its `last_error`
only when it failed.

POSTs:

| Path | Body | Does | Errors |
|---|---|---|---|
| `/start` | none | running; `next_due` as above | |
| `/pause` | none | stopped, after any tick in progress commits | |
| `/tunables` | `{values: {name: number}, note?: string}` | appends a version: current overrides with these applied, only non-defaults stored; `candidates`, `affinity_floor` or `min_size` changing marks the graph dirty | 400 with the `TunableError` text, `nothing to change` for `{}`, `note too long` past 200 |
| `/regrow` | none | `graph_dirty = true` | |
| `/reset` | none | before `public_since`: full wipe of network and colony tables, numbering restarts at 0; after: edge state, waiting clicks and neighbor levels cleared, forming colonies `failed` and living ones `dead`, cause `reset`, `died_tick = last_tick`, a `reset` event each, serials and tombstones kept. Both keep running, seed, clock and tunables and mark the graph dirty | |
| `/genesis` | none | enqueues the job; a second press collapses into a queued or running one | 409 `running`, 409 `public` |

### Public

`GET /api/images/{slug}` gains `hyphae`: at most 8 items of the `similar`
shape plus `strength` and `layer`, `{slug, score, blurhash, src, overgrowth,
strength, layer}`. One item per neighbor: the strongest edge over both
directions and every layer, strength at least `hypha_visible`, live
neighbors only, by strength desc then slug. `score` is the strength, since a
hypha has no similarity of its own. Read from `edge_state` only, so need
phrases cannot reach it. Empty before the first tick; frozen while paused.

### Web

- Image page: a `reaching` strip under `dissimilar`, the same `Strip` with a
  `Reach` mark under each tile; hidden when `hyphae` is empty. `Reach` is a row
  of dots on a 112 by 6 box: the kind sets dot radius and spacing (`whole`
  1.1 every 4.5, `facet:*` 0.6 every 2.4, `need` 1.7 every 8), never colour or
  a label, so kinds read apart in greyscale. Strength sets how far the row runs
  across the tile and its opacity (0.35 to 1).
- Admin `mycelium` page, nav entry `mycelium`: a state line, start and pause,
  regrow, reset and genesis as links, sparklines per series metric (genesis
  ticks drawn dim before the lived ones), forming and living colonies with
  members (non-founders dim), the tunables table with history, recent events.
- Reset is armed by the first click: the label turns to `sure` in the accent
  colour, and a second click within 5 s resets. No browser dialog.
- The page refreshes every 30 s while visible and idle. Responses carry the
  order their requests were sent in, and one older than what is on screen is
  dropped, so a refresh that was in flight when an action answered cannot put
  the old state back. Only tunables the owner typed in survive a refresh; the
  rest follow the server, so a value saved elsewhere is never posted back.
  Save and undo start the draft over.
