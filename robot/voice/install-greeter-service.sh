#!/usr/bin/env bash
# Install greeter.service — the unit that plays the introduction when someone
# settles in front of the robot.
#
# Run on the Jetson as:  sudo bash install-greeter-service.sh
#
# THIS SCRIPT DOES NOT COPY CODE. It installs a systemd unit from a file that
# must ALREADY be on the device. Deploy first, from the repo checkout:
#
#   cd ~/Projects/nvidia-orin && HOST=jetson@orin ./robot/deploy.sh
#
# The pre-flight below refuses to run if anything is missing. An installer that
# cannot fail is worse than no installer: an earlier one in this repo printed a
# success-shaped summary after `install` had already failed, leaving the operator
# believing a service had been installed.
#
# The unit is ENABLED but deliberately NOT STARTED. Starting it arms the robot to
# speak to whoever sits down, and that should be a decision someone makes rather
# than a side effect of installing. It comes up on the next boot.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
SRC_DIR=/home/jetson/robot/voice
STATUS=/run/face/gaze-status.json
UNIT=greeter.service

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
    "$SRC_DIR/$UNIT" \
    "$SRC_DIR/greeter.py" \
    "$SRC_DIR/lipsync.py" \
    "$SRC_DIR/display.conf" \
    "$SRC_DIR/clips/rung1-cartoon-intro.wav"
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

       This script installs a systemd unit; it does NOT copy code. From the repo
       checkout on the workstation, deploy first:

         cd ~/Projects/nvidia-orin && HOST=jetson@orin ./robot/deploy.sh

       then re-run this script."
fi

# ---------------------------------------------------------------------------
# Pre-flight: is everything the unit EXECUTES actually runnable?
# ---------------------------------------------------------------------------
# ExecStart= names the INTERPRETER and passes greeter.py as an argument, so
# greeter.py's own mode is irrelevant and checking it here would be a bug of the
# kind this repo has already made once (a needless chmod +x on a correct 0644
# file, plus a misleading success line). What matters is the interpreter, and the
# player the greeter will spawn.
say
say "== pre-flight: can the unit actually run? =="
runmissing=0
if [ -x /usr/bin/python3 ]; then
  say "  +x      /usr/bin/python3"
else
  say "  NOT +x  /usr/bin/python3  <- greeter.py cannot start"
  runmissing=$((runmissing + 1))
fi
# lipsync.py falls back to aplay, but on this board the analogue default sink is
# a trap with no physical output, so paplay is what will really be used.
if command -v paplay >/dev/null 2>&1; then
  say "  found   paplay ($(command -v paplay))"
else
  say "  MISSING paplay  <- the robot would greet in silence"
  runmissing=$((runmissing + 1))
fi
if [ "$runmissing" != 0 ]; then
  die "$runmissing runtime dependency/dependencies missing. The unit would be
       installed and then fail at the first greeting."
fi

# ---------------------------------------------------------------------------
# Stale-code detection
# ---------------------------------------------------------------------------
# Python reads source at IMPORT time: editing greeter.py does NOT affect a
# process already running it, which keeps executing the old code from memory
# until it is restarted. The symptom is "I deployed the change and nothing
# happened", which is indistinguishable from a broken edit.
say
say "== 0. is the greeter already running on stale code? =="
stale=0
now=$(date +%s)
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  pid=${p#/proc/}
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *robot/voice/greeter.py*) ;;
    *) continue ;;
  esac
  et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ') || continue
  [ -n "$et" ] || continue
  started=$((now - et))
  mtime=$(stat -c %Y "$SRC_DIR/greeter.py" 2>/dev/null || echo 0)
  if [ "$mtime" -gt "$started" ]; then
    say "  STALE: pid $pid started $(date -d "@$started" '+%H:%M:%S'), but greeter.py changed $(date -d "@$mtime" '+%H:%M:%S')"
    stale=1
  fi
done
if [ "$stale" = 0 ]; then
  say "  no (nothing running, or it is already on current code)"
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
say
say "== 1. installing the unit =="
install -m 0644 "$SRC_DIR/$UNIT" "$UNIT_DIR/$UNIT" \
  || die "could not install $UNIT_DIR/$UNIT"
say "  wrote $UNIT_DIR/$UNIT"

