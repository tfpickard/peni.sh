-- migrate:up

-- Starting text for the Pansophicon's gloss, catalogue record and vendor's description.
-- All of it is the owner's to rewrite from the admin; this only fills what is still
-- empty, so it never undoes an edit. Separate from 019 so a test can put it back.
update collections
   set gloss = 'Pansophikon ton panton. The all-wise thing of all things.'
 where slug = 'pansophicon' and gloss = '';

update collections
   set record = jsonb_build_array(
         jsonb_build_array('extent', 'believed seven books of seventy-seven leaves'),
         jsonb_build_array('languages',
           'Greek. Marginalia in Latin and Polish. A third script in places, not yet identified.'),
         jsonb_build_array('support',
           'parchment and paper, mixed. Some leaves are later than the books they were found in.'),
         jsonb_build_array('condition', 'disbound; sequence reconstructed. Restoration ongoing.'),
         jsonb_build_array('provenance',
           concat_ws(E'\n',
             'Said to have been at a monastery in Epirus before 1913.',
             'Private collection, Ioannina, by 1931 (label on a box).',
             'By descent.',
             'A dealer in Tirana, 2024. Export documentation not supplied.',
             'Acquired 2026 in accordance with applicable law.')),
         jsonb_build_array('exhibited', 'never'),
         jsonb_build_array('literature', 'none'))
 where slug = 'pansophicon' and record = '[]'::jsonb;

update collections
   set vendor_text = 'IMPORTANT BYZANTINE CODEX OF UNIVERSAL KNOWLEDGE. COMPLETE IN SEVEN BOOKS.'
                     ' Ex-monastic. Exceptional state of preservation. A unique opportunity.'
 where slug = 'pansophicon' and vendor_text is null;

-- migrate:down

update collections set gloss = '', record = '[]'::jsonb, vendor_text = null
 where slug = 'pansophicon';
