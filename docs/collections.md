# Collections and the Pansophicon

Spec for a hand-curated, auto-organized collection inside peni.sh. Read `CLAUDE.md` first. The house rules apply throughout: no em dashes (use `--`), comments say why, owner text is never overwritten, and the site does not explain itself.

This spec describes what to build and why. Where it guesses at existing code (table names, job names, form fields), match the repo instead of the spec.

## What it is

The Pansophicon is a subset of peni.sh pictures presented as leaves from a pre-modern codex that is being restored. Its full title is Greek: **Πανσοφικὸν τῶν πάντων** (*Pansophikon ton panton*), abbreviated **ΠΤΠ** or **PTP**. It is believed to have had **7 books of 77 leaves** each. That number is the shape of the thing. It will never be filled. The shape shows in the leaf numbers, not in drawn empty slots: the site never makes anyone scroll past gaps.

Three rules shape the whole design:

1. **Membership is by hand. Placement is automatic.** Tom decides that a picture is a leaf. The machine decides which book it goes in and where. Filing has to cost nothing at upload time, or it won't get done.
2. **Owner placement always wins.** If Tom sets a book or position, it is pinned, and the auto-filer never moves it. This is the existing "owner text wins" rule applied to placement.
3. **Reorganization is visible, not hidden.** Leaf numbers can change when the filer re-sorts. The old position is recorded and shown, the way a codicologist records a revised attribution. A slug never changes.

The collection is general (`collections`) so a second one could exist later. Build only what the Pansophicon needs, but don't hardcode it where a table row would do.

## Data model

New tables (names are suggestions):

**`collections`**
- `slug` (`pansophicon`), `title` (`Πανσοφικὸν τῶν πάντων`), `abbrev` (`ΠΤΠ`), `abbrev_latin` (`PTP`)
- `preamble` text. Owner-written. It is shown on the collection page and never generated.
- `book_count` (7) and `leaves_per_book` (77)
- `published` bool, so the collection can be built and filled before it appears in the nav

**`collection_books`**
- `collection_id`, `number` (1 to 7)
- `title`, nullable and owner-written. When it's null the page shows only the numeral.
- `anchor_text`: an internal description used for filing. It is never shown publicly.
- `anchor_embedding`: `anchor_text` embedded with the same model as picture analyses (see Filing)
- `tag_hints`: an optional text array, a small secondary filing signal

**`collection_leaves`**
- `picture_id` (unique: a picture is in at most one position per collection), `collection_id`, `book_id`, `position` (1 to 77, unique per book)
- `pinned` bool: true when Tom placed it. Auto-filed leaves are false.
- `filed_at`, `updated_at`

**`collection_leaf_history`**
- `picture_id`, `collection_id`, `from_book`, `from_position`, `to_book`, `to_position`, `reason` (`auto`, `resort`, `owner`), `at`
- A row is appended whenever a leaf moves. The public page shows only the most recent prior position.

A picture can also be **marked for a collection but not yet filed** (the job is pending or failed). Represent that however fits: a nullable `book_id`/`position`, or a separate `collection_requests` row. Unfiled pictures never appear on collection pages.

## Getting a picture into the collection

### Admin upload form (`/admin`)

Add a single checkbox: **pansophicon**. If later there's more than one collection, this becomes a short checkbox list driven by the `collections` table. Don't build a "collections" toggle that unhides sub-checkboxes yet.

Below it, optionally, two small fields: **book** (i to vii) and **leaf** (1 to 77). Both are blank by default. If either is filled, the placement is pinned. A book with no leaf pins the book and lets the filer pick the position.

### rsync inbox

Add a sidecar to the existing set: **`<stem>_collection.txt`**. Its contents are one line:

- `pansophicon`: file it automatically
- `pansophicon iii`: pin to book iii, auto position
- `pansophicon iii 14`: pin to book iii, leaf 14

Accept roman or arabic numerals. A malformed sidecar must not fail the picture. The picture still publishes, the error is logged and shown in `/admin/queue`, and the collection request is dropped.

### Existing pictures

On the admin picture page, add the same checkbox plus book/leaf fields, so already-published pictures (the illuminated manuscript, the sampler) can be filed after the fact.

### Never automatic

The model never decides membership. Nothing in describe, categorize or mycelium may add a picture to a collection. Only the owner's checkbox, sidecar or admin action does.

