# Content hygiene (brief section F4)

Checked 2026-11-21 against 190 live pictures. Pictures, captions, descriptions, tags, aliases and comments come from the live admin api (read-only token). Embeddings and `corpus_stats` come from the local read-only database copy of 2026-11-20 (it holds the same 190 live pictures, none unpublished).

## 1. Duplicate

Both pictures are live. Dimensions are the stored width x height; neither has a crop set and both originals match the stored size. Neither has an alias.

| | `crosswalk-use-at-a-city-intersection` | `astronaut-on-roller-skates-carries-a-toaster-holding-a-skele` |
|---|---|---|
| url | https://peni.sh/i/crosswalk-use-at-a-city-intersection | https://peni.sh/i/astronaut-on-roller-skates-carries-a-toaster-holding-a-skele |
| id, status | 128, live | 16, live |
| size | 1024 x 1536 | 1024 x 1331 |
| published | 2026-11-11 23:35 | 2026-11-11 07:01 |
| caption | crosswalk use at a city intersection | crosswalk traffic at a city intersection |
| scene in description | striped crosswalk, toaster on roller skates with an astronaut's body, skeleton and small creature holding mugs, six stripes | same scene, plus a sign reading WHY NOT, a cactus in a police cap, two jellyfish, four to six story buildings |

Cosine similarity on the `description` embedding (Qwen/Qwen3-Embedding-0.6B, database copy):

- centered (corpus mean from `corpus_stats`, n=190, the same centering the site uses for `similar`): 0.859, which matches the audit's 0.858.
- raw (uncentered): 0.927.

Same width and a shorter height on 16 is consistent with it being a vertical crop of 128, but I cannot see the images and 16's description records details (the sign, the jellyfish) that 128's does not, so the crop is likely and not confirmed.

Keep: `crosswalk-use-at-a-city-intersection`, because it is the taller full-height frame and its slug already matches its caption, while the other slug is cut mid-word from an old caption.

Other live pairs above 0.85 (database copy):

- Centered: none. The pair above is the only one.
- Raw, three more, all conference rooms:

| raw cosine | a | b |
|---|---|---|
| 0.868 | `boardroom-table-set-for-a-morning-meeting` (1392 x 1130) | `conference-room-set-up-for-a-krill-presentation` (1774 x 887) |
| 0.866 | `conference-room-set-up-for-a-staff-meeting` (1402 x 1122) | `boardroom-table-set-for-a-morning-meeting` |
| 0.859 | `conference-room-set-up-for-a-staff-meeting` | `conference-room-set-up-for-a-krill-presentation` |

All three differ in dimensions and are above 0.85 only uncentered; they read as a shared subject (an empty conference room), not as the same file.

## 2. Slugs

Mechanism: a slug is the caption lowercased, non-alphanumerics turned to hyphens and cut at 60 characters (`penish/text.py` `slugify`, applied at publish by `_slug_base` and `_free_slug` in `penish/worker/ingest.py`, with `-2`, `-3` suffixes on collision against live slugs and aliases). The 60 cut is character-based, so it can land mid-word. A rename through the admin edit (`_set_slug` in `penish/api/admin_pictures.py`) keeps the old slug as a row in `slug_aliases`, and `/i/<old>` answers with a 301 to the current slug (`_image_page` in `penish/api/index.py`), so a rename keeps the old slug as a permanent redirect.

Found 21 of 190. Every one is a picture whose slug was cut from an earlier caption that described what the picture shows (figures, creatures), while the caption now in place is the clerk's concern. The other 169 slugs equal their current caption cut at 60 (checked mechanically). Ids 1 to 20 (id 9 is not live) account for 19 of them; 103 and 175 are the other two. Source: live api for slugs, captions and aliases. No live picture has an alias (live api `aliases` is empty for all 190; the database copy's `slug_aliases` table is also empty).

Mid-word cuts, clearly: 3 (`sol`), 8 (`para`), 16 (`skele`). Others sit exactly at 60 characters (7, 12, 15, 20) or at 59 (10); whether they end mid-word cannot be told without the old caption.

Proposed slugs are the current caption at a word boundary, 60 characters or fewer; all are shorter than 60, so no cut was needed. Each was checked as unique against every existing live slug and against the other proposals; no aliases exist to collide with. None of the 21 has an alias today.

