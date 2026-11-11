-- migrate:up

-- The 40 haikus from 1.0's src/lib/haikus.ts, verbatim, so the home page has a deck on day one.
insert into haikus (l1, l2, l3, source) values
  ('Teeth in the salad', 'nobody stops eating though', 'the dog levitates', 'seed'),
  ('Her bob haircut screams', 'just the hair, not her, the hair', 'it has its own mouth', 'seed'),
  ('Tentacles from where', 'tentacles should never grow', 'she tips the waiter', 'seed'),
  ('Fungus ate the church', 'the congregation still sits', 'singing through the spores', 'seed'),
  ('Beam of light descends', 'selects the worst guy at the', 'picnic -- him? really?', 'seed'),
  ('Robot funeral', 'the casket bolts upright and', 'walks itself outside', 'seed'),
  ('Red eyes in the corn', 'red eyes in the milk jug too', 'red eyes in my red eyes', 'seed'),
  ('Denim shorts unbuckle', 'themselves and crawl toward the', 'edge of the portrait', 'seed'),
  ('Mechanical arms', 'braiding mechanical hair', 'on a meat scaffold', 'seed'),
  ('Small dog won''t stop staring', 'at the wall where the wall was', 'before the wall left', 'seed'),
  ('Oil painting drips', 'onto the dungeon floor and', 'becomes a new man', 'seed'),
  ('Purple top dissolves', 'revealing only more purple', 'all the way down', 'seed'),
  ('Teal sky opens like', 'a mouth and swallows the crowd', 'blanket and all', 'seed'),
  ('Sepia twilight', 'tastes like a copper penny', 'licked by something huge', 'seed'),
  ('She ascends but wrong', 'sideways through the barn wall', 'hooves and all', 'seed'),
  ('Spider wearing shorts', 'denim ones, very small ones', 'at the funeral', 'seed'),
  ('The caption just says', '"this is fine" but the dog is', 'inside out now', 'seed'),
  ('Portrait subject blinks', 'the gallery screams and then', 'forgets completely', 'seed'),
  ('Fungal growth whispers', 'investment advice at three', 'a.m. in the basement', 'seed'),
  ('Her transformation', 'started with the left kneecap', 'now she''s mostly door', 'seed'),
  ('Rapture came on a', 'Tuesday, took the dogs, left us', 'standing there like fools', 'seed'),
  ('Robot holds a flower', 'crushes it, eats the petals', 'writes a poem, weeps', 'seed'),
  ('Shallow depth of field', 'means you can''t see what''s behind', 'her -- good, don''t look', 'seed'),
  ('Crowd watches the sky', 'the sky watches back harder', 'somebody flinches', 'seed'),
  ('Three mechanical', 'arms where there should be zero', 'mechanical arms', 'seed'),
  ('Dungeon basement hums', 'the frequency of mother', 'the walls are lactating', 'seed'),
  ('Small dog at the edge', 'of the rendering, aware', 'it is not real', 'seed'),
  ('Oil painting style', 'but the oil is alive and', 'angry about it', 'seed'),
  ('Indoor darkness thick', 'enough to chew, she chews it', 'her eyes go red', 'seed'),
  ('Rural nothing stretched', 'across forever, one spider', 'owns all of it', 'seed'),
  ('Tentacle reaches', 'through the photograph and grabs', 'your actual thumb', 'seed'),
  ('Muted fine art screams', 'at frequencies only the', 'funeral hears', 'seed'),
  ('Her glowing eyes ask', 'a question there''s no language', 'built to answer yet', 'seed'),
  ('Bob haircut so sharp', 'it cuts the twilight open', 'eerie juice spills out', 'seed'),
  ('Picnic blanket folds', 'itself into a person', 'demands a sandwich', 'seed'),
  ('3D character', 'steps out of the teal background', 'smells like burnt algebra', 'seed'),
  ('Vibrant render melts', 'she was beautiful before', 'she became the floor', 'seed'),
  ('Ascension scene but', 'everyone''s going DOWN and', 'smiling about it', 'seed'),
  ('Dark humor caption:', '"the spiders were inside us', 'the whole time" -- oh no', 'seed'),
  ('She is almost here', 'purple top, red eyes, no skin', 'we built her from want', 'seed')
on conflict do nothing;

-- migrate:down

delete from haikus where source = 'seed';
