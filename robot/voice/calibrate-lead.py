#!/usr/bin/env python3
"""calibrate-lead.py — re-tune the lip-sync offset, and prove it took.

WHY A TOOL RATHER THAN A PROCEDURE IN A COMMENT
  The offset in display.conf is a property of the DISPLAY: the projector's own
  audio buffering minus the face's own visual latency. Both halves can move (a
  sound setting on the projector, a busier board, a different face), and when they
  do the mouth is out of sync again with nothing in any log to say so. The value
  can only be found by eye, so the thing worth keeping is not the number, it is
  the INSTRUMENT that finds it: one genuinely comparable trial per round, a
  direction-only question, a bisect, and a written record of what was judged when.

WHAT IT DOES, IN ORDER
  1. Preflight. Refuses to run at all if the instrument is not the signal the
     number belongs to, if a session is running, if another player holds the
     mouth, or if the sink in display.conf is not on this board. Every one of
     those turns a measurement into a confident wrong answer.
  2. Brings the face up. The face is drawn only for a RUNNING session, and at
     standby the screen says "Standby" with no face at all, so a trial would have
     nothing to watch. This writes /run/face/calibration.json (the same
     publish-a-file mechanism the listening pill uses) and face.py draws the face
     while it stays fresh, with the conductor and session.json untouched.
  3. One trial per round: a 440 Hz cue beep, then tones/sync-calibration.wav - three
     180 ms bursts over pure digital silence - at the candidate offset. The mouth
     must pop exactly on each beep and do nothing in between.
  4. ONE question, four keys: e = the mouth pops BEFORE the beep, l = after,
     d = dead on, q = quit, r = play it again. Never a magnitude: a direction is a
     reliable judgement and a magnitude is not, so the bisect halves the step and
     the DIRECTION does the searching (0.20 -> 0.10 -> 0.05 -> 0.025 -> 0.0125).
  5. Writes display.conf, with a stamp naming the display it was tuned against and
     the bisect that produced it, and keeps the previous value visible.

  Human sync judgement bottoms out near 10-30 ms. When two adjacent values can no
  longer be told apart that pair IS the finish, and the tool says so rather than
  spending another round on a difference nobody can perceive.

NEVER ANNOUNCE A TRIAL ON THE CHANNEL UNDER TEST
  An earlier sweep labelled trial N by flashing the mouth N times. That is mouth
  movement with no sound, which is the very fault being tuned out: the observer
  reported the counter as the fault, and reported it getting worse every trial.
  The cue is therefore AUDIO ONLY, at 440 Hz - a different pitch from the 880 Hz
  bursts, so it cannot be mistaken for a test event - and the mouth is held shut
  until the clip starts.

usage:
    ./calibrate-lead.py                 # interactive bisect from the configured value
    ./calibrate-lead.py --trial 0.15    # one trial at a value; nothing is written
    ./calibrate-lead.py --show          # what is configured, and the stamp, no audio
    ./calibrate-lead.py --dry           # resolve everything and print it, no audio
    ./calibrate-lead.py --write 0.09    # write a value already judged, after confirming
    ./calibrate-lead.py --selftest      # exercise the bisect and the guards, no audio
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONF = HERE / "display.conf"
DEFAULT_CLIP = HERE / "tones/sync-calibration.wav"
DEFAULT_CUE = HERE / "tones/cue.wav"
DEFAULT_MOUTH = os.environ.get("FACE_MOUTH", "/run/face/mouth.json")
DEFAULT_SESSION = os.environ.get("FACE_SESSION", "/run/face/session.json")
DEFAULT_TUNING = os.environ.get("FACE_CALIBRATION", "/run/face/calibration.json")

# The instrument, PINNED BY HASH. The tuned number belongs to this exact signal:
# make-calibration.sh reproduces it byte for byte, and a 10 ms difference in an
# edge fade is invisible while silently invalidating every measurement made
# against it. Regenerate with ./make-calibration.sh and compare, never by hand.
PINNED = {
    "sync-calibration.wav":
        "01b9ee3c7e317a9c2ce9f663902ae883d562a1c6408b07dcac474099c2ce2536",
    "cue.wav":
        "0668e332a3e6fe1b29f90c2db585015f31e9eaa94a8332c92557f14acb5fd674",
}

# 1.0 + 3.0 + 5.0 s bursts in an 8.0 s clip: 6.5 s shows all three and stops.
TRIAL_SECONDS = 6.5
CUE_GAP = 0.4
HEARTBEAT = 0.25            # face.py treats the tuning file as stale after 1.5 s
# Stricter than face.py's own 1.5 s, deliberately: this is the check that the face is
# still up BEFORE a verdict is asked for, so it has to fail first, not after the face
# has already dropped back to the standby word.
FACE_STALE = 1.0
FIRST_STEP = 0.20
MIN_STEP = 0.0125
# The plausible band for (audio buffering - visual latency). Outside it, something
# other than the offset is wrong: refuse rather than write a number that hides it.
LEAD_LIMIT = 0.35
EX_TEMPFAIL = 75            # lipsync's "another player holds the mouth"
EX_CONFIG = 2


def say(msg="", *args):
    text = msg % args if args else msg
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def warn(msg, *args):
    sys.stderr.write("calibrate-lead: " + (msg % args if args else msg) + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Reading the world
# --------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_conf(path=DEFAULT_CONF):
    """display.conf is a shell fragment of KEY=VALUE lines. Read it, never source."""
    conf = {}
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip().strip('"').strip("'")
    return conf


def instrument_report(clip=DEFAULT_CLIP, cue=DEFAULT_CUE):
    """[(name, measured, pinned, ok)] for every pinned signal."""
    rows = []
    for path in (clip, cue):
        name = pathlib.Path(path).name
        pin = PINNED.get(name)
        try:
            got = sha256_file(path)
        except OSError as exc:
            rows.append((name, "unreadable: %s" % exc, pin, False))
            continue
        rows.append((name, got, pin, pin is not None and got == pin))
    return rows


def session_phase(path=DEFAULT_SESSION):
    """The session phase, or None when the file is absent (which means standby)."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    phase = data.get("phase")
    return str(phase) if phase is not None else None