## Filing

### Trigger

When a picture that has a collection request publishes, enqueue a `collection_file` job on the existing `SKIP LOCKED` queue. It runs after embedding, so it waits for publish. It is not on a timer.

### Choosing a book

Work in the same vector space the site already uses for similarity: the analysis embedding, mean-subtracted once the collection is past the threshold the rest of the site uses.

For each book that is not full, compute a **centroid** that blends the written anchor with the real leaves:

```
w_anchor = k / (k + n)          # n = leaves in the book, k = `anchor_k`: 3 by default, 10 here
centroid = normalize(w_anchor * anchor + (1 - w_anchor) * mean(leaf vectors))
```

Why the blend: an empty book has no leaves to match against, so the anchor carries it at first. As the book fills, the leaves take over and the book becomes whatever its pictures actually are, not what the anchor text said. That drift is intended.

Score each book:

```
score = cos(picture, centroid)
      + alpha * tag_overlap(picture.tags, book.tag_hints)     # alpha small, e.g. 0.05
      - beta  * (n / leaves_per_book) ** 2                    # gentle fill pressure, beta e.g. 0.05
```

Pick the highest score. The fill term keeps one book from swallowing everything without overriding a clear match. All constants go in config, so they can be tuned without a deploy.

If every book is full, leave the picture unfiled and log it. That would be a nice problem to have.

### Choosing a position within a book

Positions come from a **nearest-neighbor chain** over the book's leaves:

1. Take the book's leaves (including the new one) and their pairwise cosine distances.
2. Build an open path through them: start from the leaf nearest the book's anchor, then go greedy nearest-neighbor, then do a cheap 2-opt pass. An exact solution is not needed.
3. Map cumulative path length onto slots 1 to 77: `slot_i = 1 + round(cum_i / total * 76)`. Resolve collisions by nudging to the nearest free slot.
4. **Pinned leaves are fixed points.** Run the chain in the segments between them, and keep auto leaves out of pinned slots.

The effect: related leaves sit next to each other, and the gaps fall where the subject jumps. A book with three leaves shows them spread across the 77 squares with real distances between them, which reads as surviving fragments.

### Re-sorting

- **On each filing:** re-run positions for the target book only. Leaves in that book may shift; record every move in history with reason `auto`.
- **Full re-sort:** an admin button, plus an optional weekly scheduled job (off by default). It recomputes book assignment for every unpinned leaf, then positions in every book.
- **Hysteresis on book moves:** during a full re-sort, move a leaf to a different book only if its best score beats its current book's score by a margin (config, e.g. 0.03). Without this, leaves flap between books on every run.
- Positions within a book can churn freely. Book changes should be rare.

## Public site

All labels are plain and lowercase. Greek title text is the exception. No explanations, no "about this collection", no tooltips on empty slots.

### Nav

Add **`collections`** to the header nav, after `galleries`. Show it only when at least one collection is `published`.

### `/collections`

A list with one entry. It shows the full Greek title, the abbreviation, and a count like `14 of 539 leaves`. There is no description and no reason given why there's only one.

### `/collections/pansophicon`

- The title `Πανσοφικὸν τῶν πάντων`, set in the site's normal heading treatment, not the tendril font. The tendril font stays the site's own flourish.
- The `preamble`, as owner text. Seed it with the draft below, but it is Tom's to edit.
- Seven rows, one per book. Each row has a label (`book iii`, plus the owner title if set, plus `3 leaves`) and a compact strip of the filled leaves only, in leaf order, as small thumbnails linking to `/i/<slug>`. Empty slots are not drawn.

Seed preamble (owner can replace):

> Acquired incomplete. Believed to comprise seven books of seventy-seven leaves. No book has been found complete. The books are presented in the order in which they were found, which is believed to be their original order.

### `/collections/pansophicon/<n>`

The book page: a sparse table drawn collapsed. Only filled leaves are shown, in leaf order, each with its leaf number under it, so leaf 2 is followed directly by leaf 19 with nothing drawn between. The grid keeps the book's 7 by 11 as its width: 7 across on a phone, 11 elsewhere. A book with no leaves says `0 leaves`. The API still returns all 77 slots with nulls; the page drops them.

### Picture page `/i/<slug>`

Leaves stay in the main grid, search, similarity strips and mycelium exactly like any other picture. There's no badge in the grid.

