# How well does the filer do?

> **UNCONFIRMED GOLD LABELS.** `docs/ptp_gold.json` says `confirmed: false`. Every
> accuracy figure below counts against labels the owner has not confirmed.

## Method

- Database: `postgres` at 127.0.0.1:63563, read in one read-only transaction.
- Holdings: 190 live pictures, newest published 2026-11-20T06:44:48+00:00; 15 leaves in `pansophicon`, 10 of them not book-pinned.
- Tuning from the database: anchor_k 3, tag_alpha 0.05, fill_beta 0.05, book_margin 0.03; 77 leaves per book. Values not varied in a section are these.
- Embedding model Qwen/Qwen3-Embedding-0.6B (1024 dimensions). Centering follows `store._mean`: a kind's corpus mean is subtracted when at least 30 live pictures have a vector of that kind.
- Vector sources:
  - (a) whole analysis: stored `description` kind: the whole four-paragraph analysis (what the filer uses); 190 pictures; centered on its own corpus mean.
  - (b) subject paragraph: stored `facet:subject/split`: the Subject paragraph body cut from the analysis by `facets.split` (label removed); 190 pictures; centered on its own corpus mean.
  - (c) subject + setting: embedded here: the stored analysis cut at its `Style:` label (Subject and Setting paragraphs, labels kept), one embed_documents batch; 190 pictures; centered on its own corpus mean.
  - (d) subject, extract recipe: stored `facet:subject/extract`: the subject written by a separate vision call (the `extract` recipe); (b) already stands for the split recipe; 190 pictures; centered on its own corpus mean.
  - (e) style paragraph: stored `facet:style/split`: the Style paragraph body cut from the analysis; 190 pictures; centered on its own corpus mean.
- Anchors: old = the database's anchor texts, tag hints and stored anchor vectors; new = the texts and tag hints in `docs/ptp_anchors.json` (section B of the brief), embedded here. Under every source the same anchor vector is used, centered with that source's mean, as `store.load` centers anchors with the picture mean.
- Scores: `choose_book`, `assign_books` and `plan_resort` from `penish/collection/plan.py`. `plan.score` returns only the total, so the cos, tag and fill terms are read off `choose_book` run with the other weights at zero.
- Leave-one-out (LOO): the leaf scored with every other leaf where it stands and itself removed from its book's centroid and fill; this is `choose_book` over the others, which is how a filing or a re-sort sees it.
- Books whose anchor text and tag hints in the file equal the database's: none.
- Pins at gold (C5b): a counterfactual state with the book-pinned leaves moved to their gold books and still book-pinned there. Upper bound (C5b): every leaf in its gold book, the two-book leaf in the one of its two it scores higher in.
- Tags with hyphens as spaces (C5c): simulates a change to the filer that does not exist yet. `plan.tag_overlap` splits tags on whitespace and the live tags are hyphenated; this variant hands the plan functions the tags with hyphens turned into spaces. Every other section uses the tags as stored.
- Medium words (C3b): the medium rule words plus near relatives, counted as whole words in the Subject and Setting paragraphs.
- Seed 20261120; 50 random orders, the same orders in every section.
- Checks:
  - book i anchor (batch of 7, as collection_anchor): cosine 1.000000
  - book ii anchor (batch of 7, as collection_anchor): cosine 1.000000
  - book iii anchor (batch of 7, as collection_anchor): cosine 1.000000
  - book iv anchor (batch of 7, as collection_anchor): cosine 1.000000
  - book v anchor (batch of 7, as collection_anchor): cosine 1.000000
  - book vi anchor (batch of 7, as collection_anchor): cosine 1.000000
  - book vii anchor (batch of 7, as collection_anchor): cosine 1.000000
  - analysis of illustrated-chart-with-ten-framed-panels vs stored description: cosine 1.000000
  - subject paragraph of illustrated-chart-with-ten-framed-panels vs stored facet:subject/split: cosine 1.000000
  - mean of the live description vectors vs corpus_stats description mean: max abs difference 4.79e-09
  - store.load vs this script's reads (source a, old anchors, centered): identical, max abs vector difference 0.00e+00

Run time: read 0.1s, embedding 39.9s, c1 0.0s, c2 0.0s, c3 0.2s, c3b 0.0s, grid 5.5s, c4 5.5s, c5 0.3s, pins grid 1.4s, c5b 1.4s, c5c 0.1s, c6 0.0s, before_after 0.0s; total 47.8s.

## C1. Placements

Source (a), old anchors, centered, database tuning. `with self`: centroids and fill as they stand, the leaf included in its own book. `LOO`: the leaf left out. A re-sort (`assign_books`) moves an unpinned leaf only when its LOO best beats its current book by book_margin 0.03. The tag term is the same with and without the leaf, so it is shown once.

A full re-sort today (`assign_books`, old anchors) moves 0 leaves between books.

Tag hints (old): 7 of 15 leaves match a hint of any book: illustrated-chart-with-ten-framed-panels (iii); monks-harvesting-the-shell-tree-by-the-shore (vi); aged-sheet-with-handwritten-margin-notes (i); zodiac-chart-plate-i-with-numbered-body-sections (i, iii); plate-vii-in-a-numbered-series (i, iii); plate-one-with-one-blank-panel (i); reference-chart-with-ten-labelled-roundels (i).
Tag hints (new): 4 of 15 leaves match a hint of any book: monks-harvesting-the-shell-tree-by-the-shore (ii); zodiac-chart-plate-i-with-numbered-body-sections (iv); plate-vii-in-a-numbered-series (iii, iv); plate-one-with-one-blank-panel (iv).
Of the 2402 tags on live pictures, 1431 contain a hyphen and 0 a space.

Summary (book / margin, margin being chosen minus runner-up):

| leaf | stored | book_pinned | with self | LOO | LOO = stored | re-sort |
|---|---|---|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | iii |  | iii / 0.3627 | iii / 0.2540 | yes | iii |
| bread-being-baked-over-an-open-fire | iv |  | iv / 0.2553 | iv / 0.0601 | yes | iv |
| monks-harvesting-the-shell-tree-by-the-shore | vi |  | vi / 0.3169 | vi / 0.0525 | yes | vi |
| aged-sheet-with-handwritten-margin-notes | iii |  | iii / 0.2587 | iii / 0.1292 | yes | iii |
| legibility-of-label-banners-on-a-framed-map | iv |  | iv / 0.3468 | iv / 0.1878 | yes | iv |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | v |  | v / 0.2931 | v / 0.0193 | yes | v |
| meal-service-at-a-table-inside-the-initial | iv |  | iv / 0.3390 | iv / 0.1705 | yes | iv |
| zodiac-chart-plate-i-with-numbered-body-sections | iii | yes | iii / 0.4833 | iii / 0.3920 |  | iii |
| plate-vii-in-a-numbered-series | iii | yes | iii / 0.4211 | iii / 0.3197 |  | iii |
| plate-one-with-one-blank-panel | iii | yes | iii / 0.4335 | iii / 0.3283 |  | iii |
| plate-three-on-ruled-paper | iii | yes | iii / 0.2615 | iii / 0.1452 |  | iii |
| furniture-being-moved-in-a-row | iii | yes | iii / 0.2079 | iii / 0.0610 |  | iii |
| listening-station-with-recording-attendants | iv |  | iv / 0.3051 | iv / 0.1199 | yes | iv |
| aged-plate-with-blank-caption-panel | iii |  | iii / 0.2956 | iii / 0.1613 | yes | iii |
| reference-chart-with-ten-labelled-roundels | iii |  | iii / 0.3849 | iii / 0.2794 | yes | iii |

### illustrated-chart-with-ten-framed-panels

Stored in iii, leaf 44; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.2557 | 0.0000 | 0.0000 | 0.2557 | 0.2557 | 0.0000 | 0.2557 |
| ii | 0.2021 | 0.0000 | 0.0000 | 0.2021 | 0.2021 | 0.0000 | 0.2021 |
| iii (stored) | 0.6120 | 0.0071 | -0.0007 | 0.6184 | 0.5031 | -0.0005 | 0.5097 |
| iv | 0.2132 | 0.0000 | -0.0001 | 0.2131 | 0.2132 | -0.0001 | 0.2131 |
| v | 0.1920 | 0.0000 | -0.0000 | 0.1920 | 0.1920 | -0.0000 | 0.1920 |
| vi | 0.0979 | 0.0000 | -0.0000 | 0.0979 | 0.0979 | -0.0000 | 0.0979 |
| vii | 0.1875 | 0.0000 | 0.0000 | 0.1875 | 0.1875 | 0.0000 | 0.1875 |

With self: chooses iii, runner-up i, margin 0.3627. LOO: chooses iii, runner-up i, margin 0.2540.
The LOO choice equals the stored book. A re-sort would keep it in iii.

### bread-being-baked-over-an-open-fire

