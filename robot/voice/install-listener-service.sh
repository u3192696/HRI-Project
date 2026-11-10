#!/usr/bin/env bash
# Install listener.service — the unit that listens for a spoken command and logs it.
#
# Run on the Jetson as:  sudo bash install-listener-service.sh
#
# THIS SCRIPT DOES NOT COPY CODE. It installs a systemd unit from files that must
# ALREADY be on the device. Deploy first, from the repo checkout:
#
#   cd ~/Projects/hri-project && ./robot/deploy.sh
#
# The pre-flight below refuses to run if anything is missing. An installer that
# cannot fail is worse than no installer: an earlier one in this repo printed a
# success-shaped summary after `install` had already failed, leaving the operator
# believing a service had been installed.
#
# The unit is ENABLED but deliberately NOT STARTED, matching greeter.service: the
# listener opens the microphone, and arming a device to listen should be a
# decision someone makes rather than a side effect of installing. It comes up on
# the next boot.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
SRC_DIR=/home/jetson/robot/voice
UNIT=listener.service
LOG=/tmp/voice-detection.log

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
    "$SRC_DIR/listener.py" \
    "$SRC_DIR/keywords.conf" \
    "$SRC_DIR/mic.conf" \
    "$SRC_DIR/mic.py"
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

         cd ~/Projects/hri-project && ./robot/deploy.sh

       then re-run this script."
fi

# ---------------------------------------------------------------------------
# Pre-flight: the two DEVICE-LOCAL things that deploy.sh deliberately excludes
# ---------------------------------------------------------------------------
# Both of these live only on the board. Miss either one and the service starts
# cleanly, reports nothing wrong, and hears nothing, which is the failure mode
# this whole project keeps trying to make impossible.
say
say "== pre-flight: the venv (carries vosk) and the model (68 MB) =="
runmissing=0

PY="$SRC_DIR/.venv/bin/python"
if [ -x "$PY" ]; then
  if runuser -u jetson -- "$PY" -c 'import vosk' >/dev/null 2>&1; then
    say "  ok      $PY imports vosk"
  else
    say "  BROKEN  $PY exists but cannot import vosk"
    runmissing=$((runmissing + 1))
  fi
else
  say "  MISSING $PY  <- create it on the board:"
  say "            cd $SRC_DIR && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
  runmissing=$((runmissing + 1))
fi

MODEL=$(grep -m1 '^MODEL=' "$SRC_DIR/keywords.conf" 2>/dev/null | cut -d= -f2- || true)
if [ -n "${MODEL:-}" ] && [ -d "$MODEL" ]; then
  say "  ok      model $MODEL"
else
  say "  MISSING model ${MODEL:-<unset>}  <- run $SRC_DIR/fetch-model.sh (needs network,"
  say "            and the lab has no LAN, so do it from home)"
  runmissing=$((runmissing + 1))
fi

if command -v arecord >/dev/null 2>&1; then
  say "  ok      arecord ($(command -v arecord))"
else
  say "  MISSING arecord  <- the listener could not open the microphone"
  runmissing=$((runmissing + 1))
fi
if [ "$runmissing" != 0 ]; then
  die "$runmissing device-local dependency/dependencies missing. The unit would be
       installed and then fail to hear anything."
fi

# ---------------------------------------------------------------------------
# Pre-flight: is the PINNED microphone actually offered in jetson's session?
# ---------------------------------------------------------------------------
# listener.py inherits mic.py's refusal to fall back to the default source, and on
# this board the default source is internal DMA fabric that records exact digital
# zeros. So the source named in mic.conf must really exist, or the service will
# exit 1 in a loop with a clear message (which is the correct behaviour, but it is
# better to catch it here).
say
say "== pre-flight: is the pinned capture source present? =="
SRC_NAME=$(grep -m1 '^SOURCE=' "$SRC_DIR/mic.conf" 2>/dev/null | cut -d= -f2- || true)
if [ -n "${SRC_NAME:-}" ] && command -v pactl >/dev/null 2>&1; then
  if runuser -u jetson -- env XDG_RUNTIME_DIR=/run/user/1000 pactl list sources short 2>/dev/null \
       | grep -qF "$SRC_NAME"; then
    say "  ok      $SRC_NAME"
  else
    say "  WARN    that source was NOT found. Sources in this session:"
    runuser -u jetson -- env XDG_RUNTIME_DIR=/run/user/1000 pactl list sources short 2>/dev/null \
      | sed 's/^/            /' || say "            (pactl could not list sources)"
    say "          The listener will refuse to start rather than record silence."
    say "          See the re-derivation procedure in mic.conf."
  fi