On the picture page itself, add one quiet line near the categories:

```
ptp iii, leaf 14
```

If the leaf has moved, add the most recent prior position, smaller:

```
ptp iii, leaf 14 (formerly leaf 22)
ptp iii, leaf 14 (formerly ptp v, leaf 3)
```

The line links to the book page. Use the abbreviation, never "the Pansophicon". That nickname belongs to a different joke (see "Clerk voice").

### Home page

No change. Don't add a collections count to the header.

## API

All read-only, all following existing conventions (User-Agent, cursoring where relevant):

- `GET /api/collections`: list, with counts
- `GET /api/collections/<slug>`: title, abbrev, preamble, and the books with their filled positions
- `GET /api/collections/<slug>/books/<n>`: the 77 slots, nulls included
- `/api/images/<slug>` gains `leaf: {collection, book, position, previous: {book, position} | null}` or `null`

Don't expose `anchor_text`, `anchor_embedding`, `tag_hints` or scores publicly.

## Admin (`/admin/collections`)

- Edit the collection title and preamble, and set `published`
- Per book: edit the title, `anchor_text` (re-embed on save) and `tag_hints`
- Per book: a grid view of positions. From there you can pin, unpin, move a leaf (book plus leaf, which sets `pinned`), or unfile it (removes it from the collection; the picture stays on the site)
- A list of unfiled requests with their errors, and a retry button
- A **re-sort now** button (full re-sort), with a dry-run mode that shows proposed moves before applying them
- A recent history list

## Seed anchors (internal, never public)

The site embeds the **analysis** paragraphs, so write anchors in that register: plain visual description, not titles. Tom can rewrite them. Starting text:

1. **Creatures.** Animals and composite creatures presented one at a time for study: their parts, habits, uses and dangers. Beasts with impossible anatomy, hybrids of fish, bird and mammal, monsters from the edges of the known world, creatures in the margins acting like people. Small human figures sometimes stand beside them for scale.
2. **Plants and stones.** Plants shown whole from root to flower, with seeds and cross-sections. Trees that bear creatures, people or objects as fruit, roots shaped like people, gardens and harvests of impossible crops. Gems, minerals and stones laid out in rows with their virtues.
3. **Instruments and the body.** Devices built to be applied to the body: chairs, frames, balances, tubes, gears and basins with lettered parts. Surgery, bloodletting and cures in progress, remedies and their ingredients, a body opened, flapped or diagrammed for teaching. A patient approaches the apparatus warily while practitioners wait.
4. **The sky.** The heavens and what they foretell. Spheres, rings and orbits, planets with the people they govern, constellations drawn as figures, zodiac men whose body parts are ruled by the signs, winds, maps of seas and coasts. Dreams and their meanings, omens, divination and alphabets of signs.
5. **Transmutation.** The changing of one thing into another. Flasks and furnaces with whole scenes sealed inside them, a crowned king and queen, lions, serpents, suns and moons, sequences of small stages, laboratory work.
6. **Devotion.** What heaven does. Gods, saints and the many-armed one-eyed sky god acting directly: creating, judging and weighing souls, sorting the saved from the damned, working miracles and martyrdoms. Heaven and hell, relics, processions and sacred scenes that belong to no known church.
7. **Man.** What people are and what they do. The human figure shown whole and unashamed as the measure of things, set between beasts below and heaven above. Virtues and vices as persons, the soul and its housing, knowledge carried in the body, conception, pregnancy, birth and the ages of life. The household, the bath, the table, trades and guilds, custom and festival. Law, trial and ordeal, and the prescribed punishment fitted exactly to the offence, carried out calmly by magistrates and executioners while daily life goes on around it.

The line between books vi and vii: what heaven does is vi, and what people do is vii, even in heaven's name. A book is defined by what a picture shows, never by how it was made, so the anchors and tag hints name no medium.

`tag_hints` for each book can come from the obvious words in its anchor. Keep them short.

## Voices

- The **conservator note** is built (see "As built"). It is owner-written, or model-written when blank, and describes the object, never the picture's meaning, as flat fact with no conclusions ("Greek hand, probably fifteenth century. Marginalia in Polish, later."). Its prompt is `penish/prompts/conservator.md`.
- The academic etiquette: everyone calls it "the Pansophicon", and academics believe only they have earned the one-word name. So the conservator says "the Pansophicon" freely. The public pages and the clerk use the full title or PTP.
- When the clerk's generated text refers to the collection, he writes the full title or PTP. About 1 in 20 times he slips and writes "the Pansophicon". Never correct it, never flag it. (Not built: the clerk does not refer to the collection yet.)

