# ops

Host: Ubuntu, Docker, Tailscale. App root `/srv/peni.sh`: `compose.yaml`, `.env`, `data/`, `backups/`.

```
data/inbox  processing  failed  media/orig  media/pub
```

## bootstrap

```
sudo UPLOAD_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" ./scripts/bootstrap_vps.sh
```

Re-runnable. Existing users and directories are kept. `upload` defaults to uid/gid 1002 (`UPLOAD_UID`); the worker container runs as the same uid. Firewall and sshd belong to `ops/harden.sh` (see `docs/hardening.md`); bootstrap does not touch them, nor Docker or Tailscale when already installed, nor cloudflared.

Re-run it after changing `scripts/penish-deploy`, `scripts/penish-backup`, `scripts/penish-restore-drill` or `ops/systemd/*`; it copies them into `/usr/local/bin` and `/etc/systemd/system`.

## uploads

The `upload` user can only run rrsync, rooted at the inbox. The destination path is therefore empty.

```
rsync -av x.png x_caption.txt upload@100.80.26.40:
```

`upload@peni` works too via MagicDNS. It is plain OpenSSH on 22 over the tailnet (not Tailscale SSH), so the forced rrsync command in `authorized_keys` applies. ufw only admits `tailscale0`.

Files with the same stem form one post. A group is picked up after 10 s without writes.

| file | field |
|---|---|
| `x.png` | image |
| `x_caption.txt` | caption |
| `x_description.txt` | description |
| `x_analysis.txt` | analysis |
| `x_tags.txt` | tags, one per line |
| `x_prompt.txt` | prompt |
| `x_embedding.json` | embedding, JSON only, and only if it names our model and dim |
| `x_collection.txt` | collection, one line: `pansophicon`, `pansophicon iii` or `pansophicon iii 14` |
| `x_note.txt` | conservator's note, kept with the collection line |

Accepted pictures are `.png`, `.jpg`, `.jpeg`, `.webp`, `.heic` and `.heif`, in any case. Ultra HDR JPEGs are read as plain JPEG and the gain map is dropped. HEIC is decoded and its stored original is a PNG.

Missing fields are generated. Failures land in `data/failed/<stem>/` with `x.err`. The admin queue page retries one: the api moves the files back to `data/inbox` and removes the folder (not the `.err`). `inbox` and `failed` are separate mounts in the api container, so the move is a copy into a dotfile in the inbox, an fsync, a rename, sidecars first and the picture last, and only then the delete of the source; the `failed` mount is therefore writable for the api. The api runs as uid/gid 1002, the same as `upload`, which owns both folders, so owner write on `failed/<stem>/` is all it needs (group write is not).

Every arrival is recorded in the `uploads` table before it is decoded, so an upload that fails to decode still shows on `/admin/collections` under `unfiled`, with its stage and reason. The stages, in the order they are checked, are `filed`, `unfiled`, `live`, `down`, `ingesting`, `inbox`, `failed` and `received`. The stage is worked out from the images row, the collection leaves and the data folders on each request, and is not stored.

### bulk push

`scripts/ptp_push.py` sends a folder of pictures for one collection over the same rsync path as above. It skips any file whose sha256 the site already holds. It takes its sha list from `GET /api/admin/shas` with a read token (see admin), or from `--shas-file`.

It is a dry run unless `--send` is given. The dry run prints each file and whether the site holds it. `--send` needs `--files-from`, a file of names, one per line, that must be files in the folder (`#` starts a comment). The script never chooses the files. Each picture goes with a `<stem>_collection.txt` sidecar, sent first.

- `folder`: default `~/src/claude-share`
- `--glob`: file pattern, repeatable (default `ptp-*.png` and `zm*.png`)
- `--base`: site to ask for the sha list (default `https://peni.sh`)
- `--shas-file`: json or one sha256 per line, instead of the api
- `--json`: print json instead of text
- `--files-from`: the files to send
- `--send`: rsync the files named by `--files-from`
- `--dest`: rsync destination (default `upload@100.80.26.40:`)
- `--collection`: collection written into the sidecars (default `pansophicon`)
- `--limit N`: send at most N files; files already on the site do not count

```
uv run python scripts/ptp_push.py
uv run python scripts/ptp_push.py --send --files-from finals.txt --limit 5
```

## cloudflare

- Zone on the free plan, DNS for `peni.sh` managed there.
- Zero Trust, Tunnels: the tunnel runs as the host `cloudflared.service` with `--token-file /etc/cloudflared/token`. It is not in compose and bootstrap does not manage it.
- Public hostname `peni.sh` to `http://localhost:8000` (the api publishes on loopback only). The DNS record is created by the tunnel.
- SSL/TLS: Full (strict). Always Use HTTPS on.
- Cache rule: URI path starts with `/media/pub/`, edge TTL 1 year, browser TTL 1 year.