Stored in iv, leaf 77; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.0644 | 0.0000 | 0.0000 | 0.0644 | 0.0644 | 0.0000 | 0.0644 |
| ii | 0.0801 | 0.0000 | 0.0000 | 0.0801 | 0.0801 | 0.0000 | 0.0801 |
| iii | 0.1604 | 0.0000 | -0.0007 | 0.1597 | 0.1604 | -0.0007 | 0.1597 |
| iv (stored) | 0.4607 | 0.0000 | -0.0001 | 0.4606 | 0.2654 | -0.0001 | 0.2653 |
| v | 0.1165 | 0.0000 | -0.0000 | 0.1165 | 0.1165 | -0.0000 | 0.1165 |
| vi | 0.2052 | 0.0000 | -0.0000 | 0.2052 | 0.2052 | -0.0000 | 0.2052 |
| vii | 0.0899 | 0.0000 | 0.0000 | 0.0899 | 0.0899 | 0.0000 | 0.0899 |

With self: chooses iv, runner-up vi, margin 0.2553. LOO: chooses iv, runner-up vi, margin 0.0601.
The LOO choice equals the stored book. A re-sort would keep it in iv.

### monks-harvesting-the-shell-tree-by-the-shore

Stored in vi, leaf 1; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.0578 | 0.0000 | 0.0000 | 0.0578 | 0.0578 | 0.0000 | 0.0578 |
| ii | 0.0713 | 0.0000 | 0.0000 | 0.0713 | 0.0713 | 0.0000 | 0.0713 |
| iii | -0.0020 | 0.0000 | -0.0007 | -0.0026 | -0.0020 | -0.0007 | -0.0026 |
| iv | 0.2114 | 0.0000 | -0.0001 | 0.2113 | 0.2114 | -0.0001 | 0.2113 |
| v | 0.0924 | 0.0000 | -0.0000 | 0.0924 | 0.0924 | -0.0000 | 0.0924 |
| vi (stored) | 0.5210 | 0.0071 | -0.0000 | 0.5282 | 0.2567 | 0.0000 | 0.2638 |
| vii | 0.0295 | 0.0000 | 0.0000 | 0.0295 | 0.0295 | 0.0000 | 0.0295 |

With self: chooses vi, runner-up iv, margin 0.3169. LOO: chooses vi, runner-up iv, margin 0.0525.
The LOO choice equals the stored book. A re-sort would keep it in vi.

### aged-sheet-with-handwritten-margin-notes

Stored in iii, leaf 32; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.1477 | 0.0083 | 0.0000 | 0.1560 | 0.1477 | 0.0000 | 0.1560 |
| ii | 0.1038 | 0.0000 | 0.0000 | 0.1038 | 0.1038 | 0.0000 | 0.1038 |
| iii (stored) | 0.4786 | 0.0000 | -0.0007 | 0.4780 | 0.3491 | -0.0005 | 0.3485 |
| iv | 0.2194 | 0.0000 | -0.0001 | 0.2193 | 0.2194 | -0.0001 | 0.2193 |
| v | 0.0014 | 0.0000 | -0.0000 | 0.0013 | 0.0014 | -0.0000 | 0.0013 |
| vi | -0.0102 | 0.0000 | -0.0000 | -0.0102 | -0.0102 | -0.0000 | -0.0102 |
| vii | 0.0489 | 0.0000 | 0.0000 | 0.0489 | 0.0489 | 0.0000 | 0.0489 |

With self: chooses iii, runner-up iv, margin 0.2587. LOO: chooses iii, runner-up iv, margin 0.1292.
The LOO choice equals the stored book. A re-sort would keep it in iii.

### legibility-of-label-banners-on-a-framed-map

Stored in iv, leaf 1; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.0386 | 0.0000 | 0.0000 | 0.0386 | 0.0386 | 0.0000 | 0.0386 |
| ii | 0.1830 | 0.0000 | 0.0000 | 0.1830 | 0.1830 | 0.0000 | 0.1830 |
| iii | 0.2870 | 0.0000 | -0.0007 | 0.2863 | 0.2870 | -0.0007 | 0.2863 |
| iv (stored) | 0.6332 | 0.0000 | -0.0001 | 0.6331 | 0.4742 | -0.0001 | 0.4741 |
| v | 0.0967 | 0.0000 | -0.0000 | 0.0966 | 0.0967 | -0.0000 | 0.0966 |
| vi | 0.2263 | 0.0000 | -0.0000 | 0.2263 | 0.2263 | -0.0000 | 0.2263 |
| vii | 0.0543 | 0.0000 | 0.0000 | 0.0543 | 0.0543 | 0.0000 | 0.0543 |

With self: chooses iv, runner-up iii, margin 0.3468. LOO: chooses iv, runner-up iii, margin 0.1878.
The LOO choice equals the stored book. A re-sort would keep it in iv.

### towel-and-slipper-racks-in-a-bathhouse-changing-room

Stored in v, leaf 1; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.1217 | 0.0000 | 0.0000 | 0.1217 | 0.1217 | 0.0000 | 0.1217 |
| ii | 0.1309 | 0.0000 | 0.0000 | 0.1309 | 0.1309 | 0.0000 | 0.1309 |
| iii | 0.1366 | 0.0000 | -0.0007 | 0.1359 | 0.1366 | -0.0007 | 0.1359 |
| iv | 0.1991 | 0.0000 | -0.0001 | 0.1989 | 0.1991 | -0.0001 | 0.1989 |
| v (stored) | 0.4921 | 0.0000 | -0.0000 | 0.4920 | 0.2183 | 0.0000 | 0.2183 |
| vi | 0.1597 | 0.0000 | -0.0000 | 0.1597 | 0.1597 | -0.0000 | 0.1597 |
| vii | 0.0663 | 0.0000 | 0.0000 | 0.0663 | 0.0663 | 0.0000 | 0.0663 |

With self: chooses v, runner-up iv, margin 0.2931. LOO: chooses v, runner-up iv, margin 0.0193.
The LOO choice equals the stored book. A re-sort would keep it in v.

### meal-service-at-a-table-inside-the-initial

Stored in iv, leaf 24; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.0594 | 0.0000 | 0.0000 | 0.0594 | 0.0594 | 0.0000 | 0.0594 |
| ii | 0.1375 | 0.0000 | 0.0000 | 0.1375 | 0.1375 | 0.0000 | 0.1375 |
| iii | 0.2574 | 0.0000 | -0.0007 | 0.2567 | 0.2574 | -0.0007 | 0.2567 |
| iv (stored) | 0.5959 | 0.0000 | -0.0001 | 0.5958 | 0.4273 | -0.0001 | 0.4272 |
| v | 0.1235 | 0.0000 | -0.0000 | 0.1235 | 0.1235 | -0.0000 | 0.1235 |
| vi | 0.2285 | 0.0000 | -0.0000 | 0.2285 | 0.2285 | -0.0000 | 0.2285 |
| vii | 0.0289 | 0.0000 | 0.0000 | 0.0289 | 0.0289 | 0.0000 | 0.0289 |

With self: chooses iv, runner-up iii, margin 0.3390. LOO: chooses iv, runner-up iii, margin 0.1705.
The LOO choice equals the stored book. A re-sort would keep it in iv.

### zodiac-chart-plate-i-with-numbered-body-sections

Stored in iii, leaf 16; book_pinned true, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.2123 | 0.0083 | 0.0000 | 0.2206 | 0.2123 | 0.0000 | 0.2206 |
| ii | 0.0499 | 0.0000 | 0.0000 | 0.0499 | 0.0499 | 0.0000 | 0.0499 |
| iii (stored) | 0.6975 | 0.0071 | -0.0007 | 0.7040 | 0.6060 | -0.0005 | 0.6126 |
| iv | 0.1383 | 0.0000 | -0.0001 | 0.1381 | 0.1383 | -0.0001 | 0.1381 |
| v | 0.0142 | 0.0000 | -0.0000 | 0.0142 | 0.0142 | -0.0000 | 0.0142 |
| vi | 0.0329 | 0.0000 | -0.0000 | 0.0329 | 0.0329 | -0.0000 | 0.0329 |
| vii | 0.0565 | 0.0000 | 0.0000 | 0.0565 | 0.0565 | 0.0000 | 0.0565 |

With self: chooses iii, runner-up i, margin 0.4833. LOO: chooses iii, runner-up i, margin 0.3920.
Book-pinned by the owner; the filer does not choose its book.

### plate-vii-in-a-numbered-series