## Out of scope

- A narrative history of the collection, or any text that explains, justifies or apologizes for it. Stated facts about the collection (provenance, extent, condition, per-leaf conservation notes) are in scope.
- Any public explanation of how filing works
- Model-decided membership
- Collections as a category or a mycelium layer (they stay independent)
- Drag-and-drop ordering (pin and move by number is enough)

## Tests

- Sidecar parsing: valid forms, roman and arabic numerals, malformed input that doesn't fail publish
- Filing with empty books (anchors only), partially filled books, and a full book excluded
- Pinned leaves are never moved by auto filing or a full re-sort
- Positions are unique per book, stay within 1 to 77, and pinned slots are respected
- Hysteresis: a full re-sort run twice in a row produces no book moves the second time
- History rows are written on every move, and the API's `previous` shows the latest one
- Unpublished collections appear in neither the nav nor the API
- Run `ruff`, `mypy`, `pytest` and `pnpm check` before calling it done

## As built

Where the spec left a choice open, or guessed at the repo, this is what was done.

### Data

- `migrations/017_collections.sql` makes the tables; `018_seed_pansophicon.sql` seeds the
  collection unpublished, with the seven anchors and short `tag_hints`. The seed is its own
  file so tests can put it back between runs.
- Pictures are `image_id`, as everywhere else in the repo.
- `collection_leaves` refers to its book as `(collection_id, book)` with the book's number,
  not a surrogate id, so history rows and leaves speak the same numbers.
- Pinning has two levels: `book_pinned` (the book is the owner's, the leaf is the filer's)
  and `pinned` (both are the owner's; implies `book_pinned`). A book with no leaf sets only
  `book_pinned`.
- The slot uniqueness constraint is deferred, so a re-sort can permute positions inside one
  transaction.
- Unfiled requests are a separate `collection_requests` table holding what the owner asked
  for (book and leaf, each nullable) and the last refusal, if any. Leaves never have a null
  book or position.
- History also records a filing (`from` null) and an unfiling (`to` null), so the admin list
  shows a leaf's whole life. The public `previous` is the latest row with a `from`, and is
  null when that equals where the leaf is now.
- `migrations/024_leaf_history_scores.sql` gives each history row `chosen_by` (`score`,
  `owner`, `judge`) and `scores` (jsonb), set on the row where a leaf's book was decided:
  a filing, an owner move, a re-sort or re-judging that changes its book.
- `migrations/025_collection_judgements.sql` keeps the judge's answers (see "The judge").
- `migrations/026_anchor_embedded_at.sql` gives each book `anchor_embedded_at`, set with
  the vector and cleared with it on an anchor edit. Null with a vector means embedded
  before the time was kept.
- `migrations/019_collection_text.sql` adds the owner text and the note's provenance;
  `020_seed_pansophicon_text.sql` seeds the Pansophicon's text, filling only what is still
  empty. Tests re-apply 018 and 020 between runs.
- `collections` carries owner text nothing generates: `gloss` (under the title), `record`
  (jsonb, an ordered list of `[label, value]` pairs; a value may hold several lines) and
  `vendor_text` (nullable). All three, like the preamble, are edited on `/admin/collections`.
- `collection_leaves.note` is the conservator's note, with `note_source` (`owner` or
  `model`) and `note_model`, as text rows carry theirs. A note always has a source.
- `collection_requests.note` holds an owner note that arrived with the request (the upload
  form or `_note.txt`) until the picture is filed; filing writes it to the leaf.
- The filer's constants are `collections.tuning` (jsonb): `anchor_k` 3 (10 on the Pansophicon since `023`), `tag_alpha` 0.05,
  `fill_beta` 0.05, `book_margin` 0.03, `weekly_resort` false. Absent keys mean the defaults
  in `penish/collection/plan.py`. They are edited on `/admin/collections`, so tuning needs no
  deploy.

### Code

- `penish/collection/numerals.py`: roman numerals and the sidecar line grammar.
- `penish/collection/plan.py`: the filer, pure numpy. Book choice, the hysteresis re-sort and
  the nearest-neighbor chain.