## tailscale

Disable key expiry for the `peni` machine in the Tailscale admin console, or the box drops off the tailnet (and ufw leaves no other way in) when the key lapses.

## images

The three packages `penish-api`, `penish-worker` and `penish-migrate` must be made public so the host pulls without credentials. GitHub, profile, Packages, each package, Package settings, Change visibility, Public. Packages are created private on the first push, so do this once after the first CI run on `main`.

## deploy

`penish-deploy.timer` runs every 60 s. It reads the head sha of the latest successful `ci.yml` push run on `main` and compares it to `/var/lib/penish-deploy/deployed_sha`. On a difference: fetch `compose.yaml` for that sha from raw.githubusercontent.com, validate it with `docker compose config -q` (YAML, interpolation and the required `.env` variables, run against the real `.env`; bind-mount sources, modes and ownership are not checked) and overwrite the local copy (previous one kept as `/var/lib/penish-deploy/compose.yaml.prev`, restored on rollback; a fetch or validation failure aborts the attempt), set `IMAGE_TAG` in `.env`, `docker compose pull`, `up -d --remove-orphans`, then wait up to 120 s for `api` to be healthy. `deployed_sha` is written only on success. On failure the journal gets an error, `IMAGE_TAG` goes back to the previous sha, and the same sha is not retried for 10 minutes.

`GH_READ_TOKEN` in `.env`: GitHub, Settings, Developer settings, Fine-grained tokens. Repository access: only `tfpickard/peni.sh`. Permission: Actions, read-only. Until it is set the script does nothing, unless `/var/lib/penish-deploy/pinned_sha` exists.

```
journalctl -fu penish-deploy.service
```

### deploy status on GitHub

With `GH_DEPLOY_TOKEN` in `.env`, every deploy attempt is also a GitHub deployment in the `production` environment. GitHub, Settings, Developer settings, Fine-grained tokens. Repository access: only `tfpickard/peni.sh`. Permission: Deployments, read and write. It is separate from `GH_READ_TOKEN`, which stays read-only and alone decides what gets deployed. Unset, nothing is reported and the deploy is unchanged.

The script calls out only (`POST /repos/tfpickard/peni.sh/deployments` and `.../deployments/<id>/statuses`); nothing on GitHub can reach the box.

| moment | status | description |
|---|---|---|
| a new sha is picked up | created, then `in_progress` | `from main` or `pinned`, then `pulling` |
| compose fetch or validation fails | `error` | `compose.yaml failed validation; nothing changed` (or `fetch failed`) |
| `docker compose pull` fails | `failure` | `pull failed; running containers untouched` |
| api not healthy in time | `failure` | `api not healthy within 120s; rolled back to <short sha>`, `...; nothing to roll back to` or `...; rollback to <short sha> failed` |
| healthy | `success` | `api healthy in 43s`, environment url `https://peni.sh`; older production deployments go inactive |
| the script dies mid-deploy | `error` | `deploy script aborted; see journal` |

It shows up on the repo's Deployments page (`github.com/tfpickard/peni.sh/deployments`, also linked from the code tab sidebar), one entry per attempt naming the commit. A PR timeline shows it only when the deployed sha is that PR's own head commit; a merge commit on `main` is not, so merged PRs normally show nothing. A poll with nothing new makes no call. A sha retried after the 10-minute pause gets a new deployment.

Reporting is best-effort: each call has a 10 s timeout, and a GitHub error or timeout only logs one line, `penish-deploy: WARNING: github deployment create failed (HTTP 500)`, `(no response within 10s)` for a timeout, or `(no connection, curl exit N)` for a refused connection, DNS or TLS failure. It never changes the deploy or its exit code. Neither the token nor response bodies are logged; the token reaches curl on stdin, not argv.

The timer runs the installed copy, `/usr/local/bin/penish-deploy`. A change to `scripts/penish-deploy` reaches the box only when `scripts/bootstrap_vps.sh` is re-run as root (a pulled image does not update it).

`DRY_RUN=1` prints the calls (`+ github deployment ...`) instead of making them. `DEPLOYMENTS_URL` and `GH_TIMEOUT_SECS` override the endpoint and the timeout, for tests.

## rollback

```
echo <40-char-sha> > /var/lib/penish-deploy/pinned_sha
```

Run it as `deploy`, or as root followed by `chown deploy /var/lib/penish-deploy/pinned_sha`. State lives there rather than in `data/` because `data/` is bind-mounted into the containers, and anything they can write must not steer a root-equivalent deploy.

The poller deploys that sha and nothing newer. Remove the file to resume following `main`.

## backups

`penish-backup.timer` runs at 03:30 as `deploy`. It writes `backups/<date>.dump` (`pg_dump -Fc`), keeps 14, then ships off-box:

