# The filer, read

A reading of `docs/reports/ptp-filing/report.md` (the numbers, made by
`scripts/ptp_filing_report.py` against a copy of the live database taken 2026-11-20:
190 live pictures, 15 leaves). Section names below are that report's.

Every accuracy figure counts against `docs/ptp_gold.json`, which the owner has not
confirmed. Fifteen leaves is a small sample: one leaf is 7 points of 15 and 10 points of
the 10 that are not book-pinned. Differences of one leaf are noise.

## What the numbers say

1. **The brief's figures hold.** Stored books are right for 1 of the 10 unpinned leaves
   (the map) and 1 of 15 in all. Chance is 1.57 of 10. The listening chair is in iv, the
   bathhouse in v, the barnacle tree in vi, the bonnacon in iv, and book iii's old anchor
   does say bathhouses. (C2)

2. **The stored state is stable.** A re-sort today moves nothing, and replaying the
   original filing order reproduces all 15 first filings. The filer is doing what it was
   built to do. (C1, C5)

3. **The new anchors help, a little.** 1 of 10 becomes 2 or 3 of 10. Averaged over the
   whole grid they add about 8 points. A re-sort under them moves one leaf at `anchor_k`
   3 (the barnacle tree to ii, right) and four at 10 (two right, two wrong).
   (C2, C4, before and after)

4. **Leaves resemble each other by medium, not by book.** Pairwise similarity of the
   whole analysis follows same-medium (rho 0.57) and same-stored-book (0.65), and barely
   follows same-gold-book (0.13). Two leaves of one gold book but different media are
   hardly more alike than unrelated leaves (mean cosine 0.148 against 0.131). (C3, C3b)

5. **Dropping the Style paragraph does not cure that.** The Subject paragraph alone
   tracks medium nearly as much (rho 0.50). The Subject paragraphs do not name a
   technique: none of the 15 contains woodcut, engraving or illuminated. Prints simply
   show plates, panels, labels and numbered parts, and the descriptions say so. The
   medium is in what the picture shows, as layout. (C3, C3b)

6. **So a book's leaves pull in more of their own medium.** Book iii holds nine leaves,
   five placed there by the owner, most of them prints. With those five moved to their
   gold books, iv becomes the book that draws the prints: the pregnancy chart, the
   physiognomy sheet and the beast plate all follow the zodiac plates into iv. (C5b)

7. **Order decides a great deal.** Refiled in 50 random orders at `anchor_k` 3, nine of
   the ten unpinned leaves land in more than one book, one of them in six. At 10 it is
   seven leaves and at most four books. (C5)

8. **Tag hints do nothing today.** `tag_overlap` splits tags on whitespace; 1431 of the
   2402 live tags are hyphenated and none has a space, so the hint `zodiac` never meets
   the tag `zodiac-man`. With the old hints no leaf matches a hint of its gold book.
   `tag_alpha` has no effect anywhere in the grid. Read hyphens as spaces and 9 of 15
   leaves match a hint of their gold book under the new hints, but accuracy moves by at
   most one leaf, because one matching hint is worth about 0.1 even at `tag_alpha` 1.
   (C1, C4, C5c)

9. **No book is an attractor for the rest of the site.** Of the 175 live pictures that
   are not leaves, the largest share any book takes is 23%. (C5)

10. **The ceiling is low.** Put every other leaf in its gold book and ask the filer to
    place the one left out: it gets 2 to 7 of 15, depending on source and `anchor_k`.
    With the five pins moved to gold and the rest re-sorted, 3 to 5 of 10. No vector
    source, `anchor_k`, `tag_alpha` or centering in the grid gets past 7 of 15. (C4, C5b)

## What that means

The medium hypothesis is right about the symptom and wrong about the cure. Medium does
drive misfiling, but it rides in the subject itself, so filing on Subject vectors buys
one leaf at best (with pins at gold, sequential filing goes from about 3 of 10 to 4 or
5). Tuning cannot fix this: the best cell in the grid is still wrong more often than
right, on a collection arranged to help it.

The gold labels rest on what a picture is of: the bonnacon, the barnacle tree,
physiognomy, a body pregnant with knowledge. Nearest-vector filing can only say which
pictures read alike.

## A different filer, tried once