- `penish/collection/store.py`: load, plan and write under a per-collection advisory lock,
  plus requests, owner moves, pins, unfiling and the public leaf lookup.
- `penish/worker/collection_jobs.py`: `collection_file`, `collection_anchor`,
  `collection_resort`, the worker start sweeps and the weekly check.
- `penish/api/routes_collections.py` (public) and `penish/api/admin_collections.py` (admin).
- `penish/worker/conservator.py`: the `collection_note` job, its prompt input and its
  output check. `scripts/conservator_sample.py` runs the same call against live leaves
  without storing anything, for checking the prompt.

### Conservator notes

- Filing queues `collection_note` in the filing's own transaction when the leaf has no
  note. The admin queues it for one leaf (`write note`, which rewrites a model note) or for
  every leaf with none (`write missing notes`). A plain job and an asked-for rewrite have
  separate dedupe keys, so the rewrite never folds into a job that would keep the old note.
- Worker start queues a note for every leaf with none and no note job on record (done or
  failed), the first five minutes out and the rest 30 seconds apart, so uploads, filings and
  judging queued meanwhile are claimed first. The backlog is noted once, and a leaf the
  model keeps refusing is not paid for at every restart.
- Owner wins twice: the job skips a leaf with an owner note, and the model's write is an
  update conditional on `note_source` not being `owner`, so an owner note saved while the
  model ran stands. Clearing an owner note (saving it empty) hands the leaf back.
