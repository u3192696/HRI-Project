#!/usr/bin/env bash
# Install session.service — the unit that runs the run-through (stages, timers,
# what the screen says).
#
# Run on the Jetson as:  sudo bash install-session-service.sh
#
# THIS SCRIPT DOES NOT COPY CODE. It installs a systemd unit from files that must
# ALREADY be on the device. Deploy first, from the repo checkout:
#
#   cd ~/Projects/hri-project && ./robot/deploy.sh
#
# The pre-flight below refuses to run if anything is missing, and reports ALL the
# missing things at once. An installer that cannot fail is worse than no
# installer: an earlier one in this repo printed a success-shaped summary after
# `install` had already failed.
#
# The unit is ENABLED but deliberately NOT STARTED, matching greeter.service and
# listener.service: starting it takes over the projector's screen, and replacing
# what is on a screen someone may be looking at should be a decision, not a side
# effect of installing. It comes up on the next boot.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
SRC_DIR=/home/jetson/robot/voice
UNIT=session.service
PY=/usr/bin/python3
LOG=/tmp/session-log.txt

say() { printf '%s\n' "$*"; }
die() { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight: is the code actually here?
# ---------------------------------------------------------------------------
say "== pre-flight: are the source files deployed? =="
missing=0
for f in \
    "$SRC_DIR/$UNIT" \
    "$SRC_DIR/session.py" \
    "$SRC_DIR/greeter.py" \
    "$SRC_DIR/display.conf" \
    "$SRC_DIR/clips/rung1-cartoon-intro.wav" \
    "$SRC_DIR/clips/stage2-intro.wav" \
    "$SRC_DIR/clips/rung3-warm-conversational-intro.wav" \
    "$SRC_DIR/clips/rung3-warm-conversational-pass.wav" \
    "$SRC_DIR/clips/rung3-warm-conversational-retry.wav" \
    "$SRC_DIR/clips/rung4-natural-human-intro.wav" \
    "$SRC_DIR/clips/rung4-natural-human-pass.wav" \
    "$SRC_DIR/clips/rung4-natural-human-retry.wav"
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
# Pre-flight: the interpreter and the directory it writes into
# ---------------------------------------------------------------------------
# session.py runs on the SYSTEM python, not the listener's venv: it is stdlib only
# (it imports greeter for the player, and greeter is stdlib too). Saying so here
# matters because "the venv" is the reflex for anything voice-related in this repo,
# and getting it wrong produces a unit that fails to start at all.
say
say "== pre-flight: interpreter and the file directory =="
if [ -x "$PY" ]; then
  say "  ok      $PY ($("$PY" -V 2>&1))  <- stdlib only, deliberately not the venv"
else
  die "$PY missing. session.py runs on the system python, not the venv."
fi

if [ -d /run/face ]; then
  say "  ok      /run/face: $(stat -c '%a %U:%G' /run/face)"
else
  die "/run/face missing - tmpfiles should have created it, and this unit writes
       /run/face/session.json. Check /etc/tmpfiles.d/robot-face.conf."
fi

# ---------------------------------------------------------------------------
# Pre-flight: can this unit DO anything? It is driven by the listener's outbox.
# ---------------------------------------------------------------------------
# The conductor reads commands and writes the screen. With no listener there is
# nothing to read, so it is installed, enabled, running, and permanently in
# standby, which looks exactly like a broken conductor.
say
say "== pre-flight: is the listener (the thing that speaks the commands) installed? =="
if systemctl is-enabled listener.service >/dev/null 2>&1; then
  say "  ok      listener.service: $(systemctl is-enabled listener.service 2>/dev/null) / $(systemctl is-active listener.service 2>/dev/null || echo -)"
else
  say "  WARN    listener.service is not installed. This unit will run and sit in"
  say "          standby forever, because nothing publishes commands to it:"
  say "              sudo bash $SRC_DIR/install-listener-service.sh"
fi

# ---------------------------------------------------------------------------
# Stale-code detection
# ---------------------------------------------------------------------------
# Python reads source at IMPORT time: editing session.py does NOT affect a process
# already running it, which keeps executing the old code from memory until it is
# restarted. The symptom is "I deployed the change and nothing happened", which is
# indistinguishable from a broken edit.
say
say "== 0. is a conductor already running on stale code? =="
stale=0
now=$(date +%s)
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  pid=${p#/proc/}
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *robot/voice/session.py*) ;;
    *) continue ;;
  esac
  et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ') || continue
  [ -n "$et" ] || continue
  started=$((now - et))
  mtime=$(stat -c %Y "$SRC_DIR/session.py" 2>/dev/null || echo 0)
  if [ "$mtime" -gt "$started" ]; then
    say "  STALE: pid $pid started $(date -d "@$started" '+%H:%M:%S'), but session.py changed $(date -d "@$mtime" '+%H:%M:%S')"
    say "         restart it:  sudo systemctl restart $UNIT"
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
if systemd-analyze verify "$UNIT_DIR/$UNIT" 2>/tmp/session-verify.$$; then
  say "  unit parses cleanly"
