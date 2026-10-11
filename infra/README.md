# infra

How the peni.sh host is built. One Hostinger KVM 4 VPS (4 vCPU, 16 GB RAM,
200 GB disk) behind Cloudflare. Everything here is free on top of the plan
already paid for.

## Files

- `bootstrap.sh` -- the whole host setup. Runs once as root on a fresh
  Ubuntu 26.04 LTS image; safe to re-run by hand. Stages are independent and
  non-fatal, and their results land in `/srv/peni.sh/www/healthz.json` for the
  first 24 hours (see below) and in `/post_install.log` for good.
- `post-install.sh` -- what Hostinger actually runs (its post-install hook
  takes at most 48 KB). It fetches `bootstrap.sh` at a pinned commit and
  refuses to run it unless the sha256 matches. To ship a new bootstrap:
  commit it, update the commit and hash in the loader, upload the loader as
  a new Hostinger post-install script, and recreate the VM with it.

## What the bootstrap sets up

- `tom` user, key-only SSH (password logins off, root by key only),
  passwordless sudo, zsh.
- fail2ban on sshd; ufw allowing 22/80/443.
- unattended-upgrades daily, rebooting at 09:30 UTC when a reboot is needed.
- 4 GB swapfile, swappiness 10, timezone UTC, hostname `peni`.
- Postgres from the Ubuntu archive with pgvector. Database `penish` owned by
  the `penish` role; peer auth only, so no database password exists.
  `tom` is a superuser role.
- `penish` system user owning `/srv/peni.sh/{app,media,backups}`.
- uv in `/usr/local/bin`; restic installed but unconfigured.
- Caddy serving an in-character placeholder on `:80`, plain text for
  curl/wget/httpie/xh user agents.

## The 24-hour verification window

A fresh box has no DNS, so the bootstrap also serves the placeholder on
`<ipv4-with-dashes>.sslip.io` with a real certificate, plus `healthz.json`
and a copy of the install log. `penish-bootstrap-cleanup.timer` removes the
sslip.io site and both files 24 hours after install. Nothing in them is
secret.

## Network firewall (Hostinger, not in this repo's scripts)

Firewall group `peni-sh-cloudflare-only`, applied through the Hostinger API:

- TCP 22 from anywhere, ICMP and ICMPv6 from anywhere.
- TCP 80 and 443 only from Cloudflare's published ranges
  (https://www.cloudflare.com/ips-v4, https://www.cloudflare.com/ips-v6).
  When Cloudflare changes those lists, the rules have to follow.

The origin IP is deliberately absent from this public repo. With 80/443
closed to everyone but Cloudflare, knowing the IP doesn't bypass the proxy
anyway.

## Still to do

- Point DNS at the box through Cloudflare (proxied), SSL mode Full (strict)
  with a Cloudflare Origin CA certificate in Caddy.
- An R2 bucket and restic credentials for nightly `pg_dump` and media
  backups.