Stored in iii, leaf 9; book_pinned true, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.1623 | 0.0083 | 0.0000 | 0.1707 | 0.1623 | 0.0000 | 0.1707 |
| ii | 0.1839 | 0.0000 | 0.0000 | 0.1839 | 0.1839 | 0.0000 | 0.1839 |
| iii (stored) | 0.6502 | 0.0071 | -0.0007 | 0.6566 | 0.5486 | -0.0005 | 0.5552 |
| iv | 0.2356 | 0.0000 | -0.0001 | 0.2355 | 0.2356 | -0.0001 | 0.2355 |
| v | 0.0591 | 0.0000 | -0.0000 | 0.0591 | 0.0591 | -0.0000 | 0.0591 |
| vi | 0.0253 | 0.0000 | -0.0000 | 0.0253 | 0.0253 | -0.0000 | 0.0253 |
| vii | 0.1566 | 0.0000 | 0.0000 | 0.1566 | 0.1566 | 0.0000 | 0.1566 |

With self: chooses iii, runner-up iv, margin 0.4211. LOO: chooses iii, runner-up iv, margin 0.3197.
Book-pinned by the owner; the filer does not choose its book.

### plate-one-with-one-blank-panel

Stored in iii, leaf 1; book_pinned true, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.1138 | 0.0083 | 0.0000 | 0.1222 | 0.1138 | 0.0000 | 0.1222 |
| ii | 0.1161 | 0.0000 | 0.0000 | 0.1161 | 0.1161 | 0.0000 | 0.1161 |
| iii (stored) | 0.6306 | 0.0000 | -0.0007 | 0.6299 | 0.5252 | -0.0005 | 0.5247 |
| iv | 0.1966 | 0.0000 | -0.0001 | 0.1964 | 0.1966 | -0.0001 | 0.1964 |
| v | 0.0356 | 0.0000 | -0.0000 | 0.0356 | 0.0356 | -0.0000 | 0.0356 |
| vi | 0.0251 | 0.0000 | -0.0000 | 0.0251 | 0.0251 | -0.0000 | 0.0251 |
| vii | 0.0806 | 0.0000 | 0.0000 | 0.0806 | 0.0806 | 0.0000 | 0.0806 |

With self: chooses iii, runner-up iv, margin 0.4335. LOO: chooses iii, runner-up iv, margin 0.3283.
Book-pinned by the owner; the filer does not choose its book.

### plate-three-on-ruled-paper

Stored in iii, leaf 66; book_pinned true, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.3063 | 0.0000 | 0.0000 | 0.3063 | 0.3063 | 0.0000 | 0.3063 |
| ii | 0.1021 | 0.0000 | 0.0000 | 0.1021 | 0.1021 | 0.0000 | 0.1021 |
| iii (stored) | 0.5685 | 0.0000 | -0.0007 | 0.5679 | 0.4521 | -0.0005 | 0.4515 |
| iv | 0.0596 | 0.0000 | -0.0001 | 0.0595 | 0.0596 | -0.0001 | 0.0595 |
| v | 0.0432 | 0.0000 | -0.0000 | 0.0432 | 0.0432 | -0.0000 | 0.0432 |
| vi | 0.0086 | 0.0000 | -0.0000 | 0.0086 | 0.0086 | -0.0000 | 0.0086 |
| vii | 0.0346 | 0.0000 | 0.0000 | 0.0346 | 0.0346 | 0.0000 | 0.0346 |

With self: chooses iii, runner-up i, margin 0.2615. LOO: chooses iii, runner-up i, margin 0.1452.
Book-pinned by the owner; the filer does not choose its book.

### furniture-being-moved-in-a-row

Stored in iii, leaf 77; book_pinned true, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.0785 | 0.0000 | 0.0000 | 0.0785 | 0.0785 | 0.0000 | 0.0785 |
| ii | -0.0092 | 0.0000 | 0.0000 | -0.0092 | -0.0092 | 0.0000 | -0.0092 |
| iii (stored) | 0.2871 | 0.0000 | -0.0007 | 0.2864 | 0.1400 | -0.0005 | 0.1395 |
| iv | -0.0144 | 0.0000 | -0.0001 | -0.0145 | -0.0144 | -0.0001 | -0.0145 |
| v | -0.0974 | 0.0000 | -0.0000 | -0.0974 | -0.0974 | -0.0000 | -0.0974 |
| vi | -0.0521 | 0.0000 | -0.0000 | -0.0521 | -0.0521 | -0.0000 | -0.0521 |
| vii | -0.0485 | 0.0000 | 0.0000 | -0.0485 | -0.0485 | 0.0000 | -0.0485 |

With self: chooses iii, runner-up i, margin 0.2079. LOO: chooses iii, runner-up i, margin 0.0610.
Book-pinned by the owner; the filer does not choose its book.

### listening-station-with-recording-attendants

Stored in iv, leaf 47; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | -0.0175 | 0.0000 | 0.0000 | -0.0175 | -0.0175 | 0.0000 | -0.0175 |
| ii | 0.1319 | 0.0000 | 0.0000 | 0.1319 | 0.1319 | 0.0000 | 0.1319 |
| iii | 0.2146 | 0.0000 | -0.0007 | 0.2139 | 0.2146 | -0.0007 | 0.2139 |
| iv (stored) | 0.5191 | 0.0000 | -0.0001 | 0.5190 | 0.3339 | -0.0001 | 0.3338 |
| v | 0.1652 | 0.0000 | -0.0000 | 0.1652 | 0.1652 | -0.0000 | 0.1652 |
| vi | 0.1465 | 0.0000 | -0.0000 | 0.1465 | 0.1465 | -0.0000 | 0.1465 |
| vii | -0.0456 | 0.0000 | 0.0000 | -0.0456 | -0.0456 | 0.0000 | -0.0456 |

With self: chooses iv, runner-up iii, margin 0.3051. LOO: chooses iv, runner-up iii, margin 0.1199.
The LOO choice equals the stored book. A re-sort would keep it in iv.

### aged-plate-with-blank-caption-panel

Stored in iii, leaf 56; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.1107 | 0.0000 | 0.0000 | 0.1107 | 0.1107 | 0.0000 | 0.1107 |
| ii | 0.0920 | 0.0000 | 0.0000 | 0.0920 | 0.0920 | 0.0000 | 0.0920 |
| iii (stored) | 0.4382 | 0.0000 | -0.0007 | 0.4375 | 0.3038 | -0.0005 | 0.3033 |
| iv | 0.1421 | 0.0000 | -0.0001 | 0.1420 | 0.1421 | -0.0001 | 0.1420 |
| v | 0.0382 | 0.0000 | -0.0000 | 0.0382 | 0.0382 | -0.0000 | 0.0382 |
| vi | 0.0521 | 0.0000 | -0.0000 | 0.0521 | 0.0521 | -0.0000 | 0.0521 |
| vii | -0.0523 | 0.0000 | 0.0000 | -0.0523 | -0.0523 | 0.0000 | -0.0523 |

With self: chooses iii, runner-up iv, margin 0.2956. LOO: chooses iii, runner-up iv, margin 0.1613.
The LOO choice equals the stored book. A re-sort would keep it in iii.

### reference-chart-with-ten-labelled-roundels

Stored in iii, leaf 24; book_pinned false, pinned false.

| book | cos | tag | fill | total | LOO cos | LOO fill | LOO total |
|---|---|---|---|---|---|---|---|
| i | 0.0753 | 0.0083 | 0.0000 | 0.0836 | 0.0753 | 0.0000 | 0.0836 |
| ii | 0.0656 | 0.0000 | 0.0000 | 0.0656 | 0.0656 | 0.0000 | 0.0656 |
| iii (stored) | 0.6296 | 0.0000 | -0.0007 | 0.6290 | 0.5241 | -0.0005 | 0.5235 |
| iv | 0.2442 | 0.0000 | -0.0001 | 0.2441 | 0.2442 | -0.0001 | 0.2441 |
| v | -0.0239 | 0.0000 | -0.0000 | -0.0240 | -0.0239 | -0.0000 | -0.0240 |
| vi | 0.0048 | 0.0000 | -0.0000 | 0.0047 | 0.0048 | -0.0000 | 0.0047 |
| vii | -0.0072 | 0.0000 | 0.0000 | -0.0072 | -0.0072 | 0.0000 | -0.0072 |

With self: chooses iii, runner-up iv, margin 0.3849. LOO: chooses iii, runner-up iv, margin 0.2794.
The LOO choice equals the stored book. A re-sort would keep it in iii.

## C2. Gold accuracy

Gold labels are **unconfirmed**.

Source (a), centered. `stored` is the book in the database. `LOO` is what the filer chooses for each leaf against everyone else where they stand. `re-sort` is `assign_books` (book-pinned leaves stay). A leaf with two gold books is right in either.

Chance (one uniform random book per leaf): 1.57 of 10 unpinned, 2.29 of 15 in all (1/7 per leaf, 2/7 for the leaf with two gold books).

