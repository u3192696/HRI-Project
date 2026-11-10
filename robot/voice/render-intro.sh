#!/usr/bin/env bash
# render-intro.sh — pre-render the robot's spoken introduction to a WAV.
#
# Runs on a machine WITH network (serenity/athena), NOT on the Orin. The voice
# is baked into the WAV at build time, so the robot needs no model, no TTS
# engine and no network at runtime — it just plays a file.
#
# Usage:
#   ./render-intro.sh                      # the chosen voice (preset: pip)
#   ./render-intro.sh --preset cori        # a different auditioned voice
#   ./render-intro.sh --list-presets
#   ./render-intro.sh --voice en_US-amy-medium   # any Piper voice, ad hoc
#   ./render-intro.sh --pitch 3 --speed 1.05     # override the transformation
#
# Writes to robot/voice/intro.wav. Does NOT deploy — run deploy.sh for that.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOICE_DIR="${PIPER_VOICE_CACHE:-${HOME}/.cache/piper-voices}"
TEXT="${HERE}/intro.txt"
OUT="${HERE}/intro.wav"
PIPER="${PIPER:-piper}"
FFMPEG="${FFMPEG:-ffmpeg}"

# --- voice presets ---------------------------------------------------------
# NAME|voice|speaker|pitch(semitones)|speed
#
# These are the voices that were actually auditioned (see auditions/README.md).
# "pip" is the chosen one: Alan's voice brightened, not replaced. The transform
# is a pitch/formant shift that preserves duration (asetrate moves pitch AND
# speed; atempo puts the duration back).
PRESETS=(
  "pip|en_GB-alan-medium||2|1.06"
  "alan-stock|en_GB-alan-medium||0|1"
  "cori|en_GB-cori-high||0|1"
  "vctk-s0|en_GB-vctk-medium|0|0|1"
  "vctk-s51|en_GB-vctk-medium|51|0|1"
  "hfc-up3|en_US-hfc_female-medium||3|1"
  "ljspeech-up3|en_US-ljspeech-high||3|1"
)
DEFAULT_PRESET="pip"
PRESET="${DEFAULT_PRESET}"
VOICE="" SPEAKER="" PITCH="" SPEED=""

die() { printf 'render-intro: %s\n' "$1" >&2; exit 1; }

preset_line() {
    local want="$1" p
    for p in "${PRESETS[@]}"; do
        [ "${p%%|*}" = "${want}" ] && { printf '%s' "$p"; return 0; }
    done
    return 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --preset) PRESET="$2"; shift 2 ;;
        --voice)  VOICE="$2";  shift 2 ;;
        --speaker) SPEAKER="$2"; shift 2 ;;
        --pitch)  PITCH="$2";  shift 2 ;;
        --speed)  SPEED="$2";  shift 2 ;;
        --out)    OUT="$2";    shift 2 ;;
        --text)   TEXT="$2";   shift 2 ;;
        --list-presets)
            printf 'Auditioned voice presets (default: %s):\n\n' "${DEFAULT_PRESET}"
            printf '  %-14s %-26s %s\n' NAME VOICE TRANSFORM
            for p in "${PRESETS[@]}"; do
                IFS='|' read -r n v sp pi sw <<<"${p}"
                local_t="stock"
                [ "${pi}" != "0" ] && local_t="pitch ${pi:+$pi}st"
                [ "${sw}" != "1" ] && local_t="${local_t}, speed ${sw}x"
                [ -n "${sp}" ] && local_t="${local_t}, speaker ${sp}"
                printf '  %-14s %-26s %s\n' "${n}" "${v}" "${local_t}"
            done
            exit 0 ;;
        -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# Resolve preset unless the user named a voice explicitly.
if [ -z "${VOICE}" ]; then
    line="$(preset_line "${PRESET}")" || die "no such preset: ${PRESET} (try --list-presets)"
    IFS='|' read -r _ VOICE SPEAKER PITCH SPEED <<<"${line}"
    VOICE="${VOICE:-en_GB-alan-medium}"
fi
: "${PITCH:=0}"; : "${SPEED:=1}"

[ -f "${TEXT}" ] || die "no script text at ${TEXT}"

