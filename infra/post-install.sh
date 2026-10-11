#!/bin/bash
# peni.sh post-install loader. Fetches infra/bootstrap.sh from
# github.com/tfpickard/peni.sh at a pinned commit, refuses to run it unless
# the sha256 matches, then runs it. Output lands in /post_install.log.
set -euo pipefail
url=https://raw.githubusercontent.com/tfpickard/peni.sh/f49e1283a04128589e6d2ae34736750c20d81231/infra/bootstrap.sh
want=9b3e61f79f035038c29055f9c06180a93002d0a872fe85125e23fd70d5de2fba
for i in 1 2 3 4 5 6; do
  curl -fsSL --retry 3 -o /root/bootstrap.sh "$url" && break
  sleep $((i * 10))
done
echo "$want  /root/bootstrap.sh" | sha256sum -c -
exec bash /root/bootstrap.sh
