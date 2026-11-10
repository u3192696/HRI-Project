#!/usr/bin/env python3
"""calibrate-look.py — re-fit what THIS camera calls "looking at the screen".

THE PROBLEM IT SOLVES
  `looking` - the verdict the face's expression and the session's gaze bar both run
  on - is judged from ONE number: the magnitude of the smoothed combined gaze,
  compared against a pair of thresholds. That number adds WHERE the head sits in the
  frame to WHICH WAY it is turned, so the value produced by "a person facing the
  screen" belongs to the whole arrangement: the camera's position, its aim, the
  screen's position, the seat.

  On the robot in its case, 0.35/0.80 is right. Move the same camera onto the
  testing bench and the arrangement changes: someone looking at the screen can now
  sit off-centre in the frame and/or turn their head further to face it, so the same
  intent reads as a bigger number, and the robot calls a person looking straight at
  it "not looking". Widening the window until it stops complaining would hide that;
  measuring the two states on this rig and fitting the thresholds to the gap does
  not.

WHAT IT DOES, IN ORDER
  1. Preflight. Reads the status file the running gaze.service publishes and refuses
     if it is absent or stale, because a dead publisher and a quiet room look
     identical from here. It NEVER takes the camera: the OAK-D is exclusive, and
     stopping gaze in order to calibrate gaze's own verdict is the one thing that
     must not happen.
  2. Phase A: hold the pose "looking at the screen" for --phase seconds.
  3. Phase B: same seat, look away, for --phase seconds.
  4. Fit. The edges that matter are the WORST sample of phase A (its p95 - looking is
     a range of poses, and the whole range has to be admitted) and the BEST sample of
     phase B (its p5). The margin is the gap between those two, not the distance
     between the clusters' middles, which overstates it by however much the tails
     wander. If the two states overlap, nothing is written and the report says so,
     with the components split out so framing and pose can be told apart.
  5. Confirms, then writes look.conf and the per-rig archive looks/<rig>.conf.
     gaze.py re-reads look.conf whenever it changes, so a re-fit is live with NO
     restart - "wrote it but it did not take" is not possible.

  Samples where the camera could not see anyone are EXCLUDED rather than counted as
  "not looking": a lost face leaves the last gaze or a recentred 0.0 on disk, and
  0.0 is the most looking-like number there is. That exclusion is why a phase can
  report fewer kept frames than it polled.

HOW YOU KNOW WHICH POSE TO HOLD
  This terminal is BEHIND the person being measured, so its prompts are not the cue
  that works. Each window is announced three ways:

    on the screen  face-kiosk.service draws an instruction panel from
                   /run/face/coach.json: the pose, which phase it is, and a countdown
                   drained from the same deadline this tool measures against
    as a tone      a rising pair when a window OPENS, a falling pair when it SHUTS.
                   This is the one that reaches phase B, where the person is looking
                   away from the screen because that is what phase B asks for
    here           the same text, for whoever is watching the run

  Every window is preceded by --lead seconds of "get ready", so the pose is HELD when
  the measurement starts rather than moved into, and phase B is preceded by --between
  seconds in which its instruction is on screen and readable.

  The panel is written with a heartbeat and removed on every exit path - a refusal, a
  Ctrl-C, an exception - because a killed tool must not leave an instruction on a
  screen. If it is killed outright, face.py expires the panel 1.5 s later: a stamp
  that has stopped moving is not evidence that the tool is alive.

  Both cues can be switched off (--no-coach, --no-tones), and preflight says what will
  actually reach you: whether the kiosk is running at all, and whether the sink
  display.conf names is present on this board. A bench rig with no display and no
  audio is a legitimate arrangement, and then this terminal is the only cue - which is
  why the phase B instruction also tells you what to do BEFORE you turn away.

WHAT IT CANNOT DO
  Fit a threshold when the measure does not separate the two states on this rig. That
  is a real answer rather than a failure, and it points at the next decision.

usage:
    ./calibrate-look.py                    # full run: hold, hold, confirm, write
    ./calibrate-look.py --show             # the live reading, the active conf, the profiles
    ./calibrate-look.py --watch            # live one line, no capture (Ctrl-C to stop)
    ./calibrate-look.py --rig bench        # label the fit; archives looks/bench.conf
    ./calibrate-look.py --use bench        # make looks/bench.conf the active numbers
    ./calibrate-look.py --phase 12         # seconds per phase (default 8)
    ./calibrate-look.py --lead 5           # seconds of "get ready" before each window
    ./calibrate-look.py --between 6        # seconds between the windows
    ./calibrate-look.py --no-tones         # screen cue only, no boundary tones
    ./calibrate-look.py --no-coach         # this terminal is the only cue
    ./calibrate-look.py --dry-run          # measure and report, write nothing
    ./calibrate-look.py --write 0.40,0.55  # write values you judged yourself
    ./calibrate-look.py --yes              # no prompt (batch / not a terminal)
    ./calibrate-look.py --selftest         # the fit, the guards and the cues; no camera

  exit 0 wrote nothing and needed to write nothing / wrote the fit
  exit 2 nothing written: a preflight refusal, or the two states do not separate
  exit 3 nothing written: declined at the prompt
"""

from __future__ import annotations

import argparse
import array
import datetime
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import wave

# Same directory, and sys.path[0] is the script's own directory when it is run
# directly - the same mechanism that makes a stdlib-named module here a real bug
# (see README). lookconf is shared with gaze.py on purpose: one reader, so the
# numbers the tool writes and the numbers the daemon acts on cannot drift.
import lookconf

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_STATUS = os.environ.get("FACE_GAZE_STATUS", "/run/face/gaze-status.json")
DEFAULT_SESSION = os.environ.get("FACE_SESSION", "/run/face/session.json")

PHASE_SECONDS = 8.0         # per phase; ~50-60 samples at gaze's measured 6.7 fps
POLL = 0.05                 # how often the status file is re-read
STALE = 2.0                 # a status file older than this is a dead service
MIN_SAMPLES = 20            # below this, a percentile is a guess with a number on it
TAIL_LOOKING = 95.0         # the WORST sample of phase A we still call looking
TAIL_AWAY = 5.0             # the BEST sample of phase B we still call not-looking
SAFETY = 0.15               # how far into the gap each threshold sits
GAP_MIN = 0.05              # narrower than this and the "gap" is the tails breathing
RATIO_MIN = 1.15            # the tail margin must beat this ratio (1.00 = touching)
CEIL_LOOKING = 0.70         # above this, "looking" admits almost any pose: warn
SPREAD_WARN = 0.15          # phase A's max this far above its p95 means it was not one pose

EX_OK, EX_NOTHING, EX_DECLINED = 0, 2, 3


def say(msg="", *args):
    sys.stdout.write((msg % args if args else msg) + "\n")
    sys.stdout.flush()


def warn(msg, *args):
    sys.stderr.write("calibrate-look: " + (msg % args if args else msg) + "\n")
    sys.stderr.flush()


def is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# Reading the world
# --------------------------------------------------------------------------
def read_json(path):
    """Return (data, reason). Tolerant, because the writer is not atomic.

    gaze.py's emit_status does a plain open()+json.dump(), so a reader really can
    catch it mid-write. A parse error here is a shrug - the sample is counted and
    skipped, never allowed to become a 0.0 that looks like a perfect "looking".
    """
    try:
        with open(path, "r") as fh:
            return json.load(fh), ""
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"unreadable ({exc.__class__.__name__})"
    except ValueError:
        return None, "half-written"


def sample_mag(sample) -> float:
    """The magnitude the policy compares: max(|gaze.x|, |gaze.y|).

    Taken from the file's own `gaze_mag` when it is there, so the number measured
    here is the number the daemon published rather than a second opinion about it.
    """
    mag = sample.get("gaze_mag")
    if isinstance(mag, (int, float)):
        return float(mag)
    gaze = sample.get("gaze") or {}
    return max(abs(float(gaze.get("x", 0.0))), abs(float(gaze.get("y", 0.0))))


