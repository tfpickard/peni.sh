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