def sink_names():
    """Every PulseAudio sink name this board currently exposes."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in out.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) > 1:
            names.append(bits[1])
    return names


def pcm_running():
    """True while any PLAYBACK pcm reads RUNNING.

    `pcm*p` only: a capture pcm on this board sits RUNNING permanently because the
    camera is capturing, and a wider glob would report "audio has started" the
    instant it is asked, every time.
    """
    import glob
    for p in glob.glob("/proc/asound/card*/pcm*p/sub0/status"):
        try:
            with open(p) as fh:
                if "RUNNING" in fh.read(256):
                    return True
        except OSError:
            pass
    return False


def wait_clean_baseline(limit=8.0):
    """Wait for the sink to stop reporting RUNNING from a previous playback.

    Measured on this board: after a player exits, the playback pcm keeps reading
    RUNNING until module-suspend-on-idle suspends the sink, about 5 s later.
    lipsync sets its clock on the FIRST RUNNING it sees, so a trial started inside
    that window gets a clock that started early - a mouth ahead of the sound by the
    real start cost, which is small (15-31 ms measured) but not nothing.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if not pcm_running():
            return True, time.monotonic() - t0
        time.sleep(0.1)
    return False, time.monotonic() - t0


def mouth_lock_holder(mouth=DEFAULT_MOUTH):
    """The pid in the mouth lock file, if it is a live process."""
    lock = pathlib.Path(str(mouth) + ".lock")
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if os.path.exists("/proc/%d" % pid) else None


