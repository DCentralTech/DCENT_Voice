#!/bin/bash
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# Full kernel-level Wayland typing e2e: ydotoold (uinput) -> wlroots libinput ->
# sway keybinding. Verifies the injector's ydotool paste path actually delivers
# keystrokes through the compositor, not just that the binary runs.
#
# Needs a privileged container (uinput) and an image whose repos carry ydotool
# (Ubuntu universe; Debian stable doesn't):
#   MSYS_NO_PATHCONV=1 docker run --rm --privileged -v "<repo>:/src:ro" \
#     ubuntu:24.04 bash /src/scripts/docker_linux_ydotool_check.sh
set -e
apt-get update -qq >/dev/null 2>&1
# ydotoold (the uinput daemon) is a separate package from the ydotool client;
# seatd gives wlroots' libinput backend a seat inside the container; udev must
# run because libinput discovers devices through udev enumeration, not by
# scanning /dev/input.
apt-get install -y -qq sway ydotool ydotoold wl-clipboard seatd udev >/dev/null 2>&1

export XDG_RUNTIME_DIR=/tmp/xdg
mkdir -p "$XDG_RUNTIME_DIR" && chmod 0700 "$XDG_RUNTIME_DIR"

/lib/systemd/systemd-udevd --daemon >/dev/null 2>&1 || udevd --daemon >/dev/null 2>&1
seatd >/dev/null 2>&1 &
sleep 1

# Start the uinput daemon FIRST so its virtual keyboard exists when the
# compositor enumerates input devices, then let udev register it.
ydotoold --socket-path=/tmp/.ydotool_socket >/dev/null 2>&1 &
sleep 2
udevadm trigger --action=add >/dev/null 2>&1 || true
udevadm settle --timeout=5 >/dev/null 2>&1 || true
# udevd populates its database (which libinput enumerates) but does not mknod
# into the container's tmpfs /dev — create the nodes from sysfs ourselves.
mkdir -p /dev/input
for ev in /sys/class/input/event*; do
  [ -e "$ev" ] || continue
  node="/dev/input/$(basename "$ev")"
  if [ ! -e "$node" ]; then
    dev=$(cat "$ev/dev")
    mknod "$node" c "${dev%%:*}" "${dev##*:}"
  fi
done
ls /dev/input/ || true

# sway config: pressing "a" proves a synthesized keystroke traversed
# uinput -> libinput -> compositor.
cat > /sway.conf <<'CONF'
bindsym a exec touch /tmp/key-a-received
CONF

WLR_BACKENDS=libinput,headless WLR_RENDERER=pixman \
  sway --config /sway.conf >/tmp/sway.log 2>&1 &
sleep 3

echo '=== ydotool kernel-level typing e2e (uinput -> sway) ==='
rm -f /tmp/key-a-received
YDOTOOL_SOCKET=/tmp/.ydotool_socket ydotool key 30:1 30:0   # KEY_A press+release
sleep 1.5
if [ -f /tmp/key-a-received ]; then
  echo '[PASS] synthesized keystroke reached the compositor keybinding'
else
  echo '[FAIL] keystroke did not reach the compositor'
  grep -iE 'libinput|seat|input|error' /tmp/sway.log | head -12 || true
  exit 1
fi

# And the injector's actual paste chord (ctrl+v = 29, 47) must not error.
YDOTOOL_SOCKET=/tmp/.ydotool_socket ydotool key 29:1 47:1 47:0 29:0
echo '[PASS] injector paste chord (ctrl+v) synthesized without error'
echo
echo 'ALL YDOTOOL CHECKS PASSED'
