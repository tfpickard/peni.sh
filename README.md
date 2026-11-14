# peni.sh

⁂

A gallery of pictures. One person puts them in. Nobody else can.

Each picture is described, embedded, and published. Its page lists the pictures
most like it and the pictures least like it, and three numbers.

The pictures are of whatever they are of. The descriptions are accurate.


## ☙ arrival

Pictures are rsynced into an inbox over the tailnet.

    rsync -av x.png x_caption.txt upload@peni:

The `upload` key can run rsync and nothing else, and only inside the inbox. Files
still waiting there can be listed and fetched with it. Nothing outside can.

Files sharing a stem are one picture. The worker waits until a stem has been
quiet for 10 seconds, then takes it.

| file | becomes |
|---|---|
| `x.png` `.jpg` `.jpeg` `.webp` | the picture |
| `x_caption.txt` | caption |
| `x_description.txt` | description |
| `x_analysis.txt` | analysis |
| `x_tags.txt` | tags, one per line |
| `x_prompt.txt` | prompt |
| `x_embedding.json` | embedding, if it names the current model and width |

A missing caption, description, analysis or tags are written by a model. A
missing prompt stays missing. Anything supplied is kept. A model never
overwrites a field the owner wrote.

A picture that cannot become a picture, or that the model declines, goes to
`data/failed/<stem>/` with an `.err` file saying why.

A picture that could not be described or embedded for a passing reason stays in
`data/processing/` as `pending_enrichment` and is tried again. If the tries run
out, the job is marked failed in `/admin/queue` and the files stay where they
are. A restart picks them up again.

There is also an upload form at `/admin`. It puts files in the same inbox and
they wait the same 10 seconds.


## ⌬ processing

The worker does the following, in order, for each picture.

1. Hashes it. The same bytes twice is the same picture.
2. Makes avif and webp derivatives at 480, 960 and 1600 pixels, stopping at the
   picture's own width, then a blurhash and a five-colour palette.
3. Shows it to Claude, which returns an analysis, a caption, a description and
   6 to 14 tags.
4. Embeds the analysis with Qwen3-Embedding-0.6B, locally, on the CPU.
5. Records how far it is from its nearest neighbour at that moment.
6. Publishes it under a slug made from the caption.

The analysis is a four-paragraph record of what is in the picture. It is
embedded and never shown.

The description is shown. It is written by a clerk who looked directly at the
picture and attended to something else, such as the seating or the weather.
The caption is the clerk's as well. The tags are not; they are about the
picture.


## ⍟ the page

Under each picture: the caption, the description, the tags, eight similar
pictures, eight dissimilar pictures, votes, comments, and these.

| | |
|---|---|
| loneliness | mean cosine distance to the five nearest pictures |
| typicality | cosine similarity to the mean of all pictures |
| surprise | distance to the nearest picture when this one arrived. It does not change afterwards |

Once there are 30 pictures, similarity is measured after subtracting the mean,
so that what every picture has in common stops counting.

The home page shows a count and a temperature. The temperature is the mean
distance between every pair of pictures.

Above the home grid is a haiku. It is set in a variable font whose letters
grow tendrils. The tendrils differ on every load.


## ⌗ tags and search

`/tags` is the 60 most used tags that sit on at least 2 pictures, arranged in
a spiral, larger when more common. Both numbers are settings and will be raised.

`/t/<tag>` is every picture with that tag.

`/search` puts pictures carrying the exact tag first, then everything else by
distance from the query. If the embedding model is not answering, it falls back
to tags. If there are no tags either, it says so.


## ⏚ stack

| | |
|---|---|
| api | Python 3.12, FastAPI, psycopg 3 |
| database | Postgres 17, pgvector. Exact scan, no index. The corpus is small |
| worker | watchfiles, a `skip locked` job queue, pyvips, the Anthropic API, sentence-transformers on CPU torch |
| web | SvelteKit, adapter-static, served by the api |
| host | one VPS, Cloudflare Tunnel in front, nothing published to the internet |

The embedding model lives only in the worker. The api asks it for query vectors
over the compose network, on a port that is not published.

Vectors are stored in an untyped `vector` column alongside the model name and
width. Every query filters on both. Changing the model needs no migration.


## ⌑ layout

    penish/          the package
      api/           routes, the admin surface, og tags, the haiku renderer
      worker/        watcher, jobs, ingest, describe, embed, metrics
      prompts/       what the models are told
    migrations/      plain sql, applied in order
    web/             the site
    tests/           pytest, against a throwaway pgvector container
    scripts/         bootstrap, deploy and backup scripts for the host
    ops/             systemd units, host hardening
    docs/            ops.md, hardening.md, admin-spec.md


## ⎔ running it

On a Mac:

    cp .env.example .env
    docker compose -f compose.yaml -f compose.dev.yaml up --build

Python alone:

    uv sync                    # api and dev tools
    uv sync --extra worker     # also torch, which is large

Web alone, with the api on :8000:

    cd web && pnpm install && pnpm dev

Settings come from `.env`. `.env.example` lists them. Unset `ADMIN_PASSWORD` and
there is no admin. Unset `ANTHROPIC_API_KEY` and nothing is described.


## ⍾ checks

    uv run ruff check .
    uv run ruff format .
    uv run mypy penish
    uv run pytest
    cd web && pnpm check

The tests start their own Postgres in Docker unless `TEST_DATABASE_URL` is set.
Tests marked `live` call the real API and are skipped without a key.


## ⏃ deploy

There is no way to push to the server. The server pulls.

CI builds three images on every push to `main`: `penish-api`, `penish-worker`
and `penish-migrate`. A timer on the host checks every 60 seconds for a newer
green build, pulls it, and waits up to two minutes for the api to report
healthy. If it does not, the previous build is put back and the new one is not
tried again for ten minutes.

To hold the site at a particular commit:

    echo <sha> > /var/lib/penish-deploy/pinned_sha

Remove the file to resume.

The database is dumped at 03:30 every day. Fourteen are kept.

Details are in `docs/ops.md`. The host's state is in `docs/hardening.md`.


## ℞ rules

- No em dashes anywhere. Use `--`.
- Comments say why.
- Owner text is not overwritten.
- The site does not explain itself.

⸎
