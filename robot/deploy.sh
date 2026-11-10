#!/usr/bin/env bash
# Deploy the robot code tree to the Jetson.
#
# Run from the WORKSTATION (serenity):   ./robot/deploy.sh
#
# NOT ON THE ROBOT. This is a PUSH: it copies the workstation's tree to $DEST on
# the Jetson over ssh. On the robot there is nothing to push to - the tree is
# already there and it is not a git checkout, so `git pull` is not the alternative
# either. If you are on the robot, the files are current as of the last push and
# the only step left is the restart, which this script never does:
#
#     sudo systemctl restart session.service face-kiosk.service
#
# WHY THIS EXISTS
#   The install scripts install systemd units from files already on the device;
#   they do not copy code. Deploying by hand with a few `scp` commands is how the
#   unit files get forgotten - the .py files land, the .service files do not, and
#   the installer fails with `cannot stat`. One command that copies the whole
#   tree removes that class of mistake.
#
#   Files are the ONLY thing this does. It never runs sudo and never restarts a
#   service - installing units is a separate, deliberate step.
set -euo pipefail

HOST=${HOST:-jetson@orin.home}
DEST=${DEST:-/home/jetson/robot}
SRC=$(cd "$(dirname "$0")" && pwd)

say() { printf '%s\n' "$*"; }

