#!/usr/bin/env bash
# One-time (and re-runnable) host setup for Ubuntu 24.04+ (26.04 in use). Run as root.
#
#   sudo ./scripts/bootstrap_vps.sh
#
# Firewall and sshd are owned by ops/harden.sh (ufw, sshd drop-in, sudo). This script never
# touches them, nor cloudflared (the host connector is installed and managed by hand).
#
# Env flags:
#   UPLOAD_PUBKEY="ssh-ed25519 AAAA..."  public key allowed to rsync into the inbox
#   UPLOAD_UID=1002                      uid/gid of the upload user; the worker container runs as this
#   APP_ROOT=/srv/peni.sh                app root holding compose.yaml, .env, data/, backups/
#   SKIP_INSTALL=1                       skip apt/docker/tailscale/systemd (container testing)
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BASE=${APP_ROOT:-/srv/peni.sh}
DATA=$BASE/data
APP_UID=${UPLOAD_UID:-1002}
SKIP_INSTALL=${SKIP_INSTALL:-0}

step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
have_systemd() { [ -d /run/systemd/system ]; }

export DEBIAN_FRONTEND=noninteractive

if [ "$SKIP_INSTALL" != 1 ]; then
  step "base packages"
  # Only touch apt when something is missing; harden.sh already installed most of this.
  missing=()
  for pkg in ca-certificates curl gnupg jq rsync; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    apt-get update -qq
    apt-get install -y -qq "${missing[@]}"
  fi

  step "docker engine + compose plugin"
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    codename=$(. /etc/os-release; echo "$VERSION_CODENAME")
    cat >/etc/apt/sources.list.d/docker.sources <<DOCKER
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  systemctl enable --now docker.service >/dev/null 2>&1 || true

  step "tailscale"
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
fi

step "users"
# Fixed UID/GID: the worker container runs as this uid and must own what upload writes.
if ! getent group upload >/dev/null; then
  getent group "$APP_UID" >/dev/null && { echo "gid $APP_UID already taken" >&2; exit 1; }
  groupadd --gid "$APP_UID" upload
fi
if ! id upload >/dev/null 2>&1; then
  getent passwd "$APP_UID" >/dev/null && { echo "uid $APP_UID already taken" >&2; exit 1; }
  useradd --uid "$APP_UID" --gid "$APP_UID" --create-home --shell /bin/bash upload
fi
[ "$(id -u upload)" = "$APP_UID" ] || { echo "user upload exists with uid $(id -u upload), need $APP_UID" >&2; exit 1; }

getent group docker >/dev/null || groupadd --system docker
# No password is ever set, so the account is key/tailscale-only. An existing deploy user is kept as is.
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
# deploy joins the upload group so it can write state files under data/.
usermod -aG docker,upload deploy

step "directories"
# An existing app root (and its app/, media/, www/ dirs) is left alone; only missing pieces are created.
[ -d "$BASE" ] || install -d -o deploy -g deploy -m 0755 "$BASE"
if [ -d "$BASE/backups" ]; then
  # Pre-existing and empty, but the backup timer runs as deploy and has to write here.
  chown deploy:deploy "$BASE/backups"
else
  install -d -o deploy -g deploy -m 0750 "$BASE/backups"
fi
# setgid + group write: deploy (via the upload group) can write deployed_sha/pinned_sha here.
for d in "$DATA" "$DATA/inbox" "$DATA/processing" "$DATA/failed" "$DATA/media" "$DATA/media/orig" "$DATA/media/pub"; do
  install -d -o "$APP_UID" -g "$APP_UID" -m 2775 "$d"
done
if [ ! -f "$BASE/.env" ]; then
  install -o deploy -g deploy -m 0640 /dev/null "$BASE/.env"
fi
if [ ! -f "$BASE/compose.yaml" ]; then
  if [ -f "$REPO_DIR/compose.yaml" ]; then
    install -o deploy -g deploy -m 0644 "$REPO_DIR/compose.yaml" "$BASE/compose.yaml"
  else
    warn "no compose.yaml in repo; copy one to $BASE/compose.yaml"
  fi
fi

step "upload user: rsync into inbox only"
rrsync=$(command -v rrsync || true)
for cand in /usr/bin/rrsync /usr/share/doc/rsync/scripts/rrsync; do
  [ -n "$rrsync" ] || { [ -x "$cand" ] && rrsync=$cand; } || true
done
if [ -z "$rrsync" ]; then
  warn "rrsync not found (is the rsync package installed?); skipping authorized_keys"
else
  install -d -o upload -g upload -m 0700 /home/upload/.ssh
  if [ -n "${UPLOAD_PUBKEY:-}" ]; then
    # restrict disables pty, forwarding and everything else; the forced command pins rsync to the inbox.
    printf 'command="%s %s/inbox",restrict %s\n' "$rrsync" "$DATA" "$UPLOAD_PUBKEY" >/home/upload/.ssh/authorized_keys
    chown upload:upload /home/upload/.ssh/authorized_keys
    chmod 0600 /home/upload/.ssh/authorized_keys
  elif [ ! -s /home/upload/.ssh/authorized_keys ]; then
    warn "no upload key installed; re-run with UPLOAD_PUBKEY=\"\$(cat ~/.ssh/id_ed25519.pub)\""
  fi
fi

if [ "$SKIP_INSTALL" != 1 ] && have_systemd; then
  step "systemd units"
  install -m 0755 "$REPO_DIR/scripts/penish-deploy" /usr/local/bin/penish-deploy
  install -m 0755 "$REPO_DIR/scripts/penish-backup" /usr/local/bin/penish-backup
  for u in penish-deploy.service penish-deploy.timer penish-backup.service penish-backup.timer; do
    install -m 0644 "$REPO_DIR/ops/systemd/$u" "/etc/systemd/system/$u"
  done
  systemctl daemon-reload
  # The deploy script exits quietly until .env has GH_READ_TOKEN, so enabling now is safe.
  systemctl enable --now penish-deploy.timer penish-backup.timer
fi

cat <<NEXT

== done

next:
  1. fill $BASE/.env: POSTGRES_PASSWORD, ANTHROPIC_API_KEY, GH_READ_TOKEN
     backups: RESTIC_REPOSITORY + credentials, or BACKUP_TARGET=user@host:/path
  2. watch the first deploy: journalctl -fu penish-deploy.service
  3. uploads: rsync -av x.png x_caption.txt upload@100.80.26.40:
NEXT
