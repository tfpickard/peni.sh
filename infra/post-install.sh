#!/bin/bash
# peni.sh post-install loader. Fetches infra/bootstrap.sh from
# github.com/tfpickard/peni.sh at a pinned commit, refuses to run it unless
# the sha256 matches, then runs it. Output lands in /post_install.log.
set -euo pipefail
url=https://raw.githubusercontent.com/tfpickard/peni.sh/cfcc63ddedfd23512733dfef28e4a2568588a5ce/infra/bootstrap.sh
want=e626d233caad167929faba72ec2ad07a01fadecd220078592c0d94faf750417f
for i in 1 2 3 4 5 6; do
  curl -fsSL --retry 3 -o /root/bootstrap.sh "$url" && break
  sleep $((i * 10))
done
echo "$want  /root/bootstrap.sh" | sha256sum -c -
exec bash /root/bootstrap.sh
