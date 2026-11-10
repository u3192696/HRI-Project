#!/usr/bin/env python3
"""
lipsync.py — play a pre-rendered voice clip while driving the face's mouth.

WHY THIS IS A SEPARATE PROCESS
------------------------------
The face is a pure function of (context, size, time, state) and never touches
audio. This process owns two things the face should not: decoding a WAV and
talking to the sound card. It writes the lip-sync envelope to the face's MOUTH
file (see face.py), and the face draws whatever it finds there.

That keeps the seam the same shape as the rest of the robot: a helper writes
JSON, the renderer renders. It also means a crash here cannot take the face
down — the face sees the mouth file go stale and simply closes the mouth.

THE MOUTH IS DRIVEN BY THE REAL ENVELOPE, NOT A TIMER
-----------------------------------------------------
The built-in `speaking` expression oscillates the mouth at a fixed ~1.75 Hz
regardless of what is being said, so it flaps happily through silence and never
matches a syllable. Here the opening comes from measured loudness in 10 ms
frames, so the mouth closes between words and opens on the vowels.

Shape follows audio loudness, and WIDTH follows spectral brightness (a
zero-crossing-rate proxy). Wide-ish for fricatives and /i/, narrow for closed
vowels — a crude viseme, but it stops the mouth reading as a single flap.

usage:
    ./lipsync.py clip.wav --sink <pulse-sink>
    ./lipsync.py clip.wav --sink alsa_output.platform-3510000.hda.hdmi-stereo
    ./lipsync.py clip.wav --dry-run          # drive the mouth, play nothing
"""
from __future__ import annotations

import argparse
import array
import fcntl
import glob
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import wave

DEFAULT_MOUTH = os.environ.get("FACE_MOUTH", "/run/face/mouth.json")

# Exit code meaning "the mouth is already claimed". The greeter reads this to
# tell a conflict apart from a genuine playback failure.
EX_TEMPFAIL = 75


def acquire_mouth_lock(mouth_path):
    """Take an exclusive lock so two players never drive the mouth at once.

    Two players means two audio streams and two writers on one file: the sound
    doubles up, and the mouth follows whichever wrote last - which reads as
    broken lip-sync rather than as a conflict. Non-blocking on purpose: a second
    player should refuse loudly rather than queue, because a queued playback
    looks like the robot ignoring the request and then talking over itself later.

    The lock lives for the process (flock is released when the fd closes), so the
    caller MUST keep the returned fd referenced - a returned-but-discarded fd is
    garbage collected, closed, and the lock silently released.
    """
    lock_path = str(mouth_path) + ".lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        sys.stderr.write("lipsync: cannot open %s: %s\n" % (lock_path, exc))
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write(
            "lipsync: %s is already being driven by another player.\n"
            "  Refusing to start: two players would double the audio and the\n"
            "  mouth would follow whichever wrote last. Pass --no-lock to\n"
            "  overlap on purpose.\n" % mouth_path)
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode())
    except OSError:
        pass
    return fd


# --------------------------------------------------------------------------
# Audio -> envelope
# --------------------------------------------------------------------------
def read_wav_mono(path: pathlib.Path) -> tuple[array.array, int]:
    """Return (samples as signed 16-bit, sample rate). Downmixes to mono."""
    with wave.open(str(path), "rb") as w:
        n_ch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("lipsync: expected 16-bit PCM, got %d-bit" % (width * 8))
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    if n_ch != 1:
        # Take the first channel rather than mixing: for a single voice both
        # channels are normally identical, and mixing cannot add information.
        samples = array.array("h", samples[0::n_ch])
    return samples, rate


