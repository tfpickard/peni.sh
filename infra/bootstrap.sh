#!/usr/bin/env bash
# peni.sh VPS bootstrap.
#
# Runs once as root via Hostinger's post-install hook after a fresh Ubuntu
# 26.04 LTS image is laid down; Hostinger writes its output to
# /post_install.log. Safe to re-run by hand.
#
# Each stage is non-fatal: a failure is recorded in healthz.json and the
# remaining stages still run, so one flaky mirror can't leave the box
# half-hardened. Nothing here generates or embeds a secret.

set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

SRV=/srv/peni.sh
WWW=$SRV/www

# Keys that already had root on this VM before the rebuild. The second one
# carried no label in Hostinger; it is kept so nobody gets locked out, and is
# flagged for review in $SRV/README.md.
AUTHORIZED_KEYS='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeVmxihx1I+thjuJy7gcev3UIAgzKfryrwUiyClcx7J tom@pickard.dev
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIED/u4UhZag1O6EgR21a7R2NdiQh9Qm4jYZEbx4UHmHY unlabelled-key-from-previous-root'

STAGES=()
declare -A RESULT

stage() {
	local name=$1
	shift
	echo "=== stage: $name ($(date -u +%FT%TZ))"
	if "$@"; then
		RESULT[$name]=ok
	else
		RESULT[$name]=FAILED
		echo "!!! stage $name failed"
	fi
	STAGES+=("$name")
}

# The hook can fire while the image's first-boot apt run still holds the dpkg
# lock, so wait for it rather than failing the stage.
APT_OPTS=(-y -o DPkg::Lock::Timeout=600
	-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

apt_install() {
	apt-get install "${APT_OPTS[@]}" "$@"
}

# One package renamed or dropped in a new release must not cost the whole
# list, so retry one at a time if the batch fails.
apt_install_each() {
	apt_install "$@" && return 0
	local p rc=0
	for p in "$@"; do
		apt_install "$p" || { echo "!!! package $p unavailable"; rc=1; }
	done
	return $rc
}

public_ipv4() {
	ip -4 route get 1.1.1.1 2>/dev/null |
		awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}'
}

add_keys() {
	local file=$1 key
	touch "$file"
	while IFS= read -r key; do
		[ -n "$key" ] || continue
		# Match on the key body so a relabelled key isn't added twice.
		grep -qF "$(printf '%s' "$key" | awk '{print $2}')" "$file" ||
			printf '%s\n' "$key" >>"$file"
	done <<<"$AUTHORIZED_KEYS"
	chmod 600 "$file"
}

s_base() {
	timedatectl set-timezone UTC
	hostnamectl set-hostname peni
	if grep -q '^127\.0\.1\.1' /etc/hosts; then
		sed -i 's/^127\.0\.1\.1.*/127.0.1.1 peni/' /etc/hosts
	else
		echo '127.0.1.1 peni' >>/etc/hosts
	fi
	# Without this, cloud-init puts the provider hostname back on reboot.
	if [ -d /etc/cloud/cloud.cfg.d ]; then
		echo 'preserve_hostname: true' >/etc/cloud/cloud.cfg.d/99-penish-hostname.cfg
	fi
	apt-get -o DPkg::Lock::Timeout=600 update &&
		apt-get "${APT_OPTS[@]}" full-upgrade || return 1
	# Security-relevant packages decide the stage; conveniences are best effort.
	apt_install_each ca-certificates curl gnupg fail2ban python3-systemd ufw \
		unattended-upgrades || return 1
	apt_install_each git jq zsh neovim tmux ripgrep fd-find htop ncdu unzip \
		rsync build-essential pkg-config restic apt-listchanges || true
}

s_users() {
	# zsh is a convenience install; a login shell that doesn't exist would lock
	# tom out, so fall back to bash.
	local shell=/bin/bash
	[ -x /usr/bin/zsh ] && shell=/usr/bin/zsh
	id tom >/dev/null 2>&1 || useradd -m -s "$shell" tom || return 1
	usermod -aG sudo tom
	chsh -s "$shell" tom
	# An empty .zshrc stops zsh's new-user wizard on first login.
	[ -e /home/tom/.zshrc ] || install -m 644 -o tom -g tom /dev/null /home/tom/.zshrc
	install -d -m 700 -o tom -g tom /home/tom/.ssh
	add_keys /home/tom/.ssh/authorized_keys
	chown tom:tom /home/tom/.ssh/authorized_keys
	install -d -m 700 /root/.ssh
	add_keys /root/.ssh/authorized_keys
	# Key-only logins and no password on the account, so sudo can't prompt.
	echo 'tom ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/90-tom
	chmod 440 /etc/sudoers.d/90-tom
	visudo -cf /etc/sudoers.d/90-tom
}

