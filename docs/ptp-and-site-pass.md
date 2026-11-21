# ΠΤΠ and the whole site: fix the intake, measure the filer, add the book of Man, connect the site

Work in plan mode, and report to Tom before building anything past part A.
- Read `CLAUDE.md`, `docs/collections.md` ("As built") and `penish/collection/plan.py` first.
- House rules: no em dashes, comments say why, and owner text is never overwritten.
- Run `ruff`, `mypy`, `pytest` and `pnpm check` before calling anything done.
- Never apply a live re-sort or unpin a leaf without Tom's go.

## Kickoff prompt

Paste this into Claude Code, in plan mode, from the repo root:

```
Read docs/ptp-and-site-pass.md, CLAUDE.md and docs/collections.md in full before doing anything.

Work in this order and stop for my go at each checkpoint:

1. Backups first (F1). Find out why /admin/system reports last_backup_at as null. Tell me
   whether a dump exists, fix the timer or the reporting, and show me a restore drill result.
   CHECKPOINT.
2. Part A, intake. Start with scripts/ptp_push.py in dry-run mode against ~/src/claude-share,
   so I can see which Pansophicon pictures are missing from the site and pick the finals.
   Then the decode fix (HEIC, Ultra HDR), the uploads table and the upload form. CHECKPOINT
   after the dry-run list, and again before anything is pushed.
3. Part B migration as a PR, with the dry-run re-sort diff shown to me, not applied. CHECKPOINT.
4. Part C report (scripts/ptp_filing_report.py), pasted into the PR with your recommendation
   for D. CHECKPOINT.
5. D, E and the rest of F in the order you propose, one PR each.

Rules: plan before code. Never apply a live re-sort, unpin a leaf, delete a picture or
change owner text without my go. Book titles and all 15 conservator notes are owner text
now; leave them alone. Never ask for or store the admin password. No em dashes. Run ruff,
mypy, pytest and pnpm check before calling anything done. Do not commit or push unless I ask.
```

## Models and effort

Tom's defaults: Sonnet wherever it will do, Opus for harder implementation, Fable for planning and for work where judgement decides the result. State the model and effort for each PR in the plan, and say so when a step turns out harder than its row.

| step | model | effort | why |
|---|---|---|---|
| The overall plan, and each checkpoint review | Fable | high | a bad plan costs every step after it |
| F1 backups: diagnose and fix | Sonnet | high | ops inspection; little design |
| A5 `ptp_push.py` | Sonnet | medium | a small script with a dry run |
| A1 to A4 decode, uploads table, upload form | Opus | medium | touches ingest ordering and client state |
| B migration and dry-run diff | Sonnet | high | the text is given; care is needed around pins |
| C report script | Opus | high | must reuse `plan.py` correctly |
| C reading the results, recommending D | Fable | high | the one real judgement call in the brief |
| D filer changes | Opus | high | scoring and backfill logic with tests |
| Tuning `conservator.md` against the 15 owner notes | Fable | medium | voice work |
| E admin form fixes | Sonnet | medium | mechanical UI |
| F2 previews, sitemap, feed, 404s | Sonnet | medium | well-trodden |
| F3 connecting the site | Opus | medium | small features, but voice-sensitive copy |
| F4 hygiene lists and slug redirects | Sonnet | medium | listing and proposing |

## Owner text set by hand on 2026-11-20 (do not overwrite)

- **Book titles**, all seven, in the form `Περὶ ζῴων. Of beasts.`: beasts; plants and stones; instruments and remedies; the heavens and signs; transmutation; divine things; man. Book vii is already titled Of Man while its anchor still describes dreams, until the part B migration lands.
- **Conservator notes** on all 15 leaves, saved as owner notes. `write missing notes` will skip them. Three of them flag anachronisms as physical facts: wove paper on `illustrated-chart-with-ten-framed-panels`, and machine-ruled ledger paper on the two leaves numbered III. Use these 15 as style examples when tuning `conservator.md`.

## What the admin showed on 2026-11-20

Read from `/admin/queue`, `/admin/logs`, `/admin/system`, `/admin/stats` and `/api/admin/collections/pansophicon`:

