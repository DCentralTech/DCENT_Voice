#!/bin/bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Linux desktop e2e driver: overlay click-through + tray docking under
# Xvfb+openbox, plus the Wayland clipboard path under headless Weston.
#
# From Windows (Docker Desktop):
#   MSYS_NO_PATHCONV=1 docker run --rm -v "<repo>:/src:ro" python:3.11-slim \
#     bash /src/scripts/docker_linux_desktop_check.sh
set -e
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq xvfb openbox stalonetray x11-utils sway wl-clipboard wtype \
  python3-pip python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  gir1.2-javascriptcoregtk-4.1 >/dev/null 2>&1
# pywebview/pystray must run on Debian's python3 — the interpreter python3-gi
# was built for; the docker /usr/local python would load a second libpython.
/usr/bin/python3 -m pip install -q --break-system-packages \
  pywebview pystray pillow python-xlib >/dev/null 2>&1

mkdir /app
cd /src
tar cf - --exclude=.venv --exclude=.git --exclude=dist --exclude=build --exclude=__pycache__ . | (cd /app && tar xf -)

echo '=== X11: overlay click-through + tray docking (Xvfb + openbox) ==='
cat > /xrun.sh <<'INNER'
#!/bin/bash
openbox >/dev/null 2>&1 &
sleep 1
timeout 90 /usr/bin/python3 /app/scripts/linux_desktop_e2e.py
INNER
chmod +x /xrun.sh
xvfb-run -a -s "-screen 0 1280x800x24" /xrun.sh

echo '=== Wayland: full inject() e2e under headless sway (wlroots) ==='
# sway implements zwlr_data_control, which wl-clipboard uses to work without
# keyboard focus — Weston lacks it, so its clipboard needs a focused surface.
# A ctrl+v compositor keybinding proves the wtype-synthesized paste chord is
# actually delivered through the compositor's input pipeline.
export XDG_RUNTIME_DIR=/tmp/xdg
mkdir -p "$XDG_RUNTIME_DIR" && chmod 0700 "$XDG_RUNTIME_DIR"
cat > /sway.conf <<'CONF'
bindsym Ctrl+v exec touch /tmp/paste-chord-received
CONF
WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
  sway --config /sway.conf >/dev/null 2>&1 &
sleep 3
WL_SOCKET=$(basename "$(ls "$XDG_RUNTIME_DIR"/wayland-* 2>/dev/null | grep -v '\.lock' | head -1)")
echo "wayland socket: ${WL_SOCKET:-none}"
WAYLAND_DISPLAY="$WL_SOCKET" XDG_SESSION_TYPE=wayland PYTHONPATH=/app/src \
timeout 60 /usr/bin/python3 - <<'PY'
from dcent_voice.inject.linux import (
    LinuxClipboardPasteInjector,
    _clipboard_tools,
    _run,
    is_wayland,
)

failures = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{f' — {detail}' if detail else ''}")
    if not condition:
        failures.append(name)


import os
import time

check("session detected as Wayland", is_wayland())
set_argv, get_argv = _clipboard_tools()
check("wl-copy/wl-paste selected", set_argv == ["wl-copy"] and get_argv is not None)
_run(set_argv, text="wayland-payload")
check("wayland clipboard round-trip", (_run(get_argv) or "").strip() == "wayland-payload")

# Full injector path: set clipboard -> wtype ctrl+v -> restore previous.
_run(set_argv, text="previous-content")
LinuxClipboardPasteInjector(restore_previous=True).inject("injected-payload")
time.sleep(1.0)
check(
    "paste chord delivered through the compositor",
    os.path.exists("/tmp/paste-chord-received"),
)
check(
    "clipboard restored after wayland paste",
    (_run(get_argv) or "").strip() == "previous-content",
)

# Without wtype/ydotool the injector must fail loudly, not silently no-op.
import shutil as _shutil

real_which = _shutil.which
_shutil.which = lambda name: None if name in {"wtype", "ydotool", "xdotool"} else real_which(name)
try:
    LinuxClipboardPasteInjector().inject("x")
    check("inject without a paste tool raises a clear error", False)
except RuntimeError as exc:
    check("inject without a paste tool raises a clear error", "wtype" in str(exc))
finally:
    _shutil.which = real_which
print("\n" + ("FAILED: " + ", ".join(failures) if failures else "ALL WAYLAND CHECKS PASSED"))
raise SystemExit(1 if failures else 0)
PY