| choice | unpinned right | all right |
|---|---|---|
| stored | 1 of 10 | 1 of 15 |
| LOO, old anchors, k 3 | 1 of 10 | 1 of 15 |
| re-sort, old anchors, k 3 | 1 of 10 | 1 of 15 |
| LOO, old anchors, k 10 | 1 of 10 | 1 of 15 |
| re-sort, old anchors, k 10 | 1 of 10 | 1 of 15 |
| LOO, new anchors, k 3 | 3 of 10 | 3 of 15 |
| re-sort, new anchors, k 3 | 2 of 10 | 2 of 15 |
| LOO, new anchors, k 10 | 3 of 10 | 3 of 15 |
| re-sort, new anchors, k 10 | 3 of 10 | 3 of 15 |

Stored books, unpinned leaves: 1 of 10 right.

| leaf | gold | stored | result |
|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | vii | iii | wrong |
| bread-being-baked-over-an-open-fire | i | iv | wrong |
| monks-harvesting-the-shell-tree-by-the-shore | ii | vi | wrong |
| aged-sheet-with-handwritten-margin-notes | vii | iii | wrong |
| legibility-of-label-banners-on-a-framed-map | iv | iv | right |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | vii | v | wrong |
| meal-service-at-a-table-inside-the-initial | i or vii | iv | wrong |
| listening-station-with-recording-attendants | iii | iv | wrong |
| aged-plate-with-blank-caption-panel | i | iii | wrong |
| reference-chart-with-ten-labelled-roundels | iv | iii | wrong |

Stored books, all leaves: 1 of 15 right; the book-pinned leaves that are wrong: zodiac-chart-plate-i-with-numbered-body-sections, plate-vii-in-a-numbered-series, plate-one-with-one-blank-panel, plate-three-on-ruled-paper, furniture-being-moved-in-a-row.

Per leaf, every choice:

| leaf | gold | stored | LOO, old anchors, k 3 | re-sort, old anchors, k 3 | LOO, old anchors, k 10 | re-sort, old anchors, k 10 | LOO, new anchors, k 3 | re-sort, new anchors, k 3 | LOO, new anchors, k 10 | re-sort, new anchors, k 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | vii | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |
| bread-being-baked-over-an-open-fire | i | iv x | iv x | iv x | vi x | iv x | iv x | iv x | vii x | vii x |
| monks-harvesting-the-shell-tree-by-the-shore | ii | vi x | vi x | vi x | vi x | vi x | ii | ii | ii | ii |
| aged-sheet-with-handwritten-margin-notes | vii | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |
| legibility-of-label-banners-on-a-framed-map | iv | iv | iv | iv | iv | iv | iv | iv | iv | iv |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | vii | v x | v x | v x | v x | v x | v x | v x | v x | v x |
| meal-service-at-a-table-inside-the-initial | i or vii | iv x | iv x | iv x | iv x | iv x | iv x | iv x | iii x | iii x |
| zodiac-chart-plate-i-with-numbered-body-sections | iv | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |
| plate-vii-in-a-numbered-series | iv | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |
| plate-one-with-one-blank-panel | iv | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |
| plate-three-on-ruled-paper | vii | iii x | iii x | iii x | i x | iii x | iii x | iii x | iii x | iii x |
| furniture-being-moved-in-a-row | vii | iii x | iii x | iii x | i x | iii x | iii x | iii x | iii x | iii x |
| listening-station-with-recording-attendants | iii | iv x | iv x | iv x | iv x | iv x | iii | iv x | iii | iii |
| aged-plate-with-blank-caption-panel | i | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |
| reference-chart-with-ten-labelled-roundels | iv | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x | iii x |

`x` marks a book outside the leaf's gold.

The brief's claims:

| claim | leaf | stored | result |
|---|---|---|---|
| the listening chair is in iv | listening-station-with-recording-attendants | iv | confirmed |
| the bathhouse is in v | towel-and-slipper-racks-in-a-bathhouse-changing-room | v | confirmed |
| the barnacle tree is in vi | monks-harvesting-the-shell-tree-by-the-shore | vi | confirmed |
| the bonnacon is in iv | bread-being-baked-over-an-open-fire | iv | confirmed |
| book iii's old anchor says bathhouses | - | - | confirmed |
| about 1 of the 10 unpinned leaves is right (the map) | legibility-of-label-banners-on-a-framed-map | 1 of 10 | confirmed |

## C3. The medium hypothesis

Gold accuracy of each vector source on the leaves, four ways: the filer's LOO choice (database tuning) under old and new anchors; the nearest anchor by cosine alone (no leaves, tags or fill); and the nearest gold centroid built from the other leaves' gold books (no anchors; the two-book leaf builds no centroid; a book with no other gold leaf cannot be chosen, which rules out the leaves whose gold book has no other member). Each cell: right of 15 / right of the 10 unpinned.

| source | LOO old | anchor only old | LOO new | anchor only new | gold centroid |
|---|---|---|---|---|---|
| (a) whole analysis | 1 / 1 | 2 / 2 | 3 / 3 | 5 / 2 | 7 / 2 |
| (b) subject paragraph | 1 / 1 | 1 / 1 | 3 / 3 | 7 / 4 | 6 / 2 |
| (c) subject + setting | 1 / 1 | 2 / 2 | 3 / 3 | 7 / 4 | 6 / 2 |
| (d) subject, extract recipe | 2 / 2 | 3 / 3 | 3 / 3 | 5 / 3 | 4 / 0 |
| (e) style paragraph | 2 / 2 | 1 / 1 | 1 / 1 | 2 / 2 | 5 / 1 |

Share of all 190 live pictures each book takes (leaves by LOO, every other picture against the leaves where they stand), database tuning:

| source | anchors | n | i | ii | iii | iv | v | vi | vii |
|---|---|---|---|---|---|---|---|---|---|
| (a) whole analysis | old | 190 | 34 (18%) | 19 (10%) | 33 (17%) | 19 (10%) | 23 (12%) | 34 (18%) | 28 (15%) |
| (a) whole analysis | new | 190 | 34 (18%) | 17 (9%) | 37 (19%) | 24 (13%) | 41 (22%) | 17 (9%) | 20 (11%) |
| (b) subject paragraph | old | 190 | 39 (21%) | 31 (16%) | 28 (15%) | 20 (11%) | 21 (11%) | 26 (14%) | 25 (13%) |
| (b) subject paragraph | new | 190 | 36 (19%) | 16 (8%) | 38 (20%) | 29 (15%) | 31 (16%) | 21 (11%) | 19 (10%) |
| (c) subject + setting | old | 190 | 37 (19%) | 28 (15%) | 27 (14%) | 21 (11%) | 26 (14%) | 30 (16%) | 21 (11%) |
| (c) subject + setting | new | 190 | 36 (19%) | 23 (12%) | 34 (18%) | 22 (12%) | 36 (19%) | 18 (9%) | 21 (11%) |
| (d) subject, extract recipe | old | 190 | 29 (15%) | 32 (17%) | 34 (18%) | 22 (12%) | 22 (12%) | 27 (14%) | 24 (13%) |
| (d) subject, extract recipe | new | 190 | 31 (16%) | 19 (10%) | 38 (20%) | 29 (15%) | 35 (18%) | 19 (10%) | 19 (10%) |
| (e) style paragraph | old | 190 | 28 (15%) | 21 (11%) | 45 (24%) | 25 (13%) | 17 (9%) | 28 (15%) | 26 (14%) |
| (e) style paragraph | new | 190 | 37 (19%) | 13 (7%) | 59 (31%) | 33 (17%) | 20 (11%) | 13 (7%) | 15 (8%) |

Medium of each leaf, from its Style paragraph: the first rule that matches wins.
Rules in order: mosaic: mosaic, tesserae; painted manuscript: illuminated, miniature, gouache, tempera; print: woodcut, engraving, letterpress, risograph, printed; drawing: pencil, pen linework, pen and ink. Check these by eye.

| leaf | medium | matched | Style paragraph, first sentence |
|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | print | woodcut | Printed illustration imitating woodcut or risograph work, in two inks, red-orange and navy blue, on a peach-cream ground |
| bread-being-baked-over-an-open-fire | painted manuscript | illuminated | Imitation of a medieval illuminated manuscript |
| monks-harvesting-the-shell-tree-by-the-shore | mosaic | mosaic | Imitation of a Byzantine style mosaic, built from small tesserae with visible grout lines and cracked gold leaf texture |
| aged-sheet-with-handwritten-margin-notes | print | woodcut | Woodcut or engraving imitation in black line with dense hatching, cross-hatching and stippling |
| legibility-of-label-banners-on-a-framed-map | painted manuscript | illuminated | Illuminated manuscript imitation with gold leaf, lapis blue and red |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | painted manuscript | miniature | Illustration in the manner of a Persian miniature, with flat colour, fine ink outlines and gold leaf detail |
| meal-service-at-a-table-inside-the-initial | painted manuscript | illuminated | Imitation of an illuminated manuscript in gouache and gold leaf |
| zodiac-chart-plate-i-with-numbered-body-sections | print | woodcut | Woodcut-style engraving in black line on aged paper texture |
| plate-vii-in-a-numbered-series | print | woodcut | Imitation of a woodcut or engraving print |
| plate-one-with-one-blank-panel | print | woodcut | Imitation woodcut or letterpress print in black line on mottled grey-beige paper |
| plate-three-on-ruled-paper | drawing | pencil | Hand-drawn illustration in fine pen linework with soft colored pencil or watercolor fills |
| furniture-being-moved-in-a-row | drawing | pencil | Fine pencil and colored pencil drawing on aged graph paper, with foxing spots and slightly worn edges |
| listening-station-with-recording-attendants | painted manuscript | illuminated | Imitation of a medieval illuminated manuscript, painted in flat tempera-like color with gold leaf accents and fine ink outlines |
| aged-plate-with-blank-caption-panel | print | printed | Illustration made to look like an old printed plate hand colored in ink and watercolor |
| reference-chart-with-ten-labelled-roundels | print | woodcut | Hand-colored woodcut or engraving imitation with black contour lines and hatching |

