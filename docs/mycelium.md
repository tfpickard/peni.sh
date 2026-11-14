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
| `facet_bonus` | 0.15 | Score bonus in a facet layer for a pair close in that facet and far apart in another (Increment 3) |
| `need_damp` | 0.8 | How much a fully satisfied need's edges pull less: affinity times `1 - satisfaction * need_damp` (Increment 5) |
| `need_feed` | 0.05 | Share of the way to satisfied a need moves in a tick whose edges carried flow 1 or more |
| `need_drift` | 0.002 | Share of its satisfaction every need loses each tick |
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
records its number. A version must keep `min_size <= remerge_size <= max_size`
(a remerge below the floor would leave a full gallery to die before it ever
admits again), `colony_leave <= colony_enter` and `wither_min <= colony_leave`.

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
  then `overgrowth.coverage` (times `overgrowth_cap` from the loaded tunables),
  rounded to 3 places. What leaves the server is drawn coverage, so the admin
  cap is the only cap: at 0 every payload reads 0 and nothing is drawn.
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
`round(overgrowth * 220)` dots as sent (the cap is already in the value, the
client holds no copy of it), once, near the viewport, no animation. Every slug
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
`admin_jobs._embed`) take the state row first too: the tick never locks texts
or embeddings, but a facet write and an admin analysis edit take the state row
and then the analysis row, so a writer holding that row while it waits for the
state row deadlocks with them. `_embed` marks first. `_write_text` and
`_apply_described` lock it `for update` before the analysis row whenever they
may write a new analysis and mark only once the write lands, so an owner text
that turns the model away, or an analysis that comes back unchanged, costs no
rebuild.
`refresh_corpus_stats` for
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

## Increment 3

### Providers (`penish/worker/providers.py`)

A model id may carry a provider: `openai:<id>`, `openrouter:<vendor/id>`,
`xai:<id>`. `runtime.split_model` splits on the first colon only (OpenRouter
variants such as `vendor/model:free` keep theirs); a bare id is Anthropic, so
every id stored before this increment still means what it meant.
`runtime.validate` rejects an unknown prefix or an empty id on every
`*.model` key, which is what the admin `PUT /api/admin/models` answers 422
with (`unknown provider mistral`). Whitespace around the id after the prefix
is stripped too (`openai: gpt-6-astra` is stored as `openai:gpt-6-astra`), and
`split_model` strips it from ids stored before that: a provider takes the
space as part of the id and refuses every call.

The seam is `describe.call_text`. When the id has a prefix and the client it
was given is `None`, a `describe.LazyAnthropic` or a real
`anthropic.AsyncAnthropic`, it swaps in `providers.client_for(model)`; a test
fake is kept, so tests that inject `FakeClient` stay off the network whatever
model the runtime returns. Every caller (describe, categorize, extract, haiku,
name, grow and `scripts/grow_haikus.py`) defaults to a `LazyAnthropic`, which
builds the SDK client only on the first bare-id call: with no key the SDK
resolves credentials from env and disk at construction and can raise there, so
a worker keyed only for other providers never builds one. A bare id with no
`ANTHROPIC_API_KEY` fails without a request: `no key for anthropic`, not
retryable. The
adapter exposes `.messages.create(model=..., **kwargs)` and returns
`.content = [TextBlock(type="text", text=...)]` and `.usage` with
`input_tokens` and `output_tokens`, so callers and `api_usage` (recorded under
the full prefixed id) are unchanged. `ProviderError(retryable)` becomes
`DescribeError` with the same retryability.

| Provider | Endpoint | Wire | Images sent as is | Effort |
|---|---|---|---|---|
| openai | `https://api.openai.com/v1/responses` | Responses | png, jpeg, webp, gif | `reasoning.effort`; `gpt-6*` and later take low..max, `gpt-5*` and `o*` clamp to high, `gpt-4*` and older get none |
| xai | `https://api.x.ai/v1/responses` | Responses | png, jpeg | `reasoning.effort`; grok-4.5 up to high, 4.6 and 4.7 (and any grok-5+) up to xhigh, everything else (grok-4.20 included) none |
| openrouter | `https://openrouter.ai/api/v1/chat/completions` | Chat completions | png, jpeg | `reasoning.effort` always, max..low passed through |

Translation: `system` (string or text blocks; `cache_control` dropped) goes to
`instructions` (Responses) or a leading system message (chat). Text blocks
become `input_text` / `text` parts, base64 image blocks become `input_image`
with a data URL / `image_url: {url}`. A media type the provider does not list
is re-encoded to jpeg (q 90, alpha flattened on white) with pyvips in a
thread; the describer sends webp, so xAI and OpenRouter always get jpeg.
`output_config.format` json_schema becomes `text.format = {type:
json_schema, name: output, schema, strict: true}` (Responses) or
`response_format = {type: json_schema, json_schema: {name: output, strict:
true, schema}}` (chat). `max_tokens` becomes `max_output_tokens` (Responses)
or `max_tokens` (chat). Responses requests set `store: false`. Any other
kwarg except `temperature` is refused rather than silently dropped. No
OpenRouter attribution headers (`HTTP-Referer`, `X-OpenRouter-Title`): they
are optional and would name the site.

Responses: Responses-wire text is every `output_text` part of every `message`
item; chat text is `choices[0].message.content` (string or text parts). A
refusal or null content is no text, as an empty Anthropic answer is, and the
caller's validation retries it. Usage maps `input_tokens/output_tokens` or
`prompt_tokens/completion_tokens`; missing counts are 0.