# --- work out the voice's URL from its name --------------------------------
# Piper voice names are  <lang>_<REGION>-<name>-<quality>.onnx  and the repo
# lays them out as  <lang>/<lang>_<REGION>/<name>/<quality>/<file>
voice_url() {
    local v="$1" base="https://huggingface.co/rhasspy/piper-voices/resolve/main"
    local lang family name quality
    lang="${v%%_*}"; family="${v%%-*}"; name="${v#*-}"; name="${name%%-*}"
    quality="${v##*-}"
    printf '%s/%s/%s/%s/%s/%s.onnx' "$base" "$lang" "$family" "$name" "$quality" "$v"
}

mkdir -p "${VOICE_DIR}"
MODEL="${VOICE_DIR}/${VOICE}.onnx"
CONF="${MODEL}.json"

if [ ! -s "${MODEL}" ] || [ ! -s "${CONF}" ]; then
    URL="$(voice_url "${VOICE}")"
    printf 'render-intro: fetching voice %s\n  from %s\n' "${VOICE}" "${URL}"
    command -v curl >/dev/null 2>&1 || die "curl is required to fetch the voice model"
    curl -fsSL "${URL}"      -o "${MODEL}.part" || die "could not fetch the model"
    curl -fsSL "${URL}.json" -o "${CONF}.part"  || die "could not fetch the model config"
    # Guard against an HTML error page being saved as a "model".
    [ "$(head -c 4 "${MODEL}.part" | od -An -c | tr -d ' \n')" = '\b\b022\a' ] \
        || die "downloaded model is not an ONNX file (got: $(head -c 40 "${MODEL}.part"))"
    head -c 1 "${CONF}.part" | grep -q '{' || die "downloaded config is not JSON"
    mv "${MODEL}.part" "${MODEL}"; mv "${CONF}.part" "${CONF}"
    printf '  cached to %s\n' "${VOICE_DIR}"
fi

printf 'render-intro: preset   %s\n' "${PRESET}"
printf 'render-intro: voice    %s%s\n' "${VOICE}" \
    "$([ -n "${SPEAKER}" ] && printf ' (speaker %s)' "${SPEAKER}")"
[ "${PITCH}" != "0" ] || [ "${SPEED}" != "1" ] && \
    printf 'render-intro: transform pitch %s st, speed %sx\n' "${PITCH}" "${SPEED}"
printf 'render-intro: script   %s (%s words)\n' "${TEXT}" "$(wc -w < "${TEXT}")"
printf 'render-intro: output   %s\n' "${OUT}"

# DO NOT PASS --sentence-silence. In this piper-tts build it emits BROADBAND
# NOISE where the silence should be rather than silence: with it, ~40% of the
# output measures as noise; without it, ~12% (and that residual is just
# sibilants). A single sentence in isolation looks fine either way, which is
# why this only shows up on multi-sentence input. Verified by measuring the
# energy and zero-crossing rate per 100ms window, not by ear.
#
# Piper already puts a natural pause at sentence boundaries, so nothing is lost.
RAW="${OUT}.raw.wav"
PIPER_ARGS=(
    --model "${MODEL}" --config "${CONF}"
    --input-file "${TEXT}" --output-file "${RAW}"
    --length-scale 1.02 --noise-scale 0.64 --noise-w-scale 0.80
)
[ -n "${SPEAKER}" ] && PIPER_ARGS+=(--speaker "${SPEAKER}")
"${PIPER}" "${PIPER_ARGS[@]}"
[ -s "${RAW}" ] || die "piper produced no output"

# --- pitch / speed transform ------------------------------------------------
# asetrate=<rate>*F changes pitch AND speed by F; atempo=1/F restores the
# duration. Net effect: pitch (and formants) move, tempo is preserved. This is
# what turns Alan's flat 100 Hz delivery into something brighter.
if [ "${PITCH}" != "0" ] || [ "${SPEED}" != "1" ]; then
    command -v "${FFMPEG}" >/dev/null 2>&1 || die "ffmpeg is required for --pitch/--speed"
    RATE="$(od -An -tu4 -j 24 -N 4 "${RAW}" | tr -d ' ')"
    [ -n "${RATE}" ] || die "could not read the sample rate from ${RAW}"
    F="$(awk -v st="${PITCH}" 'BEGIN{printf "%.6f", 2^(st/12)}')"
    TEMPO="$(awk -v f="${F}" -v s="${SPEED}" 'BEGIN{printf "%.6f", s/f}')"
    "${FFMPEG}" -y -v error -i "${RAW}" \
        -af "asetrate=${RATE}*${F},aresample=${RATE},atempo=${TEMPO}" \
        "${OUT}" || die "ffmpeg pitch/speed transform failed"
    rm -f "${RAW}"