Spearman rank correlation between the pairwise cosine of two leaves (each source centered on its own mean) and an indicator that the pair shares a gold book, a medium, or a stored book. Pairs share leaves, so they are not independent; no p-values are given. `same` and `diff` are the mean cosine of pairs with and without the shared property.

| source | indicator | pairs | pairs sharing | rho | same | diff |
|---|---|---|---|---|---|---|
| (a) whole analysis | same gold book | 105 | 28 | 0.134 | 0.2380 | 0.1790 |
| (a) whole analysis | same medium | 105 | 32 | 0.568 | 0.3300 | 0.1354 |
| (a) whole analysis | same stored book | 105 | 42 | 0.650 | 0.3144 | 0.1150 |
| (b) subject paragraph | same gold book | 105 | 28 | 0.138 | 0.1728 | 0.1181 |
| (b) subject paragraph | same medium | 105 | 32 | 0.495 | 0.2471 | 0.0826 |
| (b) subject paragraph | same stored book | 105 | 42 | 0.588 | 0.2403 | 0.0609 |
| (c) subject + setting | same gold book | 105 | 28 | 0.118 | 0.1948 | 0.1454 |
| (c) subject + setting | same medium | 105 | 32 | 0.514 | 0.2801 | 0.1053 |
| (c) subject + setting | same stored book | 105 | 42 | 0.670 | 0.2861 | 0.0735 |
| (d) subject, extract recipe | same gold book | 105 | 28 | 0.013 | 0.1149 | 0.1009 |
| (d) subject, extract recipe | same medium | 105 | 32 | 0.498 | 0.2130 | 0.0571 |
| (d) subject, extract recipe | same stored book | 105 | 42 | 0.649 | 0.2123 | 0.0329 |
| (e) style paragraph | same gold book | 105 | 28 | 0.058 | 0.2370 | 0.2115 |
| (e) style paragraph | same medium | 105 | 32 | 0.507 | 0.3509 | 0.1602 |
| (e) style paragraph | same stored book | 105 | 42 | 0.266 | 0.2787 | 0.1780 |

## C3b. Medium words in Subject and Setting

Words looked for, as whole words with plurals, case ignored: mosaic, tesserae, illuminated, miniature, gouache, tempera, woodcut, engraving, letterpress, risograph, printed, pencil, pen linework, pen and ink, plate, print, manuscript, parchment, page, chart, sheet, drawing. Some of them also name things a picture can show (a dinner plate, a sheet of cloth, a chart on a wall); the list counts the word, not the sense.

| leaf | medium | in Subject | in Setting |
|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | print | - | page |
| bread-being-baked-over-an-open-fire | painted manuscript | - | parchment |
| monks-harvesting-the-shell-tree-by-the-shore | mosaic | - | - |
| aged-sheet-with-handwritten-margin-notes | print | - | sheet |
| legibility-of-label-banners-on-a-framed-map | painted manuscript | manuscript | parchment |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | painted manuscript | - | - |
| meal-service-at-a-table-inside-the-initial | painted manuscript | plate, manuscript, page, sheet | parchment |
| zodiac-chart-plate-i-with-numbered-body-sections | print | - | page |
| plate-vii-in-a-numbered-series | print | plate, drawing | printed, page |
| plate-one-with-one-blank-panel | print | - | - |
| plate-three-on-ruled-paper | drawing | printed | sheet |
| furniture-being-moved-in-a-row | drawing | - | printed |
| listening-station-with-recording-attendants | painted manuscript | plate | - |
| aged-plate-with-blank-caption-panel | print | - | printed |
| reference-chart-with-ten-labelled-roundels | print | - | parchment, sheet |

Across the 190 live pictures whose analysis splits: Subject paragraphs with at least one of the words 62; Setting 44; Style 81.

| word | Subject | Setting | Style |
|---|---|---|---|
| mosaic | 0 | 0 | 2 |
| tesserae | 0 | 0 | 1 |
| illuminated | 0 | 0 | 4 |
| miniature | 2 | 0 | 2 |
| gouache | 0 | 0 | 1 |
| tempera | 0 | 0 | 1 |
| woodcut | 0 | 0 | 6 |
| engraving | 0 | 0 | 7 |
| letterpress | 0 | 0 | 1 |
| risograph | 0 | 0 | 1 |
| printed | 7 | 14 | 7 |
| pencil | 6 | 0 | 26 |
| pen linework | 0 | 0 | 1 |
| pen and ink | 0 | 0 | 21 |
| plate | 27 | 8 | 5 |
| print | 1 | 4 | 6 |
| manuscript | 2 | 0 | 6 |
| parchment | 3 | 9 | 5 |
| page | 3 | 8 | 3 |
| chart | 9 | 3 | 3 |
| sheet | 11 | 7 | 1 |
| drawing | 9 | 5 | 48 |

Spearman of pairwise cosine against `same gold book`, within pairs that share a medium and within pairs that do not. Very small samples; no p-values.

| source | pairs | n | same gold | rho | same | diff |
|---|---|---|---|---|---|---|
| (a) whole analysis | pairs sharing a medium | 32 | 10 | 0.358 | 0.3998 | 0.2982 |
| (a) whole analysis | pairs not sharing | 73 | 18 | 0.056 | 0.1481 | 0.1313 |
| (b) subject paragraph | pairs sharing a medium | 32 | 10 | 0.336 | 0.3184 | 0.2147 |
| (b) subject paragraph | pairs not sharing | 73 | 18 | 0.036 | 0.0920 | 0.0795 |

## C4. Ablation grid

192 cells: source (a to d) x anchor_k 3, 6, 10, 20 x tag_alpha 0, 0.05, 0.15 x centering on/off x old/new anchors; fill_beta 0.05 and book_margin 0.03 from the database. Every cell in `c4_grid.csv`.

- `loo all`: LOO accuracy on the 15 gold leaves. `loo unp`: the same on the 10 unpinned. `margin`: mean LOO margin, chosen minus runner-up, over the 15. `flip`: share of the 15 whose LOO book differs from the stored book. `resort unp`: accuracy of `assign_books` on the unpinned (pins stay). `resort flip`: share of the unpinned it moves. `seq unp`: mean accuracy on the unpinned over the 50 sequential refiling orders, pins honoured.

Top 15 cells by `loo all`, then `seq unp`, then `margin`:

| source | anchors | centering | k | alpha | loo all | loo unp | margin | flip | resort unp | resort flip | seq unp |
|---|---|---|---|---|---|---|---|---|---|---|---|
| b | new | on | 20 | 0.15 | 47% | 40% | 0.0674 | 80% | 40% | 70% | 41% |
| b | new | on | 20 | 0.05 | 47% | 40% | 0.0655 | 80% | 30% | 60% | 41% |
| b | new | on | 20 | 0 | 47% | 40% | 0.0645 | 80% | 30% | 60% | 41% |
| c | new | on | 20 | 0.15 | 47% | 40% | 0.0725 | 73% | 40% | 60% | 40% |
| c | new | on | 20 | 0.05 | 47% | 40% | 0.0706 | 73% | 40% | 60% | 40% |
| c | new | on | 20 | 0 | 47% | 40% | 0.0696 | 73% | 40% | 60% | 40% |
| b | new | on | 10 | 0.15 | 40% | 40% | 0.0511 | 67% | 20% | 40% | 42% |
| b | new | on | 10 | 0.05 | 40% | 40% | 0.0505 | 67% | 20% | 40% | 42% |
| b | new | on | 10 | 0 | 40% | 40% | 0.0502 | 67% | 20% | 40% | 42% |
| c | new | on | 10 | 0.15 | 33% | 40% | 0.0795 | 40% | 40% | 40% | 41% |
| c | new | on | 10 | 0.05 | 33% | 40% | 0.0787 | 40% | 40% | 40% | 41% |
| c | new | on | 10 | 0 | 33% | 40% | 0.0783 | 40% | 40% | 40% | 41% |
| b | new | on | 6 | 0.15 | 27% | 30% | 0.0770 | 40% | 20% | 30% | 43% |
| d | new | on | 20 | 0.15 | 27% | 20% | 0.0548 | 80% | 30% | 70% | 29% |
| d | new | on | 20 | 0.05 | 27% | 20% | 0.0528 | 80% | 30% | 70% | 29% |

