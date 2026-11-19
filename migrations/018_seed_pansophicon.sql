-- migrate:up

-- The first collection, unpublished. The anchors are starting text for the filer and are
-- never shown; the owner rewrites them from the admin. Their vectors are made by the worker.
-- Separate from 017 so a test can put the seed back without rebuilding the tables.
insert into collections (slug, title, abbrev, abbrev_latin, preamble, book_count, leaves_per_book)
values (
  'pansophicon',
  'Πανσοφικὸν τῶν πάντων',
  'ΠΤΠ',
  'PTP',
  'Acquired incomplete. Believed to comprise seven books of seventy-seven leaves. No book has'
  ' been found complete. The books are presented in the order in which they were found, which'
  ' is believed to be their original order.',
  7,
  77
)
on conflict (slug) do nothing;

insert into collection_books (collection_id, number, anchor_text, tag_hints)
select c.id, b.number, b.anchor_text, b.tag_hints
  from collections c
 cross join (values
  (1,
   'Engraved or painted plates of single animals and composite creatures shown in profile'
   ' against a plain ground, sometimes with small figures beside them for scale. Bestiary and'
   ' natural-history illustration, hand-coloured woodcut, marginal drolleries, beasts drawn'
   ' with careful anatomy and impossible parts.',
   array['creature', 'animal', 'beast', 'bestiary', 'drollery', 'woodcut']),
  (2,
   'Botanical plates showing a whole plant from root to flower, seeds and cross-sections, gems'
   ' and minerals drawn in rows, pseudo-script labels, herbal and lapidary pages with faded'
   ' washes.',
   array['plant', 'botanical', 'flower', 'root', 'seed', 'gem', 'mineral', 'herbal']),
  (3,
   'Engraved diagrams of surgical, mechanical or ritual apparatus in exploded view with'
   ' lettered parts, anatomical figures with flaps and cutaways, patients and practitioners,'
   ' bathhouses and infirmaries, procedures in progress.',
   array['diagram', 'apparatus', 'anatomy', 'surgery', 'machine', 'patient', 'infirmary']),
  (4,
   'Celestial spheres, nested rings and orbits, volvelles, astrolabe plates, star charts, maps'
   ' of seas and coasts, cosmological diagrams with figures at the margins, gold and lapis on'
   ' parchment.',
   array['celestial', 'stars', 'orbit', 'astrolabe', 'map', 'cosmos', 'sky']),
  (5,
   'Alchemical emblems, flasks and furnaces with scenes inside them, crowned figures, serpents,'
   ' suns and moons, sequences of small panels, laboratory interiors.',
   array['alchemy', 'flask', 'furnace', 'serpent', 'sun', 'moon', 'laboratory']),
  (6,
   'Icons with gold grounds, saints and figures in registers, relic caskets, liturgical'
   ' objects, processions, heterodox or unfamiliar sacred scenes, mosaic and fresco fragments.',
   array['icon', 'saint', 'relic', 'procession', 'mosaic', 'fresco', 'gold']),
  (7,
   'Dream images paired in grids, emblems and their meanings, divination tables, figures in'
   ' symbolic scenes, a sleeping person with a vision above, alphabets of signs.',
   array['dream', 'emblem', 'divination', 'symbol', 'sleep', 'vision', 'sign'])
 ) as b (number, anchor_text, tag_hints)
 where c.slug = 'pansophicon'
on conflict (collection_id, number) do nothing;

-- migrate:down

delete from collections where slug = 'pansophicon';