def display_identity():
    """'<monitor name> on <connector>, <first mode>', best effort.

    Recorded in the stamp because the offset is a display property: the number is
    only meaningful next to the rig it was measured on.
    """
    import glob
    for status in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        try:
            if "connected" not in pathlib.Path(status).read_text():
                continue
        except OSError:
            continue
        base = pathlib.Path(status).parent
        name, mode = "unknown monitor", "unknown mode"
        try:
            edid = (base / "edid").read_bytes()
            # The monitor-name descriptor is 00 00 00 FC, then a flag byte, then 13
            # bytes of text - and it can sit in ANY of the four 18-byte blocks from
            # 0x36, which is why all four are scanned.
            #
            # The pattern is FOUR bytes ending in FC, not three: the tag is the
            # fourth byte of the block, with the first three zero. Get this wrong in
            # the other direction (00 00 FC 00) and it still matches, one byte late,
            # which decodes the name shifted by one and reports nothing useful.
            # Measured on this board: the block is at 0x5A and the text at 0x5F.
            for i in range(0x36, 0x36 + 4 * 18, 18):
                if edid[i:i + 4] == b"\x00\x00\x00\xfc":
                    name = edid[i + 5:i + 18].decode("ascii", "replace").strip() or name
        except (OSError, IndexError):
            pass
        try:
            first = (base / "modes").read_text().split("\n")[0].strip()
            if first:
                mode = first
        except OSError:
            pass
        return "%s on %s, %s" % (name, base.name, mode)
    return "no connected display"