# Are we ON the target? Then there is nothing to push, and the ssh below would fail
# with a message about keys that has nothing to do with the real problem. Compare
# the local hostname with the target's short name: `orin.home` -> `orin`, which is
# what `hostname -s` reports there.
TARGET_HOST=${HOST#*@}
TARGET_SHORT=${TARGET_HOST%%.*}
if [ "$(hostname -s)" = "$TARGET_SHORT" ]; then
  printf 'FATAL: this is %s, which is the deploy TARGET, not the workstation.\n' "$TARGET_SHORT" >&2
  printf '\n  deploy.sh pushes the workstation tree to %s:%s.\n' "$HOST" "$DEST" >&2
  printf '  The tree here is already that copy (it is not a git checkout, so pull\n' >&2
  printf '  is not the alternative either). Run this from the workstation, or if the\n' >&2
  printf '  files are already current, just restart the unit:\n\n' >&2
  printf '    sudo systemctl restart session.service face-kiosk.service\n' >&2
  exit 1
fi

say "== checking the host is reachable =="
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true 2>/dev/null; then
  printf 'FATAL: cannot ssh to %s (is the Jetson up, and is the key loaded?)\n' "$HOST" >&2
  printf '       If you are ON the robot, this script is the wrong direction: it pushes\n' >&2
  printf '       from the workstation. See the header.\n' >&2
  exit 1
fi
say "  $HOST ok"

say
say "== deploying $SRC/ -> $HOST:$DEST/ =="
# --delete is deliberately NOT used: the device also holds .venv/ and runtime
# logs that do not exist in the repo, and a stray delete of the venv would
# silently break every script at once. Copying without pruning is the safer
# default; a stale file is obvious, a missing venv is a puzzle.
#
# .venv is excluded because it is ~200MB of aarch64 wheels that must not be
# overwritten by anything from a dev machine.
rsync -a --itemize-changes \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  "$SRC/" "$HOST:$DEST/" | sed 's/^/  /'

say
say "== verifying the files the installers require =="
# These are exactly the paths the installers' pre-flights check
# (install-gaze-service.sh and install-greeter-service.sh). Verify here too, so
# a missing file is reported at deploy time rather than discovered halfway
# through a sudo install.
missing=0
for f in \
    "$DEST/face/face.py" \
    "$DEST/face/face-kiosk.service" \
    "$DEST/face/assets/portrait-r4.png" \
    "$DEST/face/assets/portrait-r4.json" \
    "$DEST/face/assets/milo-neutral.png" \
    "$DEST/face/assets/milo-happy.png" \
    "$DEST/face/assets/milo-closed.png" \
    "$DEST/face/assets/milo.json" \
    "$DEST/camera/viewer.py" \
    "$DEST/camera/oak-viewer.service" \
    "$DEST/perception/gaze.py" \
    "$DEST/perception/gaze.service" \
    "$DEST/perception/tmpfiles-robot-face.conf" \
    "$DEST/perception/install-gaze-service.sh" \
    "$DEST/voice/greeter.py" \
    "$DEST/voice/greeter.service" \
    "$DEST/voice/lipsync.py" \
    "$DEST/voice/display.conf" \
    "$DEST/voice/mic.py" \
    "$DEST/voice/mic.conf" \
    "$DEST/voice/listener.py" \
    "$DEST/voice/keywords.conf" \
    "$DEST/voice/listener.service" \
    "$DEST/voice/install-listener-service.sh" \
    "$DEST/voice/fetch-model.sh" \
    "$DEST/voice/requirements.txt" \
    "$DEST/voice/clips/rung1-cartoon-intro.wav" \
    "$DEST/voice/clips/rung3-warm-conversational-intro.wav" \
    "$DEST/voice/clips/rung3-warm-conversational-pass.wav" \
    "$DEST/voice/clips/rung3-warm-conversational-retry.wav" \
    "$DEST/voice/clips/rung4-natural-human-intro.wav" \
    "$DEST/voice/clips/rung4-natural-human-pass.wav" \
    "$DEST/voice/clips/rung4-natural-human-retry.wav"
do
  if ssh -o BatchMode=yes "$HOST" "test -f $f" 2>/dev/null; then
    say "  ok      $f"
  else
    say "  MISSING $f"
    missing=$((missing + 1))
  fi
done

say
say "== verifying EXECUTABLE bits on scripts systemd starts =="
# A missing +x is a silent killer: systemd dies with 203/EXEC ("Failed to locate
# executable ... Permission denied"), which reads like a path problem rather than
# a mode problem, and the unit then crash-loops.
#
# This check exists because it actually happened. kiosk-start.sh was committed
# 0644, so the mode was never executable in git; rsync -a faithfully copied that
# 644 over the device's working 755 and face-kiosk died on the next restart.
# `git ls-files -s` now records 100755 for every script invoked directly.
#
# rsync -a PRESERVES modes from git, so a wrong bit in the repo is self-
# perpetuating: it will silently undo a manual chmod +x on the device. Fix the
# mode in git, not on the device.
for f in \
    "$DEST/face/kiosk-start.sh" \
    "$DEST/face/nvidia-drm-reload.sh" \
    "$DEST/perception/install-gaze-service.sh" \
    "$DEST/voice/install-greeter-service.sh" \
    "$DEST/voice/install-listener-service.sh" \
    "$DEST/voice/calibrate-lead.py" \
    "$DEST/voice/fetch-model.sh"
do
  if ssh -o BatchMode=yes "$HOST" "test -x $f" 2>/dev/null; then
    say "  +x      $f"
  else
    say "  NOT +x  $f   <- systemd will fail with 203/EXEC"
    missing=$((missing + 1))
  fi
done

say
say "== local vs remote hashes (the ones that matter) =="
for pair in \
    "face/face.py" \
    "face/assets/portrait-r4.png" \
    "face/assets/portrait-r4.json" \
    "face/assets/milo-neutral.png" \
    "face/assets/milo-happy.png" \
    "face/assets/milo-closed.png" \
    "face/assets/milo.json" \
    "perception/gaze.py" \
    "perception/gaze.service" \
    "camera/oak-viewer.service" \
    "voice/greeter.py" \
    "voice/mic.py" \
    "voice/mic.conf" \
    "voice/listener.py" \
    "voice/keywords.conf" \
    "voice/listener.service" \
    "voice/greeter.service"
do
  l=$(md5sum "$SRC/$pair" | cut -d' ' -f1)
  r=$(ssh -o BatchMode=yes "$HOST" "md5sum $DEST/$pair 2>/dev/null | cut -d' ' -f1")
  if [ "$l" = "$r" ]; then
    say "  match   $pair"
  else
    say "  DIFFER  $pair"
    say "            local  $l"
    say "            remote $r"
    missing=$((missing + 1))
  fi
done

say
if [ "$missing" != 0 ]; then
  printf 'FATAL: %d problem(s) above - do NOT run the installer yet.\n' "$missing" >&2
  exit 1
fi
say "deploy ok. Next step (on the Jetson, needs sudo) - run whichever is due:"
say "    sudo bash $DEST/perception/install-gaze-service.sh   # gaze, kiosk, viewer"
say "    sudo bash $DEST/voice/install-greeter-service.sh     # the greeter"
say "    sudo bash $DEST/voice/install-listener-service.sh    # the voice listener"