s_ssh() {
	# Close password logins only once tom has a key; the root password is
	# random and unknown, so a failed key write must not lock everyone out.
	if [ -s /home/tom/.ssh/authorized_keys ]; then
		# 10- sorts ahead of the image's 50-cloud-init.conf, and sshd keeps the
		# first value it reads for each keyword.
		cat >/etc/ssh/sshd_config.d/10-penish.conf <<-'EOF'
			PasswordAuthentication no
			KbdInteractiveAuthentication no
			PermitRootLogin prohibit-password
			X11Forwarding no
			MaxAuthTries 4
		EOF
	fi
	sshd -t && { systemctl try-restart ssh.service || systemctl try-restart sshd.service || true; }
}

s_fail2ban() {
	cat >/etc/fail2ban/jail.d/penish.local <<-'EOF'
		[DEFAULT]
		bantime = 1h
		findtime = 10m
		maxretry = 5
		backend = systemd

		[sshd]
		enabled = true
	EOF
	systemctl enable fail2ban && systemctl restart fail2ban
}

s_ufw() {
	# Second layer behind the Hostinger network firewall, which is the one that
	# limits 80/443 to Cloudflare.
	ufw --force reset >/dev/null &&
		ufw default deny incoming &&
		ufw default allow outgoing &&
		ufw allow 22/tcp &&
		ufw allow 80/tcp &&
		ufw allow 443/tcp &&
		ufw --force enable
}

s_unattended() {
	cat >/etc/apt/apt.conf.d/20auto-upgrades <<-'EOF'
		APT::Periodic::Update-Package-Lists "1";
		APT::Periodic::Unattended-Upgrade "1";
		APT::Periodic::AutocleanInterval "7";
	EOF
	# 09:30 UTC is 02:30 or 03:30 in Colorado.
	cat >/etc/apt/apt.conf.d/52penish-unattended <<-'EOF'
		Unattended-Upgrade::Automatic-Reboot "true";
		Unattended-Upgrade::Automatic-Reboot-Time "09:30";
		Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
		Unattended-Upgrade::Remove-Unused-Dependencies "true";
	EOF
	systemctl enable --now unattended-upgrades
}

s_swap() {
	# Headroom for the embedding worker's peaks, not working memory.
	if ! swapon --show | grep -q .; then
		fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile || return 1
		grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
	fi
	echo 'vm.swappiness = 10' >/etc/sysctl.d/60-penish.conf
	sysctl --system >/dev/null
}

s_appuser() {
	id penish >/dev/null 2>&1 ||
		useradd --system --home-dir "$SRV" --shell /usr/sbin/nologin penish || return 1
	install -d -m 755 -o penish -g penish "$SRV" "$SRV/app" "$SRV/media"
	install -d -m 750 -o penish -g penish "$SRV/backups"
	install -d -m 755 "$WWW"
	usermod -aG penish tom
}

s_postgres() {
	apt_install postgresql postgresql-contrib || return 1
	local v
	v=$(pg_lsclusters -h | awk '{print $1}' | sort -n | tail -1)
	[ -n "$v" ] || return 1
	apt_install "postgresql-${v}-pgvector" || return 1
	cat >"/etc/postgresql/${v}/main/conf.d/50-penish.conf" <<-'EOF'
		# Sized for a 16 GB box shared with the app, an embedding worker and Caddy.
		shared_buffers = 4GB
		effective_cache_size = 10GB
		maintenance_work_mem = 1GB
		work_mem = 32MB
		random_page_cost = 1.1
	EOF
	systemctl restart postgresql || return 1
	for _ in $(seq 1 30); do
		pg_isready -q && break
		sleep 1
	done
	# Peer auth over the local socket: the penish system user maps to the
	# penish role, tom to a superuser role. No passwords exist to leak.
	runuser -u postgres -- psql -v ON_ERROR_STOP=1 <<-'SQL' || return 1
		DO $$
		BEGIN
		  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'penish') THEN
		    CREATE ROLE penish LOGIN;
		  END IF;
		  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tom') THEN
		    CREATE ROLE tom LOGIN SUPERUSER;
		  END IF;
		END
		$$;
	SQL
	runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'penish'" | grep -q 1 ||
		runuser -u postgres -- createdb -O penish penish || return 1
	runuser -u postgres -- psql -d penish -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector;'
}

s_uv() {
	curl -LsSf https://astral.sh/uv/install.sh |
		env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh &&
		/usr/local/bin/uv --version
}