# --------------------------------------------------------------------------
# The tuning session's file (what brings the face up)
# --------------------------------------------------------------------------
def write_tuning(path, face=True, stats=True):
    """One atomic write of the tuning file, stamped on CLOCK_MONOTONIC.

    Monotonic on purpose: face.py compares it against its own monotonic clock, and
    a stamp from a different clock would read as either ancient or from the future
    and be refused.
    """
    payload = {"heartbeat": time.monotonic(), "face": bool(face),
               "stats": bool(stats)}
    directory = os.path.dirname(str(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".calibration-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def heartbeat_forever(path):
    """Re-stamp the tuning file until killed. Runs as its own process.

    Its own process because a trial blocks for its whole length, and the face
    releases a stamp that stops moving: an in-process heartbeat would let the face
    drop back to "Standby" in the middle of the very trial being judged.
    """
    while True:
        try:
            write_tuning(path)
        except OSError as exc:
            warn("cannot write %s: %s", path, exc)
            return 1
        time.sleep(HEARTBEAT)


class TuningSession:
    """Brings the face up for the duration, and takes it down afterwards."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.child = None

    def __enter__(self):
        write_tuning(self.path)
        self.child = subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).resolve()), "--heartbeat",
             str(self.path)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return self

    def __exit__(self, *exc):
        if self.child is not None:
            self.child.terminate()
            try:
                self.child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.child.kill()
        # Remove it, so the face goes back to "Standby" the moment we are done. The
        # heartbeat expiring is the fallback for a tool that is killed outright.
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------
# One trial
# --------------------------------------------------------------------------
def play_cue(cue, sink):
    """Audio-only warning beep. Never mirrored by the mouth (see the docstring)."""
    if not cue.is_file():
        return
    cmd = ["paplay"]
    if sink:
        cmd.append("--device=%s" % sink)
    cmd.append(str(cue))
    subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_trial(clip, sink, lead, mouth, seconds=TRIAL_SECONDS, cue=None):
    """Play one trial and wait for it. Returns the player's exit code."""
    if cue is not None:
        play_cue(cue, sink)
        time.sleep(CUE_GAP)
    cmd = [sys.executable, str(HERE / "lipsync.py"), str(clip),
           "--sink", sink, "--lead", "%g" % lead, "--mouth", str(mouth),
           "--duration", "%g" % seconds]
    return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TrialFailed(Exception):
    """A trial did not actually happen, so there is nothing to judge."""


def tuning_is_fresh(path, now=None, stale=FACE_STALE):
    """Is the face still up? Re-read the tool's own tuning file.

    Re-read before EVERY question rather than trusted from the start, because the
    first attempt at a trial on this rig judged nothing at all: the screen was the
    word "Standby" and there was no face to watch. A heartbeat child that dies
    mid-bisect puts the screen back to standby silently, and every answer after that
    would be about an empty screen.
    """
    now = time.monotonic() if now is None else now
    try:
        with open(path) as fh:
            stamp = float(json.load(fh).get("heartbeat"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False, "the tuning file is unreadable"
    age = now - stamp
    if age > stale:
        return False, "the tuning file last stamped %.1fs ago" % age
    if age < -1.0:
        return False, "the tuning file is stamped in the future"
    return True, ""


def must_play(clip, sink, lead, mouth, seconds=TRIAL_SECONDS, cue=None,
              tuning_file=DEFAULT_TUNING):
    """Play one trial, and REFUSE to ask about it if it did not play.

    A failed trial is a silent one: another player holds the mouth, the sink has gone,
    the clip will not open. Asking "early or late?" about silence would record a
    direction for an event that never happened, and the answer would be
    indistinguishable from a real one in the bisect history and in the stamp written
    into display.conf afterwards.
    """
    holder = mouth_lock_holder(mouth)
    if holder:
        raise TrialFailed("the mouth is held by pid %d, so this trial would be doubled "
                          "or refused" % holder)
    fresh, why = tuning_is_fresh(tuning_file)
    if not fresh:
        raise TrialFailed("%s, so the face is not up and there would be nothing on "
                          "screen to judge" % why)
    rc = run_trial(clip, sink, lead, mouth, seconds, cue=cue)
    if rc != 0:
        raise TrialFailed("the player exited %d: no audio was played, so there is "
                          "nothing to judge" % rc)


def ask_verdict(value):
    """One question, and the answer is a DIRECTION. Never a magnitude."""
    say()
    say("  --lead %g is playing. Watch the projected face:" % value)
    say("     the mouth must pop exactly on each beep, and do nothing between")
    say("        e = the mouth pops BEFORE the beep (early)")
    say("        l = the mouth pops AFTER the beep (late)")
    say("        d = dead on")
    say("        r = play it again      q = quit, change nothing")
    while True:
        try:
            answer = input("  verdict [e/l/d/r/q]: ").strip().lower()[:1]
        except EOFError:
            return "q"
        if answer in ("e", "l", "d", "r", "q"):
            return answer
        say("  %r is not one of e, l, d, r, q" % answer)


# --------------------------------------------------------------------------
# The bisect, as a pure function
# --------------------------------------------------------------------------
def next_lead(value, verdict, step):
    """The next candidate, for one judgement.

    DIRECTION IS NOT ARBITRARY. A larger --lead makes lipsync wait longer before
    performing, so the mouth moves LATER; a mouth seen ahead of the sound therefore
    wants a LARGER value, and one seen behind it a smaller one.
    """
    if verdict == "e":
        return value + step
    if verdict == "l":
        return value - step
    return value


def bisect(start, judge, span=FIRST_STEP, min_step=MIN_STEP, limit=LEAD_LIMIT):
    """Bracket the true offset, then bisect inside the bracket.

    A fixed step-halving sweep is NOT enough, and the failure is silent: one
    overshoot on the first move leaves the search walking back in with steps too
    small to cross the truth again, so it can finish 50 ms away while reporting a
    12 ms step. Measured on a synthetic observer before this was rewritten. So:

      1. BRACKET: move outward in doubling steps until the direction flips, which
         yields two values with OPPOSITE verdicts and the truth between them. This
         is what makes the tool independent of how far the offset has drifted: the
         configured value is a starting point, not an assumption.
      2. BISECT: test the midpoint, move the endpoint on the same side, stop when
         the bracket is at the ear's floor. That is log2(range/step) listens, four
         or five in practice.

    `judge(value)` returns e / l / d / r / q. Returns (chosen, history, note), with
    chosen None when the observer quit or no bracket was found.
    """
    history = []

    def ask(value):
        while True:
            verdict = judge(value)
            if verdict == "r":
                continue
            history.append((round(value, 4), verdict))
            return verdict

    first = ask(start)
    if first == "q":
        return None, history, "abandoned"
    if first == "d":
        return round(start, 4), history, "dead on"

    # The direction to travel: a mouth seen EARLY wants a larger value, one seen
    # LATE a smaller one (see next_lead).
    up = first == "e"
    lo, hi = (start, None) if up else (None, start)
    step = span / 2.0
    edge = start
    for _ in range(8):
        probe = round(min(limit, max(-limit, edge + (step if up else -step))), 5)
        if (up and probe <= edge) or (not up and probe >= edge):
            break                       # clamped at the band edge: nothing to cross
        verdict = ask(probe)
        if verdict == "q":
            return None, history, "abandoned"
        if verdict == "d":
            return probe, history, "dead on"
        if verdict != first:            # crossed the truth
            if up:
                hi = probe
            else:
                lo = probe
            break
        edge, step = probe, step * 2.0
    if lo is None or hi is None:
        return None, history, (
            "no crossing found within +/-%g, so the offset is outside the plausible "
            "band and writing a number would hide whatever else is wrong" % limit)

    while hi - lo > min_step:
        mid = round((lo + hi) / 2.0, 5)
        if mid in (lo, hi):
            break
        verdict = ask(mid)
        if verdict == "q":
            return None, history, "abandoned"
        if verdict == "d":
            return mid, history, "dead on"
        if verdict == "e":
            lo = mid
        else:
            hi = mid
    chosen = round((lo + hi) / 2.0, 4)
    note = ("bracketed to %g between %g and %g, at the ear's floor: the residual is "
            "inside what anyone can split" % (round(hi - lo, 4), lo, hi))
    return chosen, history, note


# --------------------------------------------------------------------------
# Writing the value back
# --------------------------------------------------------------------------
STAMP_HEAD = "# TUNED:"


def stamp_lines(identity, previous, value, history, note, clip_sha, when=None):
    """The comment block that records the judgement, replacing the old one."""
    when = when or datetime.datetime.now().strftime("%d %b %Y %H:%M")
    path = " -> ".join("%g %s" % (v, "dead on" if a == "d" else
                                  ("early" if a == "e" else "late"))
                       for v, a in history)
    out = [STAMP_HEAD,
           "#   %s by calibrate-lead.py, judged by eye on the projected mouth." % when,
           "#   display judged on: %s" % identity,
           "#   instrument: tones/sync-calibration.wav sha256 %s" % clip_sha[:16]]
    if history:
        out.append("#   bisect (value and the direction reported): %s" % path)
    if note:
        out.append("#   note: %s" % note)
    out.append("#   previous LEAD: %s" % previous)
    return out


def rewrite_conf(text, value, stamp):
    """The whole conf with LEAD replaced and the stamp block swapped in.

    Only the LEAD line and the stamp are touched. Everything else - the sink, and
    every note about why the offset is a display property - is preserved verbatim,
    because that file IS the documentation of this number.
    """
    lines = text.split("\n")
    # An existing stamp runs from its head to the end of the comment block or the
    # next real setting.
    head = next((i for i, ln in enumerate(lines)
                 if ln.startswith(STAMP_HEAD)), None)
    if head is not None:
        end = head
        while end < len(lines) and (lines[end].startswith("#") or not lines[end].strip()):
            end += 1
        lines[head:end] = stamp
    else:
        stamp_at = next((i for i, ln in enumerate(lines)
                         if ln.startswith("LEAD=")), len(lines))
        lines[stamp_at:stamp_at] = stamp
    for i, ln in enumerate(lines):
        if ln.startswith("LEAD="):
            lines[i] = "LEAD=%g" % value
            break
    else:
        lines.append("LEAD=%g" % value)
    # Exactly one trailing newline. Joining a split that ended in "\n" leaves an
    # empty last element, so this is normalised rather than patched up: a conf file
    # with an extra blank line at the end is noise in every future diff.
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def write_conf(path, value, stamp):
    """Atomic replace, next to the original, so a reader never sees half a file."""
    path = pathlib.Path(path)
    text = path.read_text()
    new = rewrite_conf(text, value, stamp)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".display-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def preflight(conf_path, clip, cue, sink, lead, mouth, session, check_sink=True):
    """Every reason to refuse, as a list of strings. Empty means go."""
    problems = []
    for name, got, pin, ok in instrument_report(clip, cue):
        if not ok:
            problems.append("instrument %s is not the pinned signal\n"
                            "    measured %s\n    expected %s"
                            % (name, got, pin))
    phase = session_phase(session)
    if phase is not None and phase != "standby":
        problems.append("a session is %s: the run owns the screen and the mouth, "
                        "so send `system command standby` first" % phase)
    if not os.path.isdir(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")):
        problems.append("XDG_RUNTIME_DIR is missing, so no player can reach the "
                        "sound server")
    if sink is None:
        problems.append("display.conf names no SINK")
    elif check_sink and sink not in sink_names():
        problems.append("display.conf SINK %r is not a sink on this board "
                        "(see: pactl list short sinks)" % sink)
    if lead is None:
        problems.append("display.conf has no readable LEAD")
    elif abs(lead) > LEAD_LIMIT:
        problems.append("LEAD %g is outside the plausible band (|lead| <= %g): "
                        "something other than the offset is wrong" % (lead, LEAD_LIMIT))
    holder = mouth_lock_holder(mouth)
    if holder:
        problems.append("pid %d holds the mouth (two players would double the "
                        "audio and fight over the mouth)" % holder)
    return problems


def show(conf_path, clip, cue, sink, lead, mouth, session):
    say("display.conf      : %s" % conf_path)
    say("sink              : %s" % sink)
    say("lead (configured) : %s" % ("%g" % lead if lead is not None else "<unreadable>"))
    say("display           : %s" % display_identity())
    say("session phase     : %s" % (session_phase(session) or "standby (no file)"))
    holder = mouth_lock_holder(mouth)
    say("mouth lock        : %s" % ("held by pid %d" % holder if holder else "free"))
    say("playback pcm now  : %s" % ("RUNNING" if pcm_running() else "not running"))
    for name, got, pin, ok in instrument_report(clip, cue):
        say("instrument %-22s %s" % (name, "OK" if ok else "MISMATCH\n    %s" % got))
    say()
    say("stamp in the conf:")
    try:
        text = pathlib.Path(conf_path).read_text().split("\n")
    except OSError as exc:
        say("  unreadable: %s" % exc)
        return
    for i, ln in enumerate(text):
        if ln.startswith(STAMP_HEAD):
            for follow in text[i:]:
                if not follow.startswith("#") and follow.strip():
                    break
                say("  " + follow)
            break
    else:
        say("  (none)")


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------
def selftest():
    checks, failures = [0], []

    def check(name, got, want):
        checks[0] += 1
        if got == want:
            say("  ok    %s" % name)
        else:
            say("  FAIL  %s\n          got  %r\n          want %r" % (name, got, want))
            failures.append(name)

    # The bisect converges on a synthetic observer with a known true offset, and
    # the direction convention is asserted rather than assumed: a mouth seen EARLY
    # must push the value UP.
    def observer(true_value):
        def judge(value):
            if abs(value - true_value) < MIN_STEP:
                return "d"
            return "e" if value < true_value else "l"
        return judge

    for truth in (0.00, 0.11, 0.17, 0.25):
        chosen, history, note = bisect(0.11, observer(truth))
        check("bisect converges on %g (chosen %s)" % (truth, chosen),
              chosen is not None and abs(chosen - truth) <= MIN_STEP, True)
        check("bisect on %g took a handful of rounds, not a sweep"
              % truth, 1 <= len(history) <= 6, True)

    # An observer who can never say "dead on" is the realistic worst case: the
    # bracket still closes at the ear's floor, and the answer is inside it.
    def indecisive(value):
        return "e" if value < 0.15 else "l"

    chosen, history, note = bisect(0.11, indecisive)
    check("an observer who never says 'dead on' still yields a value",
          chosen is not None and abs(chosen - 0.15) <= MIN_STEP, True)
    check("and the note admits it is a floor rather than a pinpoint",
          "floor" in (note or ""), True)

    # An offset outside the plausible band must NOT produce a number: the whole
    # point of the band is that something else is wrong when it is exceeded.
    chosen, history, note = bisect(0.05, lambda v: "e")
    check("an offset beyond the band yields no value", chosen, None)
    check("and says so", "plausible band" in (note or ""), True)

    # Direction: early means a LARGER lead, late a smaller one.
    check("early pushes the offset up", next_lead(0.11, "e", 0.05), 0.16)
    check("late pushes the offset down", next_lead(0.11, "l", 0.05), 0.06)

    # The conf rewrite: LEAD replaced, stamp swapped, everything else untouched.
    sample = ("# a comment\nSINK=alsa_x\n# HOW TO RE-TUNE\n#   do it by ear\n"
              "# TUNED: old display\n#   old note\nLEAD=0.11\n")
    stamp = stamp_lines("HiTV on card0-DP-1, 1920x1080", "0.11", 0.16,
                        [(0.11, "e"), (0.16, "d")], "", "ab" * 32, when="now")
    out = rewrite_conf(sample, 0.16, stamp)
    check("the sink survives a write", "SINK=alsa_x" in out, True)
    check("the old stamp is replaced, not duplicated", out.count(STAMP_HEAD), 1)
    check("the new value is written", "LEAD=0.16" in out, True)
    check("the old value is kept in the stamp", "previous LEAD: 0.11" in out, True)
    check("the plain comments survive",
          "# HOW TO RE-TUNE" in out and "# a comment" in out, True)
    check("no stray blank lines pile up at the end",
          out.endswith("LEAD=0.16\n"), True)

    # Guards: a value outside the plausible band, and a live session.
    check("an out-of-band lead is refused",
          bool(preflight(DEFAULT_CONF, DEFAULT_CLIP, DEFAULT_CUE, "s", 0.9,
                         DEFAULT_MOUTH, DEFAULT_SESSION, check_sink=False)), True)
    check("a perfectly ordinary lead passes",
          preflight(DEFAULT_CONF, DEFAULT_CLIP, DEFAULT_CUE, "s", 0.11,
                    DEFAULT_MOUTH, "/nonexistent/session.json",
                    check_sink=False), [])

    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        sess = os.path.join(d, "session.json")
        with open(sess, "w") as fh:
            json.dump({"phase": "running"}, fh)
        check("a running session is refused",
              any("session is running" in p for p in
                  preflight(DEFAULT_CONF, DEFAULT_CLIP, DEFAULT_CUE, "s", 0.11,
                            DEFAULT_MOUTH, sess, check_sink=False)), True)

        # The tuning file: writable, and a reader sees valid JSON always.
        tun = os.path.join(d, "calibration.json")
        write_tuning(tun)
        with open(tun) as fh:
            data = json.load(fh)
        check("the tuning file is valid JSON with a heartbeat",
              (data.get("face"), isinstance(data.get("heartbeat"), float)),
              (True, True))

        # A verdict must never be asked for against a screen that is not there. The
        # first attempt at a trial on this rig judged the standby word and recorded a
        # direction from it, so this guard is the fix for a real void measurement.
        fresh, _ = tuning_is_fresh(tun)
        check("a just-stamped tuning file reads as face-up", fresh, True)
        stale, why = tuning_is_fresh(
            tun, now=time.monotonic() + FACE_STALE + 1.0)
        check("a heartbeat that stopped reads as face-down", stale, False)
        check("and says why, with the age", "last stamped" in why, True)
        missing, _ = tuning_is_fresh(os.path.join(d, "not-there.json"))
        check("a missing tuning file reads as face-down", missing, False)

    say()
    if failures:
        say("  SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    say("  SELFTEST PASSED (%d cases)" % checks[0])
    return 0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Re-tune the lip-sync offset against the tone instrument.")
    ap.add_argument("--conf", default=str(DEFAULT_CONF))
    ap.add_argument("--clip", type=pathlib.Path, default=DEFAULT_CLIP)
    ap.add_argument("--cue", type=pathlib.Path, default=DEFAULT_CUE)
    ap.add_argument("--mouth", default=DEFAULT_MOUTH)
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--tuning-file", default=DEFAULT_TUNING)
    ap.add_argument("--trial", type=float, metavar="LEAD",
                    help="play ONE trial at LEAD, changing nothing. The fastest "
                         "way to see whether a configured value is right")
    ap.add_argument("--write", type=float, metavar="LEAD",
                    help="write LEAD into display.conf, after confirming")
    ap.add_argument("--show", action="store_true",
                    help="print the configured value, the rig and the stamp, no audio")
    ap.add_argument("--dry", action="store_true",
                    help="run the preflight and print the resolved settings, no audio")
    ap.add_argument("--seconds", type=float, default=TRIAL_SECONDS,
                    help="clip seconds per trial (default %(default)s)")
    ap.add_argument("--selftest", action="store_true",
                    help="exercise the bisect and the guards with no audio")
    ap.add_argument("--heartbeat", metavar="PATH",
                    help="internal: re-stamp the tuning file until killed")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompts (for a scripted write)")
    args = ap.parse_args()

    if args.heartbeat:
        return heartbeat_forever(args.heartbeat)
    if args.selftest:
        return selftest()

    conf_path = pathlib.Path(args.conf)
    conf = read_conf(conf_path)
    sink = conf.get("SINK")
    try:
        lead = float(conf["LEAD"])
    except (KeyError, ValueError):
        lead = None

    if args.show:
        show(conf_path, args.clip, args.cue, sink, lead, args.mouth, args.session)
        return 0

    problems = preflight(conf_path, args.clip, args.cue, sink, lead, args.mouth,
                         args.session)
    if problems:
        say("REFUSING to measure or write:")
        for p in problems:
            say("  - %s" % p)
        return EX_CONFIG

    say("sink   : %s" % sink)
    say("lead   : %g (configured)" % lead)
    say("display: %s" % display_identity())

    if args.dry:
        say("dry: everything resolved; no audio played, nothing written")
        return 0

    if args.write is not None:
        if abs(args.write) > LEAD_LIMIT:
            say("REFUSING: %g is outside the plausible band (|lead| <= %g)"
                % (args.write, LEAD_LIMIT))
            return EX_CONFIG
        if not args.yes:
            try:
                reply = input("write LEAD=%g into %s ? [y/N]: "
                              % (args.write, conf_path)).strip().lower()
            except EOFError:
                reply = ""
            if reply not in ("y", "yes"):
                say("nothing written")
                return 0
        stamp = stamp_lines(display_identity(), "%g" % lead, args.write, [],
                            "written directly, without a bisect", 
                            PINNED["sync-calibration.wav"])
        write_conf(conf_path, args.write, stamp)
        say("wrote LEAD=%g to %s" % (args.write, conf_path))
        say("no restart is needed: the greeter and the conductor re-read the conf "
            "for every playback")
        return 0

    try:
        with TuningSession(args.tuning_file):
            say("tuning session: the face is up (the standby screen is replaced "
                "while this runs)")
            say("waiting for the playback pcm to go quiet first, so each trial's "
                "clock starts from the real start")
            clean, waited = wait_clean_baseline()
            if not clean:
                warn("a playback pcm still reads RUNNING after %.0fs: the mouth may "
                     "start up to ~30 ms early on this trial", waited)
            if args.trial is not None:
                if abs(args.trial) > LEAD_LIMIT:
                    say("REFUSING: %g is outside the plausible band" % args.trial)
                    return EX_CONFIG
                must_play(args.clip, sink, args.trial, args.mouth, args.seconds,
                          args.cue, args.tuning_file)
                say("one trial at --lead %g done; nothing was written" % args.trial)
                return 0

            chosen, history, note = bisect(
                lead, lambda v: (must_play(args.clip, sink, v, args.mouth,
                                           args.seconds, args.cue,
                                           args.tuning_file),
                                 ask_verdict(v))[1])
    except KeyboardInterrupt:
        say("\ninterrupted: nothing written")
        return 130
    except TrialFailed as exc:
        say()
        say("TRIAL FAILED: %s." % exc)
        say("no verdict was taken and nothing was written: LEAD=%g stays." % lead)
        return EX_TEMPFAIL

    say()
    if chosen is None:
        say("abandoned (%s). display.conf is untouched: LEAD=%g stays." % (note, lead))
        return 0

    say("judged: %s" % ", ".join("%g %s" % (v, a) for v, a in history))
    if note:
        say("note: %s" % note)
    say("chosen LEAD=%g, configured LEAD=%g" % (chosen, lead))
    if abs(chosen) > LEAD_LIMIT:
        say("REFUSING to write %g: outside the plausible band" % chosen)
        return EX_CONFIG
    if not args.yes:
        try:
            reply = input("write LEAD=%g into %s ? [y/N]: "
                          % (chosen, conf_path)).strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            say("nothing written. To apply it later: %s --write %g"
                % (pathlib.Path(__file__).name, chosen))
            return 0

    stamp = stamp_lines(display_identity(), "%g" % lead, chosen, history, note,
                        PINNED["sync-calibration.wav"])
    write_conf(conf_path, chosen, stamp)
    say("wrote LEAD=%g to %s" % (chosen, conf_path))
    say("no restart is needed: the greeter and the conductor re-read the conf for "
        "every playback. Verify with: git diff %s" % conf_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `| head` closing the pipe is not a fault. Redirect stdout to devnull so the
        # interpreter's own exit-time flush cannot raise a second time, and report
        # success: the tool did what it was asked.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