1. `RESTIC_REPOSITORY` set in `.env`: `restic backup backups data/media/orig`, then `restic forget --keep-daily 14 --prune`. Put the repo password and backend credentials (`RESTIC_PASSWORD`, `AWS_*`, `B2_*`, ...) in `.env` too. Run `restic init` once as `deploy` with the same environment.
2. Otherwise `BACKUP_TARGET=user@host:/path`: rsync of `backups/` and `data/media/orig/`, no `--delete`. `deploy` needs an ssh key for that host.
3. Otherwise a warning, local dump only.

**Backups are local only unless `RESTIC_REPOSITORY` or `BACKUP_TARGET` is set.** The dumps and the originals then sit on the same disk as the database; losing the box loses them. The script warns about it every night.

`/admin/system` reads `backups/` through a read-only mount (`BACKUP_DIR=/backups` in the api container): `backup` is the time of the newest `*.dump` (and `local` when the last drill found no off-box target configured), `restore drill` is the last drill's result. The api runs as gid 1002 (group `upload`), so `backups/` is `deploy:upload`, mode 0750; `bootstrap_vps.sh` sets that.

Restore by hand: `docker compose exec -T db pg_restore -U <user> -d <db> --clean < backups/<date>.dump`.

### restore drill

`penish-restore-drill.timer` runs on Sundays at 05:00 as `deploy`. It restores the newest dump into a throwaway container of the db image (no network, no published port, 1 GB memory, removed on every exit path), counts `images`, `image_texts`, `embeddings`, `tags`, `haikus`, `collection_leaves`, `comments` and `votes` (a table missing from the dump is skipped) and writes `backups/restore-drill.json` by temp file and rename:

```
{"dump":"2026-11-20.dump","ran_at":"2026-11-20T05:00:03Z","ok":true,"seconds":4,
 "counts":{"images":187,"votes":3},"reason":null,"local_only":true}
```

No dump, a container that will not start, a failed `pg_restore` or zero pictures is `"ok":false` with a one-line `reason`, and a non-zero exit. `local_only` is `null` when the script cannot read `.env`. `APP_ROOT`, `BACKUP_DIR`, `DRILL_IMAGE` (default: the db image in `compose.yaml`) and `DRILL_TIMEOUT` (seconds for the restore, default 900) override the defaults.

Install it once on the box, which has no sudo for `deploy`; as root, from a checkout of this branch or `main` (or re-run `scripts/bootstrap_vps.sh`, which does all of this):

```
scp scripts/penish-restore-drill ops/systemd/penish-restore-drill.service ops/systemd/penish-restore-drill.timer root@100.80.26.40:/tmp/
ssh root@100.80.26.40
install -m 0755 /tmp/penish-restore-drill /usr/local/bin/penish-restore-drill
install -m 0644 /tmp/penish-restore-drill.service /tmp/penish-restore-drill.timer /etc/systemd/system/
chgrp upload /srv/peni.sh/backups && chmod 0750 /srv/peni.sh/backups
systemctl daemon-reload
systemctl enable --now penish-restore-drill.timer
systemctl start penish-restore-drill.service
journalctl -u penish-restore-drill.service -n 20 --no-pager
```

By hand, any time (as `deploy`, or `sudo -u deploy` from another user; it takes the same lock as the timer):

```
/usr/local/bin/penish-restore-drill; echo $?
cat /srv/peni.sh/backups/restore-drill.json
systemctl list-timers penish-restore-drill.timer
```

## admin

`/admin` is an upload form. It exists only when `ADMIN_PASSWORD` is set in `.env`; unset, every `/api/admin` route answers 404. Read the password with:

```
ssh root@100.80.26.40 grep ADMIN_PASSWORD /srv/peni.sh/.env
```

Changing it ends every session. Uploads go to `data/inbox` and take the same path as an rsync drop, including the 10 s settle.

### read token

`ADMIN_READ_TOKEN` in `.env` lets scripts read `/api/admin` with `Authorization: Bearer <token>`. It works for `GET` and `HEAD` only and can never write. It must be at least 32 characters or it is ignored, and it does nothing unless `ADMIN_PASSWORD` is set.

On the owner's Mac the token lives in `~/.config/penish/read-token`, or in the `PENISH_READ_TOKEN` env var.

```
curl -H "Authorization: Bearer $(cat ~/.config/penish/read-token)" https://peni.sh/api/admin/queue
```

Two endpoints exist for scripts:

- `GET /api/admin/shas`: `{"images": [...], "uploads": [...]}`. Each image has `sha256`, `status` and `slug`. Each upload has `sha256`, `stem`, `stage` and `error`.
- `GET /api/admin/uploads`: `{"uploads": [...]}`, every upload with its `stage`, `error`, `filename`, `collection_line` and `created_at`. `?collection=<slug>` limits it to one collection.
