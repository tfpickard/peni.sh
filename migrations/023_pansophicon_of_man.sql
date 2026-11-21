-- migrate:up

-- Book vii becomes Of Man, dreams and divination move into book iv, and the medium words
-- (parchment, engraved, woodcut, mosaic, fresco, lapis) leave every anchor and hint list.
-- The text is docs/ptp_anchors.json; tests/test_collection_seed.py keeps the two equal.
-- The owner may already have pasted text of their own from the admin, so a book is only
-- rewritten while its anchor and hints both still equal the 018 seed, and a book that
-- differs in either is left completely alone, vector included. Titles, leaves, pins and
-- notes are the owner's and are not in this file.
--
-- The vector is cleared with the text, as the admin does on an anchor edit: the old one
-- describes words that are gone, and the worker embeds any anchor with no vector at its
-- next start. The model and dim columns go with it so nothing marks the old vector current.
update collection_books b
   set anchor_text = v.new_anchor,
       tag_hints = v.new_hints,
       anchor_embedding = null,
       anchor_model = null,
       anchor_dim = null,
       updated_at = now()
  from collections c,
       (values
  (1,
   -- old (018)
   'Engraved or painted plates of single animals and composite creatures shown in profile'
   ' against a plain ground, sometimes with small figures beside them for scale. Bestiary'
   ' and natural-history illustration, hand-coloured woodcut, marginal drolleries, beasts'
   ' drawn with careful anatomy and impossible parts.',
   array['creature', 'animal', 'beast', 'bestiary', 'drollery', 'woodcut'],
   -- new
   'Animals and composite creatures presented one at a time for study: their parts,'
   ' habits, uses and dangers. Beasts with impossible anatomy, hybrids of fish, bird and'
   ' mammal, monsters from the edges of the known world, creatures in the margins acting'
   ' like people. Small human figures sometimes stand beside them for scale.',
   array['animal', 'beast', 'creature', 'hybrid', 'monster', 'bird', 'fish', 'serpent',
         'drollery']),
  (2,
   -- old (018)
   'Botanical plates showing a whole plant from root to flower, seeds and cross-sections,'
   ' gems and minerals drawn in rows, pseudo-script labels, herbal and lapidary pages'
   ' with faded washes.',
   array['plant', 'botanical', 'flower', 'root', 'seed', 'gem', 'mineral', 'herbal'],
   -- new
   'Plants shown whole from root to flower, with seeds and cross-sections. Trees that'
   ' bear creatures, people or objects as fruit, roots shaped like people, gardens and'
   ' harvests of impossible crops. Gems, minerals and stones laid out in rows with their'
   ' virtues.',
   array['plant', 'tree', 'root', 'flower', 'seed', 'fruit', 'harvest', 'garden', 'gem',
         'stone']),
  (3,
   -- old (018)
   'Engraved diagrams of surgical, mechanical or ritual apparatus in exploded view with'
   ' lettered parts, anatomical figures with flaps and cutaways, patients and'
   ' practitioners, bathhouses and infirmaries, procedures in progress.',
   array['diagram', 'apparatus', 'anatomy', 'surgery', 'machine', 'patient', 'infirmary'],
   -- new
   'Devices built to be applied to the body: chairs, frames, balances, tubes, gears and'
   ' basins with lettered parts. Surgery, bloodletting and cures in progress, remedies'
   ' and their ingredients, a body opened, flapped or diagrammed for teaching. A patient'
   ' approaches the apparatus warily while practitioners wait.',
   array['apparatus', 'machine', 'device', 'instrument', 'surgery', 'remedy', 'cure',
         'anatomy', 'patient']),
  (4,
   -- old (018)
   'Celestial spheres, nested rings and orbits, volvelles, astrolabe plates, star charts,'
   ' maps of seas and coasts, cosmological diagrams with figures at the margins, gold and'
   ' lapis on parchment.',
   array['celestial', 'stars', 'orbit', 'astrolabe', 'map', 'cosmos', 'sky'],
   -- new
   'The heavens and what they foretell. Spheres, rings and orbits, planets with the'
   ' people they govern, constellations drawn as figures, zodiac men whose body parts are'
   ' ruled by the signs, winds, maps of seas and coasts. Dreams and their meanings,'
   ' omens, divination and alphabets of signs.',
   array['celestial', 'stars', 'planet', 'zodiac', 'map', 'cosmos', 'sky', 'dream',
         'divination', 'omen']),
  (5,
   -- old (018)
   'Alchemical emblems, flasks and furnaces with scenes inside them, crowned figures,'
   ' serpents, suns and moons, sequences of small panels, laboratory interiors.',
   array['alchemy', 'flask', 'furnace', 'serpent', 'sun', 'moon', 'laboratory'],
   -- new
   'The changing of one thing into another. Flasks and furnaces with whole scenes sealed'
   ' inside them, a crowned king and queen, lions, serpents, suns and moons, sequences of'
   ' small stages, laboratory work. Baths and fountains that alter whoever enters them,'
   ' or fail to.',
   array['alchemy', 'flask', 'furnace', 'transformation', 'serpent', 'sun', 'moon',
         'laboratory']),
  (6,
   -- old (018)
   'Icons with gold grounds, saints and figures in registers, relic caskets, liturgical'
   ' objects, processions, heterodox or unfamiliar sacred scenes, mosaic and fresco'
   ' fragments.',
   array['icon', 'saint', 'relic', 'procession', 'mosaic', 'fresco', 'gold'],
   -- new
   'What heaven does. Gods, saints and the many-armed one-eyed sky god acting directly:'
   ' creating, judging and weighing souls, sorting the saved from the damned, working'
   ' miracles and martyrdoms. Heaven and hell, relics, processions and sacred scenes that'
   ' belong to no known church.',
   array['god', 'saint', 'heaven', 'hell', 'judgement', 'soul', 'miracle', 'martyr', 'relic',
         'icon']),
  (7,
   -- old (018)
   'Dream images paired in grids, emblems and their meanings, divination tables, figures'
   ' in symbolic scenes, a sleeping person with a vision above, alphabets of signs.',
   array['dream', 'emblem', 'divination', 'symbol', 'sleep', 'vision', 'sign'],
   -- new
   'What people are and what they do. The human figure shown whole and unashamed as the'
   ' measure of things, set between beasts below and heaven above. Virtues and vices as'
   ' persons, the soul and its housing, knowledge carried in the body, conception,'
   ' pregnancy, birth and the ages of life. The household, the bath, the table, trades'
   ' and guilds, custom and festival. Law, trial and ordeal, and the prescribed'
   ' punishment fitted exactly to the offence, carried out calmly by magistrates and'
   ' executioners while daily life goes on around it.',
   array['people', 'crowd', 'nude', 'bath', 'pregnancy', 'virtue', 'custom', 'law', 'trial',
         'punishment', 'execution'])
       ) as v (number, old_anchor, old_hints, new_anchor, new_hints)
 where c.slug = 'pansophicon'
   and b.collection_id = c.id
   and b.number = v.number
   and b.anchor_text = v.old_anchor
   and b.tag_hints = v.old_hints;

