#!/usr/bin/env python3
"""
greeter.py — play the introduction when someone settles in front of the robot.

THE SEQUENCE (the whole point of this file)
  1. A person sits down. The robot does NOT greet them immediately: a face seen
     for an instant is a passer-by, or a detector hiccup. It waits for PRESENCE
     seconds of them being there.
  2. The introduction then plays, and once started it runs to the END. Nothing
     interrupts it — being talked over is worse than being late.
  3. When it finishes, the person gets up. Again the robot waits, this time for
     ABSENCE seconds of NO face, so a momentary loss of tracking is not mistaken
     for "they left".
  4. Then it resets, ready for the next person. Repeat.

WHERE THE FACE SIGNAL COMES FROM
  /run/face/gaze-status.json, published by gaze.service at ~12 fps. This process
  never touches the camera: DepthAI holds the OAK-D exclusively, and a second
  reader would die with X_LINK_DEVICE_ALREADY_IN_USE. Reading the published state
  keeps this unit independent — the on-demand camera viewer can take the device
  without this one also falling over, it simply stops seeing faces.

WHY IT KEYS ON `subject`, NOT `has_face`
  `has_face` means a face-shaped thing is in view SOMEWHERE. That is not the
  question. A face on a television in the next room answers yes - persistently, and
  at confidence 0.89 - and the robot once played this entire introduction to an
  empty room off the back of exactly that. `subject` is gaze.py's verdict that the
  face is big enough, and positioned plausibly, to be someone in front of the robot;
  in practice it is a distance gate. If the field is missing (an older gaze.py) this
  falls back to `has_face` and behaves as it used to.

WHY "PRESENCE" IS NOT LITERALLY AN UNBROKEN RUN OF DETECTIONS
  At 12 fps with SCRFD, the occasional frame is missed: a blink, a turn, motion
  blur. Requiring an unbroken run means the count restarts every couple of
  seconds and the introduction NEVER plays — the feature looks dead while every
  individual part works. So presence is treated as unbroken while the face has
  been seen within DROPOUT seconds; only a longer gap restarts the count. The
  10 s exists to reject passers-by and detector glitches, and both of those are
  many seconds long, not 80 ms.

  The same tolerance applies to absence: in the settle phase the face must be
  unseen for the whole ABSENCE window. A spurious detection resets it, which is
  the conservative direction — the robot simply waits longer.

THE INTRODUCTION'S GATE
  There is ONE moment when this introduction is wanted: stage 1 of a started
  session, WAITING for the participant to begin the first timed session. So
  /run/face/session.json must say all three of
      {"phase": "running", "stage": 1, "step": "waiting"}
  and a missing, malformed or partial file does not count.

  WHY IT IS NOT SIMPLY "a session is running". The introduction is 58 seconds and
  it is STAGE 1's. Stages 2-4 each have their own short announcement from the
  conductor, and a person in front of the robot during stage 3 who heard the long
  introduction again would have been told the session was starting over. The
  narrower gate also stops the introduction replaying after every session ends,
  which the old phase-only test allowed.

  WHY STANDING STILL IS NOT ENOUGH: while the gate is shut the state machine is
  HELD at its initial state on every tick, not merely ignored. Ignoring it would
  let presence accumulate unseen: a person standing in front of a standby robot
  for a minute would have banked far more than the PRESENCE window, and would be
  greeted the instant stage 1 started, which is exactly the surprise the gate
  exists to prevent.

  AND WHY IT ALSO READS THE ATTEMPT COUNT. Stage, step and phase together cannot
  tell "someone has just arrived" from "someone is reading the timeline of the
  session they just missed": a stage the participant has already attempted also
  sits in `waiting`, with the stats screen up and the word expected again. The
  introduction belongs to the first of those only, and the attempt count is the
  difference between them. A conductor too old to publish one cannot produce that
  second state either - a failed session used to restart itself, so `waiting` at
  stage 1 existed only at the top of a run - and falls back to the wider gate,
  exactly as a missing `subject` falls back to `has_face` above.

  What counts toward the presence figure is DETECTED time, not wall-clock time.
  A tolerated gap pauses the count instead of extending it, so the greeting
  comes after PRESENCE seconds of actually being seen (plus whatever gaps were
  tolerated). Counting wall time instead has a specific failure: someone looks
  up for nine seconds and walks away, and the threshold trips a moment later
  while the dropout grace is still open — to an empty room.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_STATUS = "/run/face/gaze-status.json"
# Which status field means "someone is here". See the module docstring.
DEFAULT_FACE_KEY = "subject"
DEFAULT_MOUTH = "/run/face/mouth.json"
# The session the conductor publishes, and the one shape that asks for this
# introduction. See THE INTRODUCTION'S GATE.
DEFAULT_SESSION = "/run/face/session.json"
PHASE_RUNNING = "running"
STAGE_WAITING = "waiting"
DEFAULT_SINK = "alsa_output.platform-3510000.hda.hdmi-stereo"

# gaze publishes ~12x/s, so a gap of whole seconds means it is dead or stalled.
# Treat its data as unusable rather than acting on a frozen "has_face".
STATUS_STALE = 3.0

# lipsync.py exits with this when another player already owns the mouth.
EX_TEMPFAIL = 75


def log(msg, *args):
    """One line, prefix-tagged, to stdout — systemd puts it in the journal."""
    sys.stdout.write("greeter: " + (msg % args if args else msg) + "\n")
    sys.stdout.flush()


def read_conf(path):
    """display.conf is a shell fragment of KEY=VALUE lines.

    Read it, never source it: this process must not execute the config file.
    """
    conf = {}
    try:
        text = path.read_text()
    except OSError:
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip().strip('"').strip("'")
    return conf


def display_settings(sink=None, lead=None):
    """The display's sink and lip-sync offset, read NOW.

    Re-read for every playback rather than once at start-up. The offset is a
    property of the DISPLAY - what the projector's own audio buffering costs, minus
    the face's visual latency - which makes it the value most likely to be edited
    on a robot that is already running. Reading it once meant an edit took effect
    at the next restart, which is indistinguishable from "the tuning did not work",
    and it is how a freshly tuned offset gets tested against the OLD one.

    Explicit --sink / --lead arguments still win: those are for one-off trials.
    """
    conf = read_conf(HERE / "display.conf")
    resolved = sink or conf.get("SINK") or DEFAULT_SINK
    if lead is not None:
        return resolved, lead
    try:
        return resolved, float(conf.get("LEAD", "0.0"))
    except ValueError:
        log("display.conf has an unreadable LEAD — falling back to 0.0")
        return resolved, 0.0


def read_face(status_path, face_key=DEFAULT_FACE_KEY):
    """Return True / False / None for the face reading, where None means UNKNOWN.

    By default this reads `subject` - "someone is actually in front of the robot" -
    not `has_face`, which only says a face-shaped thing was detected somewhere in
    view. See the module docstring for why that distinction is the whole point.

    UNKNOWN covers a missing file, a stale one (gaze died or stalled), a
    malformed one, or a missing field. gaze.py writes its status with a plain
    open()+json.dump rather than an atomic replace, so a reader CAN catch a
    partial file; that must be a shrug, not a crash.

    Unknown is deliberately NOT the same as "no face": the caller keeps its
    last_seen timestamp, so a brief gap in the data is absorbed by the same
    DROPOUT tolerance as a missed frame.
    """
    try:
        age = time.time() - os.path.getmtime(status_path)
    except OSError:
        return None
    if age > STATUS_STALE:
        return None
    try:
        with open(status_path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    value = data.get(face_key)
    if not isinstance(value, bool) and face_key != "has_face":
        # An older gaze.py, or a frame written before the field existed.
        value = data.get("has_face")
    return value if isinstance(value, bool) else None


class Greeter:
    """The decision state machine, kept separate from I/O so it can be tested.

    step(now, has_face) is pure: give it a monotonic timestamp and a tri-state
    face reading, and it returns "play" on exactly the tick the introduction
    should start, or None otherwise.
    """

    WAIT = "wait"        # waiting for a face to hold long enough
    SETTLE = "settle"    # intro done; waiting for the room to empty again

    # Cap on how much presence a single tick may credit. If this process is
    # starved, or the machine suspends, one dt can be enormous; crediting it
    # would record presence over a stretch the robot never actually observed.
    MAX_TICK = 1.0

    def __init__(self, presence, absence, dropout):
        self.presence = presence
        self.absence = absence
        self.dropout = dropout
        self.phase = self.WAIT
        self.seen_for = 0.0          # DETECTED seconds in the current unbroken run
        self.last_seen = 0.0
        self.prev = None             # previous tick's timestamp

    def step(self, now, has_face):
        dt = 0.0 if self.prev is None else min(now - self.prev, self.MAX_TICK)
        self.prev = now

        if has_face is True:
            if self.seen_for == 0.0:
                log("face seen — timing presence (needs %.0fs)", self.presence)
            self.last_seen = now
            # Only DETECTED time counts, never wall-clock time. Crediting wall
            # time lets a nine-second look and a walk-away cross the threshold a
            # moment later, while the dropout grace is still open - and the
            # robot introduces itself to an empty room.
            self.seen_for += dt
        elif self.seen_for > 0.0 and (now - self.last_seen) > self.dropout:
            # Only worth saying while a count is actually running. In the settle
            # phase there is no count to restart, and announcing one there reads
            # as the 20s gate misbehaving - exactly the wrong signal to give an
            # operator who is watching the log to calibrate these thresholds.
            if self.phase == self.WAIT:
                log("presence broken (%.1fs without a face) — restarting the count",
                    now - self.last_seen)
            self.seen_for = 0.0

        if self.phase == self.WAIT:
            if self.seen_for >= self.presence:
                self.phase = self.SETTLE
                return "play"
            return None

        # SETTLE: the face must be gone for the whole window, uninterrupted.
        if (now - self.last_seen) >= self.absence:
            log("no face for %.0fs — resetting, ready for the next person", self.absence)
            self.phase = self.WAIT
            self.seen_for = 0.0
        return None


def intro_holdoff(path=DEFAULT_SESSION):
    """Why the introduction is NOT wanted right now: "" when it is.

    True only while stage 1 is waiting for the participant to begin — and only on
    that stage's FIRST attempt. Anything else — no file, malformed, a different
    stage, an unreadable session — means the greeter's job is to stay quiet. Same
    tri-state discipline as read_face: an unusable input is not a yes. See THE
    INTRODUCTION'S GATE.

    The stage and step are read as strings rather than trusted by type, because
    this runs for hours against a file another process owns: a hand-edited file, or
    a value JSON happens to parse into something numeric, must fall out here as
    "not wanted" rather than crash the unit or be interpreted loosely.

    The reasons are returned rather than just a bool because the operator reads
    them: "held off" alone cannot say whether the robot is between stages, or
    waiting for a participant who has a stats screen in front of them.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "no readable session file"
    if not isinstance(data, dict):
        return "the session file is not an object"
    if data.get("phase") != PHASE_RUNNING:
        return "phase is %r" % (data.get("phase"),)
    if data.get("stage") != 1:
        return "stage is %r, not 1" % (data.get("stage"),)
    if data.get("step") != STAGE_WAITING:
        return "step is %r, not waiting" % (data.get("step"),)
    attempt = data.get("attempt") if "attempt" in data else 1
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        # A count that is PRESENT but unreadable is not a yes: this file has one
        # writer, so that means a hand edit, and guessing which run it meant is how
        # a 58 second introduction ends up playing at the wrong moment. An ABSENT
        # count is different - it means a conductor older than the retry that waits
        # for the word, which cannot leave stage 1 waiting after a miss, so the
        # wider gate is still right for it. The same fallback shape as `subject`.
        return "the attempt count is unreadable (%r)" % (attempt,)
    if attempt != 1:
        return "attempt %d of stage 1 has already been measured" % attempt
    # ... and the run's own tally, because a stage that PASSED waits in exactly this
    # state too, still on attempt 1: the participant is reading the stats screen of
    # the session they just finished, and the run moves on when they say the word.
    # A conductor older than this field advanced on the spot, so an absent count
    # still means "nothing has happened yet" for it.
    done = data.get("sessions_done") if "sessions_done" in data else 0
    if isinstance(done, bool) or not isinstance(done, int):
        return "the measured-session count is unreadable (%r)" % (done,)
    if done != 0:
        return "this run has already measured a session"
    return ""


