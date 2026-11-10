#!/usr/bin/env bash
# Install gaze.service, restart the face onto current code, and hand the OAK-D
# from the viewer to the gaze daemon.
#
# Run on the Jetson as:  sudo bash install-gaze-service.sh
#
# THIS SCRIPT DOES NOT COPY CODE. It installs systemd units from files that must
# ALREADY be on the device. Deploy first, from the repo checkout:
#
#   ./robot/deploy.sh             # or scp the files listed in the pre-flight
#
# The pre-flight below refuses to run if anything is missing. The previous
# version did not check: it hit
#   install: cannot stat '.../gaze.service': No such file or directory
# and then printed the destination path anyway and carried on to a
# success-shaped summary, leaving the operator believing a service had been
# installed. An installer that cannot fail is worse than no installer.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
TMPFILES_DIR=/etc/tmpfiles.d
SRC_DIR=/home/jetson/robot/perception
FACE_DIR=/home/jetson/robot/face
VIEWER_DIR=/home/jetson/robot/camera

say() { printf '%s\n' "$*"; }
die() { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight: is the code actually here?
# ---------------------------------------------------------------------------
# Check EVERY file up front and report ALL the missing ones at once, rather than
# dying on the first `install` and making the operator re-run this repeatedly.
say "== pre-flight: are the source files deployed? =="
missing=0
for f in \
    "$SRC_DIR/gaze.service" \
    "$SRC_DIR/tmpfiles-robot-face.conf" \
    "$SRC_DIR/gaze.py" \
    "$VIEWER_DIR/oak-viewer.service" \
    "$VIEWER_DIR/viewer.py" \
    "$FACE_DIR/face-kiosk.service" \
    "$FACE_DIR/face.py"
do
  if [ -f "$f" ]; then
    say "  ok      $f"
  else
    say "  MISSING $f"
    missing=$((missing + 1))
  fi
done
if [ "$missing" != 0 ]; then
  die "$missing file(s) not deployed.

       This script installs systemd units; it does NOT copy code. From the repo
       checkout on the workstation, deploy first:

         ./robot/deploy.sh

       then re-run this script."
fi

# ---------------------------------------------------------------------------
# Pre-flight: is everything the units EXECUTE actually runnable?
# ---------------------------------------------------------------------------
# Checking `test -f` is not enough. systemd needs the +x bit on a script it
# execs, and when it is missing the failure is 203/EXEC with the misleading
# message "Failed to locate executable ... Permission denied" - which points at
# the PATH, not the mode. That is what took face-kiosk down here: kiosk-start.sh
# was 0644 in the repo, rsync -a copied 0644 over the device's 755, and the unit
# crash-looped until it hit its rate limit.
#
# The interpreter and its target are checked too. The venv python is a directory
# of symlinks that a deploy can disturb, and a missing interpreter looks like a
# code error rather than a deployment error.
say
say "== pre-flight: are the ExecStart targets runnable? =="
runmissing=0
# ONLY scripts systemd execs DIRECTLY need the +x bit. kiosk-start.sh is named
# as ExecStart=, so systemd execs it and a missing bit is 203/EXEC.
#
# A .py is NOT in this list: ExecStart= names the INTERPRETER and passes the
# .py as an argument, so gaze.py's own mode is irrelevant. Checking it was a
# bug here - it failed on a correctly-0644 file, "repaired" it with a pointless
# chmod +x, and printed a misleading success line for a failure that had not
# happened. What matters for gaze.py is that the file EXISTS (checked above)
# and that the interpreter is executable (checked below).
if [ -x "$FACE_DIR/kiosk-start.sh" ]; then
  say "  +x      $FACE_DIR/kiosk-start.sh"
else
  say "  NOT +x  $FACE_DIR/kiosk-start.sh"
  runmissing=$((runmissing + 1))
fi
if [ -x /home/jetson/robot/camera/.venv/bin/python ]; then
  say "  +x      /home/jetson/robot/camera/.venv/bin/python"
else
  say "  NOT +x  /home/jetson/robot/camera/.venv/bin/python  <- gaze cannot start"
  runmissing=$((runmissing + 1))
fi
if [ "$runmissing" != 0 ]; then
  say
  say "  Attempting to repair the mode on the scripts (jetson owns the tree, so"
  say "  this needs no root). A venv symlink cannot be fixed this way."
  chmod +x "$FACE_DIR/kiosk-start.sh" 2>/dev/null || true
  if [ -x "$FACE_DIR/kiosk-start.sh" ]; then
    say "  repaired: chmod +x on kiosk-start.sh - continuing"
  else
    die "could not make kiosk-start.sh executable. Fix the mode in git (100755)
       rather than on the device: rsync -a preserves git's mode and will undo a
       manual chmod on the next deploy."
  fi
fi

# ---------------------------------------------------------------------------
# Stale-code detection
# ---------------------------------------------------------------------------
# Python reads source at IMPORT time. Editing a .py does NOT affect a process
# already running it - that process keeps executing the OLD code from memory
# until it is restarted. The symptom is "I deployed the change and nothing
# happened", which is indistinguishable from a broken edit unless you know to
# compare the process's start time against its source file's mtime.
#
# Returns 0 (true) if anything is stale, and never exits non-zero on its own, so
# it is safe to call under `set -e` from an `if`.
find_stale() {
  local stale="" now pid cmd src et started mtime p
  now=$(date +%s)
  for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    pid=${p#/proc/}
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      *robot/face/face.py*)       src=$FACE_DIR/face.py ;;
      *robot/perception/gaze.py*) src=$SRC_DIR/gaze.py ;;
      *robot/camera/viewer.py*)   src=$VIEWER_DIR/viewer.py ;;
      *) continue ;;
    esac
    [ -f "$src" ] || continue
    et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ') || continue
    [ -n "$et" ] || continue
    started=$(( now - et ))
    mtime=$(stat -c %Y "$src" 2>/dev/null || echo 0)
    if [ "$mtime" -gt "$started" ]; then
      say "  STALE: pid $pid started $(date -d "@$started" '+%H:%M:%S'), but $(basename "$src") changed $(date -d "@$mtime" '+%H:%M:%S')"
      stale="$stale $pid"
    fi
  done
  [ -n "$stale" ]
}

