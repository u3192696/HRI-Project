#!/usr/bin/env bash
# demo-check.sh — can I demo the robot right now?
#
# Run this BEFORE walking into the room. It exists because a demo failed once
# for the want of a cable, discovered at the moment it mattered. Everything it
# checks is something that has actually gone wrong at least once.
#
#   ./demo-check.sh              # talk to `orin`
#   ./demo-check.sh athena       # ...via an ssh alias / hostname
#
# Read-only. Makes no changes, touches nothing. Safe to run repeatedly.
# Exit 0 = ready, 1 = not ready, 2 = cannot reach the robot.
set -uo pipefail

HOST="${1:-orin}"
SSH=(ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST")

fail=0
ok()   { printf '  \033[32m ok \033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
hdr()  { printf '\n== %s ==\n' "$1"; }

# --- reachability -------------------------------------------------------
hdr "reaching the robot ($HOST)"
if ! "${SSH[@]}" true 2>/dev/null; then
  bad "cannot ssh to '$HOST' — check the cable/route BEFORE you are in the room"
  printf '\n  RESULT: \033[31mCANNOT DEMO\033[0m — no connection to the robot\n'
  exit 2
fi
ok "ssh to $HOST works"

# One round-trip. Emit LABELLED lines, not positional fields: an empty value
# (e.g. an empty status file) would otherwise shift every later field along.
FACTS=$("${SSH[@]}" '
  echo "uptime=$(cut -d. -f1 /proc/uptime)"
  echo "connectors=$(ls /sys/class/drm/ 2>/dev/null | grep -c -- "-DP-\|^card.-HDMI")"
  echo "dpstatus=$(cat /sys/class/drm/card0-DP-1/status 2>/dev/null || echo missing)"
  echo "kiosk=$(systemctl is-active face-kiosk.service 2>/dev/null || true)"
  echo "ksub=$(systemctl show face-kiosk.service -p SubState --value 2>/dev/null || true)"
  echo "krest=$(systemctl show face-kiosk.service -p NRestarts --value 2>/dev/null || echo 0)"
  echo "gaze=$(systemctl is-active gaze.service 2>/dev/null || true)"
  echo "camera=$(lsusb 2>/dev/null | grep -c "03e7:" || echo 0)"
  echo "statusbytes=$(stat -c%s /run/face/gaze-status.json 2>/dev/null || echo missing)"
  echo "statusfile=$(tr -d "\n\t" < /run/face/gaze-status.json 2>/dev/null || true)"
  echo "facever=$(grep -o "face v[0-9.]*" /home/jetson/robot/face/face.py 2>/dev/null | head -1 || true)"
' 2>/dev/null) || true

get() { printf '%s\n' "$FACTS" | sed -n "s/^$1=//p" | head -1; }

UPTIME=$(get uptime);           CONNECTORS=$(get connectors)
DPSTATUS=$(get dpstatus);       KIOSK=$(get kiosk);        KSUB=$(get ksub)
KREST=$(get krest);             GAZE=$(get gaze);          CAMERA=$(get camera)
STATUSBYTES=$(get statusbytes); STATUSFILE=$(get statusfile)
FACEVER=$(get facever)

# --- the display: the thing that actually failed -------------------------
hdr "the display (this is what failed last time)"
case "${DPSTATUS:-missing}" in
  connected)
    ok "projector is CONNECTED on card0-DP-1" ;;
  disconnected)
    bad "no display on card0-DP-1 — projector unplugged, off, or the cable is missing"
    printf '        this is the exact state that killed the last demo.\n'
    printf '        plug in the DisplayPort cable AND power the projector on.\n' ;;
  *)
    bad "card0-DP-1 status='${DPSTATUS}' (expected 'connected')" ;;
esac
if [ "${CONNECTORS:-0}" -lt 1 ]; then
  warn "no display connectors found at all — check the cable is in the DP port"
fi

# --- the face -----------------------------------------------------------
hdr "the face"
if [ "${KIOSK:-}" = "active" ] && [ "${KSUB:-}" != "auto-restart" ]; then
  ok "face-kiosk is active and running (${FACEVER:-version unknown})"
elif [ "${KSUB:-}" = "auto-restart" ]; then
  bad "face-kiosk is CRASH-LOOPING (${KREST:-?} restarts) — the screen will be blank"
  printf '        usual cause: no display. Fix the display and it recovers on its own.\n'
  printf '        to force it: sudo systemctl restart face-kiosk.service\n'
else
  bad "face-kiosk is '${KIOSK:-unknown}' — no face on the screen"
fi
if [ "${KREST:-0}" -gt 50 ]; then
  warn "restart count is high (${KREST}) — the unit has been cycling; the log is noisy"
fi

# --- gaze ---------------------------------------------------------------
hdr "gaze / perception"
if [ "${CAMERA:-0}" -ge 1 ]; then
  ok "OAK-D camera is present on the USB bus"
else
  bad "OAK-D camera (03e7:2485) NOT on the USB bus — it is unplugged or has no power"
  printf '        gaze cannot run without it: the log shows \"No available devices\".\n'
fi
if [ "${GAZE:-}" = "active" ]; then
  ok "gaze.service is active"
elif [ "${CAMERA:-0}" -lt 1 ]; then
  warn "gaze.service is '${GAZE:-unknown}' — expected, since the camera is absent"
else
  bad "gaze.service is '${GAZE:-unknown}' — the robot cannot see you; no expression changes"
fi
case "${STATUSBYTES:-missing}" in
  missing)
    warn "no /run/face/gaze-status.json — gaze has not published yet" ;;
  "0")
    bad "status file exists but is EMPTY — gaze is up but not writing readings" ;;
  *)
    if printf '%s' "$STATUSFILE" | grep -q has_face; then
      ok "publishing readings: ${STATUSFILE}"
    else
      warn "status file unreadable/unexpected: ${STATUSFILE}"
    fi ;;
esac

# --- boot ---------------------------------------------------------------
hdr "health"
ok "uptime ${UPTIME:-?}s"
if [ "${UPTIME:-0}" -lt 120 ]; then
  warn "still settling after boot — wait ~60s and re-run"
fi

printf '\n────────────────────────────────────────────────\n'
if [ "$fail" -eq 0 ]; then
  printf ' RESULT: \033[32mREADY TO DEMO\033[0m\n'
  exit 0
else
  printf ' RESULT: \033[31mNOT READY\033[0m — %d blocking problem(s) above\n' "$fail"
  exit 1
fi