-- An anchor is worth anchor_k filed leaves, so 10 lets the new anchors keep steering a
-- book while it fills. Only the shipped default (3, or no key at all) is changed: any other
-- value was chosen by the owner and stays, as does every other tuning key. A json null is
-- read as unset by Tuning.from_json, so it counts as unset here.
update collections
   set tuning = tuning || jsonb_build_object('anchor_k', 10),
       updated_at = now()
 where slug = 'pansophicon'
   and jsonb_typeof(tuning) = 'object'
   and ((tuning -> 'anchor_k') is null or (tuning -> 'anchor_k') in ('3'::jsonb, 'null'::jsonb));

-- migrate:down

-- The mirror of up: a book goes back to its 018 text only while it still equals the new
-- text, so an edit made after up survives, and its vector is cleared to be embedded again.
update collection_books b
   set anchor_text = v.old_anchor,
       tag_hints = v.old_hints,
       anchor_embedding = null,
       anchor_model = null,
       anchor_dim = null,
       updated_at = now()
  from collections c,
       (values
  (1,
   -- old (018)
   'Engraved or painted plates of single animals and composite creatures shown in profile'
   ' against a plain ground, sometimes with small figures beside them for scale. Bestiary'
   ' and natural-history illustration, hand-coloured woodcut, marginal drolleries, beasts'
   ' drawn with careful anatomy and impossible parts.',
   array['creature', 'animal', 'beast', 'bestiary', 'drollery', 'woodcut'],
   -- new
   'Animals and composite creatures presented one at a time for study: their parts,'
   ' habits, uses and dangers. Beasts with impossible anatomy, hybrids of fish, bird and'
   ' mammal, monsters from the edges of the known world, creatures in the margins acting'
   ' like people. Small human figures sometimes stand beside them for scale.',
   array['animal', 'beast', 'creature', 'hybrid', 'monster', 'bird', 'fish', 'serpent',
         'drollery']),
  (2,
   -- old (018)
   'Botanical plates showing a whole plant from root to flower, seeds and cross-sections,'
   ' gems and minerals drawn in rows, pseudo-script labels, herbal and lapidary pages'
   ' with faded washes.',
   array['plant', 'botanical', 'flower', 'root', 'seed', 'gem', 'mineral', 'herbal'],
   -- new
   'Plants shown whole from root to flower, with seeds and cross-sections. Trees that'
   ' bear creatures, people or objects as fruit, roots shaped like people, gardens and'
   ' harvests of impossible crops. Gems, minerals and stones laid out in rows with their'
   ' virtues.',
   array['plant', 'tree', 'root', 'flower', 'seed', 'fruit', 'harvest', 'garden', 'gem',
         'stone']),
  (3,
   -- old (018)
   'Engraved diagrams of surgical, mechanical or ritual apparatus in exploded view with'
   ' lettered parts, anatomical figures with flaps and cutaways, patients and'
   ' practitioners, bathhouses and infirmaries, procedures in progress.',
   array['diagram', 'apparatus', 'anatomy', 'surgery', 'machine', 'patient', 'infirmary'],
   -- new
   'Devices built to be applied to the body: chairs, frames, balances, tubes, gears and'
   ' basins with lettered parts. Surgery, bloodletting and cures in progress, remedies'
   ' and their ingredients, a body opened, flapped or diagrammed for teaching. A patient'
   ' approaches the apparatus warily while practitioners wait.',
   array['apparatus', 'machine', 'device', 'instrument', 'surgery', 'remedy', 'cure',
         'anatomy', 'patient']),
  (4,
   -- old (018)
   'Celestial spheres, nested rings and orbits, volvelles, astrolabe plates, star charts,'
   ' maps of seas and coasts, cosmological diagrams with figures at the margins, gold and'
   ' lapis on parchment.',
   array['celestial', 'stars', 'orbit', 'astrolabe', 'map', 'cosmos', 'sky'],
   -- new
   'The heavens and what they foretell. Spheres, rings and orbits, planets with the'
   ' people they govern, constellations drawn as figures, zodiac men whose body parts are'
   ' ruled by the signs, winds, maps of seas and coasts. Dreams and their meanings,'
   ' omens, divination and alphabets of signs.',
   array['celestial', 'stars', 'planet', 'zodiac', 'map', 'cosmos', 'sky', 'dream',
         'divination', 'omen']),
  (5,
   -- old (018)
   'Alchemical emblems, flasks and furnaces with scenes inside them, crowned figures,'
   ' serpents, suns and moons, sequences of small panels, laboratory interiors.',
   array['alchemy', 'flask', 'furnace', 'serpent', 'sun', 'moon', 'laboratory'],
   -- new
   'The changing of one thing into another. Flasks and furnaces with whole scenes sealed'
   ' inside them, a crowned king and queen, lions, serpents, suns and moons, sequences of'
   ' small stages, laboratory work. Baths and fountains that alter whoever enters them,'
   ' or fail to.',
   array['alchemy', 'flask', 'furnace', 'transformation', 'serpent', 'sun', 'moon',
         'laboratory']),
  (6,
   -- old (018)
   'Icons with gold grounds, saints and figures in registers, relic caskets, liturgical'
   ' objects, processions, heterodox or unfamiliar sacred scenes, mosaic and fresco'
   ' fragments.',
   array['icon', 'saint', 'relic', 'procession', 'mosaic', 'fresco', 'gold'],
   -- new
   'What heaven does. Gods, saints and the many-armed one-eyed sky god acting directly:'
   ' creating, judging and weighing souls, sorting the saved from the damned, working'
   ' miracles and martyrdoms. Heaven and hell, relics, processions and sacred scenes that'
   ' belong to no known church.',
   array['god', 'saint', 'heaven', 'hell', 'judgement', 'soul', 'miracle', 'martyr', 'relic',
         'icon']),
  (7,
   -- old (018)
   'Dream images paired in grids, emblems and their meanings, divination tables, figures'
   ' in symbolic scenes, a sleeping person with a vision above, alphabets of signs.',
   array['dream', 'emblem', 'divination', 'symbol', 'sleep', 'vision', 'sign'],
   -- new
   'What people are and what they do. The human figure shown whole and unashamed as the'
   ' measure of things, set between beasts below and heaven above. Virtues and vices as'
   ' persons, the soul and its housing, knowledge carried in the body, conception,'
   ' pregnancy, birth and the ages of life. The household, the bath, the table, trades'
   ' and guilds, custom and festival. Law, trial and ordeal, and the prescribed'
   ' punishment fitted exactly to the offence, carried out calmly by magistrates and'
   ' executioners while daily life goes on around it.',
   array['people', 'crowd', 'nude', 'bath', 'pregnancy', 'virtue', 'custom', 'law', 'trial',
         'punishment', 'execution'])
       ) as v (number, old_anchor, old_hints, new_anchor, new_hints)
 where c.slug = 'pansophicon'
   and b.collection_id = c.id
   and b.number = v.number
   and b.anchor_text = v.new_anchor
   and b.tag_hints = v.new_hints;

-- Only the value up wrote goes back to the default; any other is the owner's.
update collections
   set tuning = tuning || jsonb_build_object('anchor_k', 3),
       updated_at = now()
 where slug = 'pansophicon'
   and jsonb_typeof(tuning) = 'object'
   and (tuning -> 'anchor_k') = '10'::jsonb;