say
say "== 0. is any running process on stale code? =="
if find_stale; then
  say "  -> the face will be restarted to load the current face.py"
  restart_face=1
else
  say "  no (every robot process is newer than its source)"
  restart_face=0
fi

say
say "== 1. installing unit files =="
install -m 0644 "$SRC_DIR/gaze.service" "$UNIT_DIR/gaze.service" \
  || die "could not install $UNIT_DIR/gaze.service"
say "  wrote $UNIT_DIR/gaze.service"
# The viewer gets a Conflicts=gaze.service line so the camera swap works in BOTH
# directions. Installing it here keeps the two units in step - a viewer unit
# without that line would start happily alongside gaze and then die with
# X_LINK_DEVICE_ALREADY_IN_USE.
install -m 0644 "$VIEWER_DIR/oak-viewer.service" "$UNIT_DIR/oak-viewer.service" \
  || die "could not install $UNIT_DIR/oak-viewer.service"
say "  wrote $UNIT_DIR/oak-viewer.service"
# face-kiosk.service is installed here TOO, and it is the one unit that had no
# installer at all: it was only ever copied by hand. Nothing noticed until the
# kiosk's RuntimeDirectory was moved to tmpfiles.d - the repo copy lost it and
# the /etc copy kept it, so the two silently disagreed and the next reader of
# /etc would have concluded that /run/face is still created by the kiosk, which
# is exactly the belief that caused the PermissionError. Install it so the
# repo copy is always what the device runs.
install -m 0644 "$FACE_DIR/face-kiosk.service" "$UNIT_DIR/face-kiosk.service" \
  || die "could not install $UNIT_DIR/face-kiosk.service"
say "  wrote $UNIT_DIR/face-kiosk.service"

# /run/face, declared once for the whole system. NOT RuntimeDirectory: two units
# declaring that on one path means systemd deletes the directory when either
# stops, and `jetson` cannot recreate it under root-owned /run - which is what
# produced the PermissionError on gaze. tmpfiles.d ties the directory to the
# boot, not to any service's lifetime. See the file for the full reasoning.
install -m 0644 "$SRC_DIR/tmpfiles-robot-face.conf" "$TMPFILES_DIR/robot-face.conf" \
  || die "could not install $TMPFILES_DIR/robot-face.conf"
say "  wrote $TMPFILES_DIR/robot-face.conf (owns /run/face)"
# Apply it now so the directory exists before anything starts. This is also what
# repairs a /run/face left mis-owned or missing by an earlier failed run.
systemd-tmpfiles --create "$TMPFILES_DIR/robot-face.conf" \
  || say "  WARNING: systemd-tmpfiles --create failed"