- **The worker is healthy.** It runs `d923094` (current master), its heartbeat is current, and its queue is 0. Every one of the 190 pictures is `live`.
- **The server received almost nothing.** The watcher's `claimed` log lines are the full record of what arrived:
  - 2026-11-19 10:57: a few files
  - 2026-11-20 00:54: 6 files
  - 2026-11-20 05:47: 2 files
  - 2026-11-20 06:44: 1 file
  
  That's about 15 to 20 files, against about 100 Pansophicon pictures made. The rest were never uploaded. The pipeline isn't dropping them: they never reached it. Most of the pictures live in `~/src/claude-share` on Tom's Macs.
- **One upload was lost silently.** Ingest job 1301 (`img-0938-56d206`, 2026-11-20 00:54) failed with `HardError('unsupported format (uhdrload_buffer); need png, jpeg or webp')`. That's an Ultra HDR JPEG (a gain-map JPEG, the kind iPhone and iPad Photos produce). Decoding fails before `request_from_line`, so the Pansophicon request was never written, and the collection page shows nothing for it.
  - Filenames like `img-4168` and bare UUIDs show that uploads come through iOS Photos and the share sheet. Photos re-encodes images, so expect HEIC and Ultra HDR to keep arriving.
- **Backups:** `last_backup_at` is `null`. Either the 03:30 dump isn't running or it isn't reporting. Check this first: it's the only irreplaceable thing on the box.
- **Refusals:** several `haiku` and `regen` jobs failed with `model refused (claude-opus-5-5)` (2026-11-15/16). Their pictures have no haiku.
- **Filing state:**
  - 15 leaves: book iii 9, iv 4, v 1, vi 1, and i, ii and vii 0
  - conservator notes: 0 of 15 when read; all 15 were then written by hand as owner notes
  - tuning: still `anchor_k 3`
  - the five owner-placed leaves in iii have `book_pinned = true`: the three zodiac plates, plate three and the furniture leaf
- **Public side:**
  - `/sitemap.xml`, `/feed.xml` and `/rss.xml` don't exist. They return the SPA shell with status 200, so every unknown URL is a soft 404.
  - The home page and the collection page have no `og:image` or `og:description`.
  - Picture pages use a 960px webp as `og:image`, which some link previewers don't render.

Where evidence lives for anything further: the VPS (container logs, `inbox/`, `processing/`, `failed/`, Postgres), the admin pages above, and Cloudflare analytics for request counts. peni.sh isn't on Netlify. Never ask for or store the admin password.

---

## A. Intake: make every upload land, and make losses visible

1. **Accept what iPads send.** Decode HEIC/HEIF and Ultra HDR / gain-map JPEGs. Either build libvips with libheif and a JPEG decoder that ignores the gain map, or fall back to Pillow + pillow-heif and re-encode to PNG before the existing pipeline. Keep the original bytes for the sha. Then retry job 1301 (`processing/d1d4e97b-...`).
2. **Never lose the collection intent.** Write the `collection_requests` row (or the `uploads` row, item 3) before decode, so a picture that fails to decode still shows up as a failed Pansophicon upload with its error.
3. **Record uploads at the door.** Add an `uploads` table, written when `POST /api/admin/upload` succeeds: stem, sha256, original filename, bytes, collection line, created_at. `/admin/collections` then lists every Pansophicon upload that isn't a leaf yet, with its stage: received, inbox, ingesting, failed (with reason), or live but unfiled.
4. **Client robustness** (`Upload.svelte`). The loop is client-side and sends one file at a time; on an iPad it dies when the tab is backgrounded.
   - Send 2 or 3 at a time, retrying each up to 3 times.
   - Keep failed rows with a `retry failed` button.
   - Hold `navigator.wakeLock` while sending, and warn on `beforeunload`.
   - Resume when the tab becomes visible again.
   - Show a summary line: `12 of 20 sent, 9 live, 2 waiting, 1 failed`.
   - Keep the collection tick until every row has been sent.