install_caddy() {
	# Upstream's repo first (current Caddy); the distro build is the fallback.
	if curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
		gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg &&
		curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
			-o /etc/apt/sources.list.d/caddy-stable.list &&
		apt-get -o DPkg::Lock::Timeout=600 update && apt_install caddy; then
		return 0
	fi
	rm -f /etc/apt/sources.list.d/caddy-stable.list
	apt-get -o DPkg::Lock::Timeout=600 update && apt_install caddy
}

write_placeholder() {
	install -d -m 755 "$WWW"
	cat >"$WWW/index.html" <<-'EOF'
		<!doctype html>
		<html lang="en">
		<head>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
		<title>peni.sh</title>
		<style>
		  :root { color-scheme: dark; }
		  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
		         background: #000; color: #3f3;
		         font: 15px/1.6 ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; }
		  main { max-width: 38rem; padding: 2rem 1.25rem; }
		  h1 { font-size: 1rem; font-weight: normal; letter-spacing: .2em; text-transform: uppercase; }
		  p { margin: 0 0 1em; }
		  .dim { color: #1a7a1a; }
		  .cursor { animation: blink 1s steps(1) infinite; }
		  @keyframes blink { 50% { opacity: 0; } }
		  @media (prefers-reduced-motion: reduce) { .cursor { animation: none; } }
		</style>
		</head>
		<body>
		<main>
		<h1>peni.sh</h1>
		<p>NOTICE. The archive is closed for inventory.</p>
		<p>Records formerly held under another name are in transit. Nothing has been
		deleted. Location data has been stripped and will be reassigned on arrival.</p>
		<p>No date of reopening has been set. The archive does not set dates.</p>
		<p class="dim">Your visit has been noted.<span class="cursor">_</span></p>
		</main>
		</body>
		</html>
	EOF
	cat >"$WWW/index.txt" <<-'EOF'
		peni.sh

		NOTICE. The archive is closed for inventory.

		Records formerly held under another name are in transit.
		Nothing has been deleted.

		When the archive reopens, this address will answer a terminal in kind.
		Until then, your request has been received and filed.
	EOF
}

write_caddyfiles() {
	local sslip=$1
	# Steady state: a catch-all on :80 that Cloudflare proxies to until the app
	# and a real domain block replace it.
	cat >/etc/caddy/Caddyfile.steady <<-'EOF'
		# Written by infra/bootstrap.sh. The first app deploy replaces this file.

		(placeholder) {
			root * /srv/peni.sh/www
			@cli header_regexp User-Agent (?i)^(curl|wget|httpie|xh)/
			rewrite @cli /index.txt
			file_server
		}

		:80 {
			import placeholder
		}
	EOF
	cp /etc/caddy/Caddyfile.steady /etc/caddy/Caddyfile
	# Temporary: a real certificate for <ip>.sslip.io, so the install can be
	# checked over HTTPS before any DNS exists. penish-bootstrap-cleanup.timer
	# removes it 24 hours after install.
	if [ -n "$sslip" ]; then
		printf '\n%s {\n\timport placeholder\n}\n' "$sslip" >>/etc/caddy/Caddyfile
	fi
}

s_caddy() {
	install_caddy || return 1
	local ip sslip=''
	ip=$(public_ipv4)
	[ -n "$ip" ] && sslip="${ip//./-}.sslip.io"
	write_placeholder
	write_caddyfiles "$sslip"
	# The heredocs above lose their tabs to <<-; restore canonical layout.
	caddy fmt --overwrite /etc/caddy/Caddyfile.steady || true
	caddy fmt --overwrite /etc/caddy/Caddyfile || true
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile || return 1
	systemctl enable caddy && systemctl restart caddy
}

s_cleanup_timer() {
	local when
	when=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S UTC"))') || return 1
	cat >/usr/local/sbin/penish-bootstrap-cleanup <<-'EOF'
		#!/bin/sh
		# Retire the temporary sslip.io site and the public bootstrap artifacts
		# once the install has had a day to be checked.
		set -eu
		cp /etc/caddy/Caddyfile.steady /etc/caddy/Caddyfile
		rm -f /srv/peni.sh/www/healthz.json /srv/peni.sh/www/bootstrap.log
		systemctl reload caddy
		systemctl disable penish-bootstrap-cleanup.timer
	EOF
	chmod 755 /usr/local/sbin/penish-bootstrap-cleanup
	cat >/etc/systemd/system/penish-bootstrap-cleanup.service <<-'EOF'
		[Unit]
		Description=Retire peni.sh bootstrap verification artifacts

		[Service]
		Type=oneshot
		ExecStart=/usr/local/sbin/penish-bootstrap-cleanup
	EOF
	cat >/etc/systemd/system/penish-bootstrap-cleanup.timer <<-EOF
		[Unit]
		Description=Retire peni.sh bootstrap verification artifacts

		[Timer]
		OnCalendar=$when
		Persistent=true

		[Install]
		WantedBy=timers.target
	EOF
	systemctl daemon-reload && systemctl enable --now penish-bootstrap-cleanup.timer
}