The cells at today's settings (source a, centered, database k and alpha):

| anchors | loo all | loo unp | margin | flip | resort unp | resort flip | seq unp |
|---|---|---|---|---|---|---|---|
| old | 7% | 10% | 0.1787 | 0% | 10% | 0% | 11% |
| new | 20% | 30% | 0.1826 | 13% | 20% | 10% | 24% |

Marginal effect of each factor: the mean of each metric over every cell at that level, so every combination of the other factors is averaged in.

| factor | level | cells | loo all | loo unp | margin | flip | resort unp | resort flip | seq unp |
|---|---|---|---|---|---|---|---|---|---|
| source | a | 48 | 13% | 19% | 0.1170 | 28% | 15% | 40% | 21% |
| source | b | 48 | 16% | 21% | 0.0904 | 38% | 14% | 43% | 24% |
| source | c | 48 | 16% | 22% | 0.1067 | 32% | 19% | 44% | 26% |
| source | d | 48 | 13% | 18% | 0.0894 | 46% | 17% | 55% | 23% |
| anchors | new | 96 | 19% | 24% | 0.0999 | 41% | 21% | 52% | 28% |
| anchors | old | 96 | 11% | 16% | 0.1018 | 31% | 12% | 40% | 19% |
| centering | off | 96 | 12% | 17% | 0.1034 | 36% | 12% | 59% | 22% |
| centering | on | 96 | 18% | 22% | 0.0983 | 36% | 21% | 33% | 25% |
| anchor_k | 3 | 48 | 13% | 19% | 0.1295 | 26% | 14% | 39% | 20% |
| anchor_k | 6 | 48 | 12% | 17% | 0.1179 | 32% | 15% | 45% | 22% |
| anchor_k | 10 | 48 | 14% | 19% | 0.0936 | 39% | 15% | 47% | 24% |
| anchor_k | 20 | 48 | 20% | 24% | 0.0624 | 48% | 21% | 51% | 28% |
| tag_alpha | 0 | 64 | 15% | 20% | 0.1008 | 36% | 17% | 46% | 24% |
| tag_alpha | 0.05 | 64 | 15% | 20% | 0.1008 | 36% | 17% | 46% | 24% |
| tag_alpha | 0.15 | 64 | 15% | 20% | 0.1010 | 36% | 16% | 45% | 23% |

## C5. Order sensitivity and attractors