if [ -d /run/face ]; then
  say "  /run/face: $(stat -c '%a %U:%G' /run/face)"
else
  die "/run/face still does not exist after tmpfiles --create"
fi

say
say "== 2. restarting the face onto current code =="
# The face is restarted to pick up a new face.py: Python reads source at IMPORT
# time, so an edited file does nothing to the process already running it.
#
# This is INDEPENDENT of /run/face. /run/face comes from tmpfiles.d and gaze
# does not depend on the kiosk at all, so the face being down - or failing to
# start - can no longer stop gaze. Previously gaze was ordered After=face-kiosk
# and the two failures compounded into a robot that was completely dead.
if [ "$restart_face" = 1 ]; then
  systemctl restart face-kiosk.service || say "  WARNING: face-kiosk failed to restart"
  say "  face-kiosk.service restarted: $(systemctl is-active face-kiosk.service)"
  if ! systemctl is-active --quiet face-kiosk.service; then
    say "  face-kiosk is NOT running. Recent log:"
    journalctl -u face-kiosk.service -n 15 --no-pager 2>/dev/null | sed 's/^/    /' || true
    say "  (gaze is unaffected - it no longer depends on this unit)"
  fi
else
  say "  not needed (already running current code)"
fi

say
say "== 3. clearing any stray camera holder =="
# DepthAI allows exactly ONE process. A leftover manual run (a viewer, or a
# gaze.py started by hand while calibrating) makes the service fail with
# X_LINK_DEVICE_ALREADY_IN_USE, which reads like a hardware fault.
# Only processes whose cmdline names OUR scripts are touched, and each is
# printed before it is stopped.
strays=0
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  pid=${p#/proc/}
  [ "$pid" = "$$" ] && continue
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *robot/perception/gaze.py*|*robot/camera/viewer.py*)
      # Skip anything systemd already owns - those are stopped via systemctl.
      if systemctl show -p MainPID --value gaze.service 2>/dev/null | grep -qx "$pid"; then
        continue
      fi
      say "  stopping stray pid $pid: ${cmd:0:72}"
      kill "$pid" 2>/dev/null || true
      strays=$((strays + 1))
      ;;
  esac
done
if [ "$strays" = 0 ]; then say "  none"; fi

say
say "== 4. reloading systemd =="
systemctl daemon-reload || die "systemd daemon-reload failed"

say
say "== 5. handing the camera to gaze (viewer off, gaze on) =="
# The viewer was an always-on debugging tool. It is being demoted to on-demand:
# stopped at boot, started by hand when someone wants to look at the raw feed.
systemctl disable --now oak-viewer.service >/dev/null 2>&1 || true
say "  oak-viewer.service : $(systemctl is-enabled oak-viewer.service 2>/dev/null || echo -) / $(systemctl is-active oak-viewer.service 2>/dev/null || echo -)"
systemctl enable gaze.service >/dev/null 2>&1 || true
# reset-failed FIRST. If gaze previously hit a start limit it stays in the
# failed state, and `restart` on a rate-limited unit can be refused - so the
# service is left dead while the script reports that it tried.
systemctl reset-failed gaze.service >/dev/null 2>&1 || true
systemctl restart gaze.service || say "  WARNING: gaze.service restart returned non-zero"
sleep 3
say "  gaze.service       : $(systemctl is-enabled gaze.service 2>/dev/null || echo -) / $(systemctl is-active gaze.service 2>/dev/null || echo -)"

say
say "== 6. waiting for the camera to come up =="
# Opening an OAK takes ~5s (firmware upload + USB re-enumeration). A fixed
# `sleep 3` reports a false failure - it did exactly that on the viewer install.
#
# Check the WHOLE chain, not just "is the unit active". `systemctl is-active`
# said 'active' while gaze was crash-looping on a permission error, because
# Restart=always keeps the unit nominally active between restarts. What matters
# is that the process is STAYING up and publishing.
status=/run/face/gaze-status.json
ok=0
for _ in $(seq 1 25); do
  sleep 1
  if [ -f "$status" ] && grep -q '"has_face"' "$status" 2>/dev/null; then
    ok=1
    break
  fi
  # If the unit has genuinely failed (not merely restarting), stop waiting.
  if [ "$(systemctl is-active gaze.service 2>/dev/null)" = "failed" ]; then
    break
  fi