def intro_wanted(path=DEFAULT_SESSION):
    """Is the introduction wanted? The gate and its reasoning live in intro_holdoff."""
    return intro_holdoff(path) == ""


def hold(g):
    """Hold the machine at its initial state: called every tick that is not running.

    Both fields matter. `seen_for = 0.0` discards presence banked during standby,
    and `prev = None` makes the first tick after a session starts credit no time
    at all — the gap across standby is time the robot did not observe, and
    crediting it (MAX_TICK caps it at 1s, it does not remove it) would start every
    session with a free head start on the presence window.
    """
    g.phase = Greeter.WAIT
    g.seen_for = 0.0
    g.prev = None


def selftest():
    """Drive the machine with synthetic inputs and assert the outcomes.

    This is the part most likely to be subtly wrong, and the part that is
    impossible to eyeball on a real camera in real time. Runs in under a second.
    """
    def drive(g, events):
        """events: list of (duration_s, has_face). Returns list of transitions."""
        now = 1000.0
        played = []
        for dur, has in events:
            end = now + dur
            while now < end:
                now += 0.1
                if g.step(now, has) == "play":
                    played.append(round(now - 1000.0, 2))
            now = end
        return played

    failures = []
    cases = [0]

    def check(name, got, want):
        cases[0] += 1
        ok = got == want
        print("  %-56s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    def check_times(name, got, want, tol=0.3):
        """Compare play times with a tolerance.

        Exact equality is wrong here: the harness advances in 0.1s ticks while
        present_since is stamped on the first tick, so every absolute time is one
        tick late. The PROPERTY under test is when it fires relative to first
        detection, not the floating-point tick that lands on it.
        """
        cases[0] += 1
        ok = len(got) == len(want) and all(
            abs(a - b) <= tol for a, b in zip(got, want))
        print("  %-56s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    # 1. A steady face triggers once, PRESENCE seconds after it is first seen.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("steady face -> plays once, ~10s in",
                drive(g, [(12, True)]), [10.0])

    # 2. A passer-by does not trigger at all.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("face 4s then gone -> silent",
                drive(g, [(4, True), (30, False)]), [])

    # 3. Just under the threshold stays silent.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("face 9s then gone -> silent",
                drive(g, [(9, True), (30, False)]), [])

    # 4. A dropout INSIDE the grace must not RESTART the count, but nor does the
    #    gap count as presence: 5s + 0.8s gap + 5.1s seen = 10.1s detected, so
    #    it fires ~10.9s. NOT ~10s (that would count the gap) and NOT ~15.8s
    #    (that would be a restart).
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("0.8s dropout inside grace -> not restarted, delayed by the gap",
                drive(g, [(5, True), (0.8, False), (6, True)]), [10.9])

    # 5. A dropout BEYOND the grace restarts the count: fires 10s after the face
    #    RETURNS (at 7.5s), i.e. ~17.5s, not ~10s.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("2.5s dropout beyond grace -> count restarts, fires ~17.5s",
                drive(g, [(5, True), (2.5, False), (11, True)]), [17.5])

    # 6. After greeding, the robot stays quiet while the person REMAINS.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("person stays afterwards -> exactly one greeting",
                drive(g, [(12, True), (40, True)]), [10.0])

    # 7. After a FULL absence window the robot rearms and greets the next person,
    #    again ~10s after they appear (at 37s) -> ~47s.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("leave 25s, return -> greets the next person ~47s",
                drive(g, [(12, True), (25, False), (12, True)]), [10.0, 47.0])

    # 8. Leaving for LESS than the absence window must NOT rearm: the person
    #    returning is still the same visit, so no second greeting.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("leave only 10s then return -> no second greeting",
                drive(g, [(12, True), (10, False), (20, True)]), [10.0])

    # 9. Unknown readings (gaze dead, or a partially-written status file) must
    #    never be read as a face, and never trigger.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    check_times("no usable data -> never plays",
                drive(g, [(60, None)]), [])

    # 10-18. The gate on the introduction. It opens for ONE state — stage 1 of a
    #    started session, waiting for the participant — and an unusable or partial
    #    session file must not open it.
    with tempfile.TemporaryDirectory() as d:
        session = pathlib.Path(d) / "session.json"

        def wrote(text):
            session.write_text(text)
            return intro_wanted(str(session))

        waiting1 = '{"phase": "running", "stage": 1, "stages": 4, "step": "waiting"}'
        check("gate: no file -> held off", intro_wanted(str(session)), False)
        # No attempt count at all: an older conductor, whose flow cannot leave stage
        # 1 waiting after a miss. The wider gate is what that one needs.
        check("gate: stage 1 waiting, no attempt count -> armed", wrote(waiting1), True)
        check("gate: stage 1 waiting on the first attempt -> armed",
              wrote(waiting1.replace('"stages": 4', '"stages": 4, "attempt": 1')), True)
        # The one this gate was extended for: after a miss the stage waits again with
        # the stats screen up, and the introduction must not replay over it.
        check("gate: stage 1 waiting after a missed attempt -> held off",
              wrote(waiting1.replace('"stages": 4', '"stages": 4, "attempt": 2')), False)
        check("gate: an unreadable attempt count -> held off",
              wrote(waiting1.replace('"stages": 4', '"stages": 4, "attempt": "2"')), False)
        check("gate: the attempt count as a bool -> held off",
              wrote(waiting1.replace('"stages": 4', '"stages": 4, "attempt": true')), False)
        # The reason string is what an operator reads in the journal, so it is part
        # of the gate's contract too, not just a debugging aid.
        session.write_text(waiting1.replace('"stages": 4', '"stages": 4, "attempt": 2'))
        check("gate: and the reason names the attempt already measured",
              intro_holdoff(str(session)),
              "attempt 2 of stage 1 has already been measured")
        session.write_text('{"phase": "standby"}')
        check("gate: a standby reason is the phase, not a missing file",
              intro_holdoff(str(session)), "phase is 'standby'")
        check("gate: standby -> held off", wrote('{"phase": "standby"}'), False)
        check("gate: malformed -> held off", wrote("{not json"), False)
        check("gate: an unknown phase name -> held off",
              wrote('{"phase": "stage2"}'), False)
        # The three that a phase-only test used to let through, each of which would
        # have played 58 seconds of stage 1's introduction at the wrong time.
        check("gate: a running session on stage 2 -> held off",
              wrote(waiting1.replace('"stage": 1', '"stage": 2')), False)
        check("gate: stage 1 but mid-session -> held off",
              wrote(waiting1.replace('"waiting"', '"session"')), False)
        check("gate: stage 1 with the step missing -> held off",
              wrote('{"phase": "running", "stage": 1}'), False)
        check("gate: the success screen -> held off",
              wrote('{"phase": "running", "stage": 4, "step": "success"}'), False)

    # 15. Holding during standby must CLEAR banked presence, not just hide it.
    #     Five seconds of presence before the session started must not count
    #     towards the ten after it: with the hold removed this fires at ~5s, and a
    #     person who stood in front of a standby robot gets greeted instantly.
    g = Greeter(presence=10, absence=20, dropout=1.5)
    drive(g, [(5, True)])
    hold(g)
    check_times("standby hold clears banked presence -> fires 10s after the start",
                drive(g, [(12, True)]), [10.0])

    print()
    if failures:
        print("  SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    print("  SELFTEST PASSED (%d cases)" % cases[0])
    return 0


def play(clip, sink, lead, mouth, dry_run):
    """Start the introduction and wait for it to finish.

    Returns True if the intro actually played, False if it could not start —
    in which case the caller re-arms rather than assuming it happened.

    `sink` and `lead` are OVERRIDES, not values: both are re-resolved from
    display.conf here, so an offset tuned while the robot is running takes effect
    on the next clip instead of on the next service restart.
    """
    sink, lead = display_settings(sink, lead)
    if dry_run:
        log("DRY RUN — would play %s (--lead %s, sink %s)", clip.name, lead, sink)
        time.sleep(0.5)
        return True

    if not os.path.isdir(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")):
        log("REFUSING to play: XDG_RUNTIME_DIR is missing, so audio cannot connect")
        return False

    cmd = [sys.executable, str(HERE / "lipsync.py"), str(clip),
           "--sink", sink, "--lead", str(lead), "--mouth", str(mouth)]
    log("playing: %s", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == EX_TEMPFAIL:
        log("another player held the mouth — skipped, not attempted twice")
        return False
    if rc != 0:
        log("playback exited %d", rc)
        return False
    return True


def run(args):
    # Resolved here for the start-up report, and re-read for every playback (see
    # display_settings). This line is how an operator knows what the robot is
    # using NOW: a value that is written but not applied used to look identical to
    # a value that was never written.
    sink, lead = display_settings(args.sink, args.lead)

    clip = pathlib.Path(args.clip)
    if not clip.is_file() and not clip.is_absolute():
        # The unit starts in the voice directory, but a manual run from anywhere
        # should still find the library clip it names.
        clip = HERE / clip
    if not clip.is_file():
        log("FATAL: no such clip: %s", clip)
        return 2

    log("clip %s | presence %.0fs | absence %.0fs | dropout %.1fs | lead %s",
        clip.name, args.presence, args.absence, args.dropout, lead)
    log("reading faces from %s (field: %s)", args.status, args.face_key)

    g = Greeter(args.presence, args.absence, args.dropout)
    stale_since = None
    running = None      # None until the first read, so the first state is logged

    while True:
        now = time.monotonic()

        # Stage 1 has to be up and waiting before anyone is greeted — on its first
        # attempt. See THE INTRODUCTION'S GATE for why presence must not merely be
        # ignored here.
        holdoff = intro_holdoff(args.session)
        wanted = not holdoff
        if wanted != running:
            if wanted:
                log("stage 1 is waiting on its first attempt — greeting is armed")
            else:
                log("greeting held off: %s", holdoff)
            running = wanted
        if not wanted:
            hold(g)
            stale_since = None      # nothing has gone stale if we are not watching
            time.sleep(args.poll)
            continue

        has_face = read_face(args.status, args.face_key)

        # Staleness is worth saying out loud, but only once per episode: a unit
        # that logs "gaze is dead" five times a second is its own outage.
        if has_face is None:
            if stale_since is None:
                stale_since = now
            elif now - stale_since > STATUS_STALE:
                log("no usable face data for %.0fs — gaze may be down; not greeting",
                    now - stale_since)
                stale_since = now   # re-log at most once per STATUS_STALE
        else:
            stale_since = None

        if g.step(now, has_face) == "play":
            if play(clip, args.sink, args.lead, args.mouth, args.dry_run):
                log("intro finished — waiting for %.0fs of absence", args.absence)
            else:
                g.phase = Greeter.WAIT
                g.seen_for = 0.0
                log("intro did not play — re-arming, presence must build again")

        time.sleep(args.poll)


def main():
    ap = argparse.ArgumentParser(
        description="Play the introduction when someone settles in front of the robot.")
    ap.add_argument("--clip", help="the clip to play (path to a .wav)")
    ap.add_argument("--status", default=DEFAULT_STATUS,
                    help="gaze status file (default %(default)s)")
    ap.add_argument("--face-key", default=DEFAULT_FACE_KEY,
                    choices=("subject", "has_face"),
                    help="which status field counts as 'someone is here' "
                         "(default %(default)s). `has_face` restores the old "
                         "behaviour where any detected face counted")
    ap.add_argument("--mouth", default=DEFAULT_MOUTH,
                    help="face lip-sync file (default %(default)s)")
    ap.add_argument("--session", default=os.environ.get("FACE_SESSION", DEFAULT_SESSION),
                    help="the session file the conductor writes; the greeter stays "
                         "quiet unless it says stage 1 is waiting (default "
                         "%(default)s)")
    ap.add_argument("--presence", type=float, default=10.0,
                    help="seconds a face must hold before greeting (default %(default)s)")
    ap.add_argument("--absence", type=float, default=20.0,
                    help="seconds without a face before re-arming (default %(default)s)")
    ap.add_argument("--dropout", type=float, default=1.5,
                    help="dropout tolerated inside a presence run (default %(default)s)")
    ap.add_argument("--poll", type=float, default=0.1,
                    help="seconds between face readings (default %(default)s)")
    ap.add_argument("--sink", default=None,
                    help="PulseAudio sink; default from display.conf")
    ap.add_argument("--lead", type=float, default=None,
                    help="lip-sync offset; default from display.conf")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and log, but never play audio")
    ap.add_argument("--selftest", action="store_true",
                    help="run the state machine over synthetic inputs and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.clip:
        ap.error("--clip is required (or use --selftest)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