def build_envelope(samples, rate, hop_ms=10.0, attack_ms=15.0, release_ms=70.0,
                    gate=0.035, curve=0.6, width_lo=0.94, width_hi=1.12):
    """Loudness + brightness per frame, shaped into a mouth track.

    Returns (opens, wides, hop_secs), each value already in its final 0..1 /
    multiplier form, so the playback loop is a lookup and nothing more.
    """
    hop = max(1, int(rate * hop_ms / 1000.0))
    n_frames = max(1, (len(samples) - hop) // hop + 1)

    loud = [0.0] * n_frames
    bright = [0.0] * n_frames
    for i in range(n_frames):
        start = i * hop
        chunk = samples[start:start + hop]
        n = len(chunk)
        if n == 0:
            continue
        acc = 0
        crossings = 0
        prev = chunk[0]
        for v in chunk:
            acc += v * v
            if (v < 0) != (prev < 0):
                crossings += 1
            prev = v
        loud[i] = math.sqrt(acc / n) / 32768.0
        bright[i] = crossings / n

    # Normalise against a HIGH percentile, not the max: one clipped plosive
    # would otherwise squash the whole track and the mouth would barely open.
    ranked = sorted(loud)
    ref = ranked[int(len(ranked) * 0.95)] if ranked else 0.0
    if ref <= 0:
        return [0.0] * n_frames, [1.0] * n_frames, hop / rate

    # Brightness reference over SPEECH ONLY, so silence does not drag the
    # baseline toward zero and make every frame look "bright".
    speech = [b for b, l in zip(bright, loud) if l > gate * ref]
    bref = sorted(speech)[int(len(speech) * 0.9)] if speech else 0.0

    a_coef = math.exp(-hop_ms / max(1.0, attack_ms))
    r_coef = math.exp(-hop_ms / max(1.0, release_ms))

    opens = [0.0] * n_frames
    wides = [1.0] * n_frames
    level = 0.0
    for i in range(n_frames):
        target = min(1.0, loud[i] / ref) ** curve
        if target < gate:
            target = 0.0
        # Fast attack, slower release: a mouth opens quickly and closes more
        # gently. Symmetric smoothing here reads as a mechanical flap.
        coef = a_coef if target > level else r_coef
        level = target + (level - target) * coef
        opens[i] = level
        if bref > 0:
            frac = min(1.0, max(0.0, bright[i] / bref))
            wides[i] = width_lo + (width_hi - width_lo) * frac
    return opens, wides, hop / rate


# --------------------------------------------------------------------------
# Mouth file
# --------------------------------------------------------------------------
def write_mouth_atomic(path: pathlib.Path, open_v: float, wide: float) -> None:
    """Write, then rename. Renaming is atomic, so the reader never sees a
    half-written file. The same approach gaze.py uses for state.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mouth-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"open": round(open_v, 4), "wide": round(wide, 4)}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------
def alsa_any_pcm_running() -> bool:
    """True once a playback PCM has actually started.

    Used to find the real audio start instead of guessing a constant latency.
    paplay returns as soon as the stream is queued, which is well before the
    card is draining anything; driving the mouth from process-spawn time would
    make it lead the audio by the whole pipeline buffer.
    """
    # PLAYBACK PCMs only — the glob MUST be `pcm*p`. A capture PCM (`pcm*c`) on
    # this board sits RUNNING permanently because the camera is capturing, so a
    # wider glob reports "audio has started" the instant it is asked, every
    # time, and the mouth then leads the audio by the whole pipeline buffer.
    for p in glob.glob("/proc/asound/card*/pcm*p/sub0/status"):
        try:
            with open(p, "r") as fh:
                if "RUNNING" in fh.read(256):
                    return True
        except OSError:
            pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Play a clip and lip-sync the face")
    ap.add_argument("wav", type=pathlib.Path)
    ap.add_argument("--mouth", type=pathlib.Path, default=pathlib.Path(DEFAULT_MOUTH),
                    help="face lip-sync file (default %(default)s)")
    ap.add_argument("--sink", default=None,
                    help="PulseAudio sink name. Strongly recommended: the "
                         "default sink on this board is an internal analogue "
                         "device with no physical output")
    ap.add_argument("--player", default=None,
                    help="override the player command entirely")
    ap.add_argument("--rate", type=float, default=60.0,
                    help="mouth updates per second (default %(default)s)")
    ap.add_argument("--lead", type=float, default=0.0,
                    help="compensation for the display's AUDIO latency, in "
                         "seconds. The mouth runs on wall-clock time, but the "
                         "sound you actually hear is delayed by the display's "
                         "own audio processing (often 100-200ms on a TV or "
                         "projector). So: a mouth that looks AHEAD of the sound "
                         "wants a LARGER --lead; one that looks BEHIND the "
                         "sound wants a NEGATIVE value. Retune if the display "
                         "changes.")
    ap.add_argument("--start-timeout", type=float, default=0.6,
                    help="give up waiting for the card to go RUNNING after "
                         "this many seconds (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="drive the mouth but play no audio; useful for "
                         "checking the envelope on its own")
    ap.add_argument("--no-lock", action="store_true",
                    help="skip the single-player lock on the mouth file. For "
                         "deliberate overlaps only: two players double the audio "
                         "and fight over the mouth")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds into the clip to begin, for spot checks")
    ap.add_argument("--duration", type=float, default=None,
                    help="stop after this many seconds of the clip instead of "
                         "playing all of it. A whole script is too long to "
                         "compare by eye; A/B trials want a short passage")
    args = ap.parse_args()

    if not args.wav.is_file():
        raise SystemExit("lipsync: no such file: %s" % args.wav)

    # Claim the mouth before doing any work, and keep lock_fd referenced for the
    # life of the process - that reference IS what holds the lock.
    lock_fd = None
    if not args.no_lock:
        lock_fd = acquire_mouth_lock(args.mouth)
        if lock_fd is None:
            return EX_TEMPFAIL

    samples, rate = read_wav_mono(args.wav)
    opens, wides, hop_s = build_envelope(samples, rate)
    duration = len(samples) / float(rate)
    # How much of the clip to actually perform. A/B trials want a short passage:
    # asking someone to compare 58 s of continuous speech is asking too much.
    end_at = duration if args.duration is None else min(duration, args.duration)
    speech_frames = sum(1 for v in opens if v > 0.05)

    sys.stderr.write(
        "lipsync: %s  %.1fs @ %d Hz  %d frames (%.0f%% voiced)\n"
        % (args.wav.name, duration, rate, len(opens),
           100.0 * speech_frames / max(1, len(opens)))
    )

    if args.player:
        player = args.player.split()
    elif shutil.which("paplay"):
        player = ["paplay"]
        if args.sink:
            player.append("--device=%s" % args.sink)
    else:
        player = ["aplay", "-q"]
        if args.sink:
            player.append("-D%s" % args.sink)

    proc = None
    if not args.dry_run:
        cmd = player + [str(args.wav)]
        sys.stderr.write("lipsync: playing via %s\n" % " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    # Establish the clock. Prefer the moment the card actually starts draining;
    # fall back to right now if that cannot be observed.
    t_spawn = time.monotonic()
    t0 = t_spawn
    if not args.dry_run:
        deadline = t_spawn + args.start_timeout
        while time.monotonic() < deadline:
            if alsa_any_pcm_running():
                t0 = time.monotonic()
                break
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.005)

    interval = 1.0 / max(1.0, args.rate)
    last_written = None
    last_write_t = 0.0
    try:
        while True:
            if proc is not None and proc.poll() is not None:
                break
            elapsed = time.monotonic() - t0 - args.lead + args.offset
            if elapsed >= end_at:
                break
            if elapsed < 0.0:
                # Before the clip begins there is no sound, so the mouth is shut.
                # Do NOT fall through to frame 0: frame 0 is whatever the clip
                # happens to open on, and a clip starting on a plosive would hold
                # a wide-open mouth for the whole --lead window - which reads as
                # the mouth starting before the sound.
                value = (0.0, 1.0)
            else:
                idx = int(elapsed / hop_s)
                if idx >= len(opens):
                    idx = len(opens) - 1
                value = (opens[idx], wides[idx])
            now = time.monotonic()
            changed = (last_written is None
                       or abs(value[0] - last_written[0]) > 0.004
                       or abs(value[1] - last_written[1]) > 0.004)
            # Heartbeat even when the value has not changed. The face treats a
            # mouth file untouched for MOUTH_STALE_SECS as absent, so a long
            # silence in the clip - or the --lead window before it starts -
            # would otherwise release the drive and let the mouth flap on its
            # own, which looks exactly like the fault we are chasing.
            if changed or (now - last_write_t) > 0.25:
                write_mouth_atomic(args.mouth, *value)
                last_written = value
                last_write_t = now
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        # Close the mouth on the way out, whatever happened. Leaving it open
        # would read as the robot freezing mid-word.
        try:
            write_mouth_atomic(args.mouth, 0.0, 1.0)
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