done

if [ "$ok" = 1 ]; then
  say "  $status is being written"
  say
  say "== current reading =="
  if command -v jq >/dev/null 2>&1; then
    jq -c '{has_face, yaw_raw, pitch_raw, gaze_mag, expression}' "$status" | sed 's/^/  /'
  else
    sed 's/^/  /' "$status"
  fi
else
  say "  NO STATUS FILE - the service is up but is not reporting."
  say "  Recent log:"
  journalctl -u gaze.service -n 25 --no-pager 2>/dev/null | sed 's/^/    /' || true
fi

say
say "== 6b. whole-chain health check =="
# Each of these has been an ACTUAL failure at some point, and each one is
# invisible to `systemctl is-active`:
#   /run/face        PermissionError, after the kiosk failed to create it
#   kiosk running    crashed on a missing +x, so the screen went dark
#   face on current  stale Python process, so the new expression never appeared
chain=0
if [ -d /run/face ]; then
  say "  ok      /run/face exists"
else
  say "  FAIL    /run/face missing"
  chain=$((chain + 1))
fi
if systemctl is-active --quiet face-kiosk.service; then
  say "  ok      face-kiosk running (the display has a face on it)"
else
  say "  FAIL    face-kiosk is $(systemctl is-active face-kiosk.service) - the SCREEN IS BLANK"
  chain=$((chain + 1))
fi
if [ "$ok" = 1 ]; then
  say "  ok      gaze is publishing"
else
  say "  FAIL    gaze is not publishing"
  chain=$((chain + 1))
fi
if find_stale >/dev/null; then
  say "  WARN    a robot process is still older than its source"
  chain=$((chain + 1))
else
  say "  ok      no stale processes"
fi
# Network independence. The robot is used on a bench with no NIC and no LAN, so
# a unit ordered After=network-online.target is a way to fail for no reason. The
# OAK-D is local USB and the SCRFD model resolves from ~/.cache/depthai/models.
netdep=""
for u in gaze.service oak-viewer.service face-kiosk.service; do
  if systemctl show -p After --value "$u" 2>/dev/null | grep -qw 'network-online.target'; then
    netdep="$netdep $u"
  fi
done
if [ -n "$netdep" ]; then
  say "  WARN    still ordered After=network-online.target:$netdep"
  say "          (harmless when the target IS reached, but on an isolated bench"
  say "           it can leave the robot dead - redeploy the units)"
else
  say "  ok      no unit waits on network-online.target (boots with no LAN)"
fi
if [ "$chain" = 0 ]; then
  say
  say "  ALL CHECKS PASSED"
else
  say
  say "  $chain check(s) failed - the robot is NOT fully working. See the logs above."
fi

say
say "== 7. final check: is the face still on stale code? =="
if find_stale; then
  say "  STILL STALE - do not trust what the screen shows. Restart face-kiosk:"
  say "      sudo systemctl restart face-kiosk.service"
else
  say "  no - the running face matches face.py on disk"
fi

say
say "== what the face should do now =="
say "  no face              -> neutral"
say "  looking at the robot -> happy   (|gaze| < 0.35)"
say "  not looking          -> angry   (|gaze| > 0.80, with hysteresis)"
say "  the HUD should read: face v2.5"
say
say "== if something is wrong =="
say "  blank screen        : sudo systemctl restart face-kiosk.service"
say "                        (a missing +x on kiosk-start.sh does this - check"
say "                        with: journalctl -u face-kiosk -n 20 | grep EXEC)"
say "  no expressions      : sudo systemctl restart gaze.service"
say "  rate-limited/dead   : sudo systemctl reset-failed gaze.service"
say "                        (StartLimitIntervalSec=0 now disables the limit,"
say
say "== endpoints and controls =="
say "  live reading : jq -c . $status"
say "  watch it     : watch -n0.5 \"jq -c '{has_face,gaze_mag,expression}' $status\""
say "  gaze log     : journalctl -u gaze.service -f"
say "  face log     : journalctl -u face-kiosk.service -f"
say "  viewer (on demand, takes the camera back):"
say "      sudo systemctl start oak-viewer.service      # gaze stops automatically"
say "      sudo systemctl stop  oak-viewer.service      # hand it back"
say "      http://orin.home:8080/"
say
say "  NOTE: the two share one camera, so they cannot run at once. That is what"
say "  the Conflicts= lines in both units enforce."
say
say "done."