else
  say "  WARN    no SOURCE= in mic.conf, or pactl unavailable"
fi

# ---------------------------------------------------------------------------
# Stale-code detection
# ---------------------------------------------------------------------------
# Python reads source at IMPORT time: editing listener.py does NOT affect a
# process already running it, which keeps executing the old code from memory until
# it is restarted. The symptom is "I deployed the change and nothing happened",
# which is indistinguishable from a broken edit.
say
say "== 0. is a listener already running on stale code? =="
stale=0
now=$(date +%s)
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  pid=${p#/proc/}
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *robot/voice/listener.py*) ;;
    *) continue ;;
  esac
  et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ') || continue
  [ -n "$et" ] || continue
  started=$((now - et))
  mtime=$(stat -c %Y "$SRC_DIR/listener.py" 2>/dev/null || echo 0)
  if [ "$mtime" -gt "$started" ]; then
    say "  STALE: pid $pid started $(date -d "@$started" '+%H:%M:%S'), but listener.py changed $(date -d "@$mtime" '+%H:%M:%S')"
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
if systemd-analyze verify "$UNIT_DIR/$UNIT" 2>/tmp/listener-verify.$$; then
  say "  unit parses cleanly"
else
  # verify is noisy about things that are harmless; show it, but do not fail on it
  say "  systemd-analyze verify said:"
  sed 's/^/    /' /tmp/listener-verify.$$ 2>/dev/null || true
fi
rm -f /tmp/listener-verify.$$

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
say "== 4. running the listener's own self-test =="
# The ritual state machine, the vocabulary matching, the half-duplex gate and the
# stop handling. No audio, no model, about a second. This is the part that cannot
# be eyeballed in real time, so prove it here.
if runuser -u jetson -- "$PY" "$SRC_DIR/listener.py" --selftest 2>&1 | sed 's/^/  /'; then
  say "  self-test passed"
else
  say "  SELF-TEST FAILED - do not enable this until it is fixed"
fi

say
say "== 5. the face directory, which the half-duplex gate needs =="
if [ -d /run/face ]; then
  say "  ok      /run/face: $(stat -c '%a %U:%G' /run/face)"
else
  say "  WARN    /run/face missing - tmpfiles should have created it. WITHOUT IT"
  say "          THE GATE FAILS OPEN: the mouth file never appears, so the listener"
  say "          will transcribe the robot's own voice."
fi

say
say "== what the robot will do once it is started =="
say '  "system command <verb>"       -> TRIGGER, a 5 s prompt goes ON SCREEN'
say '                                   (/run/face/prompt.json), logged'
say '  "marigold wombat sierra"      -> CONFIRMED, and then depends on the verb:'
say '                                     start -> EXECUTING: publishes the outbox,'
say '                                              which the session conductor acts on'
say '                                     other -> WOULD EXECUTE, nothing more'
say '  5 s with no pass phrase       -> TIMEOUT, the prompt clears'
say '  "marigold wombat sierra" alone-> ORPHAN, nothing happens'
say '  THE STAGE WORD, alone         -> STAGE: published as `begin`, which starts the'
say '                                   session of the stage that is waiting'
say '  verbs                         -> reset, skip, shutdown, reboot, debug'
say '  while the greeter is speaking  -> suppressed entirely (half-duplex gate)'
say
say "  EXACTLY TWO THINGS ARE WIRED: start, and the stage word. The EXECUTORS table"
say "  in listener.py is still empty, so a confirmed shutdown or reboot is logged and"
say "  nothing more. That stays last on purpose: it powers off the board this unit"
say "  runs on."
say
say "  EACH EVENT IS APPENDED TO $LOG - that file is the whole interface while"
say "  no monitor or projector is connected. Watch it with:"
say "      tail -f $LOG"
say
say "== controls =="
say "  start now   : sudo systemctl start $UNIT"
say "  stop        : sudo systemctl stop $UNIT"
say "  disable     : sudo systemctl disable --now $UNIT"
say "  log         : journalctl -u $UNIT -f"
say
say "  NOTE: only ONE listener should run at a time. The unit is single-instance by"
say "  systemd's own rules, but a script run by hand alongside it would double every"
say "  log line. Stop the service before running listener.py manually."
say
say "done."
