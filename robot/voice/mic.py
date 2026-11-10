#!/usr/bin/env python3
"""
mic.py — the pinned capture device, and the only place a microphone is opened.

WHY THIS FILE EXISTS
  The Orin's default PulseAudio source is not a microphone. It is
  alsa_input.platform-sound.analog-stereo, card 1 (tegra-ape), whose PCMs are
  internal DMA fabric (tegra-dlink-N XBAR-ADMAIFN). It opens, it reports success,
  and it records EXACT digital zeros - measured, not inferred: 144000 samples, all
  0.000000.

  Anything that records @DEFAULT_SOURCE@ therefore "works" and hears nothing, and
  a listener cannot tell that apart from a quiet room. So the device is pinned by
  name in mic.conf, and this module is the one place that decision is applied.
  It REFUSES to fall back to the default source: a listener that records the wrong
  device silently is worse than one that will not start.

WHY arecord AND NOT parecord
  parecord has no duration flag - `-d` means --device - so a scripted capture
  either runs forever or needs a `timeout` wrapper, and an uncapped one keeps the
  source open after you have forgotten it. arecord has `-d SECONDS`.

WHY -D pulse AND NOT the hardware device
  The sound server keeps the ALSA PCM open even while the source is idle, so
  `arecord -D plughw:2,0` fails with "Device or resource busy" when nothing is
  recording. Capture goes through the server, with PULSE_SOURCE pinning which one.

EXIT CODES
  0   ok
  1   the configured device is missing or silent (a caller must not just retry)
  2   usage error

  $ ./mic.py --check                 # is the pinned mic actually there?
  $ ./mic.py --record out.wav -d 5   # capture 5 seconds from it
  $ ./mic.py --selftest              # config handling, no hardware needed
"""
from __future__ import annotations

import argparse
import contextlib
import math
import os
import shutil
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "mic.conf")

# What the sound server falls back to when nothing is pinned. Recording this is
# always a bug, so it is named here to be refused rather than used.
PHANTOM_SOURCE = "alsa_input.platform-sound.analog-stereo"

EX_OK, EX_DEVICE, EX_USAGE = 0, 1, 2