Errors: 408 (the provider's own timeout), 429 and 5xx, timeouts and
transport errors are retryable; other 4xx are not. The message is `<provider> <status>: <provider message>` capped at
300 characters, never the request body or the key. OpenRouter's 200 with an
`error` object and no choices uses the error's code (502 when absent). A call
to a provider with no key fails without a request: `no key for openai`, not
retryable.

Keys: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY` in `Settings`
and the worker's compose env (the api never calls a model). The heartbeat
info gains `providers`, the names of the keyed ones (`anthropic` first);
`GET /api/admin/models` returns it as `keyed` (null until a worker reports).

Verified on 2026-11-14 against:

- OpenAI: https://developers.openai.com/api/reference/resources/responses/methods/create,
  https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create,
  https://developers.openai.com/api/docs/guides/structured-outputs (strict
  needs every property required and `additionalProperties: false`, both
  already true of every schema here; `minItems`/`maxItems` supported),
  https://developers.openai.com/api/docs/guides/images-vision (png, jpeg,
  webp, non-animated gif), https://developers.openai.com/api/docs/models
  (gpt-6-astra, gpt-6.1-sol, gpt-6.1-terra, gpt-6-luna; all take images and
  reasoning effort up to max).
- xAI: https://docs.x.ai/docs/api-reference (Responses: `instructions`,
  `reasoning.effort`, `max_output_tokens`, `store`, usage
  `input_tokens/output_tokens`), https://docs.x.ai/docs/guides/chat-completions
  ("offered as a legacy endpoint", hence Responses),
  https://docs.x.ai/docs/guides/structured-outputs (`text.format` json_schema
  strict; `minItems`/`maxItems` accepted), https://docs.x.ai/docs/guides/image-understanding
  (jpg/jpeg and png only, 20 MiB), https://docs.x.ai/docs/guides/reasoning
  (grok-4.5, 4.6, 4.7 take effort; xhigh from 4.6), https://docs.x.ai/docs/models.
- OpenRouter: https://openrouter.ai/docs/api-reference/overview,
  https://openrouter.ai/docs/features/structured-outputs (strict json_schema;
  some upstreams treat it as a hint),
  https://openrouter.ai/docs/use-cases/reasoning-tokens (`reasoning.effort`
  max..none), https://openrouter.ai/docs/guides/routing/provider-selection
  (upstreams ignore unknown parameters unless `require_parameters`; the
  response format routes to supporting upstreams on its own),
  https://openrouter.ai/docs/guides/overview/multimodal/image-understanding
  (png, jpeg, webp, gif; the batch page names an upstream that rejects webp),
  and the public model list `GET https://openrouter.ai/api/v1/models` for
  which ids take images and structured outputs.

`admin_ops.KNOWN_MODELS` adds `openai:gpt-6-astra`, `openai:gpt-6.1-sol`,
`openai:gpt-6.1-terra`, `openai:gpt-6-luna`, `xai:grok-4.7`, `xai:grok-4.6`,
`openrouter:google/gemini-4-carbon`, `openrouter:qwen/qwen3.8-max-prime`,
`openrouter:mistralai/mistral-large-4-0`. Any other valid `provider:id`
typed by hand is accepted and then listed.

### Runtime keys and the daily cap

| Key | Default | Validation |
|---|---|---|
| `extract.model` | `describe_model` | non-empty, known prefix or bare |
| `extract.effort` | null | one of `EFFORTS` or null |
| `extract.prompt` | `penish/prompts/mycelium_extract.md` | non-empty |
| `name.model` | `describe_model` | as `extract.model` |
| `name.prompt` | `penish/prompts/mycelium_name.md` (Increment 6 fills its use) | non-empty |
| `mycelium.daily_calls` | 400 | int (not bool), 0..100000 |

`PURPOSES` gains `extract` and `name`; `runtime.CAPPED_PURPOSES` is those two.
`runtime.calls_today(pool, now=None)` counts `api_usage` rows of capped
purposes since UTC midnight. `providers.check_daily_cap(pool, now=None)` is
called by a job before every capped call: at or over the cap it raises
`SoftError("daily cap used/cap", run_after=next UTC midnight)`. Describe,
haiku, grow and categorize are never capped. The count is read, not
reserved, so concurrent jobs can overrun the cap by the job loop's
concurrency; it bounds spend, it is not a quota.

`jobs.SoftError` takes an optional `run_after`. With it, `_run_one` calls
`jobs.defer`: the job is queued for exactly that time with its attempt handed
back (`attempts - 1`), so waiting on the cap never reaches `MAX_ATTEMPTS`.
Without it nothing changes.

`GET /api/admin/models` adds `purposes`, `providers` (all four names),
`keyed` and `calls_today`. The models page shows `keyed`, an effort row for
describe and extract, blocks for extract and name, and `daily calls` with
`n today`.

### Facet tables (`migrations/008_facets.sql`)

| Table or column | Shape | Notes |
|---|---|---|
| `image_facets` | `image_id` (cascade), `facet` in (subject, setting, style, mood), `recipe` in (split, extract), `body text not null`, `model text null`, `created_at`; pk `(image_id, facet, recipe)` | `split` bodies are cut from the analysis (model null), or from one extract call when it has no labels (model set). No owner source yet, so no source column |
| `image_extracts` | `image_id` pk (cascade), `model`, `body jsonb`, `created_at` | The whole raw extract response; Increment 5 reads offers and needs from it |
| `embeddings.kind` | check: `description`, `image`, or `^facet:(subject\|setting\|style\|mood)/(split\|extract)$` | One kind per facet and recipe, e.g. `facet:mood/split`, under the embed model |
| `mycelium_state.facet_recipe` | `text not null default 'split'`, check in (split, extract) | Which recipe's vectors the facet layers grow on |

Both tables are in the `tests/conftest.py` truncate list. Down deletes facet
vectors and facet corpus rows before restoring the old kind check.

### Facet names and the splitter (`penish/mycelium/facets.py`)

Pure. `FACETS` is `describe.ANALYSIS_LABELS` lowercased; `RECIPES = (split,
extract)`; `LAYERS` the four `facet:<name>`; `kind(facet, recipe)`,
`kinds(recipe=None)`, `is_kind(name)`, `layer(facet)`, `facet_of(layer)`.

`split(analysis) -> {subject, setting, style, mood} | None`: a label is the
word at the start of a line (any leading whitespace, case exact), optional
whitespace, then a colon: the same pattern, `\s` and all, as
`describe.check_analysis`, so an analysis that passed validation always splits
rather than buying a capped fallback call. The four
labels must appear exactly once each, in order; a body is the text up to the
next label, stripped, and must not be empty. Text before the first label is
dropped. Anything else (owner free text, a missing, repeated, reordered or
lowercase label) is None, and the caller falls back to the extract call.

### Facet means (`penish/worker/metrics.py`)

`refresh_corpus_stats(kind='description')` also refreshes every facet kind that
has vectors or a corpus row under the same model, in the same transaction:
whatever changed the live set (go-live, delete) moved those means too. A facet
kind passed directly refreshes only itself. Facet kinds get `mean` and `n`
only: temperature is null and no history row is written, since nothing reads
or charts them and eight pairwise averages per publish would cost seconds.
`refresh_facet_stats(pool, model=, recipe=None)` refreshes one recipe's four
(or all eight) in one transaction for a job that has embedded a batch. Both
walk the facet kinds in sorted order (sorted in Python, not by collation):
each upsert holds its `corpus_stats` row to commit, and a description refresh
racing a facet job's refresh in opposite orders would deadlock. Any
refresh that touches `description` or a facet kind marks the graph dirty last,
as before.

### Facet layers and the bonus (`graph.py`, `tick.py`, `store.py`)

`tick.ACTIVE_LAYERS = (whole, facet:subject, facet:setting, facet:style,
facet:mood)`. A rebuild reads the whole layer as before and
`store.load_facet_inputs(recipe=state.facet_recipe)`: one `GraphInputs` per
facet layer, live pictures with that facet's vector of the active recipe, and
that kind's own corpus mean, applied under the same `centered_min_images`
rule as the whole layer. A picture without the vector is absent from that
layer and nowhere else. Candidates are written for all five layers (an empty
layer clears its rows). A tick steps the whole layer and every facet layer
that has candidates; a layer with old state and no candidates still steps (the
engine runs every layer with state) and decays, which is what a recipe switch
leaves behind. Tick stats carry each stepped layer.

The bonus (`graph.facet_bonuses`, applied through `candidate_edges`'
`bonus`, so the neighbor choice, the stored `raw` and the affinity quantile
all include it): for facet layer f and pair (i, j),

```
bonus_f(i, j) = facet_bonus * max(0, max over g != f of q_f(i, j) - q_g(i, j))
```

where `q_x(i, j)` is the pair's centered score as a quantile among all
unordered pairs of layer x (`graph.quantiles`, ties averaged). Quantiles, not
raw cosines, because the facets' cosines sit at different heights. A layer
missing i or j sits out for that pair; with no other layer, no bonus. This is
the exact formula, not an approximation: every layer's full pair matrix,
O(n^2) memory and an n^2 log n sort per layer. Measured with 1024-dim vectors
and four layers: `facet_bonuses` about 0.5 s and 0.25 GB at 1500 pictures,
about 2.4 s and 0.9 GB at 3000; `facet_graphs` about 0.9 s and 3.9 s. The live
tick pays it once per rebuild; a full genesis pays it on every tick with an
arrival while it holds the state row, so a genesis over a few thousand
pictures holds it for minutes longer than a whole-layer replay would. The gap per layer is computed as
`q_f - min over g of q_g`, which equals the max of the differences exactly
(rounded subtraction is monotone) with one n x n temporary per layer. Cheaper
forms (bonus only for each row's top candidates, float32) would change the
graph, so they wait for a gallery large enough to need them. `graph.BonusMatrix` holds it as a matrix over
the union of ids, so `candidate_edges` adds it in one step. `facet_bonus`
joins `store.GRAPH_TUNABLES`, so changing it rebuilds the graph.
`graph.facet_graphs` builds all four together, since each one's bonus reads
the others.

`store.facet_counts(conn, model=, dim=) -> {split, extract, live}`: live
pictures with all four bodies and all four vectors of each recipe.
`store.set_facet_recipe(conn, recipe, model=, dim=)` takes the state row,
refuses with `FacetSourceRefused("not every live picture has <recipe>
facets")` while the target count is below live, else sets `facet_recipe` and
marks dirty; a switch to the active recipe is a no-op.

### Layer-scoped genesis (`genesis.py`)

`genesis.run(conn, layers=None)`; the job reads `payload.layers` (a list of
names or absent; anything else fails the job: `layers must be a list of
names`). `genesis.scope(layers)`:

- absent, or every active layer: the full genesis. It now replays all five
  layers: the facet graphs are rebuilt (all four together) whenever a picture
  with any facet vector of the active recipe arrives, and the final check
  compares every layer with a live rebuild. Still refused once public.
- only facet layers: a scoped run. `whole` with some facets is refused
  (`whole replays with every layer`): the whole layer defines the clock the
  others replay on. Unknown names: `unknown layer <x>`.

A scoped run, in one transaction under the state row:

- Refused when running, when any colony in those layers ever had a serial
  (`<layer> has galleries`, checked in the state and the table), or when no
  tick is stored (`nothing to replay`). Allowed before or after
  `public_since`.
- Deletes those layers' candidates, edge state and colonies (their members,
  events and colony ticks go by cascade). Ticks, clicks and other layers stay.
- Replays over the stored tick rows in order at their recorded
  `scheduled_at`, so a pause is a gap as it was live. A picture enters at the
  tick whose window holds its publish time, or the first tick after it; before
  the first tick or with no publish time at the first; after the last window
  at the last (`genesis.entries_at`), so the replay ends on today's corpus.
- Graphs use today's facet vectors and means, all four facets read for the
  bonus whether replayed or not. No clicks.
- Other layers' colonies are held as they are now: they never step, still
  block duplicates (`duplicate_jaccard`), and serials and colony ids continue
  from the current counters. During the replay the fruit cap counts only the
  replayed layers' fruitings; at the end the held and replayed fruiting ticks
  are merged inside the rolling window.
- Each replayed tick's figures are merged into its stored row
  (`stats || {layer: ...}`); the row's hashes stay those of the run that wrote
  it. `last_tick`, `next_due`, `simulated_through` and the dirty mark are not
  touched; `graph_version` moves on by one.
- Neighbor levels are recomputed at the last tick over every layer's state.
- Ends by comparing the replayed graphs with a live rebuild of those layers
  (`GenesisMismatch` rolls everything back).

Each layer draws from its own random stream, so a scoped replay of a layer
over the same ticks and corpus grows exactly the edge state the full run grew
for it; only its colonies can differ, through the held galleries.

`colonies.step(..., layers=None)` and `tick.step(..., layers=None)` take the
layers to step; `store.apply_colonies(..., layers=None)` limits the per-tick
colony rows to those layers. New store helpers: `wipe_layers`, `tick_times`,
`merge_tick_stats`.

### Facet jobs (`penish/worker/facet_jobs.py`)

Two worker jobs, registered in `admin_jobs.HANDLERS` (they need the embedder),
dedupe key `<type>:<image_id>`, payload `{image_id}`. Both skip a picture that is
gone or not live, and return at once when the picture already has all four
facets of their recipe with vectors under today's embed model and dim.

`mycelium_extract`, inserted in the go-live transaction beside `haiku` and
`categorize` (so it never delays the publish and a crash cannot lose it):

- One vision call (`facet_jobs.extract`, purpose `extract`, runtime keys
  `extract.model`, `extract.effort`, `extract.prompt`), the displayed pixels as
  the describer sees them (crop applied, webp). Strict schema `{subject,
  setting, style, mood: string, offers: string[], needs: string[]}`, every key
  required, `additionalProperties: false`. No `maxItems`: not every provider's
  strict mode takes array bounds, so `clean` trims.
- `clean`: each facet through `no_em_dash`, stripped, not empty, and not
  opening with a facet label; offers and needs through `no_em_dash`, spaces
  collapsed, lowercased, trailing period dropped, deduped, five at most. A bad
  answer is asked once more (two calls, like describe), then a retryable error.
- `providers.check_daily_cap` runs before each attempt. Over the cap the job is
  deferred to the next UTC midnight plus `EXTRACT_STEP` (30 s) times the number
  of `mycelium_extract` and `mycelium_split` jobs already waiting for that
  reset, so a capped sweep comes due spaced for vision calls rather than all at
  midnight in front of the day's uploads. A split reaches the cap only on its
  paid fallback, so it shares the same line and step. The attempt is handed
  back (`jobs.defer`).
- Stores the raw parsed response in `image_extracts.body` as returned (not
  cleaned: Increment 5 reads offers and needs through `facet_jobs.clean`), the
  four cleaned bodies in `image_facets` under `extract` with the model, and
  four vectors under `facet:<name>/extract`.
- A stored extract whose vectors are missing (a new embed model) is re-embedded
  without a call.

`mycelium_split`:

- Inserted in the go-live transaction too, by every split invalidation of a
  live picture, and by the startup sweep `enqueue_unsplit` (worker start,
  beside `enqueue_uncategorized`): live pictures lacking any of the four split
  bodies or vectors, deduped, so a second sweep enqueues nothing. Spacing
  follows the cost: a picture that will need the paid fallback (no stored
  split bodies, an analysis that does not split, no stored or queued extract
  to borrow) goes at `now + EXTRACT_STEP x its rank among those` (30 s), every
  other one at `now + SPLIT_STEP x its rank among those` (3 s). The queue
  orders by `run_after`, so jobs that come due faster than they run become a
  past-due backlog claimed before a fresh upload; on its own grid each kind
  comes due about as fast as it runs, and none is due now.
- Bodies, in order of preference: split rows already stored (they belong to the
  current analysis, since an analysis change deletes them), `facets.split` of
  the analysis (model null), the picture's stored extract (copied, model set),
  and last one extract call. While the picture's own `mycelium_extract` job is
  queued or running the split waits for it (deferred to that job's
  `run_after`, or now, plus 1 min) instead of paying for the same call twice.
  A fallback call is stored as the extract recipe too, raw response included.
  Bodies with a model (a fallback, a stored one or a borrowed one) are a
  reading of the pixels, so their write checks `pub_sha` as an extract's does.
- Embeds the four bodies under `facet:<name>/split`.

Writes, both jobs: one transaction that locks the image row `for share` (a
crop's `pub_sha` update waits for it, or it sees the new value) and, for a
split, the analysis row `for share`; it refuses with a retryable
`picture changed while extracting` / `analysis changed while splitting` when
what it read has moved, because the invalidation that came with the change may
have collapsed into this very job's dedupe key. The transaction takes the state row first (`mark_dirty`), as every other
embedding write does, then images, then the analysis row. A genesis holds the
state row for its whole replay and compares the replay with a fresh read of
the facet vectors at the end, so a facet write committing mid-replay would
fail the run with `GenesisMismatch`; it waits instead. Holding the mark to commit
also keeps a tick from clearing it before the vectors are visible.

After the write and the means refresh, each job reads its recipe's
completeness again and raises a plain retryable `SoftError` (`<recipe> facets
invalidated while writing`) when it is gone. An invalidation that commits
after the write but before the job is marked done finds this job still
`running`, so its re-enqueue collapses into the dedupe key and would be lost;
the re-read turns it into a retry of the same job. The gap left between that
read and `jobs.complete` is a few statements wide, and the startup sweep
(split) or `extract all` (extract) make it good.

Facet means: after a write, `refresh_facet_stats` for the recipes written runs
when no job of the same type is still queued, or when the live vector count of
the recipe's first kind is at least `max(25, n/10)` away from the stored `n`.
A sweep therefore refreshes every few dozen pictures and once at the end, and a
single upload refreshes at once, as ingest does for the description.

`tests/integration/fake_worker.py` sets the `extractor=` keyword default of
both handlers to a fake. Its fake analysis has one label, so its splits wait
for the extract job and take that recipe's bodies.

### Invalidation

| Where | What changes | Facets |
|---|---|---|
| `ingest._write_text` (sidecar or model analysis) | analysis | split rows and split vectors deleted; split job if live |
| `admin_jobs._apply_described` (regen, redescribe all, after a crop) | analysis | same |
| `admin_pictures.edit_picture` (owner analysis) | analysis | same |
| `admin_jobs._crop_locked`, only when `pub_sha` changes | pixels | `image_extracts`, extract rows and extract vectors deleted; extract job if live. Split rows with a model set (the fallback call's reading of the old pixels) and the split vectors go too, with a split job if live; split rows cut from the analysis (model null) stay |

All in the transaction that changes the text or the image, so no facet outlives
what it was read from. Each of those transactions marks the graph dirty: the
three text writers already did; the crop now marks first, before the image
update, the order the tick takes. A re-describe that leaves the analysis as it
was touches nothing, and a re-describe never touches the extract recipe. A
picture that is not live yet gets no job; go-live queues both. Delete cascades.

### Admin (`penish/api/admin_mycelium.py`)

`GET` (and every POST) gains:

```
facets: {active: 'split'|'extract',
         counts: {split: int, extract: int, live: int},   store.facet_counts
         queued: {split: int, extract: int},              queued or running jobs
         cap: {used_today: int, daily_calls: int}}        runtime.calls_today, mycelium.daily_calls
```

`queued` is an addition to the pinned shape, so `extract all` shows that it did
something.

| Path | Body | Does | Errors |
|---|---|---|---|
| `/extract_all` | none | `facet_jobs.enqueue_unextracted`: one `mycelium_extract` per live picture without the full extract recipe, `run_after = now + 30 s x rank`, deduped; jobs over the cap wait for midnight | |
| `/facet_source` | `{recipe}` | `store.set_facet_recipe`: sets `facet_recipe` and marks dirty; the active recipe again is a no-op | 400 `not every live picture has <recipe> facets`, 400 `unknown recipe <x>` |
| `/genesis` | `{layers?: string[]}` | absent or every active layer: the full genesis as before; only facet layers: payload `{layers}` in layer order for the scoped replay | 400 from `genesis.scope` (`unknown layer x`, `no layers`, `whole replays with every layer`), 409 `running`, 409 `public` for a full one only |

A second genesis press while one waits or runs still collapses into it,
whatever layers either named; the status line shows the job that will run.

### Web (`/admin/mycelium`)

A `facets` block above `ticks`: `source split`, `split 140 of 142`,
`extract 12 of 142`, `n queued` when jobs wait, `37 of 400 calls today` (accent
colour at the cap). Links: `use extract` (or `use split`), disabled until every
live picture has that recipe; `extract all`, disabled when nothing lacks it;
`genesis facets`, a replay of the four facet layers, open while stopped and no
genesis waits, public or not (the server refuses a layer with galleries).

## Increment 4

Fruiting and death: unpublish, galleries, tombstones. Three parts: the
galleries API and projection, the reversible unpublish, and the public pages.

### Galleries

The public side of fruiting and death: the gallery list, one gallery (the same
page is the tombstone), the event log, `galleries` on the image detail and the
`/g/<n>` preview tags. Routes in `penish/api/routes_galleries.py`; projection
changes in `store.apply_colonies`; `migrations/010_galleries.sql` (009 is the
`unpublished` status value, alone in its file).

#### When addresses are public

`mycelium_state.public_since` is set by the first live tick (`tick.run_one`)
that commits with a serial anywhere in the colony state, whether that tick
fruited it or genesis left it; the value is the tick's clock (the database
clock in production). Genesis runs stopped and never sets it, so it can be run
again until then. Every public read below checks it first (`store.galleries_public`):
before it is set the list and the log are empty, a gallery is `not here`, the
image detail lists no galleries and `/g/<n>` is a 404. After it, a full genesis
refuses (`public`, as before) and reset keeps tombstones. The admin payload
carries `genesis_refused: "public" | "running" | null`, the reason a full
genesis would be refused now.

#### Tables (`migrations/010_galleries.sql`)

| Table | Adds | Notes |
|---|---|---|
| `colonies` | `peak_members jsonb`, `peak_tick bigint` | `[{id, slug}]`, the members at the largest end-of-tick size while living, earliest tick on a tie. No foreign key: a deleted picture is still named by its slug |
| `colony_ticks` | `total_strength double precision` | Sum of the latched strengths inside, beside the mean. Null on rows written before 010 (not recorded) |
| `colonies` | index on `parent_id` | Children are read by parent |

#### Projection (`store.apply_colonies`)

For every alive or fading colony in a stepped layer, after the colony rows are
written:

- **Cover**: the member with the highest summed strength to other members over
  the tick's latched edges of its layer, ties to the lowest id
  (`store.cover_of`). Written when it changes. Never touched once the gallery
  is dead, so a tombstone keeps the face it had at death. Forming colonies have
  no cover.
- **Peak**: when the colony has just become living or ends the tick with more
  members than the tick before, the members are written to `peak_members` if
  that is more than the stored snapshot holds. Exact without a read.
- **Strength**: `colony_ticks.total_strength` beside `mean_strength`.

#### Words

State `alive` is `living`, `fading` is `fading`, `dead` is `dead`; forming and
failed colonies have no address and never appear. Layer `whole` is `whole`,
`facet:<name>` is `<name>`, `need` is `need`. An untitled gallery is
`gallery <serial>`. A picture is live when `status = 'live'`; anything else
(unpublished, hidden, deleted) is treated as never having been a member: not
listed, not counted, no cover, its joins and leaves left out of every log.

#### `GET /api/galleries?state=living|fading|dead|all`

Default `all`; anything else is 400 `bad state`. A bare list, living first
(newest fruiting first), then fading (newest fruiting first), then dead (most
recent death first), serial descending on ties:

```
[{serial, title, state, layer, size, fruited_at: iso|null, died_at: iso|null,
  cause: str|null, cover: grid item|null, age_days: int}]
```

`size` is live current members for living and fading, the peak for dead.
`age_days` is whole days from fruiting to now, or to death. `cover` is the
grid item shape (`slug, width, height, blurhash, palette, variants,
overgrowth`), null when the cover is not live. Times are the scheduled times of
the ticks (`mycelium_ticks`), simulated ones included; nothing says which were
simulated.

#### `GET /api/galleries/{serial}`

`serial` must be canonical (digits, no leading zero, at most nine digits) and
name a living, fading or dead gallery; otherwise 404 `not here`.

```
{serial, title, state, layer, size, peak,
 formed_at, fruited_at, faded_at (fading only), died_at, cause, age_days,
 cover: grid item|null,
 parent: {serial, title}|null, absorbed_by: {serial, title}|null,
 children: [{serial, title}],                       colonies it parented that have a serial
 members: [grid item + caption: str|null, founder: bool, others: [{serial, title}]],
 former_members: [grid item + caption + live: true | {slug, live: false}],
 edges: [{a: slug, b: slug, strength}],
 series: [{tick, size, mean_strength, total_strength: float|null}],
 events: [{at, kind, slug|null, other: {serial, title}|null}],
 shares: [{serial, title, n}]}
```

- `members`: live current members, founders first, then by joining tick, then
  id. Empty for a dead gallery. `others` are the other alive or fading
  galleries (any layer) the picture is in now, by serial.
- `former_members`: dead galleries only, the peak snapshot in id order; a live
  picture as an item to link, anything else by the slug it had.
- `edges`: undirected, the stronger direction, this gallery's layer, between
  live current members, strength at least `hypha_visible`, by id pair.
- `series`: every `colony_ticks` row from forming on, thinned evenly to at most
  288 points and always ending on the last.
- `events`: newest first; kinds `formed, fruited, joined, left, faded, revived`
  and the deaths `depopulated, absorbed, schism, drifted, starved, reset`.
  `failed` never reaches a colony with a serial. `other` is the parent (formed),
  the survivor (absorbed) or the child (drifted), when that colony has a serial.
- `shares`: other alive or fading galleries holding any of the current
  members, `n` of them, by `n` desc then serial.

#### `GET /api/mycelium/log?before=<id>`

The public log standing in for the brief's feed: fruitings, joins, leaves and
deaths of galleries with a serial, newest first by (tick, id) since a layer
replay can write old ticks after new ones; 50 a page.

```
{events: [{id, at, kind: fruited|joined|left|died, cause: str|null,
           gallery: {serial, title}, slug: str|null, other: {serial, title}|null}],
 next: id|null}
```

`next` is the `before` of the following page, null on the last. An unknown
`before` gives an empty page.

#### Image detail

`GET /api/images/{slug}` gains `galleries: [{serial, title, state, current}]`:
galleries the picture is in now (`current`, alive or fading), living then
fading, then the galleries it was in, newest serial first. A stint counts only
if it lasted past fruiting.

#### `/g/<n>` (`index.py`)

A path `g/` followed by digits only. A canonical serial of a public gallery is
200 with `og:title` the title, `og:description` `12 pictures` (`1 picture`)
while living or fading and `died today`, `died 1 day ago`, `died 3 days ago`
once dead, and the cover's largest webp as `og:image` with its size. Anything
else made of digits (unknown, `0`, a leading zero, ten digits) is the shell
with 404, GET and HEAD alike. `g/abc`, `g/1/x` and the like fall through as
before.

### Unpublish

A reversible status for a published picture (B9). `/media/pub/` serves by
filename with no database lookup, so the status alone hides nothing: the files
move too. Code in `penish/worker/unpublish.py`; the api only enqueues.

#### Status (`migrations/009_unpublished.sql`)

`image_status` gains `unpublished`, alone in its file and without
`transaction:false` (a new enum value cannot be used in the transaction that
adds it; tests/conftest.py runs each file's up section as one statement batch).
Down rebuilds the type without the value and turns unpublished pictures into
`failed`, never `live`, since their files are still held. Nothing else goes in
this file.

#### Served ladders (`migrations/011_served_media.sql`)

`served_media (image_id -> images on delete cascade, pub_sha, width,
created_at)`, primary key `(image_id, pub_sha)`: every derivative ladder a
picture's page served, for the purge list. A row is written (`on conflict do
nothing`, via `ingest.record_served`) in the unpublish job's status-flip
transaction from the row it flips, and whenever a live picture's pub_sha is
replaced: a crop in `admin_jobs._crop_locked` (after the dirty mark, in the
same transaction as the pub_sha update) and a heal in `ingest._rebuild`. Rows
outlive a republish, as the Cloudflare copies do. The up section backfills
pictures already unpublished from their current ladder. In the conftest
truncate list.

#### Files

`Settings.media_held` is `<data>/media/held`, beside `pub` so a move is a
rename (a copy and rename across filesystems, should anyone mount them apart).
Nothing serves it and the api container does not mount it. File names are the
pub names, `<pub_sha>-<w>.<avif|webp>`.

One rule, `unpublish.settle(conn, settings, pub_sha)`, decides where a pub_sha's
files live, from every `images` row with that pub_sha:

| Rows | Files |
|---|---|
| any whose status is not `unpublished` (live, or still in ingest, whose gate looks in pub) | `pub` (held copies move back) |
| all `unpublished` | `held` (pub copies move out) |
| none | removed from both |

It runs under an advisory lock on `pub:<pub_sha>`, taken inside a sha lock or
alone and never two at once, so two pictures sharing a pub_sha (identical
crops) under different sha locks cannot interleave a republish with an
unpublish. Each call is idempotent, which is what makes every caller crash safe.

#### Jobs

Registered in `admin_jobs.HANDLERS`. `unpublish` and `republish` take payload
`{image_id}` and the per-sha advisory lock ingest, crop and delete take.

| Job | Dedupe key | Does |
|---|---|---|
| `unpublish` | none (see the admin API) | live: one transaction marks the graph dirty (state row first), sets `status = 'unpublished'`; then `settle` moves the files to held unless another shown picture shares the pub_sha. Status before files: every public route reads the status, so the picture is gone the moment it commits, and a crash before the move leaves only hash-named files a rerun moves. Already unpublished: just `settle` (a rerun finishes a partial move) |
| `republish` | none | checks the webp ladder for `(pub_sha, width)` exists in pub or held, else `HardError("media missing; drop the file again")` before anything moves; moves held files back to pub; then one transaction marks dirty, sets `status = 'live'`, resets the `picture_attention` clock as go-live does (views kept), and queues `mycelium_extract`, `mycelium_split` (each returns at once when complete) and `categorize` when the picture has no category. Files before status, so the page never goes live without its pictures. `published_at` and the slug are kept |
| `settle_media` | `settle_media:<pub_sha>` | payload `{pub_sha}` (64 hex, else `HardError`); just `settle`. Enqueued by the api's delete, which cannot reach the holding dir |

Both directions then refresh corpus stats for every `(kind, model)` the picture
has a vector in: `refresh_corpus_stats('description', model)` (which carries that
model's facet kinds and marks dirty last), `image`, and any facet kind under a
model with no description vector. A picture that is neither live nor
unpublished, or is gone, is a quiet no-op. A job skips itself when a newer
`unpublish` or `republish` of the same picture exists in any status but
`failed`, so the newest press wins even when two workers hold one of each.

#### Guards against an accidental republish

| Where | Guard |
|---|---|
| `ingest._image_row` | a re-drop flips only rows that are neither live nor unpublished to `processing` |
| `ingest._set_status` | never touches a live or unpublished row (so neither `pending_enrichment` nor `failed` puts it back in line) |
| `ingest._go_live` | `where status not in ('live', 'unpublished')` |
| `ingest._ingest_locked` | an unpublished picture is treated as published: no go-live; an owner caption still renames it. Media heal and the gate count a file present in pub or held, and missing derivatives (heal and rebuild paths alike) are written to held, never pub |
| `admin_jobs._crop_locked` | an unpublished picture's new ladder is written to held; afterwards `settle` runs for the new pub_sha and, if it changed, the old one (this replaces the old "remove unless shared": unused files go, files left only to unpublished pictures are held) |
| regen, re-describe, re-embed, categorize | never touch status; `redescribe_all` stays live only |
| `mycelium_split`, `mycelium_extract` | skip any picture that is not live (unchanged); republish queues them again |
| the tick | treats a non-live member as gone and rebuilds on the dirty mark |

#### Public

Every public read already selects `status = 'live'`, so an unpublished picture
is `not here` everywhere with no route change: `/api/images` (newest and
computed sorts, tag and category scopes, counts), the detail by slug or alias,
`similar`, `dissimilar`, `hyphae`, `/api/search`, `/api/tags`,
`/api/categories`, `/api/stats` (`pictures`), views, clicks and votes, `/i/<slug>`
(shell with 404, GET and HEAD) and the galleries (members are live only). Its
`/media/pub/` URLs 404 once the files are held. `tests/test_unpublish.py` holds
each of these.

#### Admin API (`penish/api/admin_pictures.py`)

| Path | Does | Errors |
|---|---|---|
| `POST /{id}/unpublish` | under a transaction advisory lock on `press:<id>`: deletes a queued `republish` of this picture; if the newest queued or running `unpublish`/`republish` of it is an `unpublish`, returns `{job_id: null, queued: false}` (that job is newest, so it acts); else inserts `unpublish` with no dedupe key and returns `{job_id, queued: true}` | 404, 409 `not live` unless live or unpublished |
| `POST /{id}/republish` | the mirror image | same |

- `GET ""` accepts `status=unpublished`; an unpublished item's `thumb` is
  `/api/admin/pictures/{id}/original`.
- `GET /{id}` gains `purge: [url] | null`, set only while unpublished: the page
  under the slug and every alias (`<base>/i/<slug>`), then every ladder in
  `served_media` for the picture, oldest first
  (`<base>/media/pub/<pub_sha>-<w>.<avif|webp>`, the ingest ladder for the
  recorded width), each left out while another shown picture still serves the
  same files. Never built from the current pub_sha: a crop or heal while down
  renames the files to URLs that were never served. `<base>` is the request's
  base URL, as `og:image` uses.
- `DELETE /{id}` of an unpublished picture, or of one whose pub_sha is still
  shared, also enqueues `settle_media` in its transaction.

#### Web (`/admin/pictures`)

The status filter gains `unpublished`. The editor shows an unpublished picture
from its original, hides `view`, and has `unpublish` or `republish` beside
`crop` (disabled while either job waits or runs; the page polls while a job is
active). While unpublished it lists the purge URLs under `purge`, one per line,
selected whole on click.

### Web

The public pages for galleries. Types for every payload above are in
`web/src/lib/api.ts` (`GalleryCard`, `Gallery`, `GalleryMember`,
`FormerMember`, `GalleryEdge`, `GalleryPoint`, `GalleryEvent`, `GalleryShare`,
`LogEvent`, `MyceliumLog`, `ImageGallery`, and `genesis_refused` on the admin
`Mycelium`), with `getGalleries`, `getGallery` (null on 404) and
`getMyceliumLog`. Words and drawing helpers are in `web/src/lib/galleries.ts`.

#### Nav

`galleries` sits between `categories` and `tags`, current on `/galleries` and
under `/g/`. The nav now wraps, right-aligned, when five entries do not fit
beside the mark on a phone.

#### `/galleries`

`N galleries` (`1 gallery`, `0 galleries` when there are none or before
anything is public), then up to three sections in the API's order: `living`,
`fading` and `departed`, each shown only when it has a gallery in it. A row is a
4rem square cover (the smallest webp of at least 160w, with its overgrowth) or
the palette colour without one, the title, and one muted line:
`8 pictures  age 5 days  whole` for living and fading, `died 2026-11-08  schism
5 pictures  whole` for dead (size is the peak). Under the sections the public
log, 50 a page with `more`: `<time> <gallery> <words> <picture or gallery>`.

#### `/g/<n>`

`+page.ts` loads `/api/galleries/<n>`; a 404 reads `not here`. Same-route
navigation scrolls to the top.

- Living or fading: the title, then `living  whole  8 pictures  age 5 days`,
  then `fading since <date>`, `from <parent>`, `children ...` and one
  `shares 2 with gallery 12` line per share, each only when it applies.
- Dead (the tombstone): the title, `dead  whole`, then rows `formed`,
  `fruited`, `died` (dates), `lifespan` (`age_days`, fruiting to death),
  `cause`, `peak` (`N pictures`), `absorbed by`, `from`, `children`. Below,
  `at peak`: the live peak members as tiles with captions, then the slugs of
  the ones no longer live as plain text.
- Two sparklines, `strength` (total strength) and `size`, each with its last
  value; points are placed by tick so a gap in the record stays a gap. Ticks
  with a null `total_strength` are skipped; a row with fewer than two points is
  left out.
- Members: the grid's columns (2, 3 from 40rem), `Picture` tiles with the
  caption under each. Dead galleries have no members, only the peak tiles.
- The log, newest first, 40 lines then `all N`. Words: `formed`,
  `formed from <gallery>`, `fruited`, `joined <picture>`, `left <picture>`,
  `faded`, `revived`, and deaths as `died <cause>` (`died absorbed by
  <gallery>`, `died drifted into <gallery>`). A picture is named by its caption
  where the page has one, else by slug. The public log uses the same words.
- Dates are the visitor's local `YYYY-MM-DD`, log times `YYYY-MM-DD HH:MM`.

#### Hyphae between tiles

One canvas over the members grid, `pointer-events: none`, drawn once the tiles
are laid out and again only when the grid changes size (a `ResizeObserver`);
nothing animates. Each edge is a dotted quadratic curve between tile centres,
bowed and jittered by a generator seeded with `a|b`, so a thread keeps its
shape. Strength `s` (clamped to 0..1) sets the dot spacing (10 - 5.5s px), the
radius (0.6 + 1.1s) and the opacity (0.3 + 0.6s); one dot in twelve is a knot
at 1.7 times the radius, and every tile with a thread gets a knot at its
centre. Colours are the overgrowth's pale dot with a dark halo, so threads read
over bright and dark pictures. The seed hash is `hash` from `hyphae.ts`, now
exported.

#### The overlap mark (`Overlap.svelte`)

A cyan ring with a dot in it, on a 1.75rem button: the shape is the cue (no
other mark on the site is a ring) and the colour a second one. It sits at the
end of a member's caption line when `others` is not empty, and after the
caption on the image page when the picture is in two or more living or fading
galleries (there is no "this gallery" on the image page, so one is not an
overlap). The button's name is `also in 2 galleries` (`in 2 galleries` on the
image page). The list (`also in` / `in`, then one link per gallery) hangs below
the ring without moving anything else: shown on hover (hover devices only),
while the ring or a link in it has keyboard focus, or pinned by a tap or press.
A second press, a tap elsewhere, focus leaving the mark, Escape or any
navigation (`afterNavigate`) closes it. The last one matters because `/g/<n>`
reuses its page and keys tiles by slug, so a tap on a link in the list (which
does not move focus on touch Safari) would otherwise arrive with the same
picture's list still pinned open.
`aria-expanded` follows everything but hover. Tiles are inline blocks so an
open list never continues into the next column.

#### Image page

Under the tags: `in` and the galleries the picture is in now, then `was in`
and the ones it was in, plain links in the API's order. Either line is left out
when empty.

## Increment 5

Needs and offers: the `need` layer. Each picture's extract carries a few short
phrases of what it offers and what it needs; a need of A close to an offer of B
grows a directed edge A to B. A need whose edges carry flow moves toward
satisfied, and a satisfied need pulls less, which is the layer's negative
feedback. Phrases are admin-only: no public payload names one.

### Tables (`migrations/012_offers_needs.sql`)

| Table or column | Shape | Notes |
|---|---|---|
| `offers_needs` | `id bigserial` pk, `image_id` (cascade), `role` in (offer, need), `phrase text`, `vec vector`, `model`, `dim`, `recipe` default `extract` (check split, extract), `satisfaction double precision` default 0 in 0..1, `created_at`; unique `(image_id, role, phrase, recipe)`; `vector_dims(vec) = dim`; an offer's satisfaction is always 0 | Indexed on `(role, model, dim)`. In the `tests/conftest.py` truncate list |
| `candidate_edges.need_id`, `offer_id` | `bigint null references offers_needs on delete cascade`, partial indexes | Set on need edges only: the need of `src` the edge serves and the offer of `dst` that answers it. A phrase that goes takes its edges with it |

Down drops both columns, the need layer's candidates and edge state, then the
table.

Writers: the worker's phrase jobs write phrases and vectors; only the tick
writes `satisfaction` (plus `store.wipe_network` and `store.reset`, which set it
back to 0 under the state row). Every write to `offers_needs` other than the
tick's takes the `mycelium_state` row first (`store.mark_dirty`), as every
embedding write does: the tick holds that row while it reads phrases and updates
satisfaction, the cascade into `candidate_edges` touches rows the tick writes,
and a genesis compares nothing about the need layer but holds the row for its
whole run. The mark also tells the next tick to rebuild the need candidates.

### Need edges (`graph.need_edges`)

Pure. Input: the need and offer phrases (`graph.Phrase(id, image, vec)`) of live
pictures under today's embed model and dim, recipe `extract`
(`store.load_phrases`, float32 widened to float64).

- Closeness is the raw cosine of a need vector and an offer vector. Nothing in
  the repo centers phrase vectors, and a mean over needs or offers would move
  one side of the comparison only, so the layer is never centered.
- For each pair (A, B), B != A, the best (need of A, offer of B) stands for the
  pair: highest cosine, ties to the lower need id, then the lower offer id.
- Each A keeps its `candidates` best B, ties to the lower B id. A picture with
  offers and no needs has no outgoing need edge.
- `Edge(layer='need', src=A, dst=B, raw=cosine, affinity, need, offer)`;
  affinity is the quantile of `raw` among all the layer's edges floored at
  `affinity_floor`, exactly as on the other layers. Sorted by (src, dst).
- Fewer than `min_size` pictures holding any phrase: no edges.
- Exact, one picture's needs at a time against every offer, so memory stays at
  needs of one picture times all offers.

`Edge` gains `need: int | None = None` and `offer: int | None = None`, None on
every other layer, so their edges compare and hash as before.

`tick.GRAPH_LAYERS = (*ACTIVE_LAYERS, 'need')`: a rebuild writes all six;
`tick.build_need_graph(store.PhraseInputs, tunables)`. `ACTIVE_LAYERS` stays the
five layers genesis replays. The need tunables are not in `GRAPH_TUNABLES`:
`candidates`, `affinity_floor` and `min_size` already are, and nothing else
changes who may grow toward whom.

### Damping (`engine.damped`, engine step 2)

At step time, an edge with a need sees

```
affinity * (1 - satisfaction[need] * need_damp)
```

with the need's satisfaction as it was at the start of the tick (missing is 0).
The floor is applied before the damping, as pinned, so a satisfied need's edges
can sit below `affinity_floor`. Damping lives in the engine rather than the
stored graph so a change of satisfaction or of `need_damp` needs no rebuild.
Edges without a need are untouched bit for bit: the golden hashes are unchanged.

### Feeding and drift (`engine.feed_needs`, step 7 "feed needs")

After every layer has stepped, for each need given to the tick, in id order:

```
flow = sum over the need's edges (deduped by pair) of this tick's flow, walkers and clicks
if flow > 0: s += need_feed * (1 - s) * min(1, flow)
s -= need_drift * s
clamp 0..1
```

Feed first, then drift. A need on no edge only drifts. An edge naming a need
not given to the tick feeds nothing (there is no row to write). Pure and
ordered, so the same inputs give the same floats.

`engine.run_tick(..., satisfaction=None)` and `tick.step(..., layers=None,
satisfaction=None)` take every need's satisfaction at the start of the tick;
`TickResult.satisfaction` is the result, empty when none was given.

### State hash

`engine.state_hash(states, satisfaction=None)` appends, after the edge state,
one line per need whose satisfaction is not 0: `need <id> <float.hex>`, in id
order. A zero is skipped for the reason an empty layer is: missing means zero,
a new phrase starts there, and a run with no needs hashes exactly as before. The
tick stores `state_hash(states, satisfaction)`; a reload of `edge_state` and
`store.load_satisfaction` hashes the same (double precision round trips).

### The tick (`tick.py`, `store.py`)

Inside the one tick transaction, after the state row is locked:

- On a rebuild, `store.load_phrases` and `build_need_graph`, written with the
  other layers (`replace_candidate_edges` now stores `need_id`, `offer_id`;
  `load_candidate_edges` reads them back). A stored need edge with an endpoint
  that is no longer live makes the graph stale as on any layer.
- `store.load_satisfaction(model=, dim=)`: needs of live pictures under the
  embed model and dim, recipe `extract`. A need outside that set (its picture
  unpublished) keeps its value untouched and is not in the hash until it is back.
- After stepping, `store.write_satisfaction(new, before)` updates only the values
  that changed.
- The need layer steps once it has candidates (`tick.stepping`), like a facet
  layer. Its tick stats add `needs` (count given) and `mean_satisfaction`.

Unpublished and other non-live pictures never hold a need edge: phrases are read
for live pictures only and the engine drops edges whose ends are not live.

### Colonies on the need layer

Nothing new: need edges are directed in the engine (no reverse edge, so a need
edge is fused only when both pictures need what the other offers), and colonies
group latched edges as undirected pairs, strength the larger direction, as on
every layer. Need galleries are `layer = 'need'`, kind word `need`.

### No simulated past

Genesis never replays the need layer. Satisfaction is a history of walkers on
need edges, and phrases are recent while publish dates are not, so a replay
would invent it. A full genesis wipes the need layer with the rest of the
network (`wipe_network` also sets every satisfaction back to 0), replays the
five active layers as before, and at the end writes today's need candidates with
no edge state, so the layer starts from zero on the first live tick and the
graph stays clean. A scoped genesis leaves the need layer alone; `scope(['need'])`
is refused as `unknown layer need`. A reset (full or public) also sets every
satisfaction back to 0, since it clears the edge state it belongs with.

### Admin read (`store.needs_listing`)

`needs_listing(conn, model=, dim=, tunables=None, limit=200)` returns
`[{slug, phrase, satisfaction, pull, edges: [{to, offer, affinity, strength}]}]`:
needs of live pictures, `affinity` damped by the need's current satisfaction
(`engine.damped`), `pull` its sum over the need's edges, `strength` the need
layer's edge state (0 with none), edges to live pictures only, by affinity desc
then slug; needs by pull desc, then slug, then phrase. A satisfied need's pull is
its unsatisfied pull times `1 - need_damp`.

### Phrases (`penish/worker/phrase_jobs.py`)

Offers and needs come from the extract, cleaned by `facet_jobs.clean`
(lowercased, deduped, five at most per role), and are embedded with the local
embedder as documents, with no query prompt, as every other short text is
(facets, haikus): a need is compared with an offer, both are the same kind of
text, so they share one space. One `embed_documents` batch per picture, offers
then needs. Recipe always `extract`.

Writers, each in a transaction that takes the state row first (`mark_dirty`),
then `images`, then extracts and phrases:

1. `mycelium_extract`, with the extract it read them from (a split's fallback
   call too).
2. `mycelium_extract` on a picture whose facets are complete: backfills the
   phrases without a model call and returns.
3. Job `mycelium_phrases` (payload `{image_id}`, dedupe key
   `mycelium_phrases:<id>`, no model call ever): rebuilds them from the stored
   `image_extracts` row. Quiet no-op when the picture is gone or not live, has
   no extract or one that does not parse, or already has current phrases; it
   yields when the extract is deleted or replaced during the embed.

Every writer first checks whether the stored rows already hold exactly these
phrases under today's embed model and dim; if so nothing is written. Otherwise
`replace` deletes the picture's rows whose (role, phrase) is gone (their need
edges cascade) and upserts the rest, so a phrase that comes back keeps its row
id and its satisfaction and a new one starts at 0.

A crop deletes the extract, facets and their vectors but leaves the phrases;
the re-extraction it queues replaces them. Until then the picture's need edges
come from its old reading.

`enqueue_unphrased(pool, settings)` queues a job for every live picture with a
stored extract whose phrases are missing, different or under an older embed
model, spaced one second apart in id order and deduped. It runs at worker start
beside the split sweep and from the admin button below. Pictures with no
extract are `extract all`'s.

### Admin (`penish/api/admin_mycelium.py`)

`GET` (and every POST) gains:

```
phrases: {pictures: int,     live pictures holding any phrase under today's embed model and dim
                             and a stored extract
          extracted: int,    live pictures with a stored extract
          needs: int, offers: int,
          queued: int}       mycelium_phrases jobs queued or running
needs: [{slug, phrase, satisfaction, pull,
         edges: [{to, offer, affinity, strength}]}]    store.needs_listing, at most 200
```

`pictures` counts phrases taken from a stored extract, so it never exceeds
`extracted`: a cropped picture keeps its phrases but drops out of both counts
until its re-extraction lands. `needs` and `offers` still count its phrases,
since its need edges still stand.

`needs` is `store.needs_listing` with today's embed model and dim and the
tunables the payload shows, so `affinity` and `pull` are what the walkers see
now. Floats are sent unrounded. This payload is the only place a need or offer
phrase is ever sent.

| Path | Body | Does | Errors |
|---|---|---|---|
| `/embed_phrases` | none | `phrase_jobs.enqueue_unphrased`: one `mycelium_phrases` per live picture whose stored extract's phrases are not current; no model call | |

### Admin web (`/admin/mycelium`)

Under the facet links, a line `phrased 120 of 130 extracted`, `340 needs`,
`310 offers`, `n queued` while jobs wait, and the link `embed phrases`. At the
bottom, `needs`: a plain table of picture (a link), need, `satisfied`, `pull`
(three decimals) and `offers`, one line per edge: the picture it reaches (a
link), the offer that answers, then its damped affinity and strength, dim.
`0 needs` when empty. Types `Need`, `NeedEdge`, `Phrases` in
`web/src/lib/api.ts`; `MyceliumAction` gains `embed_phrases`.

### Public

Need edges are a layer like any other in public, and never carry a phrase.

- `hyphae` on the image detail reads `edge_state` only, so a need edge appears
  as `layer: 'need'` with its strength and nothing else.
- Need galleries list and show `layer: 'need'` (`store.public_kind`); their
  `edges` are the need layer's, undirected, the stronger direction.
- The reaching strip already draws `need` as its own kind (`Reach.svelte`: dots
  of radius 1.7 every 8, against whole at 1.1 every 4.5).
- Gallery hyphae (`galleries.drawThreads(canvas, nodes, edges, layer)`, the
  gallery's kind word): on a need gallery every dot is 1.55 times the radius and
  1.8 times the spacing of a whole thread, the reaching strip's ratios, so a
  need gallery's threads read coarser and sparser. Colour and opacity are as on
  every gallery. Facet galleries keep the whole style: a gallery page only ever
  shows its own layer.

`tests/test_public_payloads_no_phrases.py` seeds pictures with distinctive
phrases (in `offers_needs` and in their raw extract, one with characters HTML
and JSON escape), runs real ticks to grow need edges, makes some visible, adds a
living and a dead need gallery with events and sparkline rows, and opens the
public gate. It then takes every GET route from `app.routes` outside
`/api/admin` (failing on a path parameter it cannot fill), fills `slug`,
`serial`, `name` and the catch-all's `path` from the seeded rows (aliases,
unknowns, `i/<slug>`, `g/<n>` included), calls each bare and once per known
query value, and scans every JSON body (keys too), the HTML shell (each `meta`
content, the inline haiku, the unescaped text) and every header. No phrase or
phrase word may appear; every route must answer 200 at least once; search's
echo of the visitor's own `q` is the one thing left out. As a control, the same
scan of the admin payload must find them.

## Increment 6

Naming and the map.

### Naming

The clerk: a title and a haiku for a fruited gallery, in one call, only while the
switch is on. Outside the tick, which never calls a model. Code in
`penish/worker/name_jobs.py`; the prompt is `penish/prompts/mycelium_name.md`.

#### Tables (`migrations/013_naming.sql`)

| Table or column | Shape | Notes |
|---|---|---|
| `haikus.colony_id` | `bigint null references colonies on delete set null`, partial index `haikus_colony_idx` | Set on a gallery's haiku, null on the deck and on picture haikus |
| `haikus_text_uniq` | now `where colony_id is null` | The deck dedupes on its text as before |
| `haikus_colony_text_uniq` | `(colony_id, lower(l1 \n l2 \n l3)) where colony_id is not null` | A gallery haiku dedupes within its gallery only, so it never collides with the deck |
| `mycelium_state.naming` | `boolean not null default false` | The switch |
| `colony_events.kind` | adds `named`, partial index on `scheduled_at where kind = 'named'` | One per title and haiku written. Admin event list only: the public log and the gallery page list their own kinds |

Down deletes gallery haikus and `named` events, restores the deck index and the
kind check, then drops both columns.

#### Where a gallery haiku shows

Only on its gallery. Every haikus query was checked:

- `/api/haiku` and the home shell (`routes_meta.random_haiku`), examples for
  picture haikus and naming (`haiku.load_examples`), the growth job's examples
  and dedupe pool (`grow.load_pool`): `colony_id is null`.
- Picture haikus (ingest, admin regen) are written with `image_id` and no
  `colony_id`; no public route reads a haiku by picture.
- `/api/admin/haikus` items gain `gallery: serial|null`; the haikus page links it
  as `gallery <n>`.
- `on delete set null` would hand a gallery haiku to the deck, so
  `store.wipe_network` deletes gallery haikus before the colonies and
  `store.wipe_layers` deletes those of its colonies (none ever had a serial, so
  none was named). `tests/conftest.py` deletes `colonies` instead of truncating
  it, since `haikus` references it.

#### Tunables

| Name | Default | Bounds | Meaning |
|---|---|---|---|
| `name_per_day` | 3 | 0..50 | galleries named per UTC day while naming is on |
| `name_batch` | 1 | 1..10 | naming jobs queued at once |

Neither touches the graph. The daily call cap (`mycelium.daily_calls`) applies
as well.

#### The scheduler (`name_jobs.schedule`, `run_loop`)

A worker task beside the tick wakes every 60 s; the admin's switch also runs it
once when turned on. A plain read of the switch first: off, nothing (and no
lock). Then one transaction: the state row `for update` (the lock order), the
switch and `public_since` read again, and

```
waiting = mycelium_name jobs queued or running (regenerates included)
room = min(name_per_day - named today - waiting, name_batch - waiting)
```

`named today` counts `named` events since UTC midnight. Up to `room` jobs are
inserted for colonies with a serial, state alive or fading, `title_source` null,
at least one current member that is live and has a caption, no naming job
queued or running, and no failed one in the last day; oldest `fruited_tick`
first, then serial. Payload `{colony_id}`, dedupe key `mycelium_name:<colony id>`.

Nothing is named before galleries are public: until then every serial comes
from a genesis that may be run again.

#### The job (`mycelium_name`)

Payload `{colony_id, regenerate?: true}`.

1. Switch off: done, no call. Colony gone or without a serial, or (automatic)
   no longer living or already titled: done. A regenerate takes living, fading
   and dead galleries.
2. Inputs, live pictures only: the current members (`left_tick` null) or, for a
   dead gallery, `peak_members`. Their captions (at most 24, by id), the tags
   two or more of them hold (at most 12, by count then tag), and their need
   phrases under today's embed model and dim, recipe `extract`, with
   satisfaction at least 0.05, which is what "being fed" means (at most 8, most
   satisfied first). No caption: done, no call.
3. One call: `name.model`; system is `name.prompt` with `{examples}` filled by six
   random deck haikus; the user message lists captions, shared tags and needs
   being fed. `output_config` is the strict schema `{title: string, haiku:
   [string]}`, no other keys. Before each attempt (the retry included) the
   switch is read again (off: the job ends quietly, no call) and
   `providers.check_daily_cap` runs; usage recorded as `name`, two attempts,
   then a retryable failure.
4. Checks: `text.no_em_dash`, whitespace collapsed, outer quotes and a trailing
   period dropped; the title lowercased, 1 to 60 characters, at most 8 words;
   exactly three non-empty haiku lines of at most 60 (`haiku.MAX_LINE`). An answer
   that contains a need phrase (whole words, any case) is refused, since the
   title and haiku are public and phrases never are.
5. Write, one transaction: the state row `for update`, then the colony row. The
   switch is read again (off: the answer is dropped) and so is the colony (an
   automatic job whose gallery the owner titled meanwhile: dropped). An owner
   title is kept, though a regenerate still files the haiku; otherwise `title`
   and `title_source = 'model'`. The haiku is inserted with `source = 'model'`,
   `colony_id`, no `image_id`; the same three lines for the same gallery
   reactivate the old row instead. Then a `named` event (tick `last_tick` or 0,
   `scheduled_at` now, not simulated).

A dead gallery keeps whatever name it had unless regenerated.

#### Admin API (`penish/api/admin_mycelium.py`)

`GET` (and every POST) gains

```
naming: {on, named_today, queued,
         galleries: [{serial, state, layer, title|null, title_source: model|owner|null,
                      haiku: [l1, l2, l3]|null, queued: bool}]}
```

living and fading first, then dead, serial descending, at most 300. `haiku` is
the newest active one.

| Path | Body | Does | Errors |
|---|---|---|---|
| `/naming` | `{on: bool}` | sets the switch; on also schedules at once | 422 for a non-bool |
| `/galleries/{serial}/title` | `{title}` | state row, then colony row. Cleaned as a model title but case kept. Empty (after trimming) sets title and source back to null: `gallery <n>`, and the clerk may name it again. Otherwise `title_source = 'owner'`, which nothing automatic overwrites | 404 `not here`, 400 `title too long` past 60 |
| `/galleries/{serial}/regenerate` | none | queues `{colony_id, regenerate: true}` for a living, fading or dead gallery; a job already queued for it becomes a regenerate. A running one is refused: its handler has read its payload, so a merge would be lost. Not held to `name_per_day`, but counts toward it and the batch, and the daily cap applies | 400 `naming is off`, 404 `not here`, 409 `already naming` |

The name model is set on the models page (`name` purpose, Increment 3).

#### Public

- A title is `title` or `gallery <serial>`, as before, everywhere.
- `GET /api/galleries/{serial}` gains `haiku: {id, lines}|null`, the newest active
  haiku filed for it. Nothing else carries it: not the list, the log, the image
  detail or the `/g/<n>` tags.
- `/g/<n>` shows it below the state lines with `FungalHaiku` (`tag="p"`, keyed
  by id), the one flourish. `FungalHaiku` gains `tag: 'h1' | 'p'`, default `h1`.

#### Admin web

`/admin/mycelium`, under the phrases: `naming`, `on` or `off`, `n named today`,
`n queued`, buttons `on` and `off`, then a row per gallery: its serial (a link),
a title field (placeholder `gallery <n>`), `save`, `regenerate` (disabled while
off or queued), state, layer, source, `queued`, and the haiku dim beneath.
`0 galleries` when empty. Types `Naming`, `NamingGallery`; `setNaming`,
`setGalleryTitle`, `regenerateName` in `web/src/lib/api.ts`.

#### Tests

`tests/test_mycelium_naming.py`: answer checks (em dash, quotes, bounds, lines,
need phrases), the prompt, inputs (live only, shared tags, fed needs), one call
with a FakeClient and its retry, the daily cap, the switch gating the scheduler,
the job, a regenerate and a write after the call, rate per day and batch, the
owner title against regenerates and a call in flight, refusals, the haiku stored
with `colony_id` and absent from the deck, examples, growth pool and home, and a
dead gallery named from its peak. Also: the switch going off while the first call
is out stops the retry (one call, nothing written); an answer repeating a fed
need is refused through the real call path (both leak: the job fails and writes
nothing; only the first: the retry is stored); a gallery whose job failed within
a day is not queued, one that failed two days ago is; a regenerate joins a
queued job (same row, `regenerate` true) and is refused with 409 `already
naming` while one is running. `tests/conftest.py` sets the seed deck active
again before each test, since a test here switches it off.

### Map

The whole network on one page, `/mycelium`, drawn static from one payload.

#### Payload (`penish/api/routes_mycelium.py`)

`GET /api/mycelium`, public, no parameters:

    {graph_version, tick,
     nodes:     [{slug, x, y, overgrowth, galleries: [serial]}],
     edges:     [{a, b, layer, strength}],
     galleries: [{serial, title, state}]}

- `nodes`: every live picture (`store.live_slugs`, id order), `x` and `y` in
  0..1 from the layout below, `overgrowth` as on the picture page
  (`store.overgrowth_by_id`), `galleries` the serials of the living or fading
  galleries it is a current member of (`left_tick is null`), serial order.
  Unpublished, queued and failed pictures are not nodes, and no edge or
  membership reaches them.
- `edges`: visible hyphae on every layer (`whole`, `facet:<name>`, `need`),
  one per unordered pair and layer, `a`/`b` the slugs of the lower and higher
  id, `strength` the larger of the two directions rounded to 3 places, kept
  only when it reaches `hypha_visible`. Sorted by layer display order
  (`engine.layer_order`), then `a`, `b` by id. Read from `edge_state` only.
- `galleries`: living and fading galleries with a serial, serial order,
  `title` through `store.gallery_title` (`gallery <serial>` until named),
  `state` the public word. Dead galleries have no current members and so no
  shape on the map; they stay on `/galleries`.
- Gate: before `public_since` both `galleries` and every node's `galleries`
  are empty. Nodes and edges show regardless, as hyphae on a picture page do.
- No need or offer phrase, extract, caption or tag is read. The route is
  covered by `tests/test_public_payloads_no_phrases.py`.
- Empty organism: `{graph_version: 0, tick: null, nodes: [], edges: [],
  galleries: []}`.

#### Layout (`penish/mycelium/layout.py`)

Pure numpy, Fruchterman-Reingold in the unit square over the whole layer's
candidate graph (`candidate_edges` where `layer = 'whole'`, both ends live,
undirected, the larger affinity of the two directions as the weight). Every
pair repels, an edge pulls in proportion to its weight, a weak pull toward the
middle keeps a picture with no edges on the map. Start positions are drawn per
picture from `SeedSequence([seed, id])`, so the organism's seed decides the
map and a picture keeps roughly its place when others arrive. Iterations are
`clamp(300_000_000 // n^2, 40, 300)`, repulsion is chunked to 2M pairs at a
time. The result is scaled into 0..1 with its aspect kept and the short side
centred, rounded to 4 places.

Walker strengths are not an input: they change every tick, and the map moves
only when the candidate graph is rebuilt (a new `graph_version`), or when a
picture is published or unpublished between ticks (the live set is an input,
so a picture that left never keeps a place).

`layout.fingerprint(ids, edges, seed)` hashes exactly those inputs (weights by
`float.hex`); layouts are kept in process memory by fingerprint, the last 4,
and computed in a thread. Two processes, or one after a restart, compute the
same positions bit for bit, so no table is needed.

#### Caching

- The whole response body is kept in memory per cache key: `last_tick`,
  `graph_version`, `graph_dirty`, `public`, `seed`, the count and id sum of
  live pictures, an md5 of the living and fading galleries' `serial:state:title`,
  the newest `mycelium_tunables` version and `mycelium_state.updated_at`. Within
  one tick edge strengths and overgrowth stay as they were whatever the tables
  do; a publish, unpublish or delete (which mark the graph dirty and change the
  live set), a reset, wipe, genesis, start or pause (every `store.save_state`
  moves `updated_at`; a public reset deletes edges and levels while the tick,
  graph, live set and an already set dirty flag stay as they were), a title
  written by the owner or the naming job, and a tunables change
  (`hypha_visible`) rebuild it at once. Titles and tunables change outside the
  tick, and while the network is stopped no tick would come to carry them.
- With no tick ahead (stopped, or running with `next_due` already past) the
  body also expires `STOPPED_MAX_AGE` (60 s) after it was built, on the
  process's monotonic clock, so overgrowth moved by neglect and views reaches
  the map without a publish, tunable change or restart. While a tick is
  scheduled ahead the body is kept for the whole tick.
- A miss builds under one `asyncio.Lock` in a repeatable read transaction, so
  a tick committing midway cannot mix two ticks; waiting requests take what it
  built.
- `Cache-Control: public, max-age=<seconds until next_due>`, at most one tick
  (600), 0 when behind schedule, 60 while stopped (no tick will come, but a
  publish still changes the map).

#### Web (`web/src/routes/mycelium/`, `web/src/lib/map.ts`)

- `getMap()` in `lib/api.ts` (`MyceliumMap`, `MapNode`, `MapEdge`,
  `MapGallery`). A failed fetch draws as the empty map.
- One square, `width: 100%`, `max-width: 85vh`, centred: phone and desktop
  widths without horizontal scroll. Count line `<n> pictures`; at none it is
  `0 pictures` and nothing else.
- A canvas draws the edges once per size: dotted quadratic threads bowed and
  jittered by `mulberry32(hash(a|b|layer))`, so a thread keeps its shape across
  visits. Kinds are told apart by dot radius and spacing only, the reaching
  strip's values (whole 1.1 every 4.5, facet 0.6 every 2.4, need 1.7 every 8);
  strength sets opacity. No animation.
- An SVG on top: per gallery a faint round-joined convex hull of its current
  members in `--secondary` (fading fainter), linking to `/g/<serial>`; per
  picture a dot dimmed by overgrowth, linking to `/i/<slug>`.
- Pointers hit the nearest dot, not a stack of hit areas: per-dot hit circles
  overlap on a crowded map and the one drawn last wins, so a tap on a dot would
  open a neighbour. The picture links take no pointer events and stay for the
  keyboard and screen readers; one capture-phase click handler on the svg finds
  the nearest dot within 12px (`map.nearest`) and goes there (meta, ctrl or
  shift opens a new tab), before a hull link or the router sees the click. A
  click from the keyboard (`detail` 0) is left to the focused link. A pointer
  near a dot turns the cursor and colours that dot `--secondary`, as focus does.
  A tap away from every dot falls through to the hull under it.
- `/galleries` shows `map` next to its count; the galleries nav entry lists
  `/mycelium` under `under`, so the map highlights galleries. No new top-level
  entry.

#### Tests (`tests/test_mycelium_map.py`)

Payload shape, max over directions, visibility threshold, layer order, the
gallery gate and memberships; no phrase and no unpublished or queued picture,
including one unpublished between ticks; the body kept within a tick and
renewed on the next, `max-age` from `next_due`, behind and stopped; a public
reset while stopped dropping the kept edges; the kept body expiring after
`STOPPED_MAX_AGE` while stopped and while behind, and not while a tick is
scheduled (an injected clock); a title
change, a cleared title and a `hypha_visible` change reaching the map at once
while the network is stopped; positions
unchanged across ticks and after the layout memory is cleared, changed on a
new graph; the empty payload; the pure layout's determinism, bounds, order
independence, fingerprint, clustering and the 0, 1 and 2 picture cases.
