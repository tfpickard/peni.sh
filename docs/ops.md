# ops

Host: Ubuntu, Docker, Tailscale. App root `/srv/peni.sh`: `compose.yaml`, `.env`, `data/`, `backups/`.

```
data/inbox  processing  failed  media/orig  media/pub
data/deployed_sha  pinned_sha  failed_sha
```

## bootstrap

```
sudo UPLOAD_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" ./scripts/bootstrap_vps.sh
```

Re-runnable. Existing users and directories are kept. `upload` defaults to uid/gid 1002 (`UPLOAD_UID`); the worker container runs as the same uid. Public 22 stays open until `CLOSE_PUBLIC_SSH=1`, which refuses to run unless tailscale is up. 80/443 stay until `CLOSE_WEB_PORTS=1`. Host Caddy and host Postgres are not touched.

Re-run it after changing `scripts/penish-deploy`, `scripts/penish-backup` or `ops/systemd/*`; it copies them into `/usr/local/bin` and `/etc/systemd/system`.

## uploads

The `upload` user can only run rrsync, rooted at the inbox. The destination path is therefore empty.

```
rsync -av --chmod=F644 -e 'ssh -p 2222' x.png x_caption.txt upload@peni:
```

Port 2222 because `tailscale up --ssh` intercepts 22 on the tailnet address and ignores `authorized_keys`. Port 2222 accepts `upload` only, and ufw allows it only on `tailscale0`.

Files with the same stem form one post. A group is picked up after 10 s without writes.

| file | field |
|---|---|
| `x.png` | image |
| `x_caption.txt` | caption |
| `x_description.txt` | description |
| `x_tags.txt` | tags, one per line |
| `x_prompt.txt` | prompt |
| `x_embedding.json` / `.npy` | embedding, only if it names our model and dim |

Missing fields are generated. Failures land in `data/failed/<stem>/` with `x.err`.

## cloudflare

- Zone on the free plan, DNS for `peni.sh` managed there.
- Zero Trust, Tunnels: create a tunnel, put its token in `.env` as `TUNNEL_TOKEN`.
- Public hostname `peni.sh` to `http://api:8000`. The DNS record is created by the tunnel.
- SSL/TLS: Full (strict). Always Use HTTPS on.
- Cache rule: URI path starts with `/media/pub/`, edge TTL 1 year, browser TTL 1 year.

## images

The three packages `penish-api`, `penish-worker` and `penish-migrate` must be made public so the host pulls without credentials. GitHub, profile, Packages, each package, Package settings, Change visibility, Public. Packages are created private on the first push, so do this once after the first CI run on `main`.

## deploy

`penish-deploy.timer` runs every 60 s. It reads the head sha of the latest successful `ci.yml` push run on `main` and compares it to `data/deployed_sha`. On a difference: set `IMAGE_TAG` in `.env`, `docker compose pull`, `up -d --remove-orphans`, then wait up to 120 s for `api` to be healthy. `deployed_sha` is written only on success. On failure the journal gets an error, `IMAGE_TAG` goes back to the previous sha, and the same sha is not retried for 10 minutes.

`GH_READ_TOKEN` in `.env`: GitHub, Settings, Developer settings, Fine-grained tokens. Repository access: only `tfpickard/peni.sh`. Permission: Actions, read-only. Until it is set the script does nothing, unless `data/pinned_sha` exists.

```
journalctl -fu penish-deploy.service
```

## rollback

```
echo <40-char-sha> > /srv/peni.sh/data/pinned_sha
```

The poller deploys that sha and nothing newer. Remove the file to resume following `main`.

## backups

`penish-backup.timer` runs at 03:30. It writes `backups/<date>.dump` (`pg_dump -Fc`), keeps 14, then ships off-box:

1. `RESTIC_REPOSITORY` set in `.env`: `restic backup backups data/media/orig`, then `restic forget --keep-daily 14 --prune`. Put the repo password and backend credentials (`RESTIC_PASSWORD`, `AWS_*`, `B2_*`, ...) in `.env` too. Run `restic init` once as `deploy` with the same environment.
2. Otherwise `BACKUP_TARGET=user@host:/path`: rsync of `backups/` and `data/media/orig/`, no `--delete`. `deploy` needs an ssh key for that host.
3. Otherwise a warning, local dump only.

Restore: `docker compose exec -T db pg_restore -U <user> -d <db> --clean < backups/<date>.dump`.
