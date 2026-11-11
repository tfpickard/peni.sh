#!/usr/bin/env bash
# One-time (and re-runnable) host setup for Ubuntu 24.04+ (26.04 in use). Run as root.
#
#   sudo ./scripts/bootstrap_vps.sh
#
# Env flags:
#   UPLOAD_PUBKEY="ssh-ed25519 AAAA..."  public key allowed to rsync into the inbox
#   CLOSE_PUBLIC_SSH=1                   drop the public port 22 rule (needs working tailscale)
#   CLOSE_WEB_PORTS=1                    also drop the 80/443 rules (leave unset while host Caddy serves)
#   UPLOAD_UID=1002                      uid/gid of the upload user; the worker container runs as this
#   APP_ROOT=/srv/peni.sh                app root holding compose.yaml, .env, data/, backups/
#   SKIP_INSTALL=1                       skip apt/docker/tailscale/ufw/systemd (container testing)
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BASE=${APP_ROOT:-/srv/peni.sh}
DATA=$BASE/data
APP_UID=${UPLOAD_UID:-1002}
SSH_PORT_UPLOAD=2222
SKIP_INSTALL=${SKIP_INSTALL:-0}
CLOSE_PUBLIC_SSH=${CLOSE_PUBLIC_SSH:-0}
CLOSE_WEB_PORTS=${CLOSE_WEB_PORTS:-0}

step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
have_systemd() { [ -d /run/systemd/system ]; }

export DEBIAN_FRONTEND=noninteractive

if [ "$SKIP_INSTALL" != 1 ]; then
  step "base packages"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg jq rsync ufw

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
  systemctl enable --now docker

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

# Tailscale SSH takes over port 22 on the tailnet address and ignores authorized_keys, so a
# restricted rsync login there is impossible. A second sshd port, upload-only, avoids that.
if [ "$SKIP_INSTALL" != 1 ] && have_systemd && [ -d /etc/ssh/sshd_config.d ]; then
  step "sshd: upload-only listener on $SSH_PORT_UPLOAD"
  cat >/etc/ssh/sshd_config.d/60-penish-upload.conf <<SSHD
Match LocalPort $SSH_PORT_UPLOAD
    AllowUsers upload
    PasswordAuthentication no
    AuthenticationMethods publickey
SSHD
  if sshd -t; then
    if systemctl is-enabled ssh.socket >/dev/null 2>&1; then
      # Ubuntu 24.04 uses socket activation, so the port must be added to the socket unit.
      install -d /etc/systemd/system/ssh.socket.d
      cat >/etc/systemd/system/ssh.socket.d/penish.conf <<SOCK
[Socket]
ListenStream=
ListenStream=0.0.0.0:22
ListenStream=[::]:22
ListenStream=0.0.0.0:$SSH_PORT_UPLOAD
ListenStream=[::]:$SSH_PORT_UPLOAD
SOCK
      systemctl daemon-reload
      systemctl restart ssh.socket
    else
      grep -qE "^Port $SSH_PORT_UPLOAD\$" /etc/ssh/sshd_config.d/60-penish-upload.conf \
        || sed -i "1i Port 22\nPort $SSH_PORT_UPLOAD" /etc/ssh/sshd_config.d/60-penish-upload.conf
    fi
    systemctl reload ssh 2>/dev/null || systemctl restart ssh
  else
    warn "sshd -t failed; removing penish sshd config"
    rm -f /etc/ssh/sshd_config.d/60-penish-upload.conf
  fi
fi

if [ "$SKIP_INSTALL" != 1 ]; then
  step "firewall"
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow in on tailscale0
  if [ "$CLOSE_PUBLIC_SSH" = 1 ]; then
    # Refuse unless tailscale is actually up; otherwise this locks the operator out.
    if ! tailscale ip -4 >/dev/null 2>&1; then
      echo "CLOSE_PUBLIC_SSH=1 but tailscale is not up; run 'tailscale up --ssh' and retry" >&2
      exit 1
    fi
    ufw --force delete allow 22/tcp >/dev/null 2>&1 || true
    ufw --force delete allow OpenSSH >/dev/null 2>&1 || true
    echo "public port 22 closed"
  else
    ufw allow 22/tcp
  fi
  # 80/443 are in use by host Caddy until the tunnel takes over, so they stay unless asked.
  if [ "$CLOSE_WEB_PORTS" = 1 ]; then
    for rule in 80/tcp 443/tcp "Nginx Full" "Nginx HTTP" "Nginx HTTPS"; do
      ufw --force delete allow "$rule" >/dev/null 2>&1 || true
    done
    echo "public ports 80/443 closed"
  fi
  ufw --force enable
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
  1. tailscale up --ssh           (skip if already up; confirm ssh over the tailnet works)
  2. CLOSE_PUBLIC_SSH=1 sudo -E ./scripts/bootstrap_vps.sh   (only after step 1 works)
     CLOSE_WEB_PORTS=1 additionally drops 80/443 once the tunnel serves the site
  3. fill $BASE/.env: POSTGRES_PASSWORD, ANTHROPIC_API_KEY, TUNNEL_TOKEN, GH_READ_TOKEN
     backups: RESTIC_REPOSITORY + credentials, or BACKUP_TARGET=user@host:/path
  4. watch the first deploy: journalctl -fu penish-deploy.service
  5. uploads: rsync -av -e 'ssh -p $SSH_PORT_UPLOAD' x.png x_caption.txt upload@<tailnet-host>:
NEXT