5. **A bulk path for the backlog.** Add `scripts/ptp_push.py`, run on a Mac. It takes a folder (`~/src/claude-share`), finds `ptp-*.png` and `zm*.png`, and skips anything whose sha256 is already on the site (check through a small read-only admin endpoint that answers which shas exist; never send the password from a script). Then it uploads the rest over the existing rsync path with a `_collection.txt` sidecar, `pansophicon`. Default to a dry run that prints the list. Tom confirms which files are finals. The folders hold rejected takes too (`-r2`, `-b`, `-jazari`...).
6. **Refusals.** Report the failed haiku jobs, and any describe refusals on leaves, before changing prompts. Whether leaves keep the erotica register is Tom's call.

## B. The books: Of Man, and no medium words

### Why

Every pre-modern encyclopedia had a book on man as such, and the ΠΤΠ doesn't. Examples: Isidore's *Etymologies* XI "De homine et portentis", Nemesius's *On the Nature of Man*, and the scala naturae, with man on the border between beasts and the divine. A large share of the corpus has no home: baths, ordeals, the muhtasib, the Psychomachia, guild floats, pregnancy and knowledge, punishments fitted exactly to the crime. That's about 20 of ptp-50 to 101 by a rough sort, plus the whole capital register (ptp-23 to 45). Book vii (dreams, 0 leaves) becomes **Of Man**, and dreams and divination fold into book iv beside astrology. It stays seven books: the public record states seven.

**The line between vi and vii:** what heaven does is vi, and what people do is vii, even in heaven's name. Who is holding the ladle?

The current anchors and tag hints also name media ("gold and lapis on parchment", "engraved", "woodcut", "mosaic, fresco, gold"). Section C tests the claim that this drives misfiling. These texts remove the medium words.

### New anchors and tag hints

| book | anchor | tag hints |
|---|---|---|
| i | Animals and composite creatures presented one at a time for study: their parts, habits, uses and dangers. Beasts with impossible anatomy, hybrids of fish, bird and mammal, monsters from the edges of the known world, creatures in the margins acting like people. Small human figures sometimes stand beside them for scale. | animal, beast, creature, hybrid, monster, bird, fish, serpent, drollery |
| ii | Plants shown whole from root to flower, with seeds and cross-sections. Trees that bear creatures, people or objects as fruit, roots shaped like people, gardens and harvests of impossible crops. Gems, minerals and stones laid out in rows with their virtues. | plant, tree, root, flower, seed, fruit, harvest, garden, gem, stone |
| iii | Devices built to be applied to the body: chairs, frames, balances, tubes, gears and basins with lettered parts. Surgery, bloodletting and cures in progress, remedies and their ingredients, a body opened, flapped or diagrammed for teaching. A patient approaches the apparatus warily while practitioners wait. | apparatus, machine, device, instrument, surgery, remedy, cure, anatomy, patient |
| iv | The heavens and what they foretell. Spheres, rings and orbits, planets with the people they govern, constellations drawn as figures, zodiac men whose body parts are ruled by the signs, winds, maps of seas and coasts. Dreams and their meanings, omens, divination and alphabets of signs. | celestial, stars, planet, zodiac, map, cosmos, sky, dream, divination, omen |
| v | The changing of one thing into another. Flasks and furnaces with whole scenes sealed inside them, a crowned king and queen, lions, serpents, suns and moons, sequences of small stages, laboratory work. Baths and fountains that alter whoever enters them, or fail to. | alchemy, flask, furnace, transformation, serpent, sun, moon, laboratory |
| vi | What heaven does. Gods, saints and the many-armed one-eyed sky god acting directly: creating, judging and weighing souls, sorting the saved from the damned, working miracles and martyrdoms. Heaven and hell, relics, processions and sacred scenes that belong to no known church. | god, saint, heaven, hell, judgement, soul, miracle, martyr, relic, icon |
| vii | What people are and what they do. The human figure shown whole and unashamed as the measure of things, set between beasts below and heaven above. Virtues and vices as persons, the soul and its housing, knowledge carried in the body, conception, pregnancy, birth and the ages of life. The household, the bath, the table, trades and guilds, custom and festival. Law, trial and ordeal, and the prescribed punishment fitted exactly to the offence, carried out calmly by magistrates and executioners while daily life goes on around it. | people, crowd, nude, bath, pregnancy, virtue, custom, law, trial, punishment, execution |

Book **titles** are already set by hand (see the top of this brief). The migration must not touch `title`.