def pct(values, p: float):
    """Linear-interpolated percentile. No numpy anywhere near this project's tools."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class Capture:
    """One measured phase: what it kept, and what it had to throw away."""

    def __init__(self, label: str, prompt: str, seconds: float):
        self.label = label
        self.prompt = prompt
        self.seconds = seconds
        self.frames: list = []      # only frames where the subject gate said yes
        self.no_face = 0            # nobody detected at all
        self.gate_rejected = 0      # a face, but not someone in front of the robot
        self.unreadable = 0         # absent, half-written, or an OSError
        self.elapsed = 0.0
        self.stale = 0

    @property
    def n(self) -> int:
        return len(self.frames)

    def mags(self) -> list:
        return [sample_mag(f) for f in self.frames]

    def field(self, getter, default=None):
        out = []
        for f in self.frames:
            v = getter(f)
            if isinstance(v, (int, float)):
                out.append(float(v))
        return out or ([default] if default is not None else [])


def classify(cap: Capture, data) -> str:
    """Bucket one published frame, and say which bucket.

    Factored out of the polling loop so the exclusion rule - the one rule in this
    tool that can silently ruin a fit - is testable with synthetic frames and no
    camera. A frame the camera could not see someone in is NOT a "not looking"
    sample: a lost face leaves a stale or recentred 0.0 on disk, and 0.0 is the most
    looking-like number there is. It gets its own count instead.
    """
    if not data.get("has_face"):
        cap.no_face += 1
        return "no_face"
    if not data.get("subject"):
        cap.gate_rejected += 1
        return "gate_rejected"
    cap.frames.append(data)
    return "kept"


def capture(status_path, cap: Capture, active=None, verbose=True, coach=None,
            deadline=None) -> Capture:
    """Poll the status file for `cap.seconds` and keep one sample per published frame.

    Deduplicated on the file's own `t`, so a slower publish rate than the poll rate
    cannot weight the distribution by however fast this loop happens to run.

    `deadline` (a CLOCK_MONOTONIC instant) supersedes `cap.seconds`, and is what the
    caller passes when the on-screen panel is counting down the same window: the time
    the person was told they have and the time being measured are then the same number
    by construction, instead of two clocks that agree until one of them drifts.
    """
    seen = set()
    t0 = time.monotonic()
    end = t0 + cap.seconds if deadline is None else deadline
    data = None
    while True:
        now = time.monotonic()
        if now >= end:
            break
        data, reason = read_json(status_path)
        if data is None:
            cap.unreadable += 1
        elif data.get("t") not in seen:
            seen.add(data.get("t"))
            classify(cap, data)
        cap.elapsed = now - t0
        if coach is not None:
            # Keep the panel alive and its clock truthful for as long as this runs.
            coach.tick(now)
        if verbose and is_tty():
            live(cap, data, active)
        time.sleep(POLL)
    if verbose and is_tty():
        sys.stdout.write("\r" + " " * 100 + "\r")
        sys.stdout.flush()
    return cap


def live(cap: Capture, data, active) -> None:
    """The one line that makes the run watchable, and the check that matters most.

    Until this says "subject", the camera has not accepted the person in front of it
    and nothing captured afterwards can be about their looking - which is
    indistinguishable from a threshold that is wrong unless it is on screen.
    """
    if data is None:
        state = "no status file"
    elif not data.get("has_face"):
        state = "nobody detected"
    elif not data.get("subject"):
        box = (data.get("face") or {}).get("box_w")
        state = f"face seen, gate rejected it (box_w {box})"
    else:
        state = f"subject   mag {sample_mag(data):.2f}"
        if active:
            ent, _exit = active
            state += "   active would say %s" % (
                "looking" if sample_mag(data) <= ent else "NOT looking")
    sys.stdout.write("\r  %4.1f/%ds  kept %3d   %-52s" %
                     (cap.elapsed, int(cap.seconds), cap.n, state))
    sys.stdout.flush()


# --------------------------------------------------------------------------
# The coach: telling the person what to do, where they can SEE it
# --------------------------------------------------------------------------
# THE PROBLEM. This tool asks for two poses in turn, and the person holding them is
# sitting in front of the camera with their back to this terminal. A prompt printed
# here is a prompt behind their head, and a window that opens silently is worse than
# no window at all: a pose held against the wrong instruction is a measurement of the
# wrong thing, and the two are indistinguishable in the numbers afterwards.
#
# So the instruction is published to /run/face/coach.json, which face-kiosk.service
# draws as a panel on the screen the pose is ABOUT, and each window boundary is also
# marked with a short tone - because phase B asks the person to be looking AWAY, when
# the screen carries nothing readable for them. The screen cue is the one that must
# not fail (a bench rig may have no audio path at all, and this board's default sink
# is the silent analog trap); the tone is the one that reaches a turned head, so both
# are on by default and each can be switched off.
#
# The file carries a CLOCK_MONOTONIC deadline rather than a countdown, because the
# window the face draws and the window this tool measures have to be the same window:
# one number, decided once, here.
COACH_PATH = os.environ.get("FACE_COACH", "/run/face/coach.json")
COACH_EVERY = 0.2            # re-stamp this often; face.py expires it after 1.5 s
LEAD_SECONDS = 3.0           # "get ready": the pose is being taken, the window is not open
BETWEEN_SECONDS = 4.0        # between the windows: read the next instruction, settle
LOOK_ACTION = "LOOK AT THE SCREEN"
LOOK_DETAIL = "phase A of 2 - the pose that has to count as looking"
AWAY_ACTION = "LOOK AWAY"
AWAY_DETAIL = "phase B of 2 - turn away, stay in the camera's view"
HOLD_DETAIL = "hold this pose"
DONE_DETAIL = "thanks - reading the numbers"
TONE_DIR = HERE.parent / "voice" / "tones"
DISPLAY_CONF = HERE.parent / "voice" / "display.conf"
TONE_RATE = 22050
TONE_SECONDS = 0.16
TONE_GAP = 0.045
TONE_GAIN = 0.40
# Three cues, and only three: one says "get ready", one says "the window is OPEN" and
# one says "the window is shut". Rising for open, falling for shut, so the pair can be
# told apart from across the room and with the screen out of sight.
TONES = {"ready": (660.0,), "open": (784.0, 1174.7), "close": (1174.7, 784.0)}


def cue_for(prev_state, next_state):
    """Which tone marks this transition, or None. The one decision, in one place."""
    if next_state == "hold" and prev_state != "hold":
        return "open"
    if prev_state == "hold" and next_state != "hold":
        return "close"
    if next_state == "lead":
        return "ready"
    return None


def write_tone(path: pathlib.Path, name: str, rate: int = TONE_RATE) -> None:
    """Write one cue tone as 16-bit mono WAV. Deterministic, stdlib only.

    A Hann envelope on every note: a bare sine switched on at full amplitude clicks,
    and a click is exactly the kind of transient the ear cannot place in time - which
    is the whole job of these tones.
    """
    frames = []
    for index, hz in enumerate(TONES[name]):
        if index:
            frames.extend([0.0] * int(rate * TONE_GAP))
        span = int(rate * TONE_SECONDS)
        for i in range(span):
            env = math.sin(math.pi * i / span) ** 2
            frames.append(TONE_GAIN * env * math.sin(2.0 * math.pi * hz * i / rate))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(array.array("h", [int(max(-1.0, min(1.0, v)) * 32767)
                                         for v in frames]).tobytes())


class Tones:
    """The four boundary cues, played on the sink display.conf names.

    `paplay --device=<sink>` rather than the default sink, for the reason
    calibrate-lead.py documents: this board's default sink is an analog output nobody
    has plugged in, so "played with no error" and "was audible" are different claims.

    A failure is reported ONCE and then the tones are off for the run. A bench rig with
    no audio path is a legitimate arrangement, and a warning per boundary would bury
    the measurement's own output in noise.
    """

    def __init__(self, sink=None, enabled=True):
        self.sink = sink
        self.enabled = enabled
        self.warned = False
        self.ok = None          # None until a tone has actually been attempted

    def play(self, name):
        if not self.enabled or name not in TONES:
            return False
        path = TONE_DIR / ("coach-%s.wav" % name)
        if not path.is_file():
            try:
                write_tone(path, name)
            except OSError as exc:
                return self._failed("could not write %s: %s" % (path, exc))
        cmd = ["paplay"]
        if self.sink:
            cmd.append("--device=%s" % self.sink)
        cmd.append(str(path))
        try:
            done = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failed("paplay: %s" % exc)
        if done.returncode != 0:
            return self._failed((done.stderr or "paplay failed").strip().splitlines()[0]
                                if done.stderr else "paplay exited %d" % done.returncode)
        self.ok = True
        return True

    def _failed(self, why):
        self.ok = False
        if not self.warned:
            self.warned = True
            warn("%s", why)
            warn("no cue tones this run: the on-screen instruction box (and, for "
                 "phase B, this terminal) is the only cue. A bench rig with no audio "
                 "path is fine - just watch the screen before turning away.")
        self.enabled = False
        return False


class Coach:
    """The instruction panel's file, and the timeline that keeps it true.

    Published atomically and re-stamped every COACH_EVERY seconds, with a heartbeat
    the face expires it against: a killed tool must not leave an instruction on a
    screen, so "the tool is alive" is proven by the file still changing rather than
    claimed in a field.

    On the way out the file is removed rather than left saying `done`, for the same
    reason - a panel that lingers is read as an instruction, and the next thing the
    person does is act on it.
    """

    def __init__(self, path=COACH_PATH, enabled=True, rig=None):
        self.path = pathlib.Path(path)
        self.enabled = enabled
        self.rig = rig
        self.state = None
        self.phase = None
        self.action = ""
        self.detail = ""
        self.deadline = None
        self.total = None
        self.last = 0.0
        self.warned = False

    def announce(self, state, action, detail, deadline=None, total=None, phase=None,
                 now=None):
        """Set what the panel says and publish it at once (not on the next tick)."""
        self.state = state
        self.phase = phase if phase is not None else self.phase
        self.action = action
        self.detail = detail
        self.deadline = deadline
        self.total = total
        self.stamp(now, force=True)

    def stamp(self, now=None, force=False):
        now = time.monotonic() if now is None else now
        if not self.enabled or (not force and now - self.last < COACH_EVERY):
            return
        self.last = now
        payload = {"state": self.state or "hold", "phase": self.phase,
                   "action": self.action, "detail": self.detail,
                   "deadline": self.deadline, "total": self.total,
                   "heartbeat": now, "rig": self.rig,
                   "tool": "calibrate-look.py"}
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".coach-",
                                       suffix=".json")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            if not self.warned:
                self.warned = True
                self.enabled = False
                warn("could not publish the on-screen instruction box (%s: %s).",
                     self.path, exc)
                warn("the poses are still described here, and the cue tones still "
                     "sound, but nothing will be drawn on the screen - so read this "
                     "terminal before turning away for phase B.")

    def tick(self, now=None):
        self.stamp(now)

    def clear(self):
        """Take the panel down. Called on EVERY exit path, including the failures."""
        try:
            self.path.unlink()
        except OSError:
            pass


def sink_from_conf(path=DISPLAY_CONF):
    """The sink display.conf names, or None. Read the fragment, never source it."""
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SINK="):
            return line.partition("=")[2].strip().strip('"').strip("'") or None
    return None


def hold_lead(seconds, label, coach, verbose=True, now=None):
    """The lead-in window: the pose is being taken, the measurement is not running.

    Counted on the same clock and the same deadline the panel draws from, so "it said
    3 s and started when it said" is true by construction rather than by arithmetic
    that happens to agree.
    """
    now = time.monotonic() if now is None else now
    end = now + max(0.0, seconds)
    while True:
        now = time.monotonic()
        left = end - now
        if left <= 0:
            break
        coach.tick(now)
        if verbose and is_tty():
            sys.stdout.write("\r  %s starts in %4.1fs   (the screen says what to do)"
                             % (label, left))
            sys.stdout.flush()
        time.sleep(POLL)
    if verbose and is_tty():
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()


def phase_lead(coach, tones, phase, action, detail, seconds, label, verbose=True):
    """Announce the pose, say "get ready", and count in to it.

    The pose is announced BEFORE the window opens, which is the point: the person
    needs to know what they are about to be measured doing, and to be in it - settled,
    not moving into position - when the window opens.
    """
    now = time.monotonic()
    coach.announce("lead", action, detail, deadline=now + seconds, total=seconds,
                   phase=phase, now=now)
    tones.play(cue_for("between", "lead"))
    hold_lead(seconds, label, coach, verbose=verbose)


def open_window(coach, tones, phase, action, seconds, verbose=True):
    """Open a measured window: the panel says HOLD, the tone says it out loud."""
    now = time.monotonic()
    coach.announce("hold", action, HOLD_DETAIL, deadline=now + seconds,
                   total=seconds, phase=phase, now=now)
    tones.play(cue_for("lead", "hold"))
    if verbose:
        say("  window OPEN for %gs - %s", seconds, action.lower())
    return now + seconds


def close_window(coach, tones, action, detail, phase=None, rest=BETWEEN_SECONDS,
                 verbose=True):
    """Shut the window, and put the next instruction where it can be read."""
    now = time.monotonic()
    coach.announce("between", action, detail, deadline=now + rest, total=rest,
                   phase=phase, now=now)
    tones.play(cue_for("hold", "between"))
    if verbose and detail:
        say("  window SHUT - %s", detail)


# --------------------------------------------------------------------------
# The fit
# --------------------------------------------------------------------------
class Fit:
    """The two thresholds this rig implies, and the margin they were fitted in."""

    def __init__(self):
        self.ok = False
        self.reason = ""
        self.enter = None
        self.exit = None
        self.worst_looking = None
        self.best_away = None
        self.gap = 0.0
        self.ratio = 0.0
        self.note = ""
        self.warns: list = []


def fit_look(pos, neg, tail=TAIL_LOOKING, safety=SAFETY, gap_min=GAP_MIN,
             ratio_min=RATIO_MIN) -> Fit:
    """Fit LOOK_ENTER/LOOK_EXIT to the gap between the two measured states.

    Order of refusal matters: a threshold cannot be fitted to something that is not
    a gap, so every path that would produce a plausible-looking number from
    overlapping clusters is closed before any arithmetic happens.
    """
    f = Fit()
    if len(pos) < MIN_SAMPLES or len(neg) < MIN_SAMPLES:
        f.reason = (f"only {len(pos)} usable 'looking' samples and {len(neg)} 'away' "
                    f"samples; {MIN_SAMPLES} of each is the floor for a percentile. "
                    f"Hold each pose longer (--phase).")
        return f

    worst, best = pct(pos, tail), pct(neg, 100.0 - tail)
    if worst is None or best is None:      # unreachable: the count floor is above
        f.reason = "no samples to fit"
        return f
    f.worst_looking, f.best_away = worst, best
    f.gap = gap = best - worst
    f.ratio = ratio = best / max(worst, 1e-6)

    if gap <= 0:
        f.reason = (f"the two states OVERLAP: the worst looking sample "
                    f"({worst:.2f}) is above the best away sample "
                    f"({best:.2f}). One threshold cannot separate them on "
                    f"this rig - see the component split above for which term is "
                    f"doing it.")
        return f
    if gap < gap_min:
        f.reason = (f"the gap is {gap:.2f}, below the {gap_min:.2f} floor. That is "
                    f"the two tails breathing, not a boundary to put a threshold in.")
        return f
    if ratio < ratio_min:
        f.reason = (f"the margin ratio is {ratio:.2f}x, below {ratio_min:.2f}x: the "
                    f"worst looking sample and the best away sample are close enough "
                    f"that a re-run would move the split.")
        return f

    enter = round(worst + safety * gap, 3)
    exit_ = round(best - safety * gap, 3)
    if exit_ < enter:
        exit_ = enter                      # only reachable through rounding
    f.enter, f.exit, f.ok = enter, exit_, True

    if worst > CEIL_LOOKING:
        f.warns.append(f"the fitted LOOK_ENTER ({enter:.2f}) is high: this rig admits "
                       f"a lot of pose as 'looking'. Check the camera's aim before "
                       f"trusting it.")
    if exit_ == enter:
        f.warns.append("the fitted thresholds are equal, so there is no hysteresis: "
                       "expect the verdict to chatter on the boundary.")
    spread = max(pos) - worst
    if spread > SPREAD_WARN:
        f.warns.append(f"phase A's largest sample ({max(pos):.2f}) sits {spread:.2f} "
                       f"above its {tail:.0f}th percentile ({worst:.2f}): "
                       f"that phase was not one pose, so the fit is loose.")
    return f


def judge(mags, enter, exit_) -> float:
    """Fraction of samples an existing pair of thresholds would call LOOKING.

    Per sample, ignoring the hysteresis and the dwell, which need history. Labelled
    as such wherever it is printed: this is a diagnosis of the numbers being
    replaced, not a claim about what the daemon did.
    """
    if not mags:
        return 0.0
    return sum(1 for m in mags if m <= enter) / float(len(mags))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def stat_line(cap: Capture, tail: float) -> list:
    """The distributions, split so framing and pose can be told apart."""
    mags = cap.mags()
    if not mags:
        return ["     (no usable samples - the camera did not accept a subject)"]
    out = [
        "     gaze mag   p50 %.2f   p%d %.2f   max %.2f" %
        (pct(mags, 50), int(tail), pct(mags, tail), max(mags)),
    ]
    cxs = cap.field(lambda f: (f.get("face") or {}).get("cx"))
    cys = cap.field(lambda f: (f.get("face") or {}).get("cy"))
    bws = cap.field(lambda f: (f.get("face") or {}).get("box_w"))
    if cxs:
        out.append("     framing    face at cx %.2f cy %.2f, box_w median %.2f min %.2f"
                   % (pct(cxs, 50), pct(cys, 50) if cys else 0.0,
                      pct(bws, 50) if bws else 0.0, min(bws) if bws else 0.0))
    yaws = cap.field(lambda f: (f.get("debug") or {}).get("yaw_raw"))
    pits = cap.field(lambda f: (f.get("debug") or {}).get("pitch_raw"))
    if yaws or pits:
        out.append("     pose       yaw %+.2f   pitch %+.2f   (medians)"
                   % (pct(yaws, 50) if yaws else 0.0,
                      pct(pits, 50) if pits else 0.0))
    return out


def phase_summary(cap: Capture, tail: float) -> list:
    lines = ["  %-28s %.1fs  %d kept, %d no-face, %d gate-rejected, %d unreadable"
             % (cap.label, cap.elapsed, cap.n, cap.no_face, cap.gate_rejected,
                cap.unreadable)]
    lines += stat_line(cap, tail)
    if cap.gate_rejected and cap.gate_rejected > cap.n:
        lines.append("     NOTE: the subject gate rejected more frames than it kept. "
                     "On this rig the DISTANCE gate is the binding constraint, and no "
                     "looking threshold can matter until it admits you.")
    if cap.no_face and cap.no_face > cap.n:
        lines.append("     NOTE: most frames had no face at all. The camera is aimed "
                     "past you, or too far away, or covered.")
    return lines


def conf_header(fit, a: Capture, b: Capture, tail, safety, rig, judged=None) -> str:
    """The stamp: a number without its provenance is a number nobody can re-check."""
    assert judged or fit is not None, "a header needs either a fit or judged values"
    now = datetime.datetime.now().strftime("%d %b %Y %H:%M")
    if judged:
        body = [
            "# TUNED:",
            "#   %s by calibrate-look.py, judged by hand rather than fitted." % now,
            "#   rig: %s" % rig,
            "#   written values: LOOK_ENTER %g  LOOK_EXIT %g" % (judged[0], judged[1]),
        ]
    else:
        body = [
            "# TUNED:",
            "#   %s by calibrate-look.py, fitted to measured poses." % now,
            "#   rig: %s" % rig,
            "#   phase A 'looking at the screen': %d samples, p%d %.2f, max %.2f"
            % (a.n, int(tail), fit.worst_looking, max(a.mags()) if a.n else 0.0),
            "#   phase B 'looking away':          %d samples, p%d %.2f, min %.2f"
            % (b.n, int(100 - tail), fit.best_away, min(b.mags()) if b.n else 0.0),
            "#   margin (worst looking -> best away): %.2f  (%.2fx on the tails)"
            % (fit.gap, fit.ratio),
            "#   each threshold sits %.0f%% into the gap from its own edge" % (safety * 100),
        ]
    return "\n".join([
        "# What THIS camera calls \"looking at the screen\".",
        "#",
        "# Read by gaze.py, and re-read whenever this file changes, so a re-fit is live",
        "# with no service restart. Written by calibrate-look.py - use it rather than",
        "# editing the numbers by hand, so the margin they were fitted in is recorded",
        "# beside them. Absent file = gaze.py's built-in 0.35/0.80.",
        "#",
        *body,
        "",
    ])


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_show(status_path, conf_path) -> int:
    """Everything needed to answer 'what is it doing, and what is it set to'."""
    d = lookconf.describe(conf_path)
    say("== active calibration ==")
    say("  file      %s%s", d["path"], "" if d["exists"] else "   (absent: built-ins in use)")
    for key in ("LOOK_ENTER", "LOOK_EXIT", "SUBJECT_MIN_W", "SUBJECT_EXIT_W"):
        if key in d["numbers"]:
            say("  %-14s %g", key, d["numbers"][key])
        else:
            say("  %-14s %g   (built-in)", key, lookconf.BUILTIN[key])
    say("  rig       %s", d["rig"] or "(unlabelled)")
    say("  fitted    %s", d["fitted"] or "(no stamp)")

    say()
    say("== live reading ==")
    data, reason = read_json(status_path)
    if data is None:
        say("  status file %s: %s - gaze.service is not publishing", status_path, reason)
    else:
        age = time.time() - float(data.get("t") or 0.0)
        where = ("nobody detected" if not data.get("has_face") else
                 "a face the SUBJECT gate rejected" if not data.get("subject") else
                 "a subject")
        say("  %s, %0.1fs old -> %s", status_path, age, where)
        if data.get("subject"):
            mag = sample_mag(data)
            ent = d["numbers"].get("LOOK_ENTER", lookconf.BUILTIN["LOOK_ENTER"])
            say("  gaze mag %.2f -> the active LOOK_ENTER %.2f says %s",
                mag, ent, "looking" if mag <= ent else "NOT looking")
        if age > STALE:
            say("  STALE: older than %.0fs, so gaze may be down and these numbers may "
                "be the last thing it said", STALE)

    profs = lookconf.profiles()
    say()
    say("== saved rigs ==")
    if not profs:
        say("  none yet. ./calibrate-look.py --rig bench writes looks/bench.conf")
    for name, path in profs.items():
        values = lookconf.read_numbers(path)
        say("  %-16s enter %s  exit %s", name,
            values.get("LOOK_ENTER", "?"), values.get("LOOK_EXIT", "?"))
    say("  switch with: ./calibrate-look.py --use <rig>")
    return EX_OK


def cmd_watch(status_path, conf_path) -> int:
    """One line, live. The loop for judging a pose by eye against the real verdict."""
    numbers = lookconf.read_numbers(conf_path)
    ent = numbers.get("LOOK_ENTER", lookconf.BUILTIN["LOOK_ENTER"])
    exit_ = numbers.get("LOOK_EXIT", lookconf.BUILTIN["LOOK_EXIT"])
    say("watching %s  (active %.2f/%.2f). Ctrl-C to stop.", status_path, ent, exit_)
    try:
        while True:
            data, reason = read_json(status_path)
            if data is None:
                sys.stdout.write("\r  %-78s" % f"status file {reason}")
            else:
                age = time.time() - float(data.get("t") or 0.0)
                mag = sample_mag(data)
                if not data.get("has_face"):
                    state = "nobody"
                elif not data.get("subject"):
                    state = "gate rejected"
                else:
                    state = "looking" if data.get("looking") else "not looking"
                sys.stdout.write(
                    "\r  mag %.2f  gaze %+0.2f %+0.2f  face %-8s active says %-11s "
                    "(%.1fs old)   "
                    % (mag, (data.get("gaze") or {}).get("x", 0.0),
                       (data.get("gaze") or {}).get("y", 0.0),
                       "yes" if data.get("subject") else "no", state, age))
            sys.stdout.flush()
            time.sleep(POLL)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return EX_OK


def launch_prompt(label: str, text: str) -> None:
    say()
    say("== %s ==", label)
    say("  %s", text)


def run_phases(status_path, seconds, conf_path, lead=LEAD_SECONDS,
               between=BETWEEN_SECONDS, coach=None, tones=None, verbose=True) -> tuple:
    """Both measured windows, walkable from where the person is sitting.

    The order is the point, and it is the same order in three places - here, on the
    screen, and in the tones - so there is no arrangement in which the person is
    holding a pose the tool is not measuring, or being measured in a pose nobody asked
    for:

        phase A lead-in   the screen says LOOK AT THE SCREEN, counting in
        phase A window    the tone says OPEN, the screen says HOLD
        between           the tone says SHUT, the screen says NOW LOOK AWAY
        phase B lead-in   the tone says GET READY, counting in
        phase B window    the tone says OPEN, the screen says HOLD
        done              the tone says SHUT, the screen says reading the numbers

    Phase B is why the tones exist: the person is looking away from the screen, so the
    tone is the only cue that the window they cannot see has opened.
    """
    coach = coach if coach is not None else Coach(enabled=False)
    tones = tones if tones is not None else Tones(enabled=False)
    a = Capture("A looking at the screen", "look at the screen", seconds)
    b = Capture("B looking away", "look away", seconds)
    numbers = lookconf.read_numbers(conf_path)
    active = (numbers.get("LOOK_ENTER", lookconf.BUILTIN["LOOK_ENTER"]),
              numbers.get("LOOK_EXIT", lookconf.BUILTIN["LOOK_EXIT"]))

    launch_prompt("phase A",
                  "Sit where you will sit and look at the screen - the pose you want "
                  "counted. Hold it when the window opens.")
    phase_lead(coach, tones, "A", LOOK_ACTION, LOOK_DETAIL, lead, "phase A",
               verbose=verbose)
    deadline = open_window(coach, tones, "A", LOOK_ACTION, seconds, verbose=verbose)
    capture(status_path, a, active, verbose=verbose, coach=coach, deadline=deadline)
    close_window(coach, tones, AWAY_ACTION, AWAY_DETAIL, phase="B", rest=between,
                 verbose=verbose)

    launch_prompt("phase B",
                  "Same seat. Look AWAY from the screen - at a phone, the wall, your "
                  "hands. Stay detectable: a head turned so far the camera loses you "
                  "measures nothing.")
    phase_lead(coach, tones, "B", AWAY_ACTION, AWAY_DETAIL, lead, "phase B",
               verbose=verbose)
    deadline = open_window(coach, tones, "B", AWAY_ACTION, seconds, verbose=verbose)
    capture(status_path, b, active, verbose=verbose, coach=coach, deadline=deadline)
    close_window(coach, tones, "DONE", DONE_DETAIL, phase="B", rest=0.0,
                 verbose=verbose)
    # Left up on purpose for a last second and then removed by the caller's finally:
    # the panel is what says "the measurement is over", and clearing the file is what
    # says "no instruction is outstanding".
    return a, b


def kiosk_active():
    """True/False/None: is the process that would DRAW the panel running?

    The panel is only ever drawn by face-kiosk.service - this tool writes a file and
    nothing else reads it. A kiosk that is not up is a silent failure otherwise: the
    calibration would still measure, and the person would still be holding poses,
    with nothing on the screen telling them which one.
    """
    try:
        done = subprocess.run(["systemctl", "is-active", "face-kiosk.service"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() == "active"


def sink_known(name):
    """True/False/None: is that sink on this board right now?"""
    if not name:
        return None
    try:
        done = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return any(name in line for line in done.stdout.splitlines())


def preflight(status_path, session_path, force, conf_path, coach=None,
              tones=None) -> int:
    say("== preflight ==")
    data, reason = read_json(status_path)
    if data is None:
        warn("status file %s: %s.", status_path, reason)
        warn("gaze.service is not publishing, so there is nothing to measure. The "
             "unit must be running WITH its --status path: check `systemctl status "
             "gaze.service` and `cat %s`.", status_path)
        return EX_NOTHING
    age = time.time() - float(data.get("t") or 0.0)
    if age > STALE:
        warn("status file is %.1fs old (limit %.0fs): gaze is stopped or stuck, and a "
             "stale file reads exactly like a quiet room.", age, STALE)
        return EX_NOTHING
    say("  status file  %s, %.1fs old, gaze.service is publishing", status_path, age)

    sdata, _ = read_json(session_path)
    phase = (sdata or {}).get("phase", "absent (which means standby)")
    say("  session      %s", phase)
    if phase == "running" and not force:
        warn("a session is RUNNING, and the poses you are about to hold would be "
             "scored into it. Re-run with --force if that is what you want.")
        return EX_NOTHING

    d = lookconf.describe(conf_path)
    say("  active conf  %s%s", d["path"],
        "" if d["exists"] else "  (absent: built-ins in use)")
    say("               LOOK_ENTER %g  LOOK_EXIT %g",
        d["numbers"].get("LOOK_ENTER", lookconf.BUILTIN["LOOK_ENTER"]),
        d["numbers"].get("LOOK_EXIT", lookconf.BUILTIN["LOOK_EXIT"]))
    say("               rig %s, %s", d["rig"] or "(unlabelled)",
        d["fitted"] or "(no stamp)")

    # The cues, reported because both fail silently: a panel file nobody draws is the
    # same as no panel, and this board's DEFAULT sink is an analog output nobody has
    # plugged in - so "paplay exited 0" and "it made a sound" are different claims.
    if coach is not None and coach.enabled:
        kiosk = kiosk_active()
        say("  instruction  panel -> %s", coach.path)
        if kiosk is False:
            warn("face-kiosk.service is NOT running, so nothing will be drawn on the "
                 "screen. Start it (or do this with a display attached) - otherwise "
                 "phase B has no cue at all, because you will be looking away.")
        elif kiosk is None:
            say("               (could not ask systemctl whether the kiosk is up)")
    else:
        say("  instruction  panel OFF (--no-coach): these terminal lines are the only "
            "cue")
    if tones is not None and tones.enabled:
        known = sink_known(tones.sink)
        say("  cue tones    %s", tones.sink or "(this profile's default sink)")
        if known is False:
            warn("that sink is not in `pactl list short sinks` right now, so the tones "
                 "will probably fail to play. The screen cue still works; say so if "
                 "you would rather run without sound.")
        elif known is None and tones.sink:
            say("               (could not list sinks; playability unverified)")
    else:
        say("  cue tones    OFF (--no-tones): watch the screen before you turn away")
    return EX_OK


def cmd_calibrate(args, coach=None, tones=None) -> int:
    coach = coach if coach is not None else Coach(enabled=False)
    tones = tones if tones is not None else Tones(enabled=False)
    rc = preflight(args.status, args.session, args.force, args.conf, coach=coach,
                   tones=tones)
    if rc:
        return rc

    a, b = run_phases(args.status, args.phase, args.conf, lead=args.lead,
                      between=args.between, coach=coach, tones=tones)

    say()
    say("== measured ==")
    for cap in (a, b):
        for line in phase_summary(cap, args.tail):
            say(line)

    pos, neg = a.mags(), b.mags()
    fit = fit_look(pos, neg, args.tail, args.safety)
    numbers = lookconf.read_numbers(args.conf)
    cur_ent = numbers.get("LOOK_ENTER", lookconf.BUILTIN["LOOK_ENTER"])
    cur_exit = numbers.get("LOOK_EXIT", lookconf.BUILTIN["LOOK_EXIT"])

    say()
    say("== fit ==")
    if pos:
        say("  worst looking sample (p%d of A)   %.2f", int(args.tail), fit.worst_looking)
    if neg:
        say("  best  away    sample (p%d of B)   %.2f",
            int(100 - args.tail), fit.best_away)
    say("  margin                            %.2f   (%.2fx on the tails)",
        fit.gap, fit.ratio)
    if pos:
        say("  the active %.2f/%.2f would call %.1f%% of phase A 'not looking' and "
            "%.1f%% of phase B 'looking'", cur_ent, cur_exit,
            (1.0 - judge(pos, cur_ent, cur_exit)) * 100.0,
            judge(neg, cur_ent, cur_exit) * 100.0)
        say("    (per sample, ignoring the hysteresis and dwell, which need history)")

    if not fit.ok:
        say()
        say("  NO FIT: %s", fit.reason)
        say("  Nothing written. The component split above is the next thing to look "
            "at: a large 'framing' term with a small 'pose' term means the camera is "
            "aimed so that facing the screen is off-centre, which a threshold cannot "
            "fix.")
        return EX_NOTHING

    say()
    say("  -> LOOK_ENTER %.3f   LOOK_EXIT %.3f   (each %.0f%% into the gap)",
        fit.enter, fit.exit, args.safety * 100)
    for w in fit.warns:
        say("  warning: %s", w)
    if fit.enter <= cur_ent and fit.exit >= cur_exit:
        say("  note: the active numbers already sit inside this gap; re-fitting "
            "changes the stamp more than the behaviour.")

    rig = args.rig or (lookconf.read(args.conf).get("RIG") or "unlabelled")
    header = conf_header(fit, a, b, args.tail, args.safety, rig)
    return write_or_decline(args, {"LOOK_ENTER": fit.enter, "LOOK_EXIT": fit.exit},
                            header, rig)


def write_or_decline(args, values: dict, header: str, rig: str) -> int:
    """Confirm, then write the active conf AND the rig archive. Never silently."""
    lookconf.validate(values["LOOK_ENTER"], values["LOOK_EXIT"])
    say()
    say("== to write ==")
    for line in header.rstrip("\n").splitlines():
        say("  " + line)
    for key, value in values.items():
        say("  %s=%g", key, value)

    if args.dry_run:
        say()
        say("  dry run: nothing written, and nothing to undo.")
        return EX_OK

    if not args.yes:
        if not is_tty():
            warn("stdin is not a terminal, so nothing was written. Re-run with --yes "
                 "to accept these numbers, or run it on the board's console.")
            return EX_NOTHING
        sys.stdout.write("\nWrite this? [y/N] ")
        sys.stdout.flush()
        answer = sys.stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            warn("declined; nothing written.")
            return EX_DECLINED

    lookconf.write(args.conf, values, header)
    say("  wrote %s", args.conf)
    archive = lookconf.PROFILE_DIR / ("%s.conf" % rig)
    lookconf.write(archive, values, header)
    say("  wrote %s", archive)
    say()
    say("  gaze.py re-reads this on the next check and logs the change - no restart.")
    say("  verify with:  journalctl -u gaze.service -n 20 | grep look.conf")
    return EX_OK


def cmd_write(args, spec: str) -> int:
    try:
        parts = [float(x) for x in spec.replace(" ", "").split(",")]
    except ValueError:
        warn("--write wants two numbers: --write ENTER,EXIT (e.g. --write 0.40,0.55)")
        return EX_NOTHING
    if len(parts) != 2:
        warn("--write wants exactly two numbers: --write ENTER,EXIT")
        return EX_NOTHING
    enter, exit_ = parts
    try:
        lookconf.validate(enter, exit_)
    except ValueError as exc:
        warn("%s", exc)
        return EX_NOTHING
    rig = args.rig or (lookconf.read(args.conf).get("RIG") or "unlabelled")
    header = conf_header(None, Capture("", "", 0), Capture("", "", 0), args.tail,
                         args.safety, rig, judged=(enter, exit_))
    return write_or_decline(args, {"LOOK_ENTER": enter, "LOOK_EXIT": exit_}, header, rig)


def cmd_use(args, rig: str) -> int:
    """Make a saved rig active. Live at gaze's next check, because it re-reads."""
    if "/" in rig or rig in (".", ".."):
        warn("a rig name is a filename: letters, digits, dashes and underscores.")
        return EX_NOTHING
    src = lookconf.profiles().get(rig)
    if src is None:
        warn("no saved rig called %r. Saved: %s", rig,
             ", ".join(lookconf.profiles()) or "(none)")
        return EX_NOTHING
    before = lookconf.read_numbers(args.conf)
    lookconf.copy_profile(src, args.conf)
    after = lookconf.read_numbers(args.conf)
    say("active conf  %s", args.conf)
    say("  look-enter   %s -> %s", before.get("LOOK_ENTER", "(built-in)"),
        after.get("LOOK_ENTER"))
    say("  look-exit    %s -> %s", before.get("LOOK_EXIT", "(built-in)"),
        after.get("LOOK_EXIT"))
    say("  rig          %s", lookconf.read(args.conf).get("RIG") or rig)
    say("gaze.py picks this up on its next check (no restart). If the camera has just "
        "MOVED, re-run ./calibrate-look.py on the new rig instead of trusting a "
        "stored profile.")
    return EX_OK


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def selftest() -> int:
    """The fit, the guards and the conf round-trip, with no camera and no board."""
    import tempfile

    failures = []
    checks = [0]

    def check(name, cond):
        checks[0] += 1
        if not cond:
            failures.append(name)

    def synth(lo, hi, n=60, step=0.0):
        """A cluster: n samples spread evenly from lo to hi."""
        if n == 1:
            return [lo]
        return [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    # -- percentiles --------------------------------------------------------
    check("p50 of 1..5 is 3", pct([1, 2, 3, 4, 5], 50) == 3)
    check("p0 is the min", pct([3, 1, 2], 0) == 1)
    check("p100 is the max", pct([3, 1, 2], 100) == 3)
    check("p95 interpolates", abs(pct(synth(0.0, 1.0, 101), 95) - 0.95) < 1e-9)
    check("percentile of one value", pct([0.4], 95) == 0.4)
    check("percentile of nothing is None", pct([], 95) is None)

    # -- a clean separation fits strictly inside the tails -------------------
    pos, neg = synth(0.05, 0.30), synth(0.55, 0.90)
    f = fit_look(pos, neg)
    e, x = f.enter, f.exit
    wl, ba = f.worst_looking, f.best_away
    check("clean gap fits", f.ok)
    check("enter is above the worst looking sample", e is not None and wl is not None and e > wl)
    check("exit is below the best away sample", x is not None and ba is not None and x < ba)
    check("enter <= exit", e is not None and x is not None and e <= x)
    check("both thresholds land inside the gap",
          wl is not None and e is not None and x is not None and ba is not None
          and wl < e <= x < ba)

    # -- overlapping clusters are refused, with no numbers ------------------
    f = fit_look(synth(0.20, 0.80), synth(0.10, 0.70))
    check("overlap refused", not f.ok)
    check("overlap names the reason", "OVERLAP" in f.reason)
    check("overlap writes no numbers", f.enter is None and f.exit is None)

    # -- a gap too narrow to hold two thresholds ---------------------------
    f = fit_look(synth(0.30, 0.50), synth(0.515, 0.60))
    check("narrow gap refused", not f.ok)
    check("narrow gap says so", "floor" in f.reason)

    # -- too few samples ---------------------------------------------------
    f = fit_look(synth(0.05, 0.30, n=5), synth(0.55, 0.90, n=5))
    check("too few samples refused", not f.ok)
    check("too few samples names the floor", "floor" in f.reason)

    # -- the margin is measured on the TAILS, not the middles --------------
    # Two clusters whose MEDIANS are 0.68 apart but whose tails overlap: 20 of
    # phase A's samples sit at 0.15-0.20 and 10 reach 0.50-0.60, while 10 of phase
    # B's sit down at 0.55-0.60. Centre-to-centre this looks like a huge gap; the
    # tails say otherwise, and the fit must follow the tails.
    f = fit_look(synth(0.50, 0.60, n=10) + synth(0.15, 0.20, n=20),
                 synth(0.55, 0.60, n=10) + synth(0.80, 0.90, n=20))
    check("far-apart middles with overlapping tails is refused", not f.ok)
    check("and it is refused as an overlap", "OVERLAP" in f.reason)
    check("and no thresholds are offered", f.enter is None and f.exit is None)

    # -- one outlier is NOT a fat tail: p95 drops it and the WARNING carries it
    f = fit_look([0.10, 0.15, 0.20, 0.68] + synth(0.30, 0.40, n=26),
                 [0.72, 0.74, 0.76, 0.78] + synth(0.80, 0.90, n=26))
    check("a lone outlier does not stop the fit", f.ok)
    check("a lone outlier is warned about instead", bool(f.warns))
    check("the warning names the spread", any("not one pose" in w for w in f.warns))

    # -- equal thresholds are allowed but warned about ---------------------
    f = fit_look(synth(0.10, 0.20), synth(0.24, 0.30))
    check("a 0.04-ish gap still fits or refuses cleanly",
          f.ok or "floor" in f.reason)

    # -- a high ceiling warns rather than lies -----------------------------
    f = fit_look(synth(0.60, 0.75, n=30), synth(0.85, 0.99, n=30, step=0))
    check("a high fitted enter warns", (not f.ok) or bool(f.warns))

    # -- samples the camera could not see are excluded, not counted as 0.0 --
    cap = Capture("A", "", 1.0)
    for i in range(38):                       # nobody there: gaze recentres to 0.0
        classify(cap, {"t": i, "has_face": False, "subject": False,
                       "looking": False, "gaze_mag": 0.0, "gaze": {"x": 0.0, "y": 0.0}})
    classify(cap, {"t": 98, "has_face": True, "subject": False, "looking": False,
                   "face": {"box_w": 0.05, "cx": 0.5}, "gaze_mag": 0.0,
                   "gaze": {"x": 0.0, "y": 0.0}})
    classify(cap, {"t": 99, "has_face": True, "subject": True, "looking": False,
                   "face": {"box_w": 0.19, "cx": 0.5}, "gaze_mag": 0.9,
                   "gaze": {"x": 0.9, "y": 0.0}})
    check("lost frames are not usable samples", cap.n == 1)
    check("lost frames keep their own count", cap.no_face == 38)
    check("a gate-rejected face is its own answer", cap.gate_rejected == 1)
    check("a recentred 0.0 never enters the cluster", cap.mags() == [0.9])
    check("a window of look-aways cannot pass as looking",
          judge(cap.mags(), 0.35, 0.80) == 0.0)
    check("a recentred 0.0 reclassified as looking is the hazard being avoided",
          judge([0.0], 0.35, 0.80) == 1.0)

    # -- judging the samples an existing config would have got wrong -------
    check("judge counts the ones inside enter", judge([0.1, 0.5, 0.7, 0.2], 0.35, 0.8) == 0.5)
    check("judge of nothing is zero", judge([], 0.35, 0.8) == 0.0)

    # -- the conf round-trips, and gaze.py's reader agrees -----------------
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "look.conf"
        header = conf_header(fit_look(pos, neg), Capture("A", "", 8), Capture("B", "", 8),
                             TAIL_LOOKING, SAFETY, "bench")
        lookconf.write(path, {"LOOK_ENTER": 0.371, "LOOK_EXIT": 0.423,
                              "RIG": "bench"}, header)
        back = lookconf.read_numbers(path)
        check("conf round-trips enter", back.get("LOOK_ENTER") == 0.371)
        check("conf round-trips exit", back.get("LOOK_EXIT") == 0.423)
        check("conf carries the rig", lookconf.read(path).get("RIG") == "bench")
        check("conf carries the stamp",
              "TUNED:" in path.read_text() and "0.37" in path.read_text())

        # a half-written file: the keys that made it are readable, the rest absent
        path.write_text("LOOK_ENTER=0.4\nLOOK_EX\n# comment\nRIG=bench\n")
        half = lookconf.read(path)
        check("a half-written line drops only that key", half.get("LOOK_ENTER") == "0.4")
        check("a half-written key is absent", "LOOK_EXIT" not in half)
        check("the rest of a torn file survives", half.get("RIG") == "bench")
        check("resolve falls back per key",
              lookconf.resolve(None, lookconf.read_numbers(path), "LOOK_EXIT")
              == (0.80, "built-in"))

        # garbage, and an absent file
        path.write_text("this is not a conf at all\n\x00\x01\n")
        check("garbage yields no numbers", lookconf.read_numbers(path) == {})
        check("an absent conf yields nothing",
              lookconf.read_numbers(pathlib.Path(td) / "nope.conf") == {})

        # precedence: flag beats conf beats built-in
        check("flag wins", lookconf.resolve(0.5, {"LOOK_ENTER": 0.4}, "LOOK_ENTER")
              == (0.5, "flag"))
        check("conf beats built-in",
              lookconf.resolve(None, {"LOOK_ENTER": 0.4}, "LOOK_ENTER") == (0.4, "conf"))
        check("built-in is the floor",
              lookconf.resolve(None, {}, "LOOK_ENTER") == (0.35, "built-in"))

        # an inverted pair is refused rather than written
        try:
            lookconf.validate(0.6, 0.4)
            check("an inverted pair is refused", False)
        except ValueError:
            check("an inverted pair is refused", True)
        lookconf.validate(0.4, 0.4)          # equal is legal: no hysteresis
        check("equal thresholds are legal", True)

        # rig switching is a byte copy, stamp included
        arch = pathlib.Path(td) / "bench.conf"
        lookconf.write(arch, {"LOOK_ENTER": 0.22, "LOOK_EXIT": 0.31, "RIG": "bench"}, header)
        lookconf.copy_profile(arch, path)
        check("switching rigs copies the stamp", "TUNED:" in path.read_text())
        check("switching rigs copies the numbers",
              lookconf.read_numbers(path).get("LOOK_ENTER") == 0.22)

    # -- the cue tones -------------------------------------------------------
    # What is testable without a speaker: the samples are deterministic, they do not
    # clip, they start and end at silence (a click is the one thing that would make the
    # cue unplaceable in time), and the rising/falling pair really is two different
    # signals rather than one played twice.
    for name in sorted(TONES):
        stop = pathlib.Path(td) / ("tone-%s.wav" % name)
        write_tone(stop, name)
        with wave.open(str(stop)) as fh:
            frames = array.array("h")
            frames.frombytes(fh.readframes(fh.getnframes()))
            rate = fh.getframerate()
        peak = max(abs(v) for v in frames)
        check("tone %s: non-trivial and unclipped" % name, 0 < peak < 32767)
        check("tone %s: starts and ends at silence" % name,
              abs(frames[0]) < 200 and abs(frames[-1]) < 200)
        check("tone %s: lasts about the notes asked for" % name,
              0.05 < len(frames) / rate < 1.0)
        again = pathlib.Path(td) / ("tone-%s-again.wav" % name)
        write_tone(again, name)
        check("tone %s: generation is deterministic" % name,
              again.read_bytes() == stop.read_bytes())

    def tone_energy(name):
        stop = pathlib.Path(td) / ("energy-%s.wav" % name)
        write_tone(stop, name)
        with wave.open(str(stop)) as fh:
            frames = array.array("h")
            frames.frombytes(fh.readframes(fh.getnframes()))
        half = len(frames) // 2
        # Zero crossings per half: a proxy for pitch that needs no FFT, and enough to
        # tell "rising" from "falling" - which is the whole distinction between the two
        # boundary cues.
        def crossings(chunk):
            return sum(1 for i in range(1, len(chunk))
                       if (chunk[i - 1] < 0) != (chunk[i] < 0))
        return crossings(frames[:half]), crossings(frames[half:])

    up = tone_energy("open")
    down = tone_energy("close")
    check("open: it RISES (%d -> %d crossings)" % up, up[1] > up[0])
    check("close: it FALLS (%d -> %d crossings)" % down, down[1] < down[0])
    check("open and close are not the same signal", up != down)

    # -- which cue belongs to which transition -------------------------------
    check("from nothing to lead says get ready", cue_for(None, "lead") == "ready")
    check("lead to hold says OPEN", cue_for("lead", "hold") == "open")
    check("hold to between says SHUT", cue_for("hold", "between") == "close")
    check("between to lead says get ready", cue_for("between", "lead") == "ready")
    check("a re-stamp of hold says nothing", cue_for("hold", "hold") is None)
    check("nothing else speaks", cue_for("between", "done") is None)

    # -- the panel the face will draw ----------------------------------------
    # The contract between the two files, checked here as well as in face.py: a payload
    # this tool would publish must be one face.py accepts. They are two programs that
    # have to agree, and the agreement is a data format, so it is tested as one.
    stop = pathlib.Path(td) / "coach.json"
    coach = Coach(path=stop, enabled=True, rig="bench")
    coach.announce("hold", LOOK_ACTION, HOLD_DETAIL, deadline=time.monotonic() + 8.0,
                   total=8.0, phase="A")
    payload = json.loads(stop.read_text())
    check("the panel file is valid JSON with a heartbeat",
          isinstance(payload.get("heartbeat"), (int, float)))
    check("the panel names the state face.py knows",
          payload.get("state") in ("lead", "hold", "between", "done"))
    check("the panel carries the instruction", payload.get("action") == LOOK_ACTION)
    check("the panel carries a monotonic deadline, not a countdown",
          isinstance(payload.get("deadline"), float) and payload["deadline"] > 0)
    check("the panel names the rig it was measured on", payload.get("rig") == "bench")

    # It re-stamps, it is throttled, and it goes away.
    before = payload["heartbeat"]
    coach.tick(now=before + COACH_EVERY / 2.0)
    check("a tick inside the interval does not rewrite the file",
          json.loads(stop.read_text())["heartbeat"] == before)
    coach.tick(now=before + COACH_EVERY * 2)
    check("a tick after it does", json.loads(stop.read_text())["heartbeat"] > before)
    coach.clear()
    check("clearing removes the panel", not stop.exists())

    # A disabled coach writes nothing at all, and says so rather than crashing: the
    # --no-coach path has to be exactly as safe as the enabled one.
    quiet = Coach(path=stop, enabled=False)
    quiet.announce("hold", LOOK_ACTION, HOLD_DETAIL, deadline=1.0, total=1.0)
    quiet.tick()
    check("a disabled panel writes no file", not stop.exists())
    quiet.clear()

    # -- reading display.conf for the sink -----------------------------------
    conf = pathlib.Path(td) / "display.conf"
    conf.write_text("# comment\nSINK=alsa_output.named.sink\nLEAD=-0.04\n")
    check("the sink is read from display.conf",
          sink_from_conf(conf) == "alsa_output.named.sink")
    conf.write_text("SINK=\"quoted sink\"\n")
    check("quotes are stripped", sink_from_conf(conf) == "quoted sink")
    conf.write_text("LEAD=0.1\n")
    check("a conf with no SINK yields None", sink_from_conf(conf) is None)
    check("an absent conf yields None", sink_from_conf(pathlib.Path(td) / "no.conf") is None)

    total = checks[0]
    if failures:
        say("SELFTEST FAILED: %d of %d checks", len(failures), total)
        for name in failures:
            say("  FAIL %s", name)
        return 1
    say("selftest: %d checks pass (fit, guards, exclusion, conf round-trip, cues)",
        total)
    return EX_OK


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", default=DEFAULT_STATUS,
                    help="the status file gaze.service publishes (default %(default)s). "
                         "This tool reads that file and never opens the camera")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="the session file, to refuse to calibrate during a run")
    ap.add_argument("--conf", default=str(lookconf.DEFAULT_CONF),
                    help="the active calibration file (default %(default)s)")
    ap.add_argument("--rig", default=None,
                    help="what this fit is for (robot, bench, ...). Stamped into the "
                         "conf and used as the archive name. Default: whatever the "
                         "active conf already says, else unlabelled")
    ap.add_argument("--phase", type=float, default=PHASE_SECONDS,
                    help="seconds to hold each pose (default %(default)s). Below ~4s "
                         "there are not enough samples for a percentile")
    ap.add_argument("--lead", type=float, default=LEAD_SECONDS, metavar="SECS",
                    help="seconds of 'get ready' before each window opens, so the pose "
                         "is held rather than moved into (default %(default)s)")
    ap.add_argument("--between", type=float, default=BETWEEN_SECONDS, metavar="SECS",
                    help="seconds between the windows, long enough to read the next "
                         "instruction and turn away (default %(default)s)")
    ap.add_argument("--no-coach", action="store_true",
                    help="do not publish /run/face/coach.json, so face-kiosk draws no "
                         "instruction panel (the terminal and the tones remain)")
    ap.add_argument("--no-tones", action="store_true",
                    help="do not play the cue tones at each window boundary")
    ap.add_argument("--tail", type=float, default=TAIL_LOOKING,
                    help="percentile used as each cluster's edge (default %(default)s: "
                         "p95 of the looking samples, p5 of the away ones)")
    ap.add_argument("--safety", type=float, default=SAFETY,
                    help="how far into the gap each threshold sits (default %(default)s)")
    ap.add_argument("--show", action="store_true",
                    help="the active conf, the live reading and the saved rigs; no capture")
    ap.add_argument("--watch", action="store_true",
                    help="one live line: magnitude, the face's own verdict, and the age "
                         "of the reading. Ctrl-C to stop")
    ap.add_argument("--use", metavar="RIG",
                    help="make looks/<RIG>.conf the active numbers, without re-measuring")
    ap.add_argument("--write", metavar="ENTER,EXIT",
                    help="write values you judged yourself, after confirming")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and report, but never write")
    ap.add_argument("--yes", action="store_true",
                    help="do not prompt (required when stdin is not a terminal)")
    ap.add_argument("--force", action="store_true",
                    help="calibrate even while a session is running")
    ap.add_argument("--selftest", action="store_true",
                    help="the fit, the guards and the conf round-trip; no camera, no board")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.show:
        return cmd_show(args.status, args.conf)
    if args.watch:
        return cmd_watch(args.status, args.conf)
    if args.use:
        return cmd_use(args, args.use)
    if args.write:
        return cmd_write(args, args.write)

    # The cues are built here and torn down in the finally for one reason: the panel is
    # an INSTRUCTION, and an instruction that outlives the tool that posted it is a lie
    # the next person to walk past will act on. Every exit path - a refusal, a keyboard
    # interrupt, an exception - takes it down. (A SIGKILL cannot, which is why face.py
    # expires the panel after 1.5 s of a heartbeat that has stopped moving.)
    coach = Coach(enabled=not args.no_coach, rig=args.rig)
    tones = Tones(sink=sink_from_conf(), enabled=not args.no_tones)
    try:
        return cmd_calibrate(args, coach, tones)
    finally:
        coach.clear()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.exit(130)
