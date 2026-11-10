#!/usr/bin/env bash
# watch-boot.sh — record the robot's health over time, so a reboot can be
# diagnosed after the fact.
#
# WHY THIS EXISTS
#   The robot is moved to a lab bench with no LAN, where this agent can no
#   longer watch it directly. This samples the whole chain on a timer and
#   appends, with a flush after every sample, so the file survives the machine
#   going away mid-run (a reboot, a pulled cable, a power cut).
#
#   Run it before a reboot and read the file after: the last line before the
#   gap says whether the robot was healthy when it went down, and boot_id and
#   uptime let you tell a restart from a hang.
#
# Usage:  bash watch-boot.sh [seconds] [interval] [outfile]
#   defaults: 2700s (45 min), 3s, /home/jetson/lab-boot.log
#
# Read-only: it inspects state and writes only its own log.
set -uo pipefail

DURATION=${1:-2700}
INTERVAL=${2:-3}
OUT=${3:-/home/jetson/lab-boot.log}

UNITS="nvidia-drm-reload.service face-kiosk.service gaze.service"
STATUS=/run/face/gaze-status.json

say() { printf '%s\n' "$*" >>"$OUT"; }

: >"$OUT"
say "=== watch-boot.sh started $(date '+%F %T') ==="
say "duration=${DURATION}s interval=${INTERVAL}s"
say "boot_id at start : $(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
say "booted at        : $(uptime -s 2>/dev/null)"
say "kernel           : $(uname -r)"
say ""

# Remember the last values so a CHANGE can be flagged rather than buried in a
# wall of identical lines. The file should be skimmable: only transitions and
# anomalies carry information.
prev_nrestarts=""
prev_flags=""
prev_uptime=""
stale_samples=0
prev_t=""
gap_count=0
anomalies=0

end=$(( $(date +%s) + DURATION ))
while [ "$(date +%s)" -lt "$end" ]; do
  now=$(date +%s)
  flags=""

  # unit states, condensed: "active|0" per unit, plus a flag for anything not active
  states=""
  for u in $UNITS; do
    a=$(systemctl is-active "$u" 2>/dev/null || echo unknown)
    n=$(systemctl show -p NRestarts --value "$u" 2>/dev/null || echo "?")
    states="$states $u=$a/$n"
    case "$a" in
      active) ;;
      *) flags="$flags ${u}_NOT_ACTIVE($a)" ;;
    esac
  done
  sleep_info="$states"

  # a restart count that moves means the unit is crash-looping
  if [ -n "$prev_nrestarts" ] && [ "$states" != "$prev_nrestarts" ]; then
    flags="$flags UNIT_STATE_OR_RESTART_CHANGED"
  fi

  # /run/face must exist, and the status file must be advancing
  if [ ! -d /run/face ]; then
    flags="$flags RUN_FACE_MISSING"
  fi
  if [ -f "$STATUS" ]; then
    t=$(jq -r '.t // empty' "$STATUS" 2>/dev/null)
    st=$(jq -c '{has_face,gaze_mag,expression,fps_avg,hit_rate}' "$STATUS" 2>/dev/null)
    if [ -n "$t" ] && [ -n "$prev_t" ] && [ "$t" = "$prev_t" ]; then
      stale_samples=$((stale_samples + 1))
      # ~15s of no update is a real stall, not a scheduling hiccup
      [ "$stale_samples" -ge 5 ] && flags="$flags STATUS_STALLED(${stale_samples})"
    else
      stale_samples=0
    fi
    prev_t="$t"
  else
    flags="$flags STATUS_FILE_MISSING"
  fi

  up=$(cut -d. -f1 /proc/uptime 2>/dev/null)
  # uptime going BACKWARDS means the machine rebooted mid-run
  if [ -n "$prev_uptime" ] && [ -n "$up" ] && [ "$up" -lt "$prev_uptime" ]; then
    flags="$flags *** REBOOTED (uptime ${prev_uptime}s -> ${up}s) ***"
    gap_count=$((gap_count + 1))
  fi
  prev_uptime=$up

  if [ -n "$flags" ]; then
    anomalies=$((anomalies + 1))
    say "$(date '+%H:%M:%S') UP=${up}s ANOMALY:$flags |$sleep_info"
    say "    status: $st"
  elif [ "$states" != "$prev_nrestarts" ] || [ -z "$prev_nrestarts" ]; then
    # only print a full line when something changed, or on the very first sample
    say "$(date '+%H:%M:%S') UP=${up}s ok |$sleep_info"
    say "    status: $st"
  fi

  prev_nrestarts="$states"
  sleep "$INTERVAL"
done

say ""
say "=== finished $(date '+%F %T') ==="
say "anomalies: $anomalies   reboots seen: $gap_count"
say "final boot_id: $(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
say "final uptime : $(cut -d. -f1 /proc/uptime)s"