### How to apply

- Add a migration (`021_...`) that updates each book's `anchor_text` and `tag_hints` **only where they still equal the 018 seed**. Report any book it skips: Tom may already have pasted new text by hand.
- Set `tuning.anchor_k` to 10 in the same migration, unless C recommends otherwise.
- Anchors re-embed on their own (`collection_anchor`). Wait until every book shows anchored, then run a **dry-run** re-sort. Show Tom the before/after table. Don't apply it.
- **Owner pins.** History shows five leaves placed by the owner in book iii on 2026-11-20 between 00:56 and 00:57: `zodiac-chart-plate-i-with-numbered-body-sections`, `plate-vii-in-a-numbered-series`, `plate-one-with-one-blank-panel`, `plate-three-on-ruled-paper`, `furniture-being-moved-in-a-row`. Report their current `pinned` and `book_pinned` flags. **Don't unpin anything.** Under the new anchors the zodiac plates belong in iv, so Tom decides.
- Update `docs/collections.md` with the new books and the vi/vii rule.

---

## C. How well does the filer do? (`scripts/ptp_filing_report.py`, read-only)

### C1. Explain every placement

For each leaf, print:
- for every book: cosine to the centroid, the tag term, the fill term and the total
- the chosen book and the runner-up, and the margin between them
- `pinned` and `book_pinned`
- for an unpinned leaf, a **leave-one-out** score: the leaf removed from its own book's centroid and fill

Emit markdown and CSV.

### C2. Gold labels

Use these proposed labels (under the new books) for the current 15 leaves, saved as `docs/ptp_gold.json`. Tom confirms or edits them before any metric counts.

| leaf | gold | why |
|---|---|---|
| plate-one-with-one-blank-panel | iv | zodiac man |
| plate-vii-in-a-numbered-series | iv | zodiac body-part grid |
| zodiac-chart-plate-i-with-numbered-body-sections | iv | zodiac man |
| reference-chart-with-ten-labelled-roundels | iv | ptp-50, the signs move into an old man's body |
| legibility-of-label-banners-on-a-framed-map | iv | world map |
| listening-station-with-recording-attendants | iii | ptp-48, the listening chair |
| bread-being-baked-over-an-open-fire | i | the bonnacon: a bestiary beast and its use |
| aged-plate-with-blank-caption-panel | i | beast parts with a small man for scale |
| monks-harvesting-the-shell-tree-by-the-shore | ii | the barnacle tree |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | vii | the bath |
| illustrated-chart-with-ten-framed-panels | vii | zm2-7, pregnant with knowledge |
| aged-sheet-with-handwritten-margin-notes | vii | physiognomy: men set beside beasts |
| plate-three-on-ruled-paper | vii | creatures housed in a man |
| furniture-being-moved-in-a-row | vii | the conjoined household |
| meal-service-at-a-table-inside-the-initial | i or vii | drolleries around a table; count either |

Today, under these labels, auto filing gets about 1 of the 10 unpinned leaves right (the map). Under the **old** anchors' own definitions it does about as badly: the listening chair is in iv, the bathhouse is in v (book iii's anchor literally says bathhouses), the barnacle tree is in vi and the bonnacon is in iv. Chance is 1 in 7. Confirm or refute these numbers.

### C3. The medium hypothesis

- The filer embeds the whole `analysis`. `describe.md` makes that four paragraphs: Subject, Setting, **Style**, Mood. Style (medium, technique, palette) is a quarter of the text. It is shared across very different subjects and differs between pictures of the same subject. Book iii is now nine leaves, most of them grey woodcut zodiac plates, five of them owner-pinned. The 16:04 UTC re-sort on 2026-11-20 moved `aged-sheet-...` and `illustrated-chart-...` from i into iii and emptied book i.
- Measure it. For the 15 leaves (and for all live pictures as a larger sample), compare how well each vector reproduces the gold labels by nearest centroid:
  - (a) whole analysis (current)
  - (b) Subject paragraph only
  - (c) Subject + Setting
  - (d) the existing `facet:subject/<recipe>` vectors
  - (e) Style only, as a control that should do worst on subject labels
