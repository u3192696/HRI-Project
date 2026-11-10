#!/usr/bin/env bash
# make-calibration.sh — build the lip-sync test tones from scratch.
#
# WHY THESE EXIST
#   You cannot tune lip-sync by eye against continuous speech. Readings of the
#   same speech clip contradict each other inside a ~50-70 ms band you cannot see
#   into, and each individual reading still feels confident. Two short tones fix
#   that:
#
#     cue.wav               low "get ready" blip, played before a trial.
#     sync-calibration.wav  three 180 ms tone bursts over PURE DIGITAL SILENCE.
#                           The mouth must pop exactly on each beep and do
#                           NOTHING in between. Judge only dead on / early /
#                           late - which way, never how much.
#
#   cue.wav is a DIFFERENT PITCH from the bursts on purpose. At the same pitch
#   the observer counts four events and mistakes the cue for a trial event.
#
# WHY tones/ AND NOT clips/
#   clips/ is the VOICE LIBRARY: speech the robot actually performs, generated in
#   ~/Projects/breeze-tts and promoted here once chosen. These two files are the
#   opposite - test signals, meaningless as speech, useful only for measurement.
#   Keeping them apart means "what the robot says" and "how we measure it" can
#   never be confused for one another.
#
# Regenerate with this script rather than copying the WAVs between machines, so
# the instrument is reproducible and reviewable.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/tones"
mkdir -p "${OUT}"

python3 - "${OUT}" <<'PY'
import math
import struct
import sys
import wave

out = sys.argv[1]
SR = 24000


def tone(freq, secs, amp=0.35, fade_secs=0.005):
    n = int(SR * secs)
    fade = int(SR * fade_secs)      # short fades: no click at the edges
    return [int(amp * min(1.0, i / fade, (n - i) / fade)
                * 32767 * math.sin(2 * math.pi * freq * i / SR))
            for i in range(n)]


def write(path, frames):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<%dh" % len(frames), *frames))


# Get-ready blip: 440 Hz, an octave below the bursts so it cannot be confused
# with a trial event.
write(out + "/cue.wav", tone(440.0, 0.10))

# The instrument itself: bursts at 1.0 / 3.0 / 5.0 s, digital silence between.
# True zeros mean the drive is exactly 0.0 in the gaps, so the mouth cannot move
# with no sound - the failure mode we are hunting.
#
# The 10 ms burst fade is not cosmetic tidying: it is the value the deployed
# offset was tuned against. Reproduce the instrument byte-for-byte, or the tuned
# number no longer provably belongs to this signal. Verify after any edit:
#   sha256sum tones/cue.wav tones/sync-calibration.wav
buf = [0] * int(SR * 8.0)
burst = tone(880.0, 0.18, amp=0.40, fade_secs=0.01)
for start in (1.0, 3.0, 5.0):
    off = int(SR * start)
    buf[off:off + len(burst)] = burst
write(out + "/sync-calibration.wav", buf)

print("  wrote tones/cue.wav (440 Hz blip) and "
      "tones/sync-calibration.wav (3 x 880 Hz bursts)")
PY
