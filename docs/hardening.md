# hardening

Host `peni`, 187.77.0.51, tailnet 100.80.26.40. Applied 2026-11-11 UTC. `ops/harden.sh` reproduces it and is safe to re-run; unchanged steps print `ok`.

```
scp ops/harden.sh root@100.80.26.40: && ssh root@100.80.26.40 ./harden.sh
```

If it changes sshd or ufw it arms `penish-harden-deadman.timer` (10 min). Open a new tailnet session, then `./harden.sh confirm`. Without the confirm, the timer puts public 22 back and moves the sshd drop-in aside.

## lockout

SSH is reachable only over the tailnet. If that fails:

1. Hostinger hPanel, VPS, Browser terminal. Log in as `root` with the root password. Only root has a password, on purpose. If it is lost, hPanel can reset it (uses `qemu-guest-agent`; leave that running).
2. `ufw allow 22/tcp`. The Hostinger network firewall already allows 22, so public ssh works again.
3. Still nothing: hPanel recovery mode, mount the disk, fix `/etc/ssh/sshd_config.d/` or `/etc/ufw/`.

The tailnet path ends when the node key expires: **2027-05-10**. Turn off key expiry for `peni` in the Tailscale admin console.

Do not run `tailscale up --ssh` casually. Tailscale SSH takes over port 22 on the tailnet address and uses tailnet ACLs, not `authorized_keys`. With public 22 closed, a missing or `check`-mode ssh ACL is a lockout. Test from a second session first.

`scripts/bootstrap_vps.sh` adds `ufw allow 22/tcp` unless run with `CLOSE_PUBLIC_SSH=1`, and keeps 80/443 unless `CLOSE_WEB_PORTS=1`. Pass both.

## changes

| what | detail | roll back |
|---|---|---|
| docker | Docker apt repo (`resolute`), engine 29.9, compose v5. `/etc/docker/daemon.json`: log driver `local` 20m x 5, live-restore, no-new-privileges, no userland proxy, pools from `172.20.0.0/14` in /24s | `apt-get purge docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`; `rm /etc/apt/sources.list.d/docker.sources` |
| caddy | disabled, masked. Package and `/etc/caddy/*` kept: the unit, `/root/bootstrap.sh` and the bootstrap cleanup script reference the Caddyfile | `systemctl unmask caddy && systemctl enable --now caddy` |
| host postgres | `postgresql` and `postgresql@18-main` disabled, masked. Database `penish` had no relations. Packages and data dir kept | `systemctl unmask postgresql postgresql@18-main && systemctl enable --now postgresql` |
| sshd | `/etc/ssh/sshd_config.d/10-hardening.conf`: no passwords or keyboard-interactive, root by key only, no X11 or agent forwarding, MaxAuthTries 3, LoginGraceTime 20, keepalive 300 x 2, kex mlkem768x25519 / sntrup761x25519 / curve25519, ciphers chacha20-poly1305 / aes-gcm, etm MACs. Sorts first, so it beats `50-cloud-init.conf` (`PasswordAuthentication yes`) | `rm` the file, `sshd -t`, `systemctl reload ssh` |
| ufw | default deny in. Only rule: allow in on `tailscale0`. 22/80/443 removed. fail2ban unchanged | `ufw allow 22/tcp` (and 80/443 if needed) |
| sysctl | `/etc/sysctl.d/90-hardening.conf`: no redirects or source routing, syncookies, log martians, kptr_restrict 2, dmesg_restrict, unprivileged BPF off, BPF JIT hardening, fs.protected_*. ip_forward untouched. rp_filter stays loose (2): strict drops Tailscale's fwmark-routed traffic | `rm` the file, `sysctl -p /root/sysctl-before-hardening.conf`. `kernel.unprivileged_bpf_disabled=1` needs a reboot to undo |
| sudo | `/etc/sudoers.d/deploy` (`NOPASSWD:ALL`) removed, copy at `/root/sudoers.d-deploy.removed`. tom keeps `90-tom` | `install -m 0440 /root/sudoers.d-deploy.removed /etc/sudoers.d/deploy && visudo -c` |
| users | deploy added to `docker`. tom, deploy, penish were already password-locked; key logins work (UsePAM yes). root's password left set for the console | `gpasswd -d deploy docker` |
| auditd | installed. `/etc/audit/rules.d/50-penish.rules` watches sshd config, sudoers, passwd/shadow/group, `/etc/docker`, and `connect()` to `/run/docker.sock` (a plain watch misses clients). `ausearch --input-logs -k docker_socket -i` | `rm` the rules file, `augenrules --load`, `apt-get purge auditd` |
| unattended-upgrades | unchanged: active, reboots at 09:30 UTC when required. Docker's repo is not in Allowed-Origins, so Docker updates are manual | none |

## notes

- Membership of `docker` is root-equivalent. deploy has no sudo, but `docker run -v /:/host` gets the same result. The audit rule on the socket is the record of it.
- Docker writes its own iptables rules ahead of ufw. A published port (`ports:` in compose) is reachable from the internet whatever ufw says. Publish nothing; cloudflared reaches the api over the compose network. If a port is ever needed, bind it to `127.0.0.1` or add a rule to the `DOCKER-USER` chain.
- Listening after hardening: sshd on 22 (ufw drops it except on tailscale0), tailscaled on its tailnet addresses, systemd-resolved and chrony on loopback, cloudflared metrics on 127.0.0.1:20241.
- A host `cloudflared.service` (apt package, `--token-file /etc/cloudflared/token`) was installed on 2026-11-11 alongside this work. `docs/ops.md` plans cloudflared as a compose service. Run one or the other.
- `/root/.ssh/authorized_keys`, `/home/tom/.ssh/authorized_keys` and `/home/deploy/.ssh/authorized_keys` each hold `SHA256:z+wEA2OeSB4pyj6hcmwPVe8kzeUsWj+uEpLgSSKYTHI` (belzebu, `~/.ssh/id_ed25519`) and `SHA256:lQ86MO+xjoMsRsnTaR3Xn9yKtIDnK0W+6iLzACdnneI` (`unlabelled-key-from-previous-root`, not on belzebu, no logins in the journal since 2026-11-04). Nothing was removed.
