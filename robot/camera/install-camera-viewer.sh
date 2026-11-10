#!/usr/bin/env bash
# install-camera-viewer.sh — install and enable the OAK-D MJPEG viewer service.
#
# RUN AS ROOT:  sudo bash /tmp/install-camera-viewer.sh
#
# Idempotent. Verifies the service actually reaches "active" AND that /healthz
# answers, rather than trusting "systemctl start" alone - a unit can be active
# while the camera is failing to open, which is the failure mode that wastes the
# most time.

set -eu

UNIT_SRC="${1:-/tmp/oak-viewer.service}"
UNIT_DST="/etc/systemd/system/oak-viewer.service"
PORT=8080

if [ "$(id -u)" -ne 0 ]; then
    echo "run me as root:  sudo bash $0" >&2
    exit 1
fi

if [ ! -f "$UNIT_SRC" ]; then
    echo "unit file not found: $UNIT_SRC" >&2
    exit 1
fi

echo "== installing the unit =="
install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now oak-viewer.service

echo
echo "== waiting for it to come up =="
for i in $(seq 1 20); do
    state=$(systemctl is-active oak-viewer.service || true)
    if [ "$state" = "active" ]; then
        break
    fi
    sleep 1
done
echo "  state: $(systemctl is-active oak-viewer.service || true)"

echo
echo "== is the camera actually open, or just the process running? =="
# Poll rather than sleep a fixed time: opening the OAK and uploading firmware
# takes ~5s, so a short fixed sleep reports a false failure on a healthy unit.
# wget, not curl: curl is NOT installed on this L4T image.
health=""
for i in $(seq 1 20); do
    if health=$(wget -q -O - --timeout=3 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null); then
        case "$health" in
            *frames=*) break ;;
        esac
    fi
    health=""
    sleep 1
done

if [ -n "$health" ]; then
    echo "$health" | sed 's/^/  /'
else
    echo "  NO RESPONSE from /healthz after 20s - the process is up but the"
    echo "  camera is not streaming."
    echo "  Recent log:"
    journalctl -u oak-viewer.service -n 15 --no-pager | sed 's/^/    /'
    exit 1
fi

echo
echo "== endpoints =="
printf "  %-28s %s\n" "/"              "HTML view"
printf "  %-28s %s\n" "/stream"        "MJPEG stream"
printf "  %-28s %s\n" "/snapshot.jpg"  "single frame"
printf "  %-28s %s\n" "/healthz"       "status text"
echo
echo "  browse to:  http://$(hostname -I | awk '{print $1}'):${PORT}/"
echo
echo "done."