say
say "== 2. reloading systemd and checking the unit parses =="
systemctl daemon-reload || die "systemd daemon-reload failed"
if systemd-analyze verify "$UNIT_DIR/$UNIT" 2>/tmp/greeter-verify.$$; then
  say "  unit parses cleanly"
else
  # verify is noisy about things that are harmless; show it, but do not fail on it
  say "  systemd-analyze verify said:"
  sed 's/^/    /' /tmp/greeter-verify.$$ 2>/dev/null || true
fi
rm -f /tmp/greeter-verify.$$

say
say "== 3. enabling (not starting) $UNIT =="
systemctl enable "$UNIT" >/dev/null 2>&1 || die "could not enable $UNIT"
say "  $UNIT : $(systemctl is-enabled "$UNIT" 2>/dev/null || echo -) / $(systemctl is-active "$UNIT" 2>/dev/null || echo -)"
say "  It will start on the next boot. To start it NOW:"
say "      sudo systemctl start $UNIT"

# ---------------------------------------------------------------------------
# Verify what can be verified without arming it
# ---------------------------------------------------------------------------
say
say "== 4. running the greeter's own state-machine self-test =="
# 9 synthetic cases, under a second, no audio, no camera. This is the part that
# is impossible to eyeball on a real face in real time, so prove it here.
if runuser -u jetson -- /usr/bin/python3 "$SRC_DIR/greeter.py" --selftest 2>&1 | sed 's/^/  /'; then
  say "  self-test passed"
else
  say "  SELF-TEST FAILED - do not arm the robot until this is fixed"
fi

say
say "== 5. the face signal and the audio sink =="
if [ -f "$STATUS" ] && grep -q '"has_face"' "$STATUS" 2>/dev/null; then
  say "  ok      $STATUS is being written (gaze is publishing)"
else
  say "  WARN    $STATUS is not reporting - the greeter will stay silent"
  say "          (expected if gaze is stopped; check: sudo systemctl restart gaze.service)"
fi
if [ -d /run/face ]; then
  say "  ok      /run/face: $(stat -c '%a %U:%G' /run/face)"
else
  say "  WARN    /run/face missing - tmpfiles should have created it"
fi
# The offset and the sink are a matched pair, and the default sink on this board
# is an internal analogue device with no physical output. Verify the sink the
# greeter will actually use really exists in the user's session.
SINK=$(grep -m1 '^SINK=' "$SRC_DIR/display.conf" 2>/dev/null | cut -d= -f2- || true)
LEAD=$(grep -m1 '^LEAD=' "$SRC_DIR/display.conf" 2>/dev/null | cut -d= -f2- || true)
say "  display.conf: SINK=${SINK:-<unset>} LEAD=${LEAD:-<unset>}"
if [ -n "${SINK:-}" ] && command -v pactl >/dev/null 2>&1; then
  if runuser -u jetson -- env XDG_RUNTIME_DIR=/run/user/1000 pactl list sinks short 2>/dev/null \
       | grep -qF "$SINK"; then
    say "  ok      that sink exists in jetson's session"
  else
    say "  WARN    that sink was NOT found. Sinks in this session:"
    runuser -u jetson -- env XDG_RUNTIME_DIR=/run/user/1000 pactl list sinks short 2>/dev/null \
      | sed 's/^/            /' || say "            (pactl could not list sinks)"
  fi
fi

say
say "== what the robot will do once it is started =="
say "  a face appears and holds 10s  -> plays clips/rung1-cartoon-intro.wav"
say "  during playback               -> nothing interrupts it; it runs to the end"
say "  20s with no face after that   -> resets, ready for the next person"
say "  a face for less than 10s      -> silence (a passer-by is not greeted)"
say
say "== controls =="
say "  start now   : sudo systemctl start $UNIT"
say "  stop        : sudo systemctl stop $UNIT"
say "  disable     : sudo systemctl disable --now $UNIT"
say "  log         : journalctl -u $UNIT -f"
say "  state       : journalctl -u $UNIT -n 30 --no-pager"
say
say "  NOTE: only one player may drive the mouth at a time. If the greeter is"
say "  playing, a manual try-lead.sh run will refuse with 'already being driven'"
say "  - that is the lock doing its job, not a fault."
say
say "done."
