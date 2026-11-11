#!/usr/bin/env bash
# Host hardening for the peni.sh VPS. See docs/hardening.md.
#
#   sudo ./harden.sh            apply; re-runnable, unchanged steps report "ok"
#   sudo ./harden.sh confirm    disarm the rollback timer after a fresh tailnet login works
#
# Env:
#   TAILNET_SSH_VERIFIED=1   allow closing public ssh when not running over a tailnet ssh session
#   DEADMAN_MINUTES=10       how long sshd/ufw changes survive without "confirm"
#
# Ubuntu 26.04 (resolute) only. Run it over the tailnet, not the public IP.
set -euo pipefail

DEADMAN_UNIT=penish-harden-deadman
DEADMAN_MINUTES=${DEADMAN_MINUTES:-10}
SSHD_DROPIN=/etc/ssh/sshd_config.d/10-hardening.conf
SYSCTL_FILE=/etc/sysctl.d/90-hardening.conf
AUDIT_RULES=/etc/audit/rules.d/50-penish.rules
DAEMON_JSON=/etc/docker/daemon.json

step() { printf '\n== %s\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
changed() { printf '   changed: %s\n' "$*"; }
warn() { printf '   WARN: %s\n' "$*" >&2; }
die() { printf 'harden.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"

if [ "${1:-}" = confirm ]; then
  if systemctl is-active --quiet "$DEADMAN_UNIT.timer"; then
    systemctl stop "$DEADMAN_UNIT.timer"
    echo "rollback timer disarmed"
  else
    echo "no rollback timer armed"
  fi
  exit 0
fi

# shellcheck disable=SC1091
. /etc/os-release
[ "${VERSION_CODENAME:-}" = resolute ] || die "expected Ubuntu resolute, got ${VERSION_CODENAME:-unknown}"

# Writes $2 to $1 only when the content differs. Returns 0 if it wrote.
put_file() {
  local path=$1 content=$2 mode=${3:-0644}
  if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then
    return 1
  fi
  install -d "$(dirname "$path")"
  printf '%s\n' "$content" >"$path.tmp"
  chmod "$mode" "$path.tmp"
  mv "$path.tmp" "$path"
  return 0
}

rollback_cmds=()

# ---------------------------------------------------------------- 1. docker
step "docker engine + compose plugin"
DAEMON_JSON_CONTENT='{
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true,
  "no-new-privileges": true,
  "userland-proxy": false,
  "default-address-pools": [ { "base": "172.20.0.0/14", "size": 24 } ]
}'
# 172.20.0.0/14 stays clear of Tailscale's 100.64.0.0/10 and the public /24.
daemon_json_changed=0
if put_file "$DAEMON_JSON" "$DAEMON_JSON_CONTENT"; then
  daemon_json_changed=1
  changed "$DAEMON_JSON"
else
  ok "$DAEMON_JSON"
fi

if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?') installed"
  if [ "$daemon_json_changed" = 1 ]; then
    # live-restore keeps containers up across the restart. Address pool changes only
    # apply to networks created afterwards.
    systemctl restart docker
    changed "docker restarted for daemon.json"
  fi
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: resolute
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  changed "docker installed"
fi
systemctl enable --now docker.service containerd.service >/dev/null 2>&1
docker run --rm hello-world >/dev/null 2>&1 && ok "hello-world runs" || die "docker run hello-world failed"

# ------------------------------------------------------- 2. retire host services
step "retire host caddy and postgres"
retire() {
  local unit=$1
  systemctl cat "$unit" >/dev/null 2>&1 || [ -L "/etc/systemd/system/$unit" ] || { ok "$unit not present"; return; }
  if [ "$(systemctl is-enabled "$unit" 2>/dev/null)" = masked ] && ! systemctl is-active --quiet "$unit"; then
    ok "$unit masked"
    return
  fi
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
  systemctl mask "$unit" >/dev/null
  changed "$unit disabled and masked"
}

# Refuse to stop a host postgres that holds data; the app's postgres lives in compose.
pg_user_relations() {
  sudo -u postgres psql -Atq -d postgres -c "select datname from pg_database where not datistemplate and datname <> 'postgres'" 2>/dev/null |
    while read -r db; do
      sudo -u postgres psql -Atq -d "$db" -c "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname not in ('pg_catalog','information_schema','pg_toast') and n.nspname not like 'pg_temp%' and c.relkind in ('r','v','m','S','p','f')"
    done | awk '{s += $1} END {print s + 0}'
}

retire caddy.service
# The Caddyfile is still referenced by caddy.service, /root/bootstrap.sh and the
# bootstrap cleanup script, so it is left in place.
pg_units=(postgresql.service)
for f in /etc/postgresql/*/main; do
  [ -d "$f" ] || continue
  v=$(basename "$(dirname "$f")")
  pg_units+=("postgresql@$v-main.service")
done
if pgrep -x postgres >/dev/null; then
  n=$(cd /tmp && pg_user_relations)
  if [ "$n" != 0 ]; then
    warn "host postgres has $n user relations; leaving it running"
    pg_units=()
  fi
fi
for u in "${pg_units[@]}"; do retire "$u"; done

# -------------------------------------------------------------------- 3. sshd
step "sshd drop-in"
SSHD_CONTENT='# peni.sh hardening (ops/harden.sh). sshd uses the first value it sees,
# and this file sorts before 10-penish.conf and 50-cloud-init.conf.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
X11Forwarding no
AllowAgentForwarding no
MaxAuthTries 3
LoginGraceTime 20
ClientAliveInterval 300
ClientAliveCountMax 2
KexAlgorithms mlkem768x25519-sha256,sntrup761x25519-sha512,sntrup761x25519-sha512@openssh.com,curve25519-sha256,curve25519-sha256@libssh.org
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com,umac-128-etm@openssh.com'

# Every algorithm named must exist in the installed OpenSSH, or sshd refuses to start.
for pair in kex:KexAlgorithms cipher:Ciphers mac:MACs; do
  q=${pair%%:*} key=${pair#*:}
  supported=$(ssh -Q "$q")
  for alg in $(printf '%s\n' "$SSHD_CONTENT" | awk -v k="$key" '$1 == k {print $2}' | tr ',' ' '); do
    grep -qx "$alg" <<<"$supported" || die "installed OpenSSH lacks $q $alg"
  done
done

prev_sshd=""
[ -f "$SSHD_DROPIN" ] && prev_sshd=$(cat "$SSHD_DROPIN")
if put_file "$SSHD_DROPIN" "$SSHD_CONTENT"; then
  if ! sshd -t; then
    if [ -n "$prev_sshd" ]; then printf '%s\n' "$prev_sshd" >"$SSHD_DROPIN"; else rm -f "$SSHD_DROPIN"; fi
    die "sshd -t failed; previous drop-in restored, sshd not reloaded"
  fi
  rollback_cmds+=("mv $SSHD_DROPIN /root/10-hardening.conf.rolledback; systemctl reload ssh.service")
  changed "$SSHD_DROPIN"
  ssh_reload=1
else
  sshd -t || die "sshd -t fails with the current config; fix that first"
  ok "$SSHD_DROPIN"
  ssh_reload=0
fi
[ "$(sshd -T | awk '$1 == "passwordauthentication" {print $2}')" = no ] || die "effective PasswordAuthentication is not no"

# ------------------------------------------------------------------ 4. ufw
step "ufw: tailnet only"
ufw_before=$(ufw status verbose 2>/dev/null || true)
public_rules=$(ufw status | grep -E '^(22|80|443)/tcp|^(22|80|443) ' || true)

if [ -n "$public_rules" ]; then
  # Two independent proofs that the tailnet path is real before public ssh goes away.
  tailscale ip -4 >/dev/null 2>&1 || die "tailscale is not up; refusing to close public ports"
  ip -4 addr show tailscale0 >/dev/null 2>&1 || die "no tailscale0 interface; refusing to close public ports"
  src=${SSH_CONNECTION%% *}
  case "$src" in
    100.*) ;;
    *)
      [ "${TAILNET_SSH_VERIFIED:-0}" = 1 ] ||
        die "this session is not over the tailnet (from '${src:-console}'). Log in via the tailnet, or set TAILNET_SSH_VERIFIED=1 after testing it."
      ;;
  esac
  if [ "$(tailscale debug prefs 2>/dev/null | awk -F': ' '/"RunSSH"/ {print $2}' | tr -d ', ')" = true ]; then
    warn "Tailscale SSH is on: tailnet port 22 obeys tailnet ACLs, not authorized_keys"
  fi
fi

ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow in on tailscale0 comment 'tailnet only' >/dev/null
for r in 22/tcp 80/tcp 443/tcp; do
  ufw --force delete allow "$r" >/dev/null 2>&1 || true
done
grep -q '^Status: active' <<<"$(ufw status)" || ufw --force enable >/dev/null
if [ "$(ufw status verbose)" != "$ufw_before" ]; then
  rollback_cmds+=("ufw allow 22/tcp")
  changed "ufw rules"
else
  ok "ufw rules"
fi
systemctl is-active --quiet fail2ban && ok "fail2ban active" || warn "fail2ban not active"

# Arm the rollback before reloading sshd so a bad reload undoes itself.
if [ "${#rollback_cmds[@]}" -gt 0 ]; then
  systemctl stop "$DEADMAN_UNIT.timer" 2>/dev/null || true
  systemctl reset-failed "$DEADMAN_UNIT.service" 2>/dev/null || true
  joined=$(printf '%s; ' "${rollback_cmds[@]}")
  systemd-run --quiet --unit="$DEADMAN_UNIT" --on-active="${DEADMAN_MINUTES}min" /bin/sh -c "$joined"
  changed "rollback armed for ${DEADMAN_MINUTES} min: $joined"
fi
if [ "$ssh_reload" = 1 ]; then
  systemctl reload ssh.service
  changed "sshd reloaded"
fi

# ------------------------------------------------------------------ 5. sysctl
step "sysctl"
SYSCTL_CONTENT='# peni.sh hardening (ops/harden.sh).
# ip_forward is deliberately untouched: Docker and Tailscale need it.
# rp_filter is loose (2), not strict (1): strict mode drops Tailscale
# traffic, which is routed by fwmark through table 52.
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
# 1 cannot be lowered again without a reboot.
kernel.unprivileged_bpf_disabled = 1
net.core.bpf_jit_harden = 2
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
fs.protected_fifos = 2
fs.protected_regular = 2'
if put_file "$SYSCTL_FILE" "$SYSCTL_CONTENT"; then
  sysctl -p "$SYSCTL_FILE" >/dev/null
  changed "$SYSCTL_FILE applied"
else
  ok "$SYSCTL_FILE"
fi
[ "$(sysctl -n net.ipv4.ip_forward)" = 1 ] || warn "ip_forward is 0; docker networking will break"

# ---------------------------------------------------------- 6. sudo and users
step "sudo and users"
for f in /etc/sudoers.d/*; do
  [ -f "$f" ] || continue
  if grep -qE '^[[:space:]]*deploy[[:space:]].*NOPASSWD:[[:space:]]*ALL' "$f"; then
    cp -p "$f" "/root/sudoers.d-$(basename "$f").removed"
    rm -f "$f"
    if ! visudo -c >/dev/null; then
      cp -p "/root/sudoers.d-$(basename "$f").removed" "$f"
      die "visudo -c failed after removing $f; restored"
    fi
    changed "removed $f (copy in /root)"
  fi
done
grep -q 'not allowed' <<<"$(sudo -l -U deploy 2>&1)" && ok "deploy has no sudo" || warn "deploy still has sudo rights"

if id deploy >/dev/null 2>&1; then
  if grep -qw docker <<<"$(id -nG deploy)"; then ok "deploy in docker"; else
    usermod -aG docker deploy
    changed "deploy added to docker (root-equivalent)"
  fi
fi
grep -qw sudo <<<"$(id -nG tom 2>/dev/null)" && ok "tom in sudo" || warn "tom not in sudo"

# root keeps its password: it is the Hostinger browser console login, the lockout escape
# hatch. SSH password auth is off, so it is not reachable over the network.
while IFS=: read -r user hash _; do
  [ "$user" = root ] && continue
  case "$hash" in '' | '!'* | '*'*) continue ;; esac
  passwd -l "$user" >/dev/null
  changed "locked password for $user"
done </etc/shadow
ok "non-root passwords locked (key logins unaffected with UsePAM yes)"

# ----------------------------------------------------- 7. updates and audit
step "unattended-upgrades"
systemctl is-active --quiet unattended-upgrades && ok "unattended-upgrades active" || warn "unattended-upgrades not active"
reboot_time=$(apt-config dump | awk -F'"' '/^Unattended-Upgrade::Automatic-Reboot-Time /{print $2}')
auto_reboot=$(apt-config dump | awk -F'"' '/^Unattended-Upgrade::Automatic-Reboot /{print $2}')
if [ "$auto_reboot" = true ] && [ -n "$reboot_time" ]; then
  ok "automatic reboot at $reboot_time UTC"
else
  warn "automatic reboot window not set"
fi

step "auditd"
if ! dpkg -s auditd >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq auditd >/dev/null
  changed "auditd installed"
fi
systemctl enable --now auditd >/dev/null 2>&1
AUDIT_CONTENT='## peni.sh hardening (ops/harden.sh)
-w /etc/ssh/sshd_config -p wa -k sshd_config
-w /etc/ssh/sshd_config.d/ -p wa -k sshd_config
-w /etc/sudoers -p wa -k sudoers
-w /etc/sudoers.d/ -p wa -k sudoers
-w /etc/passwd -p wa -k identity
-w /etc/shadow -p wa -k identity
-w /etc/group -p wa -k identity
-a always,exit -F arch=b64 -S connect -F path=/run/docker.sock -k docker_socket
-w /run/docker.sock -p wa -k docker_socket
-w /etc/docker/ -p wa -k docker_config'
# A plain -w on the socket never fires for clients; connect() needs the syscall rule.
if put_file "$AUDIT_RULES" "$AUDIT_CONTENT" 0640; then
  augenrules --load >/dev/null 2>&1
  changed "$AUDIT_RULES loaded"
else
  ok "$AUDIT_RULES"
fi
grep -q docker_socket <<<"$(auditctl -l)" || die "audit rules not loaded"

# ------------------------------------------------------------------ report
step "state"
ufw status verbose
echo
ss -tlnp
if systemctl is-active --quiet "$DEADMAN_UNIT.timer"; then
  cat <<EOF

Rollback timer armed ($DEADMAN_MINUTES min). Keep this session open, then from
another machine open a NEW ssh session over the tailnet:

  ssh root@100.80.26.40 true

If that works, run: $0 confirm
If it does not, do nothing; the timer restores public ssh / the old sshd config.
EOF
fi