else
    mv "${RAW}" "${OUT}"
fi

[ -s "${OUT}" ] || die "no output produced"

# --- normalise the container -------------------------------------------------
# Piper writes an ODD data-chunk size for 16-bit PCM, which is invalid: a
# 16-bit sample cannot be split across a byte boundary, and ffmpeg/aplay report
# "Invalid PCM packet, data has size 1 but at least a size of 2 was expected".
# Trim the trailing odd byte and rewrite both size fields so the file is valid
# everywhere. Without this the WAV plays on some players and not others — the
# worst kind of bug to find during a demo.
python3 - "${OUT}" <<'PY' 2>/dev/null || true
import struct, sys, pathlib
p = pathlib.Path(sys.argv[1])
d = p.read_bytes()
if len(d) >= 44:
    data = struct.unpack("<I", d[40:44])[0]
    if data % 2:
        even = data - 1
        out = bytearray(d[:44] + d[44:44 + even])
        struct.pack_into("<I", out, 4, 36 + even)   # RIFF size
        struct.pack_into("<I", out, 40, even)       # data size
        p.write_bytes(bytes(out))
        print(f"  fixed    odd data chunk {data:,} -> {even:,} bytes")
PY

# --- report + quality gate ---------------------------------------------------
# Measure the result. The gate FAILS the build if the render is substantially
# noise — the check that would have caught the --sentence-silence bug before it
# reached a listener. It is invisible to an ear test of a single sentence, and a
# WAV that plays "fine" while being 40% static is the worst kind of demo bug.
# Also reports pitch, because "boring" correlates with a low, flat F0 and that
# should be visible at build time, not discovered on the bench.
if command -v python3 >/dev/null 2>&1; then
    python3 - "${OUT}" <<'PY'
import struct, sys, math
p = sys.argv[1]
d = open(p, "rb").read()
rate = struct.unpack("<I", d[24:28])[0]
raw = d[44:]; n = len(raw) // 2
s = struct.unpack(f"<{n}h", raw[:n*2])

W = int(rate * 0.10)
noise = silent = speech = 0
for i in range(0, n - W, W):
    w = s[i:i+W]
    rms = math.sqrt(sum(x*x for x in w) / len(w))
    zc = sum(1 for a, b in zip(w, w[1:]) if (a < 0) != (b < 0)) / len(w)
    if rms < 80: silent += 1
    elif zc > 0.32: noise += 1
    else: speech += 1
tot = max(noise + silent + speech, 1)
pct = 100 * noise / tot

# F0 via autocorrelation over voiced frames only
win = int(rate*0.04); hop = int(rate*0.02)
lo, hi = int(rate/450), int(rate/60)
vals = []
for i in range(0, n - win, hop):
    w = s[i:i+win]
    if math.sqrt(sum(x*x for x in w)/win) < 300: continue
    m = sum(w)/win; w = [x-m for x in w]
    best = bl = 0
    for lag in range(lo, hi):
        c = sum(w[k]*w[k+lag] for k in range(0, win-lag, 4))
        if c > best: best, bl = c, lag
    if bl: vals.append(rate/bl)
f0 = rang = 0
if vals:
    vals.sort(); f0 = vals[len(vals)//2]
    rang = vals[9*len(vals)//10] - vals[len(vals)//10]

print(f"  wav      {rate} Hz, {n/rate:.1f}s, {len(d)/1024/1024:.2f} MB")
print(f"  speech   {100*speech/tot:.1f}%   noise {pct:.1f}%   silence {100*silent/tot:.1f}%")
if f0: print(f"  pitch    {f0:.0f} Hz median, {rang:.0f} Hz 10-90 spread")
if pct > 25:
    print(f"  FAIL: {pct:.1f}% of the render measures as noise (limit 25%).")
    print("        Usual cause: an option that inserts padded audio (see the")
    print("        --sentence-silence note above). Do not ship this file.")
    sys.exit(1)
PY
    _rc=$?
    [ "${_rc}" -eq 0 ] || die "rendered audio failed the noise check (see above)"
fi

printf 'render-intro: done.\n'
