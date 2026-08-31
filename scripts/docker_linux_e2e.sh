#!/bin/bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Linux X11 GUI e2e driver: deps -> copy project -> Xvfb+openbox -> checks.
set -e
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq libportaudio2 xvfb xclip xdotool openbox wmctrl gcc linux-libc-dev >/dev/null 2>&1
mkdir /app
cd /src
tar cf - --exclude=.venv --exclude=.git --exclude=dist --exclude=build --exclude=__pycache__ . | (cd /app && tar xf -)
cd /app
pip install -q '.[dev]' 2>&1 | grep -E 'ERROR' || true
echo '=== Linux X11 GUI e2e (Xvfb + openbox) ==='
cat > /xrun.sh <<'INNER'
#!/bin/bash
openbox >/dev/null 2>&1 &
sleep 1
timeout 120 python /app/scripts/linux_gui_e2e.py
INNER
chmod +x /xrun.sh
xvfb-run -a /xrun.sh
