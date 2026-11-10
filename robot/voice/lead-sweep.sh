#!/usr/bin/env bash
#
# SUPERSEDED — kept only as a cautionary example. Do NOT use it to set an offset.
#
#   This script announces trial N by flashing the mouth N times. That is
#   self-defeating: the label IS mouth movement with no sound, which is the very
#   fault being tuned out. The observer duly reported the counter as the fault,
#   and reported it getting worse every trial — because it was. 1 flash (0.36 s)
#   before trial 1, six flashes (2.16 s) before trial 6.
#
#   Never label a trial using the channel under test. Use try-lead.sh with
#   tones/sync-calibration.wav instead.
#
# lead-sweep.sh — play one passage at several mouth/audio offsets, back to back,
# so a human can PICK the best instead of characterising the error into a number.
#
# WHY PICK RATHER THAN MEASURE
#   Judging "is that 80 ms early or 140 ms early?" by eye is hard. Choosing the
#   best of six side by side is easy. Same reasoning as the voice audition reel:
#   offer options, do not ask for a number.
#
# WHAT THE UNKNOWN IS
#   The mouth is driven on wall-clock time, but the sound a TV or projector
#   actually emits is delayed by its own audio buffering (commonly 100-200 ms).
#   That delay cannot be read off the system, so it has to be found by eye.
#   --lead tells the mouth to wait by that much.
#
# Each trial is announced by a number of mouth flashes equal to its position, so
# counting does not depend on memory. Trials are identical apart from the offset.
#
# usage:
#   ./lead-sweep.sh clips/rung1-cartoon-intro.wav
#   LEADS="-0.3 -0.2 -0.1 0" ./lead-sweep.sh clip.wav
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIP="${1:-}"
[ -n "${CLIP}" ] && [ -f "${CLIP}" ] || {
    printf 'usage: %s CLIP.wav\n' "$(basename "$0")" >&2
    exit 2
}
[ -f "${HERE}/lipsync.py" ] || {
    printf 'FATAL: lipsync.py is not beside this script\n' >&2
    exit 2
}

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
SINK="${SINK:-alsa_output.platform-3510000.hda.hdmi-stereo}"
MOUTH="${FACE_MOUTH:-/run/face/mouth.json}"
DURATION="${DURATION:-12}"
GAP="${GAP:-2.5}"
READY="${READY:-10}"
# Default spans both directions around zero, biased slightly toward positive:
# a display's audio delay pushes the mouth EARLY, so positive is the more
# likely half of the answer.
LEADS="${LEADS:--0.20 -0.10 0.00 0.10 0.20 0.30}"

die() { printf 'sweep: %s\n' "$1" >&2; exit 1; }
command -v paplay >/dev/null 2>&1 || die "paplay not found"

# Visual trial counter: flash the mouth n times. Written straight to the mouth
# file, which is exactly what the driver does; the face notices the mtime change.
mark() {
    _n="$1"
    for _ in $(seq 1 "${_n}"); do
        printf '{"open": 1.0, "wide": 1.0}' > "${MOUTH}" 2>/dev/null || true
        sleep 0.18
        printf '{"open": 0.0, "wide": 1.0}' > "${MOUTH}" 2>/dev/null || true
        sleep 0.18
    done
}

printf 'sweep: clip     %s\n' "${CLIP}"
printf 'sweep: sink     %s\n' "${SINK}"
printf 'sweep: %ss of clip per trial, %ss gap\n\n' "${DURATION}" "${GAP}"

i=0
for lead in ${LEADS}; do
    i=$((i + 1))
    printf '  trial %d of %d   --lead %s\n' "${i}" "$(printf '%s\n' ${LEADS} | wc -l)" "${lead}"
done

printf '\n  Watch the mouth flash before each trial: 1 flash = trial 1, and so on.\n'
printf '  Starting in %ss...\n' "${READY}"
sleep "${READY}"

i=0
for lead in ${LEADS}; do
    i=$((i + 1))
    mark "${i}"
    printf '  >>> trial %d   --lead %s\n' "${i}" "${lead}"
    python3 "${HERE}/lipsync.py" "${CLIP}" \
        --sink "${SINK}" \
        --duration "${DURATION}" \
        --lead "${lead}" || die "trial ${i} failed"
    sleep "${GAP}"
done

printf '\n  Sweep done. Which trial looked best?\n\n'
i=0
for lead in ${LEADS}; do
    i=$((i + 1))
    printf '    trial %d  =  --lead %s\n' "${i}" "${lead}"
done
