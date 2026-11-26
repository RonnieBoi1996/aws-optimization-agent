#!/bin/bash
# Synthetic load for the sandbox "lightly used" instances (M4 eval fixtures).
#
# Runs one CPU-bound process for 8 minutes every hour. On a 2-vCPU instance
# that's about 7% average CPU: above the idle threshold (5%), so the instance
# isn't "idle", but far below what a t3.small is sized for, so Compute
# Optimizer should call it over-provisioned. Needs no network or packages.
set -euo pipefail

cat > /etc/systemd/system/sandbox-load.service <<'UNIT'
[Unit]
Description=Optimizer sandbox synthetic CPU load

[Service]
Type=oneshot
ExecStart=/usr/bin/timeout 480 /usr/bin/sha256sum /dev/zero
# timeout exits with 124 when it stops the command; that's the normal case here.
SuccessExitStatus=124
UNIT

cat > /etc/systemd/system/sandbox-load.timer <<'UNIT'
[Unit]
Description=Run sandbox-load every hour

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now sandbox-load.timer