Since no setting of the present method clears half, one other method was tried, outside
the report script: a model reads a leaf's caption, description and analysis and the
seven new anchors, with the vi/vii rule, and names a book and a runner-up. It was told
to judge by subject and never by medium. It did not see the slugs' current books, the
gold labels or the conservator notes. One run each, as subagents, not through the
site's own model path.

| judge | right of 15 | right of 10 unpinned | gold in its top two |
|---|---|---|---|
| filer today (stored) | 1 | 1 | - |
| filer, best cell in the grid | 7 | 4 | - |
| filer, pins at gold, best cell | - | 5 | - |
| Haiku | 12 | 9 | 14 |
| Sonnet | 10 | 7 | 15 |

Where the judges missed:

| leaf | gold | Haiku | Sonnet | their reason |
|---|---|---|---|---|
| furniture-being-moved-in-a-row | vii | i | i | animals acting like people, carrying furniture |
| plate-three-on-ruled-paper | vii | i | i | a figure carrying many catalogued animals |
| bread-being-baked-over-an-open-fire | i | vii | vii | a woman baking bread; the description never says bonnacon |
| aged-plate-with-blank-caption-panel | i | i | iv | Sonnet read the beast parts as zodiac emblems |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | vii | vii | v | book v's anchor says "Baths and fountains that alter whoever enters them" |

Both judges agree with each other and against gold on three leaves. Two of those are
the owner's own placements of creatures (i by the anchor's own words: "creatures in the
margins acting like people"); the third is a picture whose description does not contain
the thing the label depends on. These are as much questions about the labels and the
anchors as about the judge. This is one run on fifteen leaves and proves nothing by
itself, but the gap to the present method (4 or 5 of 10 against 7 or 9) is larger than
any other difference measured here.

## Recommended for part D

In order of how well the numbers support each.

1. **Ship the new anchors, `anchor_k` 10** (part B as written). Supported: more right,
   less order-dependent, never worse. 20 does slightly better in some cells and worse in
   others; there is no case for leaving the brief's 10.
2. **Fix the tag split** so hyphenated tags meet their hints. It is a bug against the
   documented behaviour. Expect at most one leaf from it; leave `tag_alpha` alone.
3. **Explain placements and make the dry run exact** (D3). Not a question for the
   numbers: every finding above came from asking why a leaf went where it did, and the
   admin cannot answer that today.
4. **The note sweep** (D4). Harmless and small. Nothing lacks a note today: all 15
   leaves carry the owner's, and no `collection_note` job has ever run, so the first
   model notes will be on the backlog.
5. **File by a model's judgement, vectors as the fallback** (new, not in the brief).
   At filing, one text-only call with the analysis and the seven anchors returns a book,
   a runner-up and a reason; the reason is kept with the leaf's history and is what D3
   shows. The vector score stays for the leaf's place within the book, for the dry run's
   margins, and for when the call fails or is refused. This is the only change measured
   here that moves accuracy past half. It needs the owner's go: it is a new paid call
   per leaf (small, text only) and a new setting for which model.
6. **File on Subject vectors** (D1): weakly supported, cheap (`facet:subject/split` is
   already stored for every picture, so no new kind and no backfill). Worth one leaf in
   ten at most. Do it only if 5 is declined; with 5 it barely matters.
7. **An anchor floor** (D2): not supported. More anchor weight is never worse here, but
   `anchor_k` 40 does no better than 20, and fifteen leaves cannot say what happens at
   eighty. Measure again once the backlog is in, with more gold labels.

## For the owner to decide

- **The gold labels.** Confirm or edit `docs/ptp_gold.json`. Three are disputed by both
  judges: the two creature plates the owner placed (vii in gold, i by the anchor) and
  the bonnacon, which no reader of the description alone can know is a bonnacon.
- **The five pins.** They are `book_pinned` in iii. Gold puts the three zodiac plates in
  iv and the other two in vii. Nothing unpins or moves them but the owner. While they
  stay, book iii's centre is theirs, and the reference chart (gold iv) follows them.
- **Baths.** The new anchor for v claims "baths and fountains that alter whoever enters
  them" and the new anchor for vii claims "the bath". The bathhouse leaf goes to v in
  every vector run and in Sonnet's judgement. If the bathhouse is vii, one of the two
  sentences should give way.
- **The backlog.** About 80 pictures will be filed on arrival by whichever filer is
  live. Sending them before 5 is decided files most of them wrongly, and each later move
  shows publicly as `formerly`.
