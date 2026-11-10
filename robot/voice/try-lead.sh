#!/usr/bin/env bash
# try-lead.sh — play one short listen at a single mouth/audio offset.
#
# WHY ONE AT A TIME, ANNOUNCED BY A BEEP
#   An earlier version swept six offsets in a row and labelled trial N by
#   flashing the mouth N times. That was self-defeating: the label WAS mouth
#   movement with no sound, so the thing being tuned and the thing announcing
#   the trials were the same channel. The listener duly reported the counter as
#   the fault, and a longer run of flashes looked like a worse offset.
#   Never label a trial using the channel under test.
#
#   Trials are now announced with an audio beep, and the mouth is explicitly
#   held shut until the clip starts. One offset per run: the listener reports
#   only the direction (leading or lagging), which is a reliable judgement, and
#   the caller bisects on it.
#
# usage:
#   ./try-lead.sh CLIP.wav 0.20
#   DURATION=8 READY=20 ./try-lead.sh CLIP.wav 0.15
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIP="${1:-}"
LEAD="${2:-}"
[ -n "${CLIP}" ] && [ -f "${CLIP}" ] || {
    printf 'usage: %s CLIP.wav [LEAD]\n' "$(basename "$0")" >&2
    printf '       LEAD defaults to the tuned value in display.conf\n' >&2
    exit 2
}
_lead_arg="${LEAD}"

# Display-specific sink and tuned offset. Sourced so the tuned value does not
# have to be remembered per run, and so it travels with the sink it was tuned
# against. An explicit environment SINK, or a positional LEAD, still wins.
CONF="${CONF:-${HERE}/display.conf}"
DEFAULT_SINK=alsa_output.platform-3510000.hda.hdmi-stereo
_env_sink="${SINK:-}"
SINK=""
LEAD=""
# shellcheck disable=SC1090
[ -f "${CONF}" ] && . "${CONF}" || true
SINK="${_env_sink:-${SINK:-${DEFAULT_SINK}}}"
LEAD="${_lead_arg:-${LEAD:-0.0}}"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
MOUTH="${FACE_MOUTH:-/run/face/mouth.json}"
DURATION="${DURATION:-12}"
READY="${READY:-20}"
CUE="${CUE:-${HERE}/tones/cue.wav}"

printf '{"open": 0.0, "wide": 1.0}' > "${MOUTH}" 2>/dev/null || true
printf 'try-lead: clip     %s\n' "${CLIP}"
printf 'try-lead: sink     %s\n' "${SINK}"
printf 'try-lead: --lead %s   %ss of clip   starting in %ss\n' \
    "${LEAD}" "${DURATION}" "${READY}"
if [ -n "${DRY:-}" ]; then
    printf 'try-lead: DRY set - resolved settings only, nothing played.\n'
    exit 0
fi
printf 'try-lead: you will hear a beep, then the clip.\n'
sleep "${READY}"

# Audio-only cue. Deliberately NOT mirrored by the mouth: the mouth stays shut
# until the clip itself starts, so nothing moves before the sound.
if [ -f "${CUE}" ]; then
    paplay --device="${SINK}" "${CUE}" 2>/dev/null || true
fi
sleep 0.4

python3 "${HERE}/lipsync.py" "${CLIP}" \
    --sink "${SINK}" \
    --duration "${DURATION}" \
    --lead "${LEAD}"