| id | current slug (https://peni.sh/i/...) | current caption | proposed slug | alias exists |
|---|---|---|---|---|
| 1 | `pig-generals-surround-a-pig-in-bed-speaking-greek` | visitors gathered around a bed in a palace room | `visitors-gathered-around-a-bed-in-a-palace-room` | no |
| 2 | `giant-larva-in-top-hat-and-monocle-in-a-gallery` | gallery visitor behind rope barrier with attendant | `gallery-visitor-behind-rope-barrier-with-attendant` | no |
| 3 | `cartoon-pig-in-armor-on-rocking-horse-leads-marching-pig-sol` | marching formation with route map on a sunny day | `marching-formation-with-route-map-on-a-sunny-day` | no |
| 4 | `horse-in-crown-sits-at-birthday-cake-while-guests-watch` | birthday cake and refreshments in a community hall | `birthday-cake-and-refreshments-in-a-community-hall` | no |
| 5 | `fish-headed-man-pours-tea-for-seated-man-with-flower` | tea being poured in a paneled sitting room | `tea-being-poured-in-a-paneled-sitting-room` | no |
| 6 | `puppet-family-on-a-sofa-holding-a-horseshoe-magnet-baby` | family on the couch with a swaddled newborn | `family-on-the-couch-with-a-swaddled-newborn` | no |
| 7 | `wedding-group-portrait-with-bride-beside-large-pink-creature` | wedding group photo in the reception hall | `wedding-group-photo-in-the-reception-hall` | no |
| 8 | `mock-government-poster-about-identifying-and-caring-for-para` | agriculture department notice on declaration form deadlines | `agriculture-department-notice-on-declaration-form-deadlines` | no |
| 10 | `woman-measures-elongated-father-between-two-armchairs-under` | woman measuring fabric in a living room | `woman-measuring-fabric-in-a-living-room` | no |
| 11 | `seven-men-cut-a-red-ribbon-in-front-of-a-pink-monument` | ribbon cutting ceremony in a convention hall | `ribbon-cutting-ceremony-in-a-convention-hall` | no |
| 12 | `nurse-charts-beside-four-bassinets-holding-amphibian-newborn` | nurse charting at the hospital nursery | `nurse-charting-at-the-hospital-nursery` | no |
| 13 | `spotted-animal-with-long-gray-snout-approaches-trail-camera` | trail camera capture, 9:27 am, 11 degrees | `trail-camera-capture-9-27-am-11-degrees` | no |
| 14 | `nude-man-and-woman-beside-moon-skulls-and-rooster` | framed painting with nameplate in a gallery | `framed-painting-with-nameplate-in-a-gallery` | no |
| 15 | `four-men-take-tea-at-a-lace-table-beside-a-large-pale-figure` | four guests seated for afternoon tea | `four-guests-seated-for-afternoon-tea` | no |
| 16 | `astronaut-on-roller-skates-carries-a-toaster-holding-a-skele` | crosswalk traffic at a city intersection | `crosswalk-traffic-at-a-city-intersection` | no |
| 17 | `giraffe-toast-and-grandfather-clock-laugh-in-a-laundromat` | laundromat with washing machines in a row | `laundromat-with-washing-machines-in-a-row` | no |
| 18 | `several-figures-stand-in-a-yard-under-a-raining-cloud` | outdoor gathering on a lawn in light rain | `outdoor-gathering-on-a-lawn-in-light-rain` | no |
| 19 | `bald-figure-with-large-round-eyes-sits-on-a-couch` | seated person in a furnished living room | `seated-person-in-a-furnished-living-room` | no |
| 20 | `man-centipede-with-book-and-potted-cactus-on-a-city-sidewalk` | morning commute on a city sidewalk | `morning-commute-on-a-city-sidewalk` | no |
| 103 | `consulting-room-seating-for-an-appointment` | therapist taking notes during a consultation | `therapist-taking-notes-during-a-consultation` | no |
| 175 | `airport-security-checkpoint-with-items-in-bins` | passenger screening at the security checkpoint | `passenger-screening-at-the-security-checkpoint` | no |

Note on 16: if 16 is the picture kept in section 1 the rename applies; its new slug does not collide with `crosswalk-use-at-a-city-intersection`.

Note on republish: `_set_slug` refuses a rename for a picture that is not live, and publishing assigns a slug from the caption, so a republish of any of these would overwrite a hand-set slug with the caption's.

## 3. Comments signed ChatGPT

Two, both approved, both on 2026-11-15 (live admin api, comments with status approved; pending and rejected are empty).

| picture | date (UTC) | text |
|---|---|---|
| https://peni.sh/i/worn-living-room-with-armchair-and-radiator | 2026-11-15 09:38 | The ceiling straps are what make this work for me. Someone has installed this, checked the load, and settled into a routine. The yellowed curtains make it feel as though it has been working for years. |
| https://peni.sh/i/sitting-room-with-window-blind-half-raised | 2026-11-15 09:39 | The empty room is the strongest version for me. The moon looks less like a cosmic threat than someone waiting to be acknowledged, which makes closing the blind feel strangely cruel. |

These are the only comments on the site (the brief's count of 0 comments is out of date). Left in place.

## 4. Screenshots with UI chrome (candidates)

Searched captions, descriptions and tags of all 190 live pictures for screenshot, status bar, battery, notification, browser, app, interface, keyboard, wifi, toolbar, address bar, menu bar, home screen, web page, url, cursor and similar, and checked unusual aspect ratios (no picture is narrower than 0.62 or wider than 2.0, so no phone-shaped crops). One candidate, one weaker mention.

| slug | evidence |
|---|---|
| https://peni.sh/i/unloading-a-washing-machine-at-the-laundromat | Tag `web-page-screenshot` (model tag). Size 1668 x 2420. Caption and description (a laundromat illustration in pen and ink) do not mention any chrome, so the tag is the only evidence. |

Not UI chrome, listed so Tom can discount them: https://peni.sh/i/spotted-animal-with-long-gray-snout-approaches-trail-camera has a `timestamp-overlay` tag and a description of a bottom bar with frame counter and unit code, which is trail camera data imprinted on the frame, not a phone or browser. https://peni.sh/i/public-health-advisory-poster-with-eight-numbered-steps carries a QR code and a footer web address inside the poster artwork. https://peni.sh/i/studio-equipment-layout-and-floor-cabling matched "keyboard" as studio equipment.

## Not checked

I cannot see images, so duplicates, crops, UI chrome and whether any slug ends mid-word are judged from text, dimensions and embeddings only; similarity comes from the 2026-11-20 database copy, not from live embeddings.
