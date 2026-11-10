#!/usr/bin/env bash
# install-camera-udev.sh — install the Luxonis udev rule so the `jetson` user can
# talk to the OAK-D without root.
#
# RUN AS ROOT:  sudo bash /tmp/install-camera-udev.sh
#
# Idempotent: safe to run again. Verifies the mode change took effect on the
# already-connected device rather than assuming `udevadm trigger` worked.

set -eu

RULE_SRC="${1:-/tmp/80-movidius.rules}"
RULE_DST="/etc/udev/rules.d/80-movidius.rules"

if [ "$(id -u)" -ne 0 ]; then
    echo "run me as root:  sudo bash $0" >&2
    exit 1
fi

if [ ! -f "$RULE_SRC" ]; then
    echo "rule file not found: $RULE_SRC" >&2
    exit 1
fi

echo "== installing the rule =="
install -o root -g root -m 0644 "$RULE_SRC" "$RULE_DST"
ls -la "$RULE_DST"

echo
echo "== reloading udev =="
udevadm control --reload-rules
udevadm trigger
sleep 2

echo
echo "== did it take effect on the already-connected device? =="
found=0
for node in /dev/bus/usb/*/*; do
    [ -c "$node" ] || continue
    # Match by the USB device path in sysfs rather than by major/minor guessing.
    dev=$(basename "$node")
    bus=$(basename "$(dirname "$node")")
    sys="/sys/bus/usb/devices"
    for d in "$sys"/*; do
        [ -f "$d/idVendor" ] || continue
        [ "$(cat "$d/idVendor" 2>/dev/null)" = "03e7" ] || continue
        if [ "$(cat "$d/busnum" 2>/dev/null)" = "$((10#$bus))" ] && \
           [ "$(cat "$d/devnum" 2>/dev/null)" = "$((10#$dev))" ]; then
            mode=$(stat -c '%a %U:%G' "$node")
            echo "  $node (usb path $(basename "$d")) -> $mode"
            found=1
        fi
    done
done

if [ "$found" -eq 0 ]; then
    echo "  no OAK device node found right now."
    echo "  Is the camera plugged in? If it is, unplug and replug it so the"
    echo "  rule applies on a fresh enumeration."
else
    echo
    echo "  MODE should be 666. If it still shows 664 root:root, unplug and"
    echo "  replug the camera (the rule applies on device add)."
fi

echo
echo "done."