else
  say "  systemd-analyze verify said:"
  sed 's/^/    /' /tmp/session-verify.$$ 2>/dev/null || true
fi
rm -f /tmp/session-verify.$$

say
say "== 3. enabling (not starting) $UNIT =="
systemctl enable "$UNIT" >/dev/null 2>&1 || die "could not enable $UNIT"
say "  $UNIT : $(systemctl is-enabled "$UNIT" 2>/dev/null || echo -) / $(systemctl is-active "$UNIT" 2>/dev/null || echo -)"
say "  It will start on the next boot. To start it NOW:"
say "      sudo systemctl start $UNIT"

# ---------------------------------------------------------------------------
# Verify what can be verified without starting it
# ---------------------------------------------------------------------------
say
say "== 4. driving the whole run-through with a synthetic clock =="
# Four stages, four 60 s sessions, a 30 s success screen, in about a second, with
# no audio and no files. This is the part that cannot be eyeballed in real time.
if runuser -u jetson -- "$PY" "$SRC_DIR/session.py" --selftest 2>&1 | sed 's/^/  /'; then
  say "  self-test passed"
else
  say "  SELF-TEST FAILED - do not enable this until it is fixed"
fi

say
say "== 5. who OWNS the screen (worth knowing before starting this) =="
if [ -f /run/face/session.json ]; then
  say "  /run/face/session.json exists right now:"
  sed 's/^/    /' /run/face/session.json
  say "  This unit becomes its only writer when it starts: it publishes standby"
  say "  first, so a run in progress is abandoned rather than resumed."
else
  say "  /run/face/session.json does not exist: standby, which is what power-on looks like."
fi

say
say "== what the robot will do once it is started =="
say "  screen at rest         -> Standby (this unit publishes standby at start-up)"
say "  \"system command start\"  -> CONFIRMED by the listener -> this unit puts stage 1's"
say "                            face up and waits"
say "  a face is detected     -> the GREETER plays stage 1's introduction (58 s)"
say "  the STAGE WORD alone   -> CONFIRMED by the listener -> this unit runs 60 s of gaze"
say "  every 60 s             -> next stage: 2 is the rung 2 face, 3 and 4 are labels,"
say "                            each announced by a spoken clip"
say "  after stage 4          -> Success for 30 s -> back to Standby, mic listening"
say
say "  THE STAGE WORD is defined in keywords.conf (WORD_TRIGGER). It needs no prefix"
say "  and no confirmation, and it cannot skip a stage: it only starts the session of"
say "  a stage that is already waiting."
say
say "  SUCCESS IS ASSUMED: a stage ends when its timer does, whatever the gaze did."
say "  Each stage's session is summarised into $LOG:"
say "      tail -f $LOG"
say
say "== controls =="
say "  start now   : sudo systemctl start $UNIT"
say "  stop        : sudo systemctl stop $UNIT"
say "  stop+restart: sudo systemctl restart $UNIT   (sends SIGTERM first)"
say "  disable     : sudo systemctl disable --now $UNIT"
say "  log         : journalctl -u $UNIT -f"
say
say "  NOTE: only ONE conductor should run at a time. It is the only writer of"
say "  session.json, and a second one (e.g. run by hand alongside the unit) would"
say "  fight it for the screen."
say
say "done."