- The model is `conservator.model` (describe's model unless changed on `/admin/models`)
  and the prompt `conservator.prompt`. It sees the displayed pixels, the collection's
  record, the leaf's book and position, its latest earlier place, and the notes already
  written for other leaves in the same book, in leaf order.
- Its output is refused and asked again (three tries) when it is empty, over 600
  characters, more than four sentences, asks or exclaims, reacts (`unusual`, `curious`,
  `remarkable`, `interestingly`, `appears to depict` and kin), or names what stays unsaid
  (the community and town the record leaves out, Byzantium as a state). Em dashes become
  `--`. The prompt itself never names the unsaid things, so it cannot prime them.
- `_note.txt` is read leniently: undecodable or empty is no note, logged, and never fails
  the picture. A note with no collection line is dropped with a log line.

### Filing

- Vectors are the analysis embeddings under the current model. When the site centers its
  similar lists (`centered_min_images` live pictures), the filer subtracts the same corpus
  mean from leaves, the candidate and the anchors before normalizing.
- Anchors are embedded as documents by the worker (`collection_anchor`), queued at worker
  start for any anchor without a vector under today's model and whenever an anchor's text
  changes. A filing or re-sort embeds any stale anchor first.
- `tag_overlap` is the overlap coefficient between the hints and the picture's tags and
  their words, so `serpent` matches `coiled serpent`. It is capped at 1: one tag can meet
  several hints (`alchemy lab` meets `alchemy` and `lab`).
- When scoring a leaf against its own book during a re-sort, the leaf is left out of that
  book's centroid and fill, so staying put earns no self-similarity.
- The re-sort runs leaf by leaf against everyone else's current book and repeats until a
  pass moves nothing (20 passes at most). A finished re-sort is a fixed point, so running it
  again at once moves no leaf between books.
- Leaves that never move: pinned ones, and ones whose picture has no vector under the
  current model or is not live. The second kind keeps its slot unless an owner pin takes it.
- Positions: the fixed leaves cut the book into segments. Each free leaf goes to the
  segment beside the fixed leaf (or the anchor, for the opening segment) it is nearest, with
  room permitting. The two segments on either side of one fixed leaf are equally near any
  leaf that is nearest it; that tie goes to the segment with more room, so a lone pin at 14
  does not crowd the book into leaves 1 to 13. Inside a segment the chain starts at the left fixed leaf, or at the leaf
  nearest the anchor; greedy nearest neighbor, then 2-opt with both ends held. Cumulative
  length maps onto the segment's slots between its two fixed leaves. Collisions shift
  forward then back, keeping chain order.
- With no pins, one leaf sits at leaf 1, two at 1 and 77, and more spread between.
- A pin onto an auto leaf moves the auto leaf aside (history reason `auto`). A pin onto a
  pinned leaf, a pin into a full book, and a filing when every book is full are refused;
  the reason stays on the request for the admin (`book iii leaf 14 is taken`), and the job
  completes rather than retrying.
- A malformed sidecar, an unknown collection, or a book or leaf outside the collection's
  shape becomes a failed `collection_file` job on `/admin/queue` with
  `collection sidecar: ...`. The picture publishes regardless.
- A request for a picture that is down waits. Going live, republishing, and the worker
  start sweep each queue its filing.
- Which book is decided in one place, `store.choose`: the owner's book when given, else
  the judge's (see "The judge"), else the best score among books with room. `plan.weigh` returns every open book's score in
  its parts (`cos`, `tag`, `fill`, which add up to the total `choose_book` ranks by), the
  best, the runner-up and the margin; the filing keeps them, with the tuning, on its
  history row, also when the owner gave the book.
- `GET /api/admin/collections/<slug>/explain?image_id=N` (readable with the read token):
  the leaf's place and pins, every book's parts now with the leaf left out of its own (as a
  re-sort sees it), the best, the runner-up, the margin and its own book's `lead`, and what
  was kept when it was filed.
- The owner's re-sort is a plan, then exactly that plan. The dry run answers `plan_id` and
  the moves (thumbnail, before, after, and the margin: how far the new book scores above
  the best other). `plan_id` is a hash of the moves and of the state they were planned
  from: shape, tuning, each book's anchor text, hints and vector, and every leaf's place
  and pins. Sent back with the moves, it applies them as they are, or answers 409 `changed`
  when that state is no longer the collection's. The leaves' own vectors are not in it, so
  an upload elsewhere on the site (which moves the corpus mean) does not void a plan.
- A dry run while any anchor has no vector answers 409 `anchors` and queues
  `collection_anchor`, instead of scoring a book blind. The weekly re-sort is still a job
  that embeds the anchors, plans and writes in one go.

### The judge

Vectors file by what a picture looks like, and pictures of one medium look alike whatever
they show (`docs/reports/ptp-filing.md`). So a model reads each new leaf and names its book.

- `penish/collection/judge.py`, prompt `penish/prompts/filer.md`. One text-only call: the
  caption, description and analysis (`image_texts`), and every book of the collection, full
  or not, with its number, title and anchor text. It answers
  `{"book", "runner_up", "reason"}`, books as roman numerals. The prompt files by subject,
  never by medium, technique, style or layout; what heaven does is vi, what people do is
  vii even in heaven's name, and what people do in baths is vii.
- An answer is a judgement only when both books are the collection's and differ, and the
  reason is one line of at most 200 characters (an em dash in it is made `--`). Anything
  else, a refusal, an error or no answer in 60 seconds is a failure, and the filing goes by
  score. Whether the named book has room is checked after the answer: a filing into a full
  book goes by score with `full`, and re-judging holds the leaf with `full`.
- Settings on `/admin/models`: `filer.model` (default `claude-haiku-5-5`, the
  `FILER_MODEL` env var; Haiku did best in the trial and costs least), `filer.prompt`, and
  `filer.judge` (on by default; off, nothing asks: a filing goes by score, the re-judging
  dry run answers 409 `off` and queues nothing, and a `collection_judge` job already queued
  returns without asking). Usage is recorded under `filer`.
- `collection_judgements` keeps every answer by collection, picture and `input_hash`:
  sha256 over the model, the prompt, the three texts and every book's number, title and
  anchor text. A book filling or emptying does not change it. The same question is never
  paid for twice. A failure is kept too, so a dry
  run can say why; a refusal is never asked again, the other failures are asked again the
  next time the question comes up. Change a text, a title, an anchor, the model or the
  prompt and it is a new question.
- Filing: the worker asks the judge (through the cache) before `file_request` takes the
  collection's lock, and hands the answer in; `store.choose` takes an answer, never a
  client, so no model call can run under the lock. A judgement for a book still open is
  `chosen_by='judge'`, and `scores` keeps the weighing with `judge: {model, book,
  runner_up, reason}`. Otherwise the scores choose, and `scores.judge` says why not
  (`{error: "off" | "no text" | "refused" | "invalid" | "timeout" | "error" | "full"}`,
  `full` being a book with no room when the lock is taken), with a log line. The
  slot within the book is chosen as before. An owner's book never asks.
- A leaf whose latest book decision in history is `judge` keeps its book in every vector
  re-sort, the owner's and the weekly one, as a `book_pinned` leaf does; it may still move
  within its book. It is not marked `book_pinned`, which stays the owner's alone. A leaf
  the judge agreed with on re-judging has no new history row, so it stays a scored leaf.
- Re-judging: `POST /api/admin/collections/<slug>/judge`. The api holds no model keys, so
  `{"dry_run": true}` plans from kept judgements only: while any leaf not `book_pinned`
  has none for what it is now, it answers 409 `judging` and queues `collection_judge`, and
  the worker asks for each (four at a time). Then it answers `{plan_id, moves, held}`:
  every leaf whose judged book is not its own, moved there with its slot chosen as the
  filer would (other leaves in that book may shift, as on a filing), each book move with
  the judge's `reason` and `runner_up`. `held` lists what stays: a leaf whose judged book
  is full (`book`, `why: "full"`), and one the judge did not judge (`why` is the failure,
  `no text` or `no vector`). 409 `anchors` as the re-sort's dry run. `{"dry_run": false,
  plan_id, moves}` applies exactly that plan or answers 409 `changed`; the id covers the
  collection's state and the question behind each book move. Applied book moves are
  history rows with `chosen_by='judge'` and the judgement; `last_resort_at` is untouched.
- `/admin/collections` has `judge` beside the dry run (it asks again every few seconds
  while the answer is `judging`) and `apply`, which sends the shown plan back. The moves
  list shows the reason beside a book move; held leaves follow it.
- The admin's `GET .../<slug>` carries, per book, `anchor_state` (`embedded` under
  today's model; `queued`, no usable vector and a `collection_anchor` job queued or
  running; `stale` otherwise), `anchor_embedded_at` and `hint_leaves` (each hint with the
  number of leaves it meets, matched as `tag_overlap` matches); `history`, the newest 20
  rows with `chosen_by`, narrowed by `?leaf=` (image id or slug); and `judge_failures`,
  the failed judgements among the last 20 filings by kind (`off` is not a failure).
- `explain` returns `judged` (the judge chose its book), `filed.scores.judge` for a judged
  filing, and `judgement`: the kept answer for the leaf as it is now, or null.

### Site

- The nav fetches `/api/collections` once and adds `collections` only when the list is not
  empty.
- Empty slots are never drawn on the public site. The collection page shows each book's
  filled leaves as a row of small thumbnails with a count; the book page shows them in a
  7-wide (phone) or 11-wide grid, in leaf order, each labelled with its leaf number. An
  earlier version drew all 77 cells; Tom ruled it out, since a book with 5 leaves meant
  scrolling past 72 empty squares.
- `/admin/collections` still draws all 77 cells per book: there the empty slots are where
  a pin or move can go.
- The picture page line uses `abbrev_latin` lowercased: `ptp iii, leaf 14`, then
  `(formerly leaf 22)` within a book or `(formerly ptp v, leaf 3)` across books.
- Server-rendered titles: the Greek title for `/collections/<slug>`, `ΠΤΠ iii` for a book.
  Unknown or unpublished addresses answer 404.
- The collection page, top to bottom: logo, Greek title, gloss (muted), preamble, the
  record as a definition list (muted lowercase labels, one line per value line; stacked on
  a phone), the vendor's description quoted under `vendor's description`, smaller and
  dimmer, then the books. The vendor's "COMPLETE IN SEVEN BOOKS" against the preamble's
  "No book has been found complete" is left standing.
- `/api/collections/<slug>` carries `gloss`, `record` and `vendor_text`; `/api/images/<slug>`
  `leaf` carries `note` and the collection's `title`. The note's source and model stay
  private. An unpublished collection exposes none of it.
- Leaf picture pages: caption, then the small logo, the leaf line and the conservator note
  (muted, unlabelled), then votes and the clerk's description. The category line is hidden
  on a leaf (one conditional in `i/[slug]/+page.svelte`); the categories stay in the data.
- Logos: 25 decorated objects in `web/src/lib/assets/ptp/`, loaded with a relative
  `import.meta.glob` (a `$lib` pattern matched nothing here). `PtpLogo.svelte` picks one
  in `onMount`, skipping the last one shown (sessionStorage), inside a box sized by height
  before the pick so nothing shifts: 10rem (7rem on a phone) on the collection page, 6rem on
  a book page, 4rem above the leaf line on a leaf page, linking to the collection. Wide
  logos are capped by width as well. No logo anywhere else.
