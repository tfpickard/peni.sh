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

Re-run it after changing `scripts/penish-deploy`, `scripts/penish-backup` or `ops/systemd/*`; it copies them into `/usr/local/bin` and `/etc/systemd/system`.

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
| `x_tags.txt` | tags, one per line |
| `x_prompt.txt` | prompt |
| `x_embedding.json` / `.npy` | embedding, only if it names our model and dim |

Missing fields are generated. Failures land in `data/failed/<stem>/` with `x.err`.

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

`penish-deploy.timer` runs every 60 s. It reads the head sha of the latest successful `ci.yml` push run on `main` and compares it to `/var/lib/penish-deploy/deployed_sha`. On a difference: fetch `compose.yaml` for that sha from raw.githubusercontent.com, validate it with `docker compose config -q` and overwrite the local copy (previous one kept as `/var/lib/penish-deploy/compose.yaml.prev`, restored on rollback; a fetch or validation failure aborts the attempt), set `IMAGE_TAG` in `.env`, `docker compose pull`, `up -d --remove-orphans`, then wait up to 120 s for `api` to be healthy. `deployed_sha` is written only on success. On failure the journal gets an error, `IMAGE_TAG` goes back to the previous sha, and the same sha is not retried for 10 minutes.

`GH_READ_TOKEN` in `.env`: GitHub, Settings, Developer settings, Fine-grained tokens. Repository access: only `tfpickard/peni.sh`. Permission: Actions, read-only. Until it is set the script does nothing, unless `/var/lib/penish-deploy/pinned_sha` exists.

```
journalctl -fu penish-deploy.service
```

## rollback

```
echo <40-char-sha> > /var/lib/penish-deploy/pinned_sha
```

Run it as `deploy`, or as root followed by `chown deploy /var/lib/penish-deploy/pinned_sha`. State lives there rather than in `data/` because `data/` is bind-mounted into the containers, and anything they can write must not steer a root-equivalent deploy.

The poller deploys that sha and nothing newer. Remove the file to resume following `main`.

## backups

`penish-backup.timer` runs at 03:30. It writes `backups/<date>.dump` (`pg_dump -Fc`), keeps 14, then ships off-box:

1. `RESTIC_REPOSITORY` set in `.env`: `restic backup backups data/media/orig`, then `restic forget --keep-daily 14 --prune`. Put the repo password and backend credentials (`RESTIC_PASSWORD`, `AWS_*`, `B2_*`, ...) in `.env` too. Run `restic init` once as `deploy` with the same environment.
2. Otherwise `BACKUP_TARGET=user@host:/path`: rsync of `backups/` and `data/media/orig/`, no `--delete`. `deploy` needs an ssh key for that host.
3. Otherwise a warning, local dump only.

Restore: `docker compose exec -T db pg_restore -U <user> -d <db> --clean < backups/<date>.dump`.

## admin

`/admin` is an upload form. It exists only when `ADMIN_PASSWORD` is set in `.env`; unset, every `/api/admin` route answers 404. Read the password with:

```
ssh root@100.80.26.40 grep ADMIN_PASSWORD /srv/peni.sh/.env
```

Changing it ends every session. Uploads go to `data/inbox` and take the same path as an rsync drop, including the 10 s settle.