- Report how much book choice tracks Style similarity versus Subject similarity, for example by rank correlation of the pairwise similarities with same-gold-book and same-medium indicators. Tag each leaf's medium from its tags or Style paragraph.

### C4. Ablations

Grid over:
- vector source: a to d above
- `anchor_k`: 3, 6, 10, 20
- `tag_alpha`: 0, 0.05, 0.15
- centering: on and off
- old versus new anchors

Report gold accuracy, mean margin, and the share of leaves whose book flips. Use `plan.py`'s pure functions, not the database writers.

### C5. Order sensitivity and attractors

- Refile the 15 leaves (later, all ptp leaves) in 50 random orders through `choose_book`. Report how often each leaf's book changes with order: first leaves set a book's centroid while `k/(k+n)` is still large.
- Run every live non-leaf picture on the site through `choose_book` (score only, write nothing), and report each book's share. A book that wins far beyond its share is an attractor.

### C6. Conservator notes

All 15 current leaves now carry owner notes, written by hand on 2026-11-20. Filing queues `collection_note` only when a leaf is filed, so check that newly filed leaves get a model note, and compare the first few against the 15 owner notes for voice.

---

## D. Filer fixes (build only what C supports; propose before building)

1. **File on content, not medium.** Switch the filer's vectors to whichever source wins C3/C4. Likely candidates are Subject + Setting, embedded as a new kind such as `analysis:content`, or `facet:subject`. Fall back to the whole analysis when the chosen kind is missing, and backfill the missing ones. Anchors are embedded to match.
2. **An anchor floor.** If C5 shows early leaves capture books, keep the written anchor's weight from falling below a floor (for example `max(k/(k+n), w_min)`, with `w_min` in `tuning`). Only add it if the ablation supports it.
3. **Explain placements in the admin.** Clicking a leaf cell shows its per-book scores and margin (C1 per leaf). The dry run returns a diff table (thumbnail, from, to, margin), and `apply` applies exactly that plan.
4. **Backfill notes.** A one-time sweep at worker start queues `collection_note` for leaves with none, like the existing anchor and unfiled sweeps.
5. The intake changes from part A.

---

## E. `/admin/collections` form fixes

From Tom's printout:

1. **Textareas are clipped.** Book i's anchor prints as an empty box, and its text spills out at the bottom of the printed page. Size every textarea to its content: `field-sizing: content` with a min-rows fallback, and no inner scrollbars. This applies to the anchors, preamble, gloss, vendor's description and record values.
2. **Public or private, at a glance.** Mark each field with a small muted marker, `public` or `filing only`: title, gloss, preamble, record and vendor text are public; anchor, tag hints and tuning are not. This is the admin, so it may explain itself.
3. **Tuning shows each value twice** (`anchor k 3` and then `3`). Use one numeric input per parameter, with a one-line plain description:
   - anchor k: how many leaves the written anchor counts as
   - tag alpha: weight of tag-hint matches
   - fill beta: how hard fuller books push back
   - book margin: how much better another book must score before a leaf moves
4. **Anchor state per book:** `embedded`, `queued` or `stale`, with the time of the last embed. Disable `dry run` while any anchor is queued.
5. **Tag hints.** Show them as chips. Next to each one, show how many current leaves carry a matching tag. A hint no leaf matches is dead weight, and the count makes that visible.
6. **Record editor.**
   - Values are auto-growing textareas; provenance is multi-line.
   - `up`, `down` and `remove` become small icon buttons.
   - Add a live preview of the public record, with a pencil icon on each row that opens it for editing.
7. **Unsaved changes.** Mark each dirty section, add a page-level `save all`, and warn on leaving with unsaved edits. Today each book has its own `save`, and one edit is easy to lose.
8. **Conservator notes inline.** Under each book's grid, list its leaves, each with its note in an editable field, a `write note` button and the note's source (`owner` or `model`).
9. **Unfiled** also lists Pansophicon uploads that aren't leaves yet, with their stage (part A).
10. **History.** Show the last 20 rows, with a filter by leaf. Color the reason (`owner`, `auto`, `resort`).

## F. Whole site: fix, extend, connect

These go after A to C. Propose the order to Tom; F1 and F2 come first.