Each of the 50 orders refiles the 15 leaves from empty books, one at a time, each through `choose_book` against those filed before it (the filing job's path). Source (a), centered, database tuning except anchor_k. With pins honoured a book-pinned leaf takes its owner's book when its turn comes, as when the owner placed it; with pins ignored the filer files it too.

| setting | leaves landing in more than one book | most books for one leaf | mean right per order (leaves the filer files) |
|---|---|---|---|
| old anchors, k 3, pins honoured | 9 | 6 | 1.14 of 10 |
| old anchors, k 3, pins ignored | 14 | 6 | 1.70 of 15 |
| old anchors, k 10, pins honoured | 7 | 4 | 1.80 of 10 |
| old anchors, k 10, pins ignored | 8 | 4 | 1.96 of 15 |
| new anchors, k 3, pins honoured | 8 | 7 | 2.40 of 10 |
| new anchors, k 3, pins ignored | 13 | 7 | 4.58 of 15 |
| new anchors, k 10, pins honoured | 6 | 5 | 2.94 of 10 |
| new anchors, k 10, pins ignored | 10 | 5 | 5.70 of 15 |

Per leaf, pins honoured: how often it lands in each book over the orders.

| leaf | gold | stored | old, k 3 | old, k 10 | new, k 3 | new, k 10 |
|---|---|---|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | vii | iii | iii 30, i 20 | i 36, iii 14 | iii 37, i 5, iv 5, ii 2, vii 1 | iii 24, iv 20, i 6 |
| bread-being-baked-over-an-open-fire | i | iv | vi 34, iv 7, ii 5, iii 2, i 1, v 1 | vi 50 | vii 26, iii 10, iv 4, vi 4, v 3, ii 2, i 1 | vii 50 |
| monks-harvesting-the-shell-tree-by-the-shore | ii | vi | vi 50 | vi 50 | ii 50 | ii 50 |
| aged-sheet-with-handwritten-margin-notes | vii | iii | i 27, iii 22, iv 1 | i 47, iii 3 | iii 27, i 12, iv 6, ii 2, vi 2, vii 1 | iii 20, i 15, iv 15 |
| legibility-of-label-banners-on-a-framed-map | iv | iv | iv 32, vi 15, i 1, ii 1, iii 1 | iv 50 | iv 24, iii 17, ii 3, vi 3, vii 2, i 1 | iv 50 |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | vii | v | v 43, vi 4, iii 3 | v 49, iii 1 | v 50 | v 50 |
| meal-service-at-a-table-inside-the-initial | i or vii | iv | vi 24, iv 17, ii 5, iii 2, i 1, v 1 | vi 45, iv 5 | iii 29, iv 6, vii 6, vi 4, ii 3, i 1, v 1 | iii 20, vi 14, iv 8, v 8 |
| zodiac-chart-plate-i-with-numbered-body-sections (pinned) | iv | iii | iii 50 | iii 50 | iii 50 | iii 50 |
| plate-vii-in-a-numbered-series (pinned) | iv | iii | iii 50 | iii 50 | iii 50 | iii 50 |
| plate-one-with-one-blank-panel (pinned) | iv | iii | iii 50 | iii 50 | iii 50 | iii 50 |
| plate-three-on-ruled-paper (pinned) | vii | iii | iii 50 | iii 50 | iii 50 | iii 50 |
| furniture-being-moved-in-a-row (pinned) | vii | iii | iii 50 | iii 50 | iii 50 | iii 50 |
| listening-station-with-recording-attendants | iii | iv | vi 24, iv 14, ii 7, iii 3, i 1, v 1 | vi 38, ii 8, iv 3, v 1 | iii 29, iv 5, vii 5, v 4, ii 3, vi 3, i 1 | iii 42, v 8 |
| aged-plate-with-blank-caption-panel | i | iii | iii 27, i 20, ii 2, iv 1 | i 40, iii 7, ii 3 | iii 38, i 4, ii 3, iv 3, vi 1, vii 1 | iii 30, ii 13, i 4, v 2, iv 1 |
| reference-chart-with-ten-labelled-roundels | iv | iii | iii 34, i 16 | iii 34, i 16 | iii 39, i 3, iv 3, ii 2, vi 2, vii 1 | iii 49, iv 1 |

Historical order replayed (old anchors, database tuning, today's corpus mean; owner placements kept): 15 of 15 leaves land where they were first filed.

| # | leaf | first filed | by | replay |
|---|---|---|---|---|
| 1 | illustrated-chart-with-ten-framed-panels | i | auto | i |
| 2 | monks-harvesting-the-shell-tree-by-the-shore | vi | auto | vi |
| 3 | bread-being-baked-over-an-open-fire | vi | auto | vi |
| 4 | aged-sheet-with-handwritten-margin-notes | i | auto | i |
| 5 | legibility-of-label-banners-on-a-framed-map | iv | auto | iv |
| 6 | towel-and-slipper-racks-in-a-bathhouse-changing-room | v | auto | v |
| 7 | meal-service-at-a-table-inside-the-initial | iv | auto | iv |
| 8 | zodiac-chart-plate-i-with-numbered-body-sections | iii | owner | iii |
| 9 | plate-vii-in-a-numbered-series | iii | owner | iii |
| 10 | plate-one-with-one-blank-panel | iii | owner | iii |
| 11 | plate-three-on-ruled-paper | iii | owner | iii |
| 12 | furniture-being-moved-in-a-row | iii | owner | iii |
| 13 | listening-station-with-recording-attendants | iv | auto | iv |
| 14 | aged-plate-with-blank-caption-panel | iii | auto | iii |
| 15 | reference-chart-with-ten-labelled-roundels | iii | auto | iii |

Attractors: each of the 175 live pictures that is not a leaf scored by `choose_book` against the leaves where they stand (nothing filed). A book is flagged when its share is at least 0.286 (twice 1/7).

| setting | n | i | ii | iii | iv | v | vi | vii | flagged |
|---|---|---|---|---|---|---|---|---|---|
| old, k 3 | 175 | 34 (19%) | 19 (11%) | 24 (14%) | 15 (9%) | 22 (13%) | 33 (19%) | 28 (16%) | none |
| old, k 10 | 175 | 36 (21%) | 15 (9%) | 23 (13%) | 21 (12%) | 22 (13%) | 32 (18%) | 26 (15%) | none |
| new, k 3 | 175 | 34 (19%) | 16 (9%) | 27 (15%) | 21 (12%) | 40 (23%) | 17 (10%) | 20 (11%) | none |
| new, k 10 | 175 | 36 (21%) | 17 (10%) | 25 (14%) | 19 (11%) | 37 (21%) | 19 (11%) | 22 (13%) | none |

## C5b. Pins at gold (counterfactual)

The book-pinned leaves moved to their gold books and still book-pinned there (zodiac-chart-plate-i-with-numbered-body-sections to iv, plate-vii-in-a-numbered-series to iv, plate-one-with-one-blank-panel to iv, plate-three-on-ruled-paper to vii, furniture-being-moved-in-a-row to vii); every other leaf as stored to start with. Centering on, database tag_alpha, fill_beta and book_margin. anchor_k 3, 6, 10, 20, 40 (LIMITS allows up to 1000). Accuracy is on the 10 unpinned leaves: `re-sort` by `assign_books`, `LOO` against the counterfactual state, `seq` the mean over the same 50 orders, pinned leaves taking their gold book.

| source | anchors | k | re-sort | LOO | seq | margin |
|---|---|---|---|---|---|---|
| (a) whole analysis | old | 3 | 20% | 20% | 20% | 0.0833 |
| (a) whole analysis | old | 6 | 20% | 20% | 22% | 0.0813 |
| (a) whole analysis | old | 10 | 20% | 20% | 21% | 0.0638 |
| (a) whole analysis | old | 20 | 20% | 20% | 20% | 0.0584 |
| (a) whole analysis | old | 40 | 20% | 20% | 20% | 0.0613 |
| (a) whole analysis | new | 3 | 30% | 30% | 29% | 0.0794 |
| (a) whole analysis | new | 6 | 30% | 40% | 29% | 0.0836 |
| (a) whole analysis | new | 10 | 30% | 40% | 29% | 0.0793 |
| (a) whole analysis | new | 20 | 40% | 30% | 23% | 0.0690 |
| (a) whole analysis | new | 40 | 30% | 30% | 20% | 0.0600 |
| (b) subject paragraph | old | 3 | 20% | 10% | 21% | 0.0488 |
| (b) subject paragraph | old | 6 | 30% | 30% | 18% | 0.0514 |
| (b) subject paragraph | old | 10 | 30% | 30% | 18% | 0.0527 |
| (b) subject paragraph | old | 20 | 30% | 20% | 13% | 0.0476 |
| (b) subject paragraph | old | 40 | 10% | 10% | 12% | 0.0466 |
| (b) subject paragraph | new | 3 | 30% | 40% | 38% | 0.0715 |
| (b) subject paragraph | new | 6 | 30% | 40% | 46% | 0.0911 |
| (b) subject paragraph | new | 10 | 30% | 40% | 48% | 0.0886 |
| (b) subject paragraph | new | 20 | 40% | 50% | 46% | 0.0763 |
| (b) subject paragraph | new | 40 | 40% | 40% | 42% | 0.0674 |
| (c) subject + setting | old | 3 | 20% | 30% | 24% | 0.0605 |
| (c) subject + setting | old | 6 | 30% | 30% | 25% | 0.0568 |
| (c) subject + setting | old | 10 | 30% | 30% | 23% | 0.0503 |
| (c) subject + setting | old | 20 | 20% | 20% | 20% | 0.0498 |
| (c) subject + setting | old | 40 | 20% | 20% | 20% | 0.0523 |
| (c) subject + setting | new | 3 | 30% | 40% | 40% | 0.0939 |
| (c) subject + setting | new | 6 | 30% | 40% | 42% | 0.1039 |
| (c) subject + setting | new | 10 | 40% | 50% | 42% | 0.0944 |
| (c) subject + setting | new | 20 | 50% | 50% | 40% | 0.0863 |
| (c) subject + setting | new | 40 | 40% | 40% | 40% | 0.0797 |
| (d) subject, extract recipe | old | 3 | 30% | 40% | 32% | 0.0513 |
| (d) subject, extract recipe | old | 6 | 40% | 40% | 33% | 0.0499 |
| (d) subject, extract recipe | old | 10 | 30% | 30% | 30% | 0.0560 |
| (d) subject, extract recipe | old | 20 | 30% | 30% | 30% | 0.0560 |
| (d) subject, extract recipe | old | 40 | 30% | 30% | 30% | 0.0547 |
| (d) subject, extract recipe | new | 3 | 20% | 40% | 28% | 0.0495 |
| (d) subject, extract recipe | new | 6 | 40% | 40% | 25% | 0.0577 |
| (d) subject, extract recipe | new | 10 | 40% | 30% | 26% | 0.0567 |
| (d) subject, extract recipe | new | 20 | 40% | 30% | 30% | 0.0532 |
| (d) subject, extract recipe | new | 40 | 40% | 30% | 30% | 0.0490 |

Per leaf, new anchors, sources (a) and (c); (c) is the one of (b) and (c) with the higher mean of the three accuracies over its new-anchor cells (b 40%, c 41%). Each cell: re-sort book / LOO book / the book it lands in most often over the orders (how often).

| leaf | gold | stored | (a) k 3 | (a) k 10 | (a) k 20 | (c) k 3 | (c) k 10 | (c) k 20 |
|---|---|---|---|---|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | vii | iii | iv x / iv x / iv x (34) | iv x / iv x / iv x (46) | iv x / iv x / iv x (47) | iv x / iv x / iv x (40) | iv x / iv x / iv x (50) | iv x / iv x / iv x (50) |
| bread-being-baked-over-an-open-fire | i | iv | iv x / iv x / vii x (31) | iv x / vii x / vii x (48) | vii x / vii x / vii x (50) | vi x / vi x / vii x (50) | vi x / vi x / vii x (50) | vii x / vii x / vii x (50) |
| monks-harvesting-the-shell-tree-by-the-shore | ii | vi | ii / ii / ii (50) | ii / ii / ii (50) | ii / ii / ii (50) | ii / ii / ii (50) | ii / ii / ii (50) | ii / ii / ii (50) |
| aged-sheet-with-handwritten-margin-notes | vii | iii | iv x / iv x / iv x (27) | iv x / iv x / iv x (34) | iv x / iv x / i x (26) | iv x / iv x / iv x (38) | iv x / iv x / iv x (43) | iv x / iv x / iv x (48) |
| legibility-of-label-banners-on-a-framed-map | iv | iv | iv / iv / iv (38) | iv / iv / iv (50) | iv / iv / iv (50) | iv / iv / iv (50) | iv / iv / iv (50) | iv / iv / iv (50) |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | vii | v | v x / v x / v x (50) | v x / v x / v x (50) | v x / v x / v x (50) | v x / v x / v x (50) | v x / v x / v x (50) | v x / v x / v x (50) |
| meal-service-at-a-table-inside-the-initial | i or vii | iv | iv x / iv x / iv x (30) | iv x / iv x / iv x (27) | iv x / iv x / vi x (26) | iv x / iv x / vii (29) | iv x / vii / vii (50) | vii / vii / vii (50) |
| listening-station-with-recording-attendants | iii | iv | iv x / iv x / iv x (20) | iv x / iii / v x (21) | iii / iii / v x (39) | iv x / iii / vii x (18) | iii / iii / iii (19) | iii / iii / v x (26) |
| aged-plate-with-blank-caption-panel | i | iii | iv x / iv x / iv x (32) | iv x / iv x / iv x (24) | iv x / iv x / ii x (45) | iv x / iv x / iv x (35) | iv x / iv x / ii x (20) | ii x / ii x / ii x (50) |
| reference-chart-with-ten-labelled-roundels | iv | iii | iv / iv / iv (31) | iv / iv / iv (26) | iv / iii x / iii x (46) | iv / iv / iv (40) | iv / iv / iv (33) | iv / iv / vii x (26) |

`x` marks a book outside the leaf's gold.

Upper bound: every leaf placed in its gold book, then each leaf's LOO choice against the rest (pinned leaves included in the count). The two-book leaf is placed in the book of its two it scores higher in, against the single-label leaves in their gold books; that book is named per row.

| source | anchors | all, k 3 | unpinned, k 3 | two-book leaf, k 3 | all, k 10 | unpinned, k 10 | two-book leaf, k 10 |
|---|---|---|---|---|---|---|---|
| (a) whole analysis | old | 6 of 15 | 3 of 10 | meal-service-at-a-table-inside-the-initial in i | 4 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in i |
| (a) whole analysis | new | 6 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in vii | 5 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in vii |
| (b) subject paragraph | old | 4 of 15 | 1 of 10 | meal-service-at-a-table-inside-the-initial in i | 2 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in i |
| (b) subject paragraph | new | 5 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in vii | 7 of 15 | 4 of 10 | meal-service-at-a-table-inside-the-initial in vii |
| (c) subject + setting | old | 4 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in i | 2 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in i |
| (c) subject + setting | new | 6 of 15 | 3 of 10 | meal-service-at-a-table-inside-the-initial in vii | 6 of 15 | 3 of 10 | meal-service-at-a-table-inside-the-initial in vii |
| (d) subject, extract recipe | old | 4 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in i | 4 of 15 | 3 of 10 | meal-service-at-a-table-inside-the-initial in i |
| (d) subject, extract recipe | new | 4 of 15 | 1 of 10 | meal-service-at-a-table-inside-the-initial in vii | 5 of 15 | 2 of 10 | meal-service-at-a-table-inside-the-initial in vii |

## C5c. Tag hints with hyphens read as spaces (simulated)

A simulated filer change: the plan functions are handed each picture's tags with hyphens turned into spaces, so `tag_overlap` can match `zodiac` inside `zodiac-man`. `plan.py` does not do this today.

| hints | tags | leaves matching any book | leaves matching a gold book |
|---|---|---|---|
| old | as stored | 7 of 15 | 0 of 15 |
| old | hyphens as spaces | 12 of 15 | 1 of 15 |
| new | as stored | 4 of 15 | 4 of 15 |
| new | hyphens as spaces | 14 of 15 | 9 of 15 |

Gold accuracy, new anchors, centered. `LOO`: of the 15 and of the unpinned. `re-sort`: unpinned, pins as stored. `pins at gold`: unpinned, re-sort with the book-pinned leaves in their gold books (C5b). tag_alpha up to 1, the LIMITS maximum.

| source | k | alpha | tags | LOO all / unp | re-sort | pins at gold |
|---|---|---|---|---|---|---|
| (a) | 3 | 0 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 0 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 0.05 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 0.05 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 0.15 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 0.15 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 1 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 3 | 1 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (a) | 10 | 0 | as stored | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 0 | hyphens as spaces | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 0.05 | as stored | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 0.05 | hyphens as spaces | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 0.15 | as stored | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 0.15 | hyphens as spaces | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 1 | as stored | 3 / 3 | 3 of 10 | 3 of 10 |
| (a) | 10 | 1 | hyphens as spaces | 3 / 3 | 3 of 10 | 3 of 10 |
| (b) | 3 | 0 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (b) | 3 | 0 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (b) | 3 | 0.05 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (b) | 3 | 0.05 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (b) | 3 | 0.15 | as stored | 3 / 3 | 2 of 10 | 3 of 10 |
| (b) | 3 | 0.15 | hyphens as spaces | 3 / 3 | 2 of 10 | 3 of 10 |
| (b) | 3 | 1 | as stored | 3 / 3 | 3 of 10 | 4 of 10 |
| (b) | 3 | 1 | hyphens as spaces | 2 / 2 | 3 of 10 | 4 of 10 |
| (b) | 10 | 0 | as stored | 6 / 4 | 2 of 10 | 3 of 10 |
| (b) | 10 | 0 | hyphens as spaces | 6 / 4 | 2 of 10 | 3 of 10 |
| (b) | 10 | 0.05 | as stored | 6 / 4 | 2 of 10 | 3 of 10 |
| (b) | 10 | 0.05 | hyphens as spaces | 6 / 4 | 3 of 10 | 3 of 10 |
| (b) | 10 | 0.15 | as stored | 6 / 4 | 2 of 10 | 4 of 10 |
| (b) | 10 | 0.15 | hyphens as spaces | 6 / 4 | 3 of 10 | 5 of 10 |
| (b) | 10 | 1 | as stored | 7 / 4 | 3 of 10 | 4 of 10 |
| (b) | 10 | 1 | hyphens as spaces | 7 / 4 | 4 of 10 | 5 of 10 |

## C6. Conservator notes

Leaves with a note: 15 of 15 (owner 15).
`collection_note` rows in the jobs table, by status: none.

## Before and after: a full re-sort under the new anchors (never applied)

`plan_resort` on source (a), centered, new anchors and tag hints, database tuning, then the same with anchor_k 10. Book-pinned leaves keep their book and pinned leaves their slot, as in production. `margin`: the proposed book's score minus the best other book, against the plan's final state with the leaf left out. `gain`: the proposed book's score minus the current book's, same state.

### anchor_k 3

1 of 15 leaves change book. Gold right: 1 of 15 now, 2 of 15 after.

| leaf | now | proposed | margin | gain | book_pinned | pinned | gold | proposed vs gold |
|---|---|---|---|---|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | iii 44 | iii 44 | 0.2247 | 0.0000 |  |  | vii | wrong |
| bread-being-baked-over-an-open-fire | iv 77 | iv 77 | 0.1195 | 0.0000 |  |  | i | wrong |
| monks-harvesting-the-shell-tree-by-the-shore | vi 1 | ii 1 * | 0.0909 | 0.1367 |  |  | ii | right |
| aged-sheet-with-handwritten-margin-notes | iii 32 | iii 32 | 0.1350 | 0.0000 |  |  | vii | wrong |
| legibility-of-label-banners-on-a-framed-map | iv 1 | iv 1 | 0.1195 | 0.0000 |  |  | iv | right |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | v 1 | v 1 | 0.0919 | 0.0000 |  |  | vii | wrong |
| meal-service-at-a-table-inside-the-initial | iv 24 | iv 24 | 0.0826 | 0.0000 |  |  | i or vii | wrong |
| zodiac-chart-plate-i-with-numbered-body-sections | iii 16 | iii 16 | 0.4000 | 0.0000 | yes |  | iv | wrong |
| plate-vii-in-a-numbered-series | iii 9 | iii 9 | 0.2874 | 0.0000 | yes |  | iv | wrong |
| plate-one-with-one-blank-panel | iii 1 | iii 1 | 0.2413 | 0.0000 | yes |  | iv | wrong |
| plate-three-on-ruled-paper | iii 66 | iii 66 | 0.2653 | 0.0000 | yes |  | vii | wrong |
| furniture-being-moved-in-a-row | iii 77 | iii 77 | 0.1744 | 0.0000 | yes |  | vii | wrong |
| listening-station-with-recording-attendants | iv 47 | iv 47 | -0.0072 | 0.0000 |  |  | iii | wrong |
| aged-plate-with-blank-caption-panel | iii 56 | iii 56 | 0.2002 | 0.0000 |  |  | i | wrong |
| reference-chart-with-ten-labelled-roundels | iii 24 | iii 24 | 0.3195 | 0.0000 |  |  | iv | wrong |

`*` marks a change of book.

### anchor_k 10

4 of 15 leaves change book. Gold right: 1 of 15 now, 3 of 15 after.

| leaf | now | proposed | margin | gain | book_pinned | pinned | gold | proposed vs gold |
|---|---|---|---|---|---|---|---|---|
| illustrated-chart-with-ten-framed-panels | iii 44 | iii 20 | 0.1528 | 0.0000 |  |  | vii | wrong |
| bread-being-baked-over-an-open-fire | iv 77 | vii 1 * | 0.0261 | 0.0585 |  |  | i | wrong |
| monks-harvesting-the-shell-tree-by-the-shore | vi 1 | ii 1 * | 0.1163 | 0.1367 |  |  | ii | right |
| aged-sheet-with-handwritten-margin-notes | iii 32 | iii 34 | 0.1460 | 0.0000 |  |  | vii | wrong |
| legibility-of-label-banners-on-a-framed-map | iv 1 | iv 1 | -0.0096 | 0.0000 |  |  | iv | right |
| towel-and-slipper-racks-in-a-bathhouse-changing-room | v 1 | v 1 | 0.0799 | 0.0000 |  |  | vii | wrong |
| meal-service-at-a-table-inside-the-initial | iv 24 | iii 43 * | 0.1044 | 0.1118 |  |  | i or vii | wrong |
| zodiac-chart-plate-i-with-numbered-body-sections | iii 16 | iii 13 | 0.2254 | 0.0000 | yes |  | iv | wrong |
| plate-vii-in-a-numbered-series | iii 9 | iii 8 | 0.1533 | 0.0000 | yes |  | iv | wrong |
| plate-one-with-one-blank-panel | iii 1 | iii 1 | 0.1817 | 0.0000 | yes |  | iv | wrong |
| plate-three-on-ruled-paper | iii 66 | iii 69 | 0.0613 | 0.0000 | yes |  | vii | wrong |
| furniture-being-moved-in-a-row | iii 77 | iii 77 | 0.0589 | 0.0000 | yes |  | vii | wrong |
| listening-station-with-recording-attendants | iv 47 | iii 50 * | 0.1249 | 0.2239 |  |  | iii | right |
| aged-plate-with-blank-caption-panel | iii 56 | iii 60 | 0.1465 | 0.0000 |  |  | i | wrong |
| reference-chart-with-ten-labelled-roundels | iii 24 | iii 28 | 0.3169 | 0.0000 |  |  | iv | wrong |

`*` marks a change of book.
