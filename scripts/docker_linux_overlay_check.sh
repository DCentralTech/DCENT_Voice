#!/bin/bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Render the real DCENT_Voice overlay under Xvfb via webkit2gtk and screenshot it.
set -e
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq xvfb openbox imagemagick x11-apps python3-pip \
  python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-javascriptcoregtk-4.1 >/dev/null 2>&1
# pywebview must run on Debian's python3 — the same interpreter python3-gi was
# built for; mixing it with the docker /usr/local python loads two libpythons.
/usr/bin/python3 -m pip install -q --break-system-packages pywebview >/dev/null 2>&1
mkdir /app
cd /src
tar cf - --exclude=.venv --exclude=.git --exclude=dist --exclude=build --exclude=__pycache__ . | (cd /app && tar xf -)
cat > /render.py <<'PY'
import os
import subprocess
import threading
import time

import webview  # noqa: E402

window = webview.create_window(
    "DCENT_Voice",
    url="/app/src/dcent_voice/ui/web/overlay.html",
    width=420,
    height=180,
    frameless=True,
    on_top=True,
)


def drive() -> None:
    time.sleep(5)
    # Put the overlay into the voice-reactive listening state like the app does.
    try:
        window.evaluate_js("window.dcent && window.dcent.setState('listening')")
        window.evaluate_js(
            "window.dcent && window.dcent.setLevel(0.8)"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"evaluate_js: {exc}", flush=True)
    time.sleep(2)
    subprocess.run(["import", "-window", "root", "/out/linux_overlay.png"], check=False)
    print("screenshot taken", flush=True)
    os._exit(0)


threading.Thread(target=drive, daemon=True).start()
webview.start(gui="gtk")
PY

cat > /xrun.sh <<'INNER'
#!/bin/bash
openbox >/dev/null 2>&1 &
sleep 1
timeout 60 /usr/bin/python3 /render.py
INNER
chmod +x /xrun.sh
echo '=== rendering overlay on Linux (webkit2gtk under Xvfb) ==='
xvfb-run -a -s "-screen 0 1280x800x24" /xrun.sh || true
ls -la /out/