### F1. Safety and correctness
- **Backups:** find out why `last_backup_at` is null. Fix the dump timer or its reporting. Then make a restore drill script that loads the latest dump into a scratch database and counts rows, and show its last result on `/admin/system`.
- **Real 404s:** unknown paths return the SPA shell with 200. Make the API return 404 for paths that match no route and no slug, so search engines and link checkers stop seeing a site with infinite pages. Keep the flat `not here` copy.
- **Haiku refusals:** retry the failed haiku jobs on another configured provider (openai and openrouter are already wired). A picture with no haiku should be rare. Log which provider wrote each haiku.

### F2. Delivery (how people arrive)
Most visitors arrive from shared links, so the preview is the front door.
- Use a 1200px **JPEG** `og:image` for picture pages, derived once, for previewers that don't take webp. Add `og:image:width` and `og:image:height`.
- **Home page:** `og:image` is a recent or random picture, and `og:description` is the current haiku.
- **Collection page:** `og:image` is a ΠΤΠ logo. `og:description` is the gloss.
- **Leaf pages:** `og:description` is the conservator note when there is one, otherwise the description.
- **Per-page `<title>`:** each picture's caption, and the collection's Greek title.
- `/sitemap.xml` covering every live picture, category, live gallery, tag and the collection, plus `Sitemap:` in robots.txt.
- An Atom feed, `/feed.xml`: newest pictures and new leaves. Each entry has the caption as the title, the description as the summary and the image enclosed. No site description beyond the title, as house style requires.

### F3. Connect the halves of the site
The gallery, the mycelium and the Pansophicon sit side by side without touching. Proposals:
- **Gallery to ΠΤΠ.** A non-leaf picture page whose nearest neighbors include leaves shows them in its `similar` strip as usual, with the leaf's `ptp iii, leaf 14` line under the thumbnail. No new copy: the line itself is the invitation.
- **Home.** One small line under the home stats when the collection changed in the last week, written as a fact: `ΠΤΠ iii, leaf 44 restored`. No link text beyond that line.
- **Mycelium.** Leaves already take part in the galleries. Show the leaf line on gallery cover cards that are leaves.
- **Search.** `ptp`, `pansophicon` and the Greek title return the collection first. A query like `iii 44` resolves to that leaf.
- **Haiku.** On the collection page, a haiku grown from the collection's own leaves (reuse `grow_haikus`), in the Fungal font.

### F4. Content hygiene (carried from the 2026-11-16 audit; confirm each is still true)
- Duplicate: `astronaut-on-roller-skates-carries-a-toaster-holding-a-skele` is a crop of `crosswalk-use-at-a-city-intersection` (similarity 0.858). Propose which one to keep; delete nothing.
- About 20 slugs describe the picture instead of the clerk's concern, and some are cut off mid-word at 60 characters. Propose new slugs from the current captions, with permanent redirects from the old ones. Slugs are how people arrive.
- Two comments are signed "ChatGPT". Leave them; just list them.
- Phone screenshots with UI chrome published as pictures: list them for Tom.

### F5. Engagement signals
There are 3 votes and 0 comments in total. Don't add features to chase engagement. Do make the admin show what exists: views per picture per day (the beacons are already sent), referrers from Cloudflare, and which shared links bring people in. Tom decides what to do with it.

## Done when

- **A:**
  - Ultra HDR and HEIC uploads decode, and job 1301's picture is live (or failed visibly with a reason).
  - A decode failure still shows on `/admin/collections` as a failed Pansophicon upload.
  - The client survives backgrounding a 20-file batch.
  - `ptp_push.py --dry-run` lists the backlog against the site's shas.
- **B:** the migration updates seed anchors only, reports skips and changes no pins. The dry-run diff has gone to Tom, not been applied.
- **C:** `scripts/ptp_filing_report.py` runs read-only and prints C1 to C5. Its report is in the PR with a recommendation for D.
- **D and E:** done as approved, with tests and screenshots at 1180px and 390px widths.
- **F1:** a backup exists from today and the restore drill passes. Unknown paths return 404.
- **F2:** link previews checked with a validator for one picture, the home page and the collection page. `sitemap.xml` and `feed.xml` validate.
- All checks pass. Don't weaken tests to get there.
