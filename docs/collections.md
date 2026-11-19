# Collections and the Pansophicon

Spec for a hand-curated, auto-organized collection inside peni.sh. Read `CLAUDE.md` first. The house rules apply throughout: no em dashes (use `--`), comments say why, owner text is never overwritten, and the site does not explain itself.

This spec describes what to build and why. Where it guesses at existing code (table names, job names, form fields), match the repo instead of the spec.

## What it is

The Pansophicon is a subset of peni.sh pictures presented as leaves from a pre-modern codex that is being restored. Its full title is Greek: **Πανσοφικὸν τῶν πάντων** (*Pansophikon ton panton*), abbreviated **ΠΤΠ** or **PTP**. It is believed to have had **7 books of 77 leaves** each. That number is the shape of the thing. It will never be filled, and the empty slots are part of the presentation.

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
w_anchor = k / (k + n)          # n = leaves in the book, k = 3 (config)
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
- Seven rows, one per book. Each row has a label (`book iii`, plus the owner title if set) and a compact strip of 77 cells. Filled cells show a tiny thumbnail or blurhash and link to `/i/<slug>`. Empty cells are a faint outline with no hover text and no link.

Seed preamble (owner can replace):

> Acquired incomplete. Believed to comprise seven books of seventy-seven leaves. No book has been found complete. The books are presented in the order in which they were found, which is believed to be their original order.

### `/collections/pansophicon/<n>`

The book page: a grid of 77 cells in leaf order, larger than the strip. Filled cells show the picture and link through. Empty cells stay empty, and the grid does not collapse around them. On a phone, a 7-wide grid with 11 rows is fine. The emptiness is the point.

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

1. **Creatures.** Engraved or painted plates of single animals and composite creatures shown in profile against a plain ground, sometimes with small figures beside them for scale. Bestiary and natural-history illustration, hand-coloured woodcut, marginal drolleries, beasts drawn with careful anatomy and impossible parts.
2. **Plants and stones.** Botanical plates showing a whole plant from root to flower, seeds and cross-sections, gems and minerals drawn in rows, pseudo-script labels, herbal and lapidary pages with faded washes.
3. **Instruments and the body.** Engraved diagrams of surgical, mechanical or ritual apparatus in exploded view with lettered parts, anatomical figures with flaps and cutaways, patients and practitioners, bathhouses and infirmaries, procedures in progress.
4. **The sky.** Celestial spheres, nested rings and orbits, volvelles, astrolabe plates, star charts, maps of seas and coasts, cosmological diagrams with figures at the margins, gold and lapis on parchment.
5. **Transmutation.** Alchemical emblems, flasks and furnaces with scenes inside them, crowned figures, serpents, suns and moons, sequences of small panels, laboratory interiors.
6. **Devotion.** Icons with gold grounds, saints and figures in registers, relic caskets, liturgical objects, processions, heterodox or unfamiliar sacred scenes, mosaic and fresco fragments.
7. **Dreams and signs.** Dream images paired in grids, emblems and their meanings, divination tables, figures in symbolic scenes, a sleeping person with a vision above, alphabets of signs.

`tag_hints` for each book can come from the obvious words in its anchor. Keep them short.

## Clerk voice (phase 2, not in this build)

Noted so it doesn't get designed out:

- A per-leaf **conservator note** that is owner-written, or model-written when blank. It states layers as flat fact with no conclusions ("Greek hand, probably fifteenth century. Marginalia in Polish, later."). It would get its own prompt in `penish/prompts/`.
- When any generated text refers to the collection, the clerk writes the full title or PTP. About 1 in 20 times he slips and writes "the Pansophicon". Never correct it, never flag it.

Don't build either now. Just leave room for a nullable `note` field on `collection_leaves`.

## Out of scope

- Any public explanation of what the Pansophicon is, where it came from, or how filing works
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