s_readme() {
	cat >"$SRV/README.md" <<-'EOF'
		# peni (peni.sh host)

		Built by infra/bootstrap.sh in tfpickard/peni.sh. Ubuntu 26.04 LTS, UTC.

		- Login: `ssh tom@<host>` with a key. Password logins are off. `tom` has
		  passwordless sudo.
		- Root keys: two ed25519 keys carried over from before the rebuild.
		  `unlabelled-key-from-previous-root` had no name in Hostinger; delete it
		  from /root/.ssh/authorized_keys and /home/tom/.ssh/authorized_keys if
		  you don't recognize it.
		- Firewall: Hostinger's network firewall allows 22 from anywhere and
		  80/443 only from Cloudflare. ufw allows 22/80/443 behind it.
		- fail2ban watches sshd. unattended-upgrades applies updates daily and
		  reboots at 09:30 UTC when a reboot is required.
		- Postgres with pgvector: database `penish`, owned by the `penish` role,
		  peer auth over the local socket. `tom` is a superuser role, so plain
		  `psql penish` works. Tuning lives in conf.d/50-penish.conf.
		- App layout: /srv/peni.sh/{app,media,backups} owned by the `penish`
		  system user; /srv/peni.sh/www holds the placeholder page.
		- Caddy serves the placeholder on :80 (Cloudflare terminates TLS for
		  now). /etc/caddy/Caddyfile.steady is the post-bootstrap config.
		- Installed but not configured: restic (needs an R2 bucket and
		  credentials), uv (/usr/local/bin/uv).
		- Bootstrap log: /post_install.log.
	EOF
}

write_healthz() {
	local pg_v caddy_v uv_v vec_v keys name first=1
	keys=$(grep -c '^ssh-' /home/tom/.ssh/authorized_keys 2>/dev/null)
	pg_v=$(runuser -u postgres -- psql -tAc 'SHOW server_version' 2>/dev/null | awk '{print $1}')
	vec_v=$(runuser -u postgres -- psql -d penish -tAc "SELECT extversion FROM pg_extension WHERE extname = 'vector'" 2>/dev/null)
	caddy_v=$(caddy version 2>/dev/null | awk '{print $1}')
	uv_v=$(/usr/local/bin/uv --version 2>/dev/null | awk '{print $2}')
	{
		printf '{\n'
		printf '  "finished_at": "%s",\n' "$(date -u +%FT%TZ)"
		# shellcheck disable=SC1091
		printf '  "os": "%s",\n' "$(. /etc/os-release && printf '%s' "$PRETTY_NAME")"
		printf '  "postgres": "%s",\n' "${pg_v:-}"
		printf '  "pgvector": "%s",\n' "${vec_v:-}"
		printf '  "caddy": "%s",\n' "${caddy_v:-}"
		printf '  "uv": "%s",\n' "${uv_v:-}"
		printf '  "tom_keys": %s,\n' "${keys:-0}"
		printf '  "password_auth_off": %s,\n' "$([ -f /etc/ssh/sshd_config.d/10-penish.conf ] && echo true || echo false)"
		printf '  "stages": {'
		for name in "${STAGES[@]}"; do
			[ $first -eq 1 ] || printf ','
			first=0
			printf '\n    "%s": "%s"' "$name" "${RESULT[$name]}"
		done
		printf '\n  }\n}\n'
	} >"$WWW/healthz.json"
	cp /post_install.log "$WWW/bootstrap.log" 2>/dev/null || true
	chmod 644 "$WWW/healthz.json" "$WWW/bootstrap.log" 2>/dev/null || true
}

stage base s_base
stage users s_users
stage ssh s_ssh
stage fail2ban s_fail2ban
stage ufw s_ufw
stage unattended s_unattended
stage swap s_swap
stage appuser s_appuser
stage postgres s_postgres
stage uv s_uv
stage caddy s_caddy
stage cleanup_timer s_cleanup_timer
stage readme s_readme
write_healthz

echo "=== bootstrap finished ($(date -u +%FT%TZ))"
for name in "${STAGES[@]}"; do
	echo "  $name: ${RESULT[$name]}"
done