def pulse_env():
    """Environment that lets a sound-server client find the server.

    A systemd system unit gets no XDG_RUNTIME_DIR, and without it a client
    cannot reach the server at all - which presents as "no audio hardware",
    not as "no session".
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    return env


def read_conf(path):
    """mic.conf is a shell fragment of KEY=VALUE lines.

    Read it, never source it: this process must not execute the config file.
    """
    conf = {}
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip().strip('"').strip("'")
    return conf


def list_sources(env=None):
    """Names the sound server currently offers. None if it cannot be asked."""
    if not shutil.which("pactl"):
        return None
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             env=env or pulse_env(), capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    names = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            names.append(parts[1])
    return names


def resolve_source(conf_path=CONF, override=None):
    """The capture device to use, or None if the config does not name one."""
    if override:
        return override
    name = read_conf(conf_path).get("SOURCE", "").strip()
    return name or None


def verify_source(name, sources):
    """Return a problem string for `name`, or None if it is usable.

    `sources` is the list the server offers, so this is testable without audio.
    """
    if not name:
        return "no SOURCE configured (see mic.conf) - refusing to use the default source"
    if name == PHANTOM_SOURCE:
        return ("%s is the APE fabric, not a microphone: it records exact zeros. "
                "See mic.conf." % name)
    if sources is None:
        return None                      # cannot check; not by itself a failure
    if name not in sources:
        return ("%s is not offered by the sound server. Available: %s"
                % (name, ", ".join(sources) or "(none)"))
    return None


def measure_wav(path):
    """Peak and RMS of a 16-bit PCM WAV, so 'it recorded' means 'it recorded sound'."""
    with wave.open(path) as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("expected 16-bit PCM, got sampwidth=%d" % sw)
    count = len(raw) // 2
    samples = struct.unpack("<%dh" % count, raw[:count * 2])
    peak = max((abs(s) for s in samples), default=0)
    rms = (sum(s * s for s in samples) / count) ** 0.5 if count else 0.0
    return {"channels": ch, "rate": rate, "seconds": n / float(rate),
            "peak": peak, "rms": rms,
            "peak_dbfs": 20.0 * math.log10(peak / 32768.0) if peak else None,
            "rms_dbfs": 20.0 * math.log10(rms / 32768.0) if rms else None}


def duration_arg(seconds):
    """arecord's -d takes WHOLE seconds only.

    Passing "3.0" is refused outright ("invalid duration argument"), so the
    caller's float is rounded UP: a capture that is a moment too long is
    harmless, one that is short is a silently truncated recording.
    """
    return max(1, int(math.ceil(seconds)))


class DeviceError(RuntimeError):
    """The pinned capture device is unusable, and we will not substitute one."""


def stream_command(source=None, conf_path=CONF, rate=None, channels=None):
    """The arecord command that writes RAW PCM to stdout, and its environment.

    `-t raw` matters: arecord writing to a pipe would otherwise emit a RIFF
    header, and a reader that assumes raw samples eats 44 bytes of it as audio.
    """
    conf = read_conf(conf_path)
    src = resolve_source(conf_path, source)
    if not src:
        raise DeviceError("no SOURCE configured (see mic.conf) - "
                          "refusing to record the default source")
    problem = verify_source(src, list_sources())
    if problem:
        raise DeviceError(problem)

    cmd = ["arecord", "-D", "pulse", "-q", "-t", "raw",
           "-f", "S16_LE",
           "-r", str(rate or conf.get("RATE", "48000")),
           "-c", str(channels or conf.get("CHANNELS", "1"))]
    env = pulse_env()
    env["PULSE_SOURCE"] = src
    return cmd, env


@contextlib.contextmanager
def open_stream(source=None, conf_path=CONF, rate=None, channels=None):
    """Yield a running arecord process whose stdout is a raw PCM stream.

    Reading 16-bit mono samples from it is what a listener does. The device is
    always stopped on the way out, including on an exception, because a stray
    arecord holds the source open and contaminates the next measurement.
    """
    cmd, env = stream_command(source, conf_path, rate, channels)
    proc = subprocess.Popen(cmd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def record(out, seconds, source=None, conf_path=CONF, rate=None, channels=None):
    """Capture `seconds` of audio from the pinned device. Returns (path, err)."""
    conf = read_conf(conf_path)
    src = resolve_source(conf_path, source)
    if not src:
        return None, ("no SOURCE configured (see mic.conf) - "
                      "refusing to record the default source")
    sources = list_sources()
    problem = verify_source(src, sources)
    if problem:
        return None, problem

    cmd = ["arecord", "-D", "pulse", "-d", str(duration_arg(seconds)),
           "-f", "S16_LE",
           "-r", str(rate or conf.get("RATE", "48000")),
           "-c", str(channels or conf.get("CHANNELS", "1")),
           str(out)]
    env = pulse_env()
    env["PULSE_SOURCE"] = src
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=seconds + 15)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "could not run arecord: %s" % exc
    if done.returncode != 0:
        return None, "arecord exited %d: %s" % (done.returncode, done.stderr.strip())
    return out, None


def selftest():
    """Config handling and the refusals, with no audio hardware involved."""
    import tempfile
    failures = []

    def check(name, got, want):
        ok = got == want
        print("  %-58s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "mic.conf")
        with open(good, "w") as fh:
            fh.write("# a comment\n\nSOURCE=alsa_input.usb-TEST.analog-stereo\nRATE=48000\n")
        empty = os.path.join(td, "empty.conf")
        open(empty, "w").close()

        # read_conf ignores comments and blanks, and strips quoting
        check("read_conf parses KEY=VALUE, skips comments", read_conf(good).get("RATE"), "48000")
        check("read_conf on a missing file is empty, not a crash",
              read_conf(os.path.join(td, "nope.conf")), {})

        # precedence: an explicit override beats the file
        check("override beats the configured source",
              resolve_source(good, "alsa_input.usb-OVERRIDE.analog-stereo"),
              "alsa_input.usb-OVERRIDE.analog-stereo")
        check("configured source is used when there is no override",
              resolve_source(good), "alsa_input.usb-TEST.analog-stereo")

        # the refusals - these are the whole point of the file
        check("no SOURCE configured -> refused",
              bool(verify_source(None, ["x"])), True)
        check("empty conf -> refused",
              bool(verify_source(resolve_source(empty), ["x"])), True)
        check("the APE phantom source -> refused even if present",
              bool(verify_source(PHANTOM_SOURCE, [PHANTOM_SOURCE])), True)
        check("a configured source the server does not offer -> refused",
              bool(verify_source("alsa_input.usb-GONE.analog-stereo", ["other"])), True)
        check("a configured source the server does offer -> allowed",
              verify_source("alsa_input.usb-TEST.analog-stereo",
                            ["alsa_input.usb-TEST.analog-stereo"]), None)
        check("an unchecked source is not a failure by itself",
              verify_source("alsa_input.usb-TEST.analog-stereo", None), None)

        # measure_wav on silence must report zeros, not a divide-by-zero or a pass
        sil = os.path.join(td, "sil.wav")
        with wave.open(sil, "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
            w.writeframes(b"\x00\x00" * 4800)
        m = measure_wav(sil)
        check("measure_wav reports silence as peak 0", m["peak"], 0)
        check("measure_wav reports silence as rms 0", m["rms"], 0.0)
        check("measure_wav reads the rate from the file", m["rate"], 48000)

        # arecord -d rejects "3.0" outright, so the float must be converted
        check("duration 3.0 -> 3 (whole seconds for arecord)", duration_arg(3.0), 3)
        check("duration 0.4 -> 1 (never zero)", duration_arg(0.4), 1)
        check("duration 2.2 -> 3 (rounds up, never truncates)", duration_arg(2.2), 3)

    print()
    if failures:
        print("  SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    print("  SELFTEST PASSED (16 cases)")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="The pinned capture device: check it, or record from it.")
    ap.add_argument("--conf", default=CONF, help="config file (default %(default)s)")
    ap.add_argument("--source", default=None,
                    help="override the pinned source (testing only)")
    ap.add_argument("--list", action="store_true",
                    help="list the sources the sound server offers and exit")
    ap.add_argument("--check", action="store_true",
                    help="verify the pinned device exists, and exit 0/1")
    ap.add_argument("--record", metavar="OUT.wav",
                    help="capture to OUT.wav")
    ap.add_argument("-d", "--duration", type=float, default=5.0,
                    help="seconds to capture with --record, rounded up to a whole "
                         "second (arecord's -d takes integers) (default %(default)s)")
    ap.add_argument("--selftest", action="store_true",
                    help="exercise the config handling and refusals, with no hardware")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    sources = list_sources()

    if args.list:
        if sources is None:
            print("pactl could not list sources (is the sound server up?)")
            return EX_DEVICE
        pinned = resolve_source(args.conf, args.source)
        for s in sources:
            marks = []
            if s == pinned:
                marks.append("<- pinned in mic.conf")
            if s == PHANTOM_SOURCE:
                marks.append("<- APE FABRIC, records exact zeros, NOT a microphone")
            print("  %s %s" % (s, " ".join(marks)))
        return EX_OK

    src = resolve_source(args.conf, args.source)
    problem = verify_source(src, sources)

    if args.check:
        print("mic.conf SOURCE = %s" % (src or "<unset>"))
        if problem:
            print("  PROBLEM: %s" % problem)
            return EX_DEVICE
        print("  ok: the sound server offers that source")
        return EX_OK

    if args.record:
        if problem:
            print("mic: %s" % problem, file=sys.stderr)
            return EX_DEVICE
        out, err = record(args.record, args.duration, args.source, args.conf)
        if err:
            print("mic: %s" % err, file=sys.stderr)
            return EX_DEVICE
        try:
            m = measure_wav(out)
        except (OSError, ValueError, wave.Error) as exc:
            print("mic: captured %s but cannot read it back: %s" % (out, exc),
                  file=sys.stderr)
            return EX_DEVICE
        print("mic: %s  %.2fs @ %d Hz, %dch  peak %d  rms %.1f"
              % (out, m["seconds"], m["rate"], m["channels"], m["peak"], m["rms"]))
        if m["peak"] == 0:
            print("mic: EXACT ZEROS - that is the phantom signature, not a recording. "
                  "Check mic.conf's SOURCE against --list.", file=sys.stderr)
            return EX_DEVICE
        return EX_OK

    ap.print_help()
    return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
