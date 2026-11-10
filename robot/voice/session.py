#!/usr/bin/env python3
"""The session conductor: the run-through, from standby to Success.

WHAT THIS OWNS
  Everything that happens over SECONDS: which stage is running, the 60 s gaze
  session inside a stage, the 30 s success screen, what the screen says while all
  that happens, and the clips the robot speaks between stages.

WHAT IT DELIBERATELY DOES NOT OWN
  * Hearing. The listener publishes a confirmed command; it does not know what a
    stage is. Keeping the script out of the audio process is what stops the
    listener growing one.
  * Drawing. The face draws what this file says and nothing else.
  * Greeting on presence. The greeter plays stage 1's real introduction, because
    that one waits for someone to settle in front of the robot rather than
    following a timer. It reads the same session file and stays quiet unless the
    session is on stage 1 and waiting.
  * Playing real audio: greeter.play() is reused, so there is one player, one
    lead offset and one lock rather than two implementations that drift.

THE ONE FILE IT WRITES
  /run/face/session.json:
      {"phase": "standby"}                                 nothing running
      {"phase": "running", "stage": 1..4, "stages": 4,
       "step": "waiting" | "session" | "success",
       "deadline": <monotonic>,                            session/success only
       "screen": {"kind": "face", "style": "cartoon"}      what to draw
                 {"kind": "label", "text": "Stage 3"}
                 {"kind": "success"},
       "gaze": {"progress": 0..1, "looking": bool,         session step only:
                "looked": <seconds>, "target": <seconds>}} the gaze bar

  Written ONLY here — one writer per file, so the face and the greeter can
  re-read on change without racing anybody. Written only when it CHANGES, so a
  steady screen costs no writes at all: the face computes the countdown from the
  deadline rather than being fed a tick. The `gaze` block is the one exception,
  and it is a real one: the bar moves while a session runs, so the file is
  rewritten at the poll rate (~10x/s) for as long as it is moving. Readers that
  LOG on a change must therefore key on what the screen SAYS rather than on the
  file having changed, or a moving bar writes a journal line ten times a second.

THE RUN, as specified
  standby
    "system command start" + pass phrase   -> stage 1 face, waiting
    "system command standby" + pass phrase -> standby, from ANY state (the
             operator's reset: stage, session, Success, all abandoned)
    stage 1: the greeter plays the real introduction when someone settles in
             in front of the robot
    the PARTICIPANT says the stage word    -> 60 s gaze session
  60 s                                     -> stage 2 face (rung 2), announced
    ... the same for stages 3 and 4 ...
  60 s of stage 4                          -> Success for 30 s -> standby

  THE GAZE BAR IS THE GATE. The stage still ends on its timer - 60 s is 60 s, and
  the gaze never extends it - but what happens AT the end now depends on the
  verdict. Reaching the target (50 s of the 60 s session; it is a FRACTION of
  whatever length the session is) moves the run to the next stage. NOT reaching it
  repeats the same stage's session, with the bar reset, until it is reached:
  nothing advances without a pass. So a run CAN be longer than four sessions when
  a participant needs the practice, which is the point of a training device, and
  `--max-attempts N` puts a ceiling on it for a run that has to end by itself.

  The verdict is read from the SAME accumulator that drew the bar, so a
  participant who watched it fill cannot be told by the log that it did not - the
  drawn number and the deciding number are one object, not two readings of a
  rule. The one case the gate must not be trusted with is a robot that cannot see
  at all (gaze down, camera on another rig, the subject gate rejecting everyone):
  no samples means no bar, which reads as a participant who never looked, so that
  is reported in its own words rather than repeated silently. See GazeFill below
  for the target.

  The participant's word is spoken alone, with no prefix and no confirmation. It
  is defined and argued in keywords.conf, under WORD_TRIGGER.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import signal
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# The player, the config reader and the sink default are the greeter's; importing
# them rather than re-implementing them is what keeps ONE player in the robot.
import greeter  # noqa: E402  (sibling module)

DEFAULT_SESSION = "/run/face/session.json"
DEFAULT_COMMAND = "/run/face/command.json"
DEFAULT_STATUS = "/run/face/gaze-status.json"
DEFAULT_LOG = "/tmp/session-log.txt"
DEFAULT_MOUTH = "/run/face/mouth.json"
DEFAULT_SINK = greeter.DEFAULT_SINK

SESSION_SECONDS = 60.0
SUCCESS_SECONDS = 30.0
# How long the new screen is given to reach the projector before a clip starts
# talking about it. One frame is enough in principle (the kiosk re-reads the file
# every frame), so this is only a handover margin - but it is the difference
# between "Owen appears and then speaks" and "Owen speaks over Pip's face".
ANNOUNCE_SETTLE = 0.4

# The verbs the listener publishes. See its COMMAND EVENTS docstring.
START_COMMAND = "start"
STAGE_WORD_COMMAND = "begin"
# The operator's reset: abandon whatever is running and go back to the power-on
# standby. Not a participant's word and not a stage: it exists so a stage can be
# tested without walking all four, and it is the only verb that can interrupt.
STANDBY_COMMAND = "standby"
# The operator's jump: put STAGE 2 up directly, without walking stage 1. It is a
# numbered phrase rather than an argument parser, which is what keywords.conf asks
# for ("add numbered trigger phrases rather than opening the vocabulary up"): the
# grammar stays closed and the conductor only ever sees whole phrases it knows.
# Spelled "two" because the decoder's vocabulary has no digit tokens - see the
# note beside listener.py's copy of this constant.
SKIP_STAGE2_COMMAND = "skip stage two"

# THE STAGES. One entry per stage, in order, and the truth about what each one
# shows. Stage 1 is the only one that does NOT announce itself: its introduction
# is the long clip the greeter plays when a person settles in front of the robot,
# so an announcement here would talk over it.
#
# `announce_skip` is the SECOND introduction a stage can have, and it exists
# because arriving at a stage two ways is two different situations: someone who
# walked stage 1 has just met Pip and been promised a more realistic face, while
# someone who skipped straight here has met nobody. Stage 2 therefore has one clip
# that congratulates and one that introduces from cold. The state machine records
# how the stage was ENTERED (Session.entry) and announce_name() picks between
# them; the events it returns are unchanged, so a player never has to know.
#
# ALL FOUR STAGES NOW SHOW A FACE. Stages 3 and 4 were label screens ("Stage 3",
# "Stage 4") while their rungs had no renderer; they now show Milo in the
# dimensional register and Elliot, the image puppet. The labels are gone rather
# than kept as a fallback: a placeholder that could pass for a finished screen is
# worse than one that admits what it is, and this one no longer needs to.
#
# EVERY STAGE NOW SPEAKS IN ITS OWN VOICE. Milo's three clips landed 5 November and
# Elliot's the same day, so the recorded decision that Owen narrates stages 3 and 4
# "until they have faces and voices of their own" is discharged: no stage borrows
# another person's voice any more, and no line is in one voice under another face.
#
# STAGE 4 ALSO ENDS THE RUN. Its pass line says the training is complete and that
# there are no more stages, which makes the stats screen that follows the run's last
# screen rather than a pause before one: the word that would have advanced to the
# next stage ends the run instead. See _finish().
STAGES = (
    # "pass"/"retry" are the SESSION-END lines: what the face that just finished the
    # session says, and EVERY STAGE HAS BOTH as of 5 November. A pass line names the
    # friend who comes next, so each one had to wait for that friend to exist: Pip's
    # names Owen, Owen's names Milo, Milo's names Elliot, and Elliot's closes the run.
    # None is still a legal value - a stage with no line is silent rather than wrong -
    # but nothing uses it now, and there is a self-test that says so.
    {"screen": {"kind": "face", "style": "cartoon"}, "announce": None,
     "pass": "rung1-cartoon-pass.wav", "retry": "rung1-cartoon-retry.wav"},
    {"screen": {"kind": "face", "style": "illustrated"},
     "announce": "stage2-intro.wav", "announce_skip": "stage2-skip.wav",
     "pass": "stage2-pass.wav", "retry": "stage2-retry.wav"},
    # rung 3: Milo, the dimensional register (an image puppet since 5 November)
    {"screen": {"kind": "face", "style": "dimensional"},
     "announce": "rung3-warm-conversational-intro.wav",
     "pass": "rung3-warm-conversational-pass.wav",
     "retry": "rung3-warm-conversational-retry.wav"},
    # rung 4: Elliot, the image puppet (a still with its eyes and mouth driven)
    {"screen": {"kind": "face", "style": "portrait"},
     "announce": "rung4-natural-human-intro.wav",
     "pass": "rung4-natural-human-pass.wav",
     "retry": "rung4-natural-human-retry.wav"},
)

CLIP_DIR = HERE / "clips"

EX_OK, EX_CONFIG = 0, 2

_STOP = {"signum": None}


def log(msg, *args):
    """One line, prefix-tagged. systemd puts stdout in the journal."""
    sys.stdout.write("session: " + (msg % args if args else msg) + "\n")
    sys.stdout.flush()


def log_line(path, text):
    """Append one line to the log file, flushed.

    journald on this board is volatile, so a file in /tmp is the only record that
    survives to be read after the fact. Same pattern, and the same reason, as the
    listener's /tmp/voice-detection.log.
    """
    try:
        with open(path, "a") as fh:
            fh.write("%s  %s\n" % (time.strftime("%FT%T%z"), text))
            fh.flush()
    except OSError as exc:
        print("session: could not append to %s: %s" % (path, exc), file=sys.stderr)


# --------------------------------------------------------------------------
# The run-through, as a state machine
# --------------------------------------------------------------------------
class Session:
    """Which stage is running and what happens next. Pure, so it is testable.

    step(now, command) returns the events for one tick and never touches a file,
    a clock or a speaker. That is what lets the whole run-through be driven with a
    synthetic clock in --selftest, including the parts that take ninety seconds in
    the room.
    """

    WAITING = "waiting"     # on a stage's screen, before the participant's word
    RUNNING = "session"     # the timed gaze session inside a stage
    # The face is saying its session-end line. A step of its own because the two
    # halves of a retry are different screens: the face talks first (the participant's
    # companion explaining), and the stats screen appears only once the voice stops.
    SPEAKING = "speaking"
    SUCCESS = "success"     # the success screen, counting down to standby

    # Events step() can return. Each is (kind, payload).
    #   ("stage", n)        a stage's screen is up and the word is expected
    #   ("session", n)      the timed session for stage n has begun
    #   ("session_end", n)  it finished; the gaze summary belongs here
    #   ("pass", (n, k))    it finished WITH a pass and stage n's face has a line to
    #                       say before the run moves on. k is the stage that comes
    #                       next, or None if n was the last. Nothing advances until
    #                       spoke() reports the voice has stopped.
    #   ("retry", (n, a))   it finished WITHOUT a pass: stage n's face explains, the
    #                       session will be repeated, and a was the attempt that
    #                       failed. The stage then waits for the word again.
    #   ("failed", n)       it finished without a pass for the last time allowed;
    #                       the run ends rather than advancing
    #   ("announce", n)     play stage n's announcement clip (which of the two is
    #                       announce_name()'s answer, not this event's business)
    #   ("left", where)     `skip stage 2` abandoned a run that was at `where`
    #   ("standby", where)  `standby` abandoned a run that was at `where`
    #   ("done", None)      the run finished; back to standby
    #   ("ignored", verb)   a command that does not apply in this state

    def __init__(self, stages, session_secs=SESSION_SECONDS,
                 success_secs=SUCCESS_SECONDS, max_attempts=0):
        self.stages = tuple(stages)
        self.session_secs = session_secs
        self.success_secs = success_secs
        # How many times one stage's session may be repeated before the run gives
        # up. 0 = no ceiling: retry until the pass happens or the operator says
        # `standby`. The cap exists because the gate can be held forever by a
        # robot that cannot see, and an operator who wants an unattended run needs
        # a way for it to end by itself.
        self.max_attempts = int(max_attempts)
        self.reset()

    def reset(self):
        """Nobody has started anything: the power-on state."""
        self.stage = None
        # Named stage_step because step() is the method: an attribute called
        # `step` would overwrite the state machine the first time it was set.
        self.stage_step = None
        self.deadline = None
        # WHICH attempt at the current stage's session this is, 1-based. It is run
        # state, not a counter for logging: the gate reads it to know whether the
        # ceiling has been reached. Invariant: every path that puts a stage up
        # starts it at 1, so it never carries an attempt from the last stage.
        self.attempt = 1
        # HOW MANY timed sessions this RUN has measured, pass or fail. Run state, not
        # per-stage: it answers "has anything happened yet?", which is the question the
        # greeter's gate asks. An attempt count cannot answer it - a stage that PASSES
        # is still on attempt 1 while it waits for the word - so the run keeps its own
        # tally. Zero at power-on and after every reset.
        self.sessions_done = 0
        # THE SESSION'S TIMELINE, as spans of [start, end, looking] in seconds from the
        # session's start. It is what the stats screen draws, and it lives here rather
        # than in the conductor's own memory because it decides a SCREEN: a stage waits
        # for the word with the stats up, so "which screen" and "which spans" have to
        # be answered by the same object. Set whether the session passed or failed -
        # the participant gets to look at either.
        self.stats = None
        # A PASSED stage whose face still has to speak before the break starts. The
        # stage number, or None. Released by spoke(), acted on by the next step().
        self.pending = None
        # TRUE while a PASSED stage is waiting for the word: the run is parked on the
        # stats screen and the word moves it ON rather than starting another session.
        # Without it, `waiting` would be ambiguous between "ready to run again" and
        # "finished, ready to move on", and the same word would have to mean both.
        self.advance_on_word = False
        # HOW the current stage was reached: None in standby, "start" for stage 1,
        # "advance" for finishing the previous session, "skip" for the operator's
        # jump. It is what chooses between a stage's two introductions, and it is
        # state about the RUN rather than about the player, so it lives here.
        self.entry = None

    @property
    def total(self):
        return len(self.stages)

    def describe(self):
        if self.stage is None:
            return "standby"
        return "stage %d/%d, %s" % (self.stage, self.total, self.stage_step)

    def announce_name(self, stage):
        """The announcement clip for a stage, or None if it has none.

        The variant is chosen from how the stage was ENTERED, not from which
        stage it is: stage 2 reached by finishing stage 1 gets the clip that
        congratulates and picks up where Pip left off, while stage 2 reached by
        the operator's jump gets the one that introduces itself from cold. A
        stage with no `announce_skip` has one introduction whatever the entry -
        there is only one thing to say.
        """
        entry = self.stages[stage - 1]
        if self.entry == "skip" and entry.get("announce_skip"):
            return entry["announce_skip"]
        return entry.get("announce")

    def screen(self):
        """The session file's contents for the current state.

        Absence of a stage is what "standby" MEANS, so standby is a phase with no
        stage, step, deadline or screen — not a screen of its own. A reader that
        only looks at `phase` and a reader that looks at `screen` both get an
        answer they can act on.
        """
        if self.stage is None:
            return {"phase": "standby"}
        if self.stage_step == self.SUCCESS:
            screen = {"kind": "success"}
        elif self.stage_step == self.WAITING and self.stats:
            # A failed session's stage waits for the word with the STATS screen up
            # rather than the face: it is the answer to "why are we doing this one
            # again", and it stays until the participant starts the next attempt
            # themselves. Empty spans are not a screen - an empty timeline is a claim
            # that they never looked, and a session with no samples cannot support it.
            screen = {"kind": "stats", "spans": self.stats}
        else:
            screen = self.stages[self.stage - 1]["screen"]
        # "seconds" is the LENGTH of the step, which every reader that draws a
        # countdown needs: the face draws a fraction of it and cannot derive it from
        # a deadline alone.
        secs = (self.success_secs if self.stage_step == self.SUCCESS
                else self.session_secs)
        # "attempt" and "sessions_done" are published for the GREETER, which must not
        # mistake a pause for a fresh arrival: after a miss AND after a pass this stage
        # sits in `waiting` with the stats screen up, and the introduction belongs to a
        # run's first attempt with nothing measured yet. Two fields because the two
        # ways to be a false arrival arrived at different times; see intro_holdoff().
        # Nothing else needs them - the listener's word gate turns on `waiting`.
        return {"phase": "running", "stage": self.stage, "stages": self.total,
                "step": self.stage_step, "deadline": self.deadline,
                "seconds": secs, "attempt": self.attempt,
                "sessions_done": self.sessions_done, "screen": screen}

    def next_stage(self, stage):
        """The stage after this one, or None when this one is the last."""
        return stage + 1 if stage < self.total else None

    def pass_name(self, stage):
        """The clip the face that PASSED a stage says, or None if it has no line."""
        return self.stages[stage - 1].get("pass")

    def retry_name(self, stage):
        """The clip the face that did NOT pass a stage says, or None."""
        return self.stages[stage - 1].get("retry")

    def spoke(self):
        """The voice has stopped: whatever was waiting on it happens now.

        Called by the conductor when the clip returns - and immediately when there is
        no clip, and from a `finally`, so a player that fails cannot leave the run
        waiting for a voice that has already stopped. The machine cannot work this out
        for itself: it has no idea how long a clip runs, and guessing with a constant
        would turn a MISSING clip into a pause with nothing in it.

        The consequence is deferred to the next tick rather than emitted here, so that
        every event in a run still comes out of step(): one source, in order, whether
        it was a clock or a voice that moved the run on.
        """
        if self.stage_step != self.SPEAKING:
            return False
        self.stage_step = self.WAITING
        self.deadline = None
        return True

    def _finish(self, finished, now, events, from_verdict=False):
        """Move on from a PASSED stage: the next one, or the end of the run.

        Only reached once the participant has said they are ready - the pass line
        has been spoken and the stats screen has had its say. See `_park_after_pass`.

        THE LAST STAGE ENDS TWO WAYS, and `from_verdict` is which. A real run has a
        verdict, and its last screen is the STATS screen: the participant has just
        been told the training is complete, the stats are what that consisted of, and
        the word returns the robot to standby directly (Danny, 5 November). The
        Success countdown therefore belongs to the other caller - a timer with no
        verdict, which has no participant to have written a closing line for and
        still needs a moment that says the run finished.
        """
        self.advance_on_word = False
        if finished >= self.total:
            if from_verdict:
                # Standby directly, through the same event and reset the Success
                # screen's deadline uses: one definition of "the run is over".
                events.append(("done", None))
                self.reset()
                return
            self.stage_step = self.SUCCESS
            self.deadline = now + self.success_secs
        else:
            self.stage = finished + 1
            self.stage_step = self.WAITING
            self.deadline = None
            self.entry = "advance"
            self.attempt = 1
            self.stats = None
            events.append(("stage", self.stage))
            events.append(("announce", self.stage))

    def _park_after_pass(self):
        """Hold a PASSED stage on the stats screen until the participant says go.

        A run does not move on the instant a session passes: the participant may want
        to look at how it went, and to take a break before meeting the next face. So
        the stage stays up, `waiting`, with the stats screen showing the session they
        just completed - the same screen, and the same word, that a failed session
        waits with. `advance_on_word` is what tells the two apart, because in both
        cases the word is the only way forward.

        `stats` was set when the verdict came in, so it survives the pass line
        speaking; nothing here needs the timeline passed in again.
        """
        self.advance_on_word = True
        self.stage_step = self.WAITING
        self.deadline = None

    def step(self, now, command=None, passed=None, spans=None):
        """Events for this tick.

        `command` is a newly published command, or None.

        `passed` is the gaze verdict for the session that is ending - the
        conductor's own accumulator, not a second opinion - and it is what the
        GATE reads. Only `False` blocks the run: None means no verdict was
        supplied, which advances on the timer as it always did, so this machine
        stays usable as a pure timer by a caller that has no gaze bar. The
        session-end LINES follow the same rule: a caller that supplies no verdict
        gets no speaking step, because it has no run for a face to talk about.

        `spans` is the ending session's looking timeline (see `stats`), handed over
        only when the verdict is a fail - it is what the stats screen draws while
        the participant decides to begin again.
        """
        events = []

        # A pass line has finished speaking, so the break starts now. Emitted here
        # rather than from spoke() so that every event in a run still comes out of
        # step(): one source, in order, whether it was a clock or a voice.
        if self.pending is not None and self.stage_step == self.WAITING:
            self.pending = None
            self._park_after_pass()

        if isinstance(command, dict):
            verb = command.get("command")
            if verb == START_COMMAND:
                if self.stage is None:
                    self.stage, self.stage_step, self.deadline = 1, self.WAITING, None
                    self.entry = "start"
                    self.attempt = 1
                    self.advance_on_word = False
                    # No announcement: stage 1's introduction is the greeter's,
                    # and it waits for a person rather than for this timer.
                    events.append(("stage", 1))
                else:
                    events.append(("ignored", verb))
            elif verb == SKIP_STAGE2_COMMAND:
                # The operator's jump. Stage 2 goes up WAITING, exactly as it
                # would after a finished stage 1, so everything downstream - the
                # greeter's gate, the stage word, the gaze bar - behaves as it
                # already does; the only difference is which introduction plays,
                # and that is `entry` telling announce_name().
                #
                # Allowed from ANY state, like `standby`: it is the other tool for
                # testing one stage, and it needs the pass phrase, so it cannot be
                # said by accident. Where the run was is reported rather than
                # silently dropped, because an operator jumping stages wants to
                # know what was abandoned.
                if self.stage is not None:
                    events.append(("left", self.describe()))
                self.stage, self.stage_step, self.deadline = 2, self.WAITING, None
                self.entry = "skip"
                self.attempt = 1
                self.stats = None
                self.pending = None
                self.advance_on_word = False
                events.append(("stage", 2))
                events.append(("announce", 2))
            elif verb == STAGE_WORD_COMMAND:
                # The participant's word. Only meaningful while a stage is up and
                # WAITING: during a session it is noise, and in standby there is no
                # stage to start. A stray one therefore cannot skip a stage, which
                # matters because it needs no confirmation to be acted on.
                if self.stage_step == self.WAITING:
                    if self.advance_on_word:
                        # The break is over: this stage already PASSED, so the word
                        # moves the run on rather than starting another session. The
                        # timeline belonged to the session that passed, and goes with
                        # it - _finish puts the next stage (or Success) up.
                        self.stats = None
                        # from_verdict: this word is a PARTICIPANT moving on from a
                        # session they passed, so a last stage ends at standby.
                        self._finish(self.stage, now, events, from_verdict=True)
                        return events
                    self.stage_step = self.RUNNING
                    self.deadline = now + self.session_secs
                    # NOTE the absent attempt reset. The word starts a SESSION, not a
                    # stage, and since a failed session now returns the stage to
                    # waiting, this branch is how a retry is started - so resetting
                    # the counter here would make every attempt look like the first
                    # and the ceiling could never fire. The stage's own entry paths
                    # (start, skip, advance) are what set it to 1.
                    events.append(("session", self.stage))
                else:
                    events.append(("ignored", verb))
            elif verb == STANDBY_COMMAND:
                # The operator's reset, and the only command that applies in EVERY
                # state, so it is checked before the fallback rather than inside a
                # state's branch. The power-on state is exactly what reset()
                # produces, so that is called rather than copied: a second
                # definition of "nothing is running" would drift from this one.
                #
                # In standby there is nothing to abandon, and reporting that is
                # more useful than a silent no-op an operator cannot distinguish
                # from a command that was never heard.
                if self.stage is None:
                    events.append(("ignored", verb))
                else:
                    where = self.describe()      # captured BEFORE the reset
                    self.reset()
                    events.append(("standby", where))
            elif verb:
                events.append(("ignored", verb))

        if self.deadline is not None and now >= self.deadline:
            if self.stage_step == self.RUNNING and self.stage is not None:
                # The `is not None` is not a possibility being handled: RUNNING is
                # only ever set together with a stage, and writing it out keeps the
                # invariant visible to a reader and to a type checker.
                finished = self.stage
                events.append(("session_end", finished))
                # Counted here, not when a stage is left: the greeter's gate asks
                # whether this run has measured anyone yet, and a stage that passed is
                # still sitting on attempt 1 while it waits for the word.
                self.sessions_done += 1
                # THE GATE. A session that did not reach the target is repeated
                # rather than advancing, and the ceiling - if there is one - is
                # what stops a run that can never pass from running for ever.
                if passed is False:
                    if self.max_attempts and self.attempt >= self.max_attempts:
                        events.append(("failed", finished))
                        self.reset()
                    else:
                        # The face explains, and THEN the stage waits for the word
                        # again: the next attempt is the participant's to start, not
                        # something that happens at them. `stats` is the timeline the
                        # stats screen draws while they decide, and `attempt` counts
                        # sessions RUN at this stage, so it moves on here.
                        events.append(("retry", (finished, self.attempt)))
                        self.attempt += 1
                        self.stats = spans
                        self.stage_step = self.SPEAKING
                        self.deadline = None
                elif passed is not None and self.pass_name(finished):
                    # The face that just finished congratulates the participant
                    # before the break starts: `pending` holds the stage that is
                    # waiting to be parked and spoke() releases it. Only when a
                    # verdict was supplied - a caller driving this as a plain timer
                    # has no run for a face to talk about.
                    events.append(("pass", (finished, self.next_stage(finished))))
                    self.pending = finished
                    # The timeline the break will show, set NOW: the pass line speaks
                    # for seconds and `spans` is per-call, so it cannot be handed over
                    # again when the voice stops.
                    self.stats = spans
                    self.stage_step = self.SPEAKING
                    self.deadline = None
                elif passed is None:
                    # No verdict at all: a caller driving this as a plain timer, with
                    # no participant to take a break. Advance on the clock, as it did
                    # before there were session-end lines.
                    self._finish(finished, now, events)
                else:
                    # A verdict, and no line for this stage: the break starts at once,
                    # silently, on the same stats screen.
                    self.stats = spans
                    self._park_after_pass()
            else:
                # The success screen has run out: the run is over and the robot is
                # ready for the next person, with nothing left to clean up.
                events.append(("done", None))
                self.reset()

        return events


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------
def write_json_atomic(path, data):
    """Replace a JSON file atomically, or raise OSError with the reason.

    ATOMIC because the face treats an unreadable session file as standby: a reader
    catching a plain open()+write() mid-write would drop the screen back for a
    frame. write + fsync + os.replace() means a reader sees the old screen or the
    new one, never half of one.
    """
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)


def write_screen(path, screen):
    """Publish the screen. Logs, rather than dying, if it cannot be written: the
    conductor continuing to run is more useful than it stopping, and the journal
    says what went wrong."""
    try:
        write_json_atomic(path, screen)
        return True
    except OSError as exc:
        print("session: could not write %s: %s" % (path, exc), file=sys.stderr)
        return False


def peek_seq(path):
    """The sequence number already in the outbox, without acting on it.

    Adopted at start-up so that a RESTART does not replay the command that was
    published before it: a conductor coming back up mid-run should wait for the
    next thing said, not re-run the last thing said.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    seq = data.get("seq") if isinstance(data, dict) else None
    return seq if isinstance(seq, int) else None


def read_command(path, last_seq):
    """(command, seq) for a command published since `last_seq`, else (None, last_seq).

    The sequence number is what makes this a poll rather than a subscription: a
    command published while this process is busy is picked up on the next tick
    instead of being lost, and the same event is never acted on twice.
    """
    seq = peek_seq(path)
    if seq is None or seq == last_seq:
        return None, last_seq
    if last_seq is not None and seq < last_seq:
        # An older file, e.g. one written by hand or left by an earlier process.
        return None, last_seq
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, last_seq
    return data, seq


# --------------------------------------------------------------------------
# Reading the gaze status file
# --------------------------------------------------------------------------
# One sample of it. A named tuple rather than a bare tuple because the third field
# is what the gaze bar runs on, and `sample[2]` at a call site would not say which
# field that is.
GazeSample = collections.namedtuple("GazeSample",
                                    "subject_rate hit_rate looking age")

# How old the status file may be before a sample stops counting as evidence. gaze
# writes it on every frame, several times a second, so a file older than this means
# the perception loop has stalled rather than that nobody happens to be looking -
# and a stalled loop must not keep banking time for a participant who may have
# walked off. Fails closed, like everything else that reads this file.
GAZE_STALE = 0.75


def read_gaze(path, now=None):
    """One GazeSample, or None if the status file is unusable.

    `looking` is the strict verdict the gaze daemon publishes: someone IN FRONT of
    the robot, with their head pointed at it. It is None - not False - in a file
    written by a build older than this one, so a caller can tell "cannot answer"
    from "the answer is no". Both fail closed, but only one of them is a deploy
    that did not happen.

    `age` is how long ago the file was written, which is the only cheap way to
    separate a live reading from the last one before the daemon died.
    """
    now = time.time() if now is None else now
    try:
        age = now - os.path.getmtime(path)
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    subject, hit = data.get("subject_rate"), data.get("hit_rate")
    if not isinstance(subject, (int, float)) or isinstance(subject, bool):
        return None
    looking = data.get("looking")
    return GazeSample(
        float(subject),
        float(hit) if isinstance(hit, (int, float)) and not isinstance(hit, bool)
        else 0.0,
        looking if isinstance(looking, bool) else None,
        max(0.0, age))


def looking_verdict(sample):
    """(counts, why_not) for one sample: does this bank time on the gaze bar?

    THREE ways to be false, and they are not the same fault: no usable file at all
    (gaze down, wrong path, permissions), a file with no `looking` field (the gaze
    daemon on the device is older than this conductor), and a file that has stopped
    moving. All three mean the same thing to the participant's score - nothing is
    banked - but only the second is a deploy that did not happen, so it is worth a
    different sentence in the log.
    """
    if sample is None:
        return (False, "no usable gaze status file")
    if sample.looking is None:
        return (False, "the status file has no 'looking' field - an older gaze build?")
    if sample.age > GAZE_STALE:
        return (False, "status file %.1fs old - gaze may be down" % sample.age)
    return (bool(sample.looking), None)


def summarise(samples):
    """One line describing what the gaze did during a session.

    Mean AND worst, because they answer different questions: the mean says whether
    attention held, the worst says whether it ever collapsed. A single mean would
    hide a session that started well and ended with nobody there.
    """
    if not samples:
        return "no usable gaze samples"
    subjects = [s.subject_rate for s in samples]
    hits = [s.hit_rate for s in samples]
    return ("%d samples, subject-rate mean %.2f worst %.2f, hit-rate mean %.2f"
            % (len(samples), sum(subjects) / len(subjects), min(subjects),
               sum(hits) / len(hits)))


# --------------------------------------------------------------------------
# The gaze bar: the seconds of a stage spent looking at the robot
# --------------------------------------------------------------------------
# WHY IT IS ACCUMULATED HERE and not in the face. The bar is not a picture of
# something else: it IS the success criterion, and the conductor is what decides
# success. If the face accumulated it, the number it drew and the number that
# decided the stage could disagree, and there would be two definitions of
# "looking". So this class accumulates, the face draws what it publishes, and both
# act on one verdict - the `looking` field of the gaze status file, which is the
# same judgement, with the same hysteresis, that used to make the face smile.
#
# WHY LOOKING AWAY HOLDS THE BAR. The bar is a running total of looking, not a
# meter of the current moment: "only fill while they are looking, and never take
# it away" is what makes it a goal the participant can work at. It therefore never
# drains, and it pins at full rather than overflowing - see `progress`.
#
# THE TARGET IS A FRACTION OF THE SESSION, not "50 seconds". The spec is 50 s of
# looking inside a 60 s session, so the ratio is the part that means something;
# expressing it that way also means a session shortened for a test
# (--session-secs) still asks for the same fraction of its own length, instead of
# demanding 50 s inside a 10 s run and being unrunnable. GAZE_TARGET_FRAC is that
# ratio; the seconds are derived from it and logged.
GAZE_TARGET_FRAC = 50.0 / 60.0

# The most one tick may credit. A stall - a suspend, a process not scheduled for a
# second - must not bank the whole gap as time the participant spent looking.
GAZE_MAX_TICK = 0.5


class GazeFill:
    """Seconds spent looking, against the target, with a verdict at the end.

    Pure, and deliberately so: it is fed (now, looking) and reads no file and no
    clock of its own, so the whole thing is driven with a synthetic clock in
    --selftest, including the parts that take a minute in the room.
    """

    # The resolution of the fill: 4 decimal places, i.e. 0.01% of the target, or
    # 5 ms of looking at a 50 s target. Both `progress` and `passed` use it, and
    # that is the point of it - see `progress`.
    PROGRESS_DP = 4

    def __init__(self, session_secs, target_frac=GAZE_TARGET_FRAC):
        self.session_secs = float(session_secs)
        self.target_frac = float(target_frac)
        # Never zero: a session shortened to 0 s would otherwise divide by zero.
        self.target = max(1e-6, self.session_secs * self.target_frac)
        self.looked = 0.0
        self.looking = False
        self._last = None

    def start(self, now):
        """Begin a session at `now`. Nothing before it is credited."""
        self.looked = 0.0
        self.looking = False
        self._last = now

    def tick(self, now, looking):
        """Credit the time since the last tick, if the participant was looking.

        The FIRST tick only starts the clock, because there is no interval to
        credit yet: crediting one would hand the participant a free head start on
        a target measured in seconds.
        """
        if self._last is None:
            self.start(now)
            return
        dt = min(max(0.0, now - self._last), GAZE_MAX_TICK)
        self._last = now
        self.looking = bool(looking)
        if self.looking:
            self.looked += dt

    @property
    def progress(self):
        """0..1, the bar's fill, pinned at full and QUANTISED to one resolution.

        Quantised deliberately, and the reason is a bug this shape prevents: the
        fill is drawn on the projector and the verdict goes in the log, and they are
        two readers of one number. Compare the raw ratio against 1.0 for the verdict
        and a participant half a millisecond short gets a bar that DRAWS full (it
        paints from the rounded number) and a log that says FAIL. That reads as a
        broken robot rather than as a near miss, and no threshold can fix it - only
        making both readers read the same value can.
        """
        raw = self.looked / self.target
        return max(0.0, min(1.0, round(raw, self.PROGRESS_DP)))

    @property
    def passed(self):
        """Whether the bar filled - the SAME number the face draws, not a second one."""
        return self.progress >= 1.0

    def published(self):
        """The bar, as it goes into the session file for the face to draw."""
        return {"progress": self.progress,
                "looking": bool(self.looking),
                "looked": round(self.looked, 2),
                "target": round(self.target, 2)}

    def verdict(self):
        """One line: whether the bar filled, with the numbers behind it."""
        return ("gaze bar %s - looked %.1fs of the %.1fs target (bar %d%%)"
                % ("PASS" if self.passed else "FAIL", self.looked, self.target,
                   round(self.progress * 100.0)))



def clip_path(name):
    """An announcement clip, or None if it is not on the device."""
    if not name:
        return None
    path = CLIP_DIR / name
    return path if path.is_file() else None


def request_stop(signum, _frame):
    _STOP["signum"] = signum


def stop_requested():
    return _STOP["signum"] is not None


def install_stop_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------
def run(args):
    # Overrides only, and deliberately so: greeter.play() re-resolves the sink and
    # the offset from display.conf for EVERY playback, so a lip-sync offset tuned
    # while the robot is running applies to the next announcement rather than to
    # the next conductor restart.
    sink, lead = greeter.display_settings(args.sink, args.lead)

    sess = Session(STAGES, args.session_secs, args.success_secs, args.max_attempts)
    last_seq = peek_seq(args.command)
    last_screen = sess.screen()
    write_screen(args.session, last_screen)

    log("conductor up: %d stages, %.0fs per session, %.0fs success screen",
        sess.total, args.session_secs, args.success_secs)
    # Reported here, and re-read for every playback. A stale copy inside this
    # process is exactly the failure the re-read exists to fix, so this line is
    # evidence of what it actually saw at start-up.
    log("display: sink %s, lead %s (both re-read from display.conf per playback)",
        sink, lead)
    log("commands from %s (adopted seq %s); screen to %s; log %s",
        args.command, last_seq, args.session, args.log)
    # Every clip a stage can be asked for counts: a stage whose SKIP introduction
    # was never rendered is still a stage that will be silent, and the warning is
    # most needed exactly when an operator jumps straight to it. The session-end
    # lines are in here for the same reason - a missing pass or retry line is a run
    # that moves on or repeats with no voice, which is a silent feature rather than
    # a fault.
    missing = sorted({s[k] for s in STAGES
                      for k in ("announce", "announce_skip", "pass", "retry")
                      if s.get(k) and not clip_path(s[k])})
    if missing:
        # Say it once, at start-up, rather than at the moment it is needed: the
        # run-through still works, it just advances silently.
        log("WARNING: clips missing, those moments will be silent: %s",
            ", ".join(missing))
    log_line(args.log, "--- conductor up (4 stages, %.0fs sessions)" % args.session_secs)
    # The bar's target is DERIVED here and said out loud, because the one thing a
    # cold reader cannot recover from the file is what "full" was asking for.
    log("gaze bar: looking fills it, %.0f%% of a session fills it (%.1fs of %.0fs)",
        GAZE_TARGET_FRAC * 100.0, args.session_secs * GAZE_TARGET_FRAC,
        args.session_secs)

    install_stop_handlers()
    samples = []
    fill = None                 # the gaze bar's accumulator, or None outside a session
    warned = {}                 # so a reason is said once, not ten times a second
    spans = None                # the running session's looking timeline, or None
    session_t0 = None           # when that session began, for the timeline's clock

    def report_abandoned(samples, fill, why):
        """Say what a run had gathered when something cut it short.

        ONE definition, called by `standby` and by `skip stage 2`, because both
        throw away a run in progress and two copies of this would eventually
        disagree about what an abandoned stage looks like in the log. The label
        is the part that matters: a session cut two thirds through must never be
        readable as a completed one.
        """
        if samples:
            partial = summarise(samples)
            log("  partial gaze from the stage abandoned by %s: %s", why, partial)
            log_line(args.log, "  partial gaze (abandoned by %s): %s" % (why, partial))
        if fill is not None:
            # The bar is reported too, and labelled the same way: a bar that was
            # two thirds full when the run was cut short is not a pass.
            verdict = fill.verdict()
            log("  partial gaze bar (abandoned by %s): %s", why, verdict)
            log_line(args.log, "  partial gaze bar (abandoned by %s): %s" % (why, verdict))
        return [], None
    last_line = _screen_line(last_screen)

    def publish():
        """Write the screen NOW if it has changed.

        Called before anything that BLOCKS, because a clip runs for as long as it
        runs: `greeter.play()` returns after 38 s for Owen's introduction. Writing
        the screen only at the end of the tick meant the previous stage's face
        stayed on the projector for the whole of the next stage's announcement -
        Owen introducing himself while Pip was still on screen, which is exactly
        what Danny saw on 4 November.
        """
        nonlocal last_screen, last_line
        screen = sess.screen()
        if fill is not None and screen.get("step") == Session.RUNNING:
            # The bar rides in the session file, because that is the file the
            # face already re-reads. It is the one field that makes the file
            # change faster than the screen does, which is why the LOG below is
            # keyed on what the screen says rather than on the file having
            # changed: a moving bar would otherwise write a line ten times a
            # second and bury the journal.
            screen = dict(screen, gaze=fill.published())
        if screen != last_screen:
            write_screen(args.session, screen)
            line = _screen_line(screen)
            if line != last_line:
                log_line(args.log, "screen %s" % line)
                last_line = line
            last_screen = screen

    try:
        while not stop_requested():
            now = time.monotonic()
            command, last_seq = read_command(args.command, last_seq)

            for kind, payload in sess.step(now, command,
                                           passed=fill.passed if fill else None,
                                           spans=spans):
                if kind == "stage":
                    log("stage %d of %d up - waiting for the stage word", payload, sess.total)
                    if payload == 1:
                        log("stage 1's introduction is the greeter's: it plays when "
                            "someone settles in front of the robot")
                elif kind == "announce":
                    # Which of a stage's two introductions plays is the state
                    # machine's answer, not the player's: it knows whether this
                    # stage was reached by finishing the last one or by the
                    # operator's jump. Asked here so a stage that only HAS one
                    # introduction behaves exactly as it did before.
                    name = sess.announce_name(payload)
                    clip = clip_path(name) if name else None
                    log("announcing stage %d with %s (%s)", payload,
                        name or "(no clip)", sess.entry)
                    if clip is not None:
                        # The new stage's screen goes up FIRST: everything below
                        # blocks until the clip has finished.
                        publish()
                        time.sleep(ANNOUNCE_SETTLE)
                        # The same player, lead offset and lock the greeter uses.
                        # Overrides (normally None) are passed, not the values
                        # resolved at start-up: the player re-reads display.conf so
                        # a re-tuned offset is live at the next announcement.
                        greeter.play(clip, args.sink, args.lead, args.mouth,
                                     args.dry_run)
                elif kind == "pass":
                    # The face that just finished speaks FIRST, and the screen does
                    # not move on until it has: the participant's companion is the
                    # one congratulating them. spoke() releases the advance, so it
                    # lands on the next tick.
                    stage, nxt = payload
                    name = sess.pass_name(stage)
                    clip = clip_path(name) if name else None
                    log("stage %d passed - %s", stage,
                        ("%s, then stage %d" % (name, nxt)) if clip and nxt
                        else (name if clip else "no line for this stage"))
                    log_line(args.log, "stage %d passed%s" % (
                        stage, "" if clip else " (no pass line)"))
                    try:
                        if clip is not None:
                            publish()
                            time.sleep(ANNOUNCE_SETTLE)
                            greeter.play(clip, args.sink, args.lead, args.mouth,
                                         args.dry_run)
                    finally:
                        # A player that fails must not leave the run waiting for a
                        # voice that has already stopped.
                        sess.spoke()

                elif kind == "retry":
                    # Two situations that look identical in the bar, and the operator
                    # needs to be able to tell them apart: a participant who did not
                    # reach the target, and a robot that could not see (no status
                    # file, a stale one, the subject gate rejecting everyone). Only
                    # the first is about the person in the chair.
                    stage, attempt = payload
                    name = sess.retry_name(stage)
                    clip = clip_path(name) if name else None
                    if spans:
                        log("stage %d attempt %d did not pass - the session will be "
                            "repeated when the participant says the word again",
                            stage, attempt)
                        log_line(args.log, "stage %d attempt %d did not pass; "
                                 "repeating when the word is said" % (stage, attempt))
                    else:
                        log("stage %d attempt %d banked NOTHING - not one usable gaze "
                            "sample: this is a gaze.service problem, not a "
                            "participant who would not look", stage, attempt)
                        log_line(args.log, "stage %d attempt %d banked nothing (no "
                                 "usable gaze samples)" % (stage, attempt))
                    try:
                        if clip is not None:
                            publish()
                            time.sleep(ANNOUNCE_SETTLE)
                            greeter.play(clip, args.sink, args.lead, args.mouth,
                                         args.dry_run)
                    finally:
                        sess.spoke()

                elif kind == "session":
                    samples = []
                    # A fresh bar for this session: the target belongs to the
                    # session it is measured over, so no fill is carried in or out.
                    fill = GazeFill(args.session_secs)
                    fill.start(now)
                    warned.clear()
                    # The looking timeline starts here, on the same clock the bar is
                    # measured against, so the stats screen and the bar can never
                    # describe two different sessions.
                    spans = []
                    session_t0 = now
                    log("stage %d session STARTED - %.0fs of gaze, gaze bar target %.1fs",
                        payload, args.session_secs, fill.target)
                    log_line(args.log, "stage %d session started (gaze bar target %.1fs)"
                             % (payload, fill.target))
                elif kind == "session_end":
                    summary = summarise(samples)
                    verdict = fill.verdict() if fill else "gaze bar: no session data"
                    log("stage %d session FINISHED - %s; %s", payload, summary, verdict)
                    log_line(args.log, "stage %d session finished: %s; %s"
                             % (payload, summary, verdict))
                    if spans:
                        # The timeline ends where the session did, so the stats bar
                        # adds up to the session's length rather than to its last
                        # sample.
                        spans[-1][1] = args.session_secs
                    fill = None
                elif kind == "failed":
                    # The ceiling was reached, so the run ends rather than advancing a
                    # stage it did not pass. The machine has already reset itself, so
                    # the screen block below writes standby with no extra work.
                    log("stage %d failed %d attempts - ending the run here, because no "
                        "stage advances without a pass. Say 'standby' to sit at the "
                        "power-on screen, or 'start' for a fresh run",
                        payload, sess.max_attempts)
                    log_line(args.log, "stage %d failed %d attempts; run ended"
                             % (payload, sess.max_attempts))
                    samples, fill, spans = [], None, None
                elif kind == "done":
                    log("run complete - the screen returns to standby")
                    log_line(args.log, "run complete")
                elif kind == "left":
                    # `skip stage 2` was said while something was already up. The
                    # state machine has already moved to stage 2 (the next events
                    # put it on screen and announce it); this branch exists so the
                    # abandoned run is reported with the same words `standby` uses.
                    log("skip stage 2 - abandoning %s; stage 2 up next", payload)
                    log_line(args.log, "skip stage 2: abandoned %s" % payload)
                    samples, fill = report_abandoned(samples, fill, "skip stage 2")
                elif kind == "standby":
                    # Already reset by step(), so the screen block below writes the
                    # standby screen on its next comparison. `payload` is where the
                    # run was when it was told to stop.
                    log("standby command - abandoning %s; screen back to standby",
                        payload)
                    log_line(args.log, "standby command: abandoned %s" % payload)
                    samples, fill = report_abandoned(samples, fill, "standby command")
                elif kind == "ignored":
                    log("ignored %r: not applicable (%s)", payload, sess.describe())
                    log_line(args.log, "ignored %r in %s" % (payload, sess.describe()))

            if sess.stage_step == Session.RUNNING:
                sample = read_gaze(args.status)
                if sample is not None:
                    samples.append(sample)
                # ONE place decides what banks time, so the bar on screen and the
                # verdict in the log cannot be produced by two different rules. A
                # missing sample is passed as None rather than counted as "not
                # looking" by accident: looking_verdict says which it was and why.
                counts, why_not = looking_verdict(sample)
                if fill is not None:
                    fill.tick(now, counts)
                    if why_not and why_not not in warned:
                        warned[why_not] = True
                        log("gaze bar not filling: %s", why_not)
                        log_line(args.log, "gaze bar not filling: %s" % why_not)
                # The session's looking timeline, for the stats screen: one span per
                # unbroken run of the same verdict, in SESSION-relative seconds
                # because that is what the screen draws (0 s to the session's end).
                if spans is not None and session_t0 is not None:
                    t = min(args.session_secs, now - session_t0)
                    if spans and spans[-1][2] == counts:
                        spans[-1][1] = t
                    else:
                        spans.append([t, t, counts])

            publish()

            time.sleep(args.poll)
    finally:
        # On any exit, including SIGTERM, leave the screen in the power-on state:
        # a stopped conductor must not leave a stage's screen on the projector
        # with nothing behind it to move it on.
        write_screen(args.session, {"phase": "standby"})
        log_line(args.log, "--- conductor stopped; screen back to standby")
        log("conductor stopped (%s)", "signal" if stop_requested() else "request")
    return EX_OK


def _screen_line(screen):
    """The screen as one log line, for the journal and the session log."""
    if screen.get("phase") != "running":
        return "standby"
    desc = screen.get("screen", {}).get("kind", "?")
    if desc == "label":
        desc = "label %r" % screen["screen"].get("text")
    return "stage %s/%s %s (%s)" % (screen.get("stage"), screen.get("stages"),
                                    screen.get("step"), desc)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def selftest():
    """Drive the whole run-through with a synthetic clock. No audio, no files."""
    failures, ran = [], []

    def check(name, got, want):
        ran.append(name)
        ok = got == want
        print("  %-62s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    stages = ({"screen": {"kind": "face", "style": "cartoon"}, "announce": None},
              {"screen": {"kind": "face", "style": "illustrated"}, "announce": "b.wav"},
              {"screen": {"kind": "face", "style": "dimensional"}, "announce": "c.wav"},
              {"screen": {"kind": "face", "style": "portrait"}, "announce": "d.wav"})
    start = {"command": START_COMMAND}
    word = {"command": STAGE_WORD_COMMAND}

    # 1. Nothing at all happens on a tick with no command.
    s = Session(stages, 60.0, 30.0)
    check("standby: a tick with no command does nothing", s.step(0.0), [])
    check("standby: and the screen is standby", s.screen(), {"phase": "standby"})

    # 2. The admin ritual's confirmed `start` puts stage 1 up and does NOT
    #    announce anything: stage 1's introduction is the greeter's.
    events = s.step(10.0, start)
    check("start: puts stage 1 up, with no announcement", events, [("stage", 1)])
    check("start: screen is the cartoon face, stage 1, waiting",
          (s.screen()["screen"], s.screen()["stage"], s.screen()["step"]),
          ({"kind": "face", "style": "cartoon"}, 1, "waiting"))
    check("start: waiting has no deadline", s.screen()["deadline"], None)

    # 3. A second start is ignored rather than restarting the run.
    check("start: a second one is ignored", s.step(11.0, start), [("ignored", "start")])

    # 4. The participant's word starts the timed session.
    check("word: starts stage 1's session", s.step(12.0, word), [("session", 1)])
    check("word: with a deadline one session long",
          s.screen()["deadline"], 12.0 + 60.0)
    # The length as well as the deadline: the face draws a countdown FRACTION, and a
    # reader cannot derive an interval's length from its end.
    check("word: and the screen carries the length of the step",
          s.screen()["seconds"], 60.0)
    check("word: the stage does not change",
          (s.screen()["stage"], s.screen()["step"]), (1, "session"))
    # ... and a second word during the session does not extend or restart it.
    check("word: during a session is ignored", s.step(20.0, word), [("ignored", "begin")])
    check("word: and does not move the deadline", s.screen()["deadline"], 72.0)

    # 5. The session ends on its timer and moves to the next stage, which IS
    #    announced (stage 2 onwards have no presence-triggered introduction).
    #    NOTE the absent third argument: `passed=None` is a caller with no verdict
    #    to give, and the run advances as it always did. The conductor is not such
    #    a caller - it always hands over the gaze bar's verdict, and test 17 below
    #    is what the gate does with it.
    check("timer: not yet at the deadline", s.step(71.9), [])
    check("timer: at the deadline, stage 1 ends and stage 2 is announced",
          s.step(72.0), [("session_end", 1), ("stage", 2), ("announce", 2)])
    check("timer: screen is the rung 2 face, stage 2, waiting",
          (s.screen()["screen"], s.screen()["stage"], s.screen()["step"]),
          ({"kind": "face", "style": "illustrated"}, 2, "waiting"))

    # 6. The participant's word cannot SKIP a stage: only a timed session ends
    #    one, so a stray word mid-stage cannot advance the run. It needs no
    #    confirmation to be acted on, which is exactly why this matters.
    check("word: cannot advance while a stage is waiting for its session",
          s.step(73.0, word), [("session", 2)])

    # 7. Stages 3 and 4 show the rung 3 and rung 4 faces, and stage 4 goes to
    #    Success. (These were label screens until the two renderers existed.)
    check("timer: stage 2 -> stage 3", s.step(133.0),
          [("session_end", 2), ("stage", 3), ("announce", 3)])
    check("timer: stage 3 screen is the rung 3 face",
          s.screen()["screen"], {"kind": "face", "style": "dimensional"})
    s.step(134.0, word)
    check("timer: stage 3 -> stage 4", s.step(194.0),
          [("session_end", 3), ("stage", 4), ("announce", 4)])
    check("timer: stage 4 screen is the rung 4 face",
          s.screen()["screen"], {"kind": "face", "style": "portrait"})
    s.step(195.0, word)
    check("timer: the last session ends into the success screen",
          s.step(255.0), [("session_end", 4)])
    check("success: screen is success with a deadline",
          (s.screen()["screen"], s.screen()["step"],
           s.screen()["deadline"]), ({"kind": "success"}, "success", 255.0 + 30.0))
    check("success: no announcement, because there is nothing to say",
          s.screen()["screen"], {"kind": "success"})

    # 8. The success screen runs out and the robot is ready for the next person.
    check("success: at the deadline the run ends",
          s.step(285.0), [("done", None)])
    check("success: and the screen is standby again",
          s.screen(), {"phase": "standby"})

    # 9. The word in standby does nothing: there is no stage to start. A person
    #    saying it before the session has begun must not start a run by accident.
    s2 = Session(stages, 60.0, 30.0)
    check("word: in standby is ignored", s2.step(5.0, word), [("ignored", "begin")])
    check("word: and leaves the robot in standby",
          s2.screen(), {"phase": "standby"})

    # 10. Every stage's screen is one the face can draw. A table entry with a typo
    #     would otherwise show up as a blank projector, not as an error.
    check("table: every stage names a screen kind the face knows",
          sorted({s["screen"]["kind"] for s in STAGES}), ["face"])

    # 11. The four stages are in the order the run specifies, and each names the
    #     renderer for its rung.
    check("table: stage 2 is the rung 2 renderer",
          STAGES[1]["screen"], {"kind": "face", "style": "illustrated"})
    check("table: stages 3 and 4 are the rung 3 and rung 4 renderers",
          [s["screen"].get("style") for s in STAGES[2:]],
          ["dimensional", "portrait"])
    check("table: stage 1 is the only stage that does not announce itself",
          [s["announce"] for s in STAGES],
          [None, "stage2-intro.wav", "rung3-warm-conversational-intro.wav",
           "rung4-natural-human-intro.wav"])
    # Stage 3 stopped borrowing another voice on 5 November: all three of its lines
    # are Milo's. Stage 4 still borrows Owen's retry, deliberately - Elliot's voice
    # has not been chosen - so that is asserted rather than left to drift.
    check("table: stage 3 speaks all three of its lines in its own voice",
          [STAGES[2][k] for k in ("announce", "pass", "retry")],
          ["rung3-warm-conversational-intro.wav",
           "rung3-warm-conversational-pass.wav",
           "rung3-warm-conversational-retry.wav"])
    check("table: stage 4 speaks all three of its lines in its own voice",
          [STAGES[3][k] for k in ("announce", "pass", "retry")],
          ["rung4-natural-human-intro.wav",
           "rung4-natural-human-pass.wav",
           "rung4-natural-human-retry.wav"])
    # No stage is silent at a session end: every pass line exists, and each names the
    # friend who comes next. Two of the four spent weeks waiting for that friend.
    check("table: every stage has a pass line and a retry line",
          [(i, STAGES[i-1].get("pass") is not None, STAGES[i-1].get("retry") is not None)
           for i in (1, 2, 3, 4)], [(1, True, True), (2, True, True),
                                    (3, True, True), (4, True, True)])
    # No stage borrows another person's voice any more. This is the invariant behind
    # the whole ladder, so it is asserted rather than described.
    check("table: no stage plays a clip belonging to another rung",
          [(i, k, st[k]) for i, st in enumerate(STAGES, 1)
           for k in ("announce", "announce_skip", "pass", "retry")
           if st.get(k) and not st[k].startswith(("rung%d" % i, "stage%d" % i))], [])

    # 12. The operator's reset: `standby` abandons a run from wherever it is.
    #     This is what lets a stage be tested without walking all four, so it has
    #     to work mid-session, not only between stages.
    standby = {"command": STANDBY_COMMAND}
    s3 = Session(stages, 60.0, 30.0)
    check("standby: in standby there is nothing to abandon, and it says so",
          s3.step(1.0, standby), [("ignored", "standby")])
    s3.step(2.0, start)
    s3.step(3.0, word)                       # stage 1, mid-session
    check("standby: mid-session it reports where it was, then resets",
          s3.step(20.0, standby), [("standby", "stage 1/4, session")])
    check("standby: and the screen is the power-on state",
          s3.screen(), {"phase": "standby"})
    # A FULL reset, not merely a screen change: the next run is stage 1 waiting,
    # with nothing carried over from the abandoned one.
    check("standby: the next run starts at stage 1, waiting",
          (s3.step(21.0, start), s3.screen()["stage"], s3.screen()["step"]),
          ([("stage", 1)], 1, "waiting"))
    check("standby: with no deadline left over", s3.screen()["deadline"], None)

    # 13. And from the Success screen, which is where an operator watching a test
    #     finish is most likely to want it. Short timers make the walk 5 s of
    #     synthetic clock instead of 5 minutes.
    fast = Session(stages, 1.0, 1.0)
    fast.step(0.0, start)
    for t in (0.1, 1.2, 2.3, 3.4):
        fast.step(t, word)
        fast.step(t + 1.05)                  # that stage's session runs out
    check("standby: the fast walk really reached the success screen",
          fast.screen()["step"], "success")
    check("standby: works from the success screen",
          fast.step(4.5, standby), [("standby", "stage 4/4, success")])
    check("standby: and back to standby from there",
          fast.screen(), {"phase": "standby"})

    # 14. `skip stage 2`: the operator's jump to rung 2, which has its own
    #     introduction because nobody who arrives this way has met Pip.
    skip2 = {"command": SKIP_STAGE2_COMMAND}
    s4 = Session(stages, 60.0, 30.0)
    check("skip stage 2: from standby, stage 2 goes up and is announced",
          s4.step(1.0, skip2), [("stage", 2), ("announce", 2)])
    check("skip stage 2: screen is the rung 2 face, stage 2, waiting",
          (s4.screen()["screen"], s4.screen()["stage"], s4.screen()["step"],
           s4.screen()["deadline"]),
          ({"kind": "face", "style": "illustrated"}, 2, "waiting", None))
    check("skip stage 2: the run records HOW stage 2 was reached",
          s4.entry, "skip")
    # The variant is chosen by announce_name(), from `entry`. A stage with no
    # announce_skip falls back to its single clip, which is what this fixture has:
    # the point is that the jump does not depend on a second clip existing.
    check("skip stage 2: a stage with one introduction still announces it",
          s4.announce_name(2), "b.wav")
    check("skip stage 2: the participant's word still starts the session",
          s4.step(2.0, word), [("session", 2)])
    check("skip stage 2: and it is a normal session, not a special one",
          (s4.screen()["stage"], s4.screen()["step"], s4.screen()["deadline"]),
          (2, "session", 62.0))

    # ... and the real table's two stage 2 clips, chosen by how it was entered.
    arrival = Session(STAGES, 60.0, 30.0)
    check("arrival: finishing stage 1 announces stage 2 with the default clip",
          (arrival.entry, arrival.announce_name(2)), (None, "stage2-intro.wav"))
    arrival.step(0.0, start)
    arrival.step(1.0, word)
    arrival.step(61.0)                       # stage 1's session runs out
    check("arrival: after a finished stage 1, stage 2 gets the CONGRATULATING clip",
          (arrival.entry, arrival.announce_name(2)),
          ("advance", "stage2-intro.wav"))
    jumped = Session(STAGES, 60.0, 30.0)
    jumped.step(0.0, skip2)
    check("arrival: after a jump, stage 2 gets the COLD-OPENING clip",
          (jumped.entry, jumped.announce_name(2)), ("skip", "stage2-skip.wav"))
    check("arrival: stages 3 and 4 have one clip either way",
          (jumped.announce_name(3), jumped.announce_name(4)),
          ("rung3-warm-conversational-intro.wav", "rung4-natural-human-intro.wav"))

    # Mid-run it is the same jump, and it reports what it abandoned - the operator
    # who said it needs to know whether a session was cut short.
    s5 = Session(stages, 60.0, 30.0)
    s5.step(1.0, start)
    s5.step(2.0, word)                       # stage 1, mid-session
    check("skip stage 2: mid-session it reports where it was, then jumps",
          s5.step(20.0, skip2),
          [("left", "stage 1/4, session"), ("stage", 2), ("announce", 2)])
    fast2 = Session(stages, 1.0, 1.0)
    fast2.step(0.0, start)
    fast2.step(0.1, word)
    fast2.step(1.2)                          # -> stage 2
    fast2.step(1.3, word)
    fast2.step(2.4)                          # -> stage 3
    check("skip stage 2: from a later stage it comes back to stage 2",
          fast2.step(2.5, skip2),
          [("left", "stage 3/4, waiting"), ("stage", 2), ("announce", 2)])

    # The jump must not leak into the NEXT run: a run that starts normally after a
    # skipped one has to announce stage 2 the ordinary way.
    jumped.step(3.0, standby)
    jumped.step(4.0, start)
    jumped.step(5.0, word)
    jumped.step(65.0)
    check("skip stage 2: a later run announces stage 2 normally again",
          (jumped.entry, jumped.announce_name(2)),
          ("advance", "stage2-intro.wav"))

    # 15. The gaze bar: the seconds of a session spent looking at the robot, and the
    #     verdict that comes out of it. Driven with a synthetic clock, because the
    #     real thing takes 60 s per stage and the arithmetic is the whole point.
    def drive_fill(fill, start, events, step=0.1):
        """events: list of (seconds, looking). Walks a synthetic clock through them."""
        fill.start(start)
        now = start
        for dur, looking in events:
            end = now + dur
            while now < end - 1e-9:
                now = min(end, now + step)
                fill.tick(now, looking)
        return now

    def check_close(name, got, want, tol=0.02):
        ran.append(name)
        ok = abs(got - want) <= tol
        print("  %-62s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    check("gaze bar: the target is 50 s of a 60 s session (a FRACTION, so a "
          "shortened test still scales)", GazeFill(60.0).target, 50.0)
    check("gaze bar: and it scales with the session", GazeFill(6.0).target, 5.0)

    bar = GazeFill(60.0)
    drive_fill(bar, 1000.0, [(50.5, True)])
    check("gaze bar: looking for the whole target passes",
          (bar.passed, bar.progress), (True, 1.0))
    check_close("gaze bar: and the bar is full, not over-full", bar.progress, 1.0)

    bar = GazeFill(60.0)
    drive_fill(bar, 1000.0, [(49.0, True)])
    check("gaze bar: a second short of the target fails",
          (bar.passed, round(bar.progress, 3)), (False, 0.98))

    # The boundary that the quantisation exists for. The bar is DRAWN from the
    # published fill and the verdict is computed from it too, so a participant a
    # millisecond short cannot get a bar that looks full beside a FAIL.
    bar = GazeFill(60.0)
    bar.looked = bar.target - 0.001
    check("gaze bar: the drawn fill and the verdict agree at the boundary",
          (bar.progress, bar.passed), (1.0, True))
    bar.looked = bar.target - 0.01
    check("gaze bar: and a visible shortfall still fails",
          (bar.progress, bar.passed), (0.9998, False))

    bar = GazeFill(60.0)
    drive_fill(bar, 1000.0, [(25.0, True), (25.0, False), (25.0, True)])
    check("gaze bar: looking away HOLDS it and looking back resumes",
          (bar.passed, round(bar.looked, 1)), (True, 50.0))

    bar = GazeFill(60.0)
    drive_fill(bar, 1000.0, [(30.0, False)])
    check("gaze bar: a whole session of not looking banks nothing",
          (bar.passed, bar.progress, round(bar.looked, 3)), (False, 0.0, 0.0))

    bar = GazeFill(60.0)
    drive_fill(bar, 1000.0, [(60.0, True)])
    check("gaze bar: looking on past the target does not overflow the bar",
          (bar.progress, round(bar.looked, 1)), (1.0, 60.0))

    bar = GazeFill(60.0)
    bar.tick(1000.0, True)
    check("gaze bar: the first tick credits nothing (no free head start)",
          bar.looked, 0.0)
    bar.tick(1005.0, True)          # a five-second stall while looking
    check("gaze bar: a stall credits at most GAZE_MAX_TICK, not the whole gap",
          round(bar.looked, 3), GAZE_MAX_TICK)

    bar = GazeFill(60.0)
    drive_fill(bar, 1000.0, [(10.0, True)])
    check("gaze bar: what the face is sent is the same number the verdict uses",
          (bar.published()["progress"], bar.published()["looking"],
           bar.published()["target"]), (round(bar.progress, 4), True, 50.0))
    check("gaze bar: the verdict names the state it is in",
          bar.verdict().split(" - ")[0], "gaze bar FAIL")

    # 16. The status file the bar banks time from. Its three refusals are the point:
    #     a file that has stopped moving, a file with no `looking` field at all, and
    #     nothing there to read must all bank NOTHING, and the second must be
    #     distinguishable from the other two in the log.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        status = os.path.join(d, "gaze-status.json")

        def write_status(payload):
            with open(status, "w") as fh:
                json.dump(payload, fh)

        write_status({"subject_rate": 0.9, "hit_rate": 0.8, "looking": True})
        sample = read_gaze(status)
        assert sample is not None, "the status fixture should parse"
        check("status: a live file saying looking banks time",
              looking_verdict(sample), (True, None))
        check("status: and the rates are still read for the summary",
              (sample.subject_rate, sample.hit_rate), (0.9, 0.8))

        write_status({"subject_rate": 0.9, "hit_rate": 0.8, "looking": False})
        check("status: looking false banks nothing",
              looking_verdict(read_gaze(status))[0], False)

        write_status({"subject_rate": 0.9, "hit_rate": 0.8})
        sample = read_gaze(status)
        assert sample is not None, "the status fixture should parse"
        check("status: no `looking` field is CANNOT TELL, not NO",
              (sample.looking, looking_verdict(sample)[0]), (None, False))
        check("status: and it is reported as an un-deployed gaze build",
              "older gaze build" in (looking_verdict(sample)[1] or ""), True)

        old = time.time() - 5.0
        write_status({"subject_rate": 0.9, "hit_rate": 0.8, "looking": True})
        os.utime(status, (old, old))
        check("status: a STALE file banks nothing even though it says looking",
              looking_verdict(read_gaze(status))[0], False)

        with open(status, "w") as fh:
            fh.write("{not json")
        check("status: garbage is no usable file",
              looking_verdict(read_gaze(status)),
              (False, "no usable gaze status file"))
        check("status: and a missing file is too",
              looking_verdict(read_gaze(os.path.join(d, "absent.json"))),
              (False, "no usable gaze status file"))

    # 17. THE GATE AND THE SESSION-END LINES. Test 5 above is the same clock with no
    #     verdict supplied, which is why it still advances: the timer path is what a
    #     caller without a gaze bar gets, and the conductor ALWAYS supplies one. This
    #     is the behaviour a participant meets - the 50-of-60 measure deciding whether
    #     the run moves - and the things it must never do: advance a stage without a
    #     pass, reach Success without one, or start the next attempt for them.
    timeline = [[0.0, 12.0, True], [12.0, 60.0, False]]
    g = Session(stages, 60.0, 30.0)
    g.step(0.0, start)
    g.step(1.0, word)                        # stage 1's session, deadline 61.0
    check("gate: a FAILED session does not advance the stage",
          g.step(61.0, None, passed=False, spans=timeline),
          [("session_end", 1), ("retry", (1, 1))])
    check("gate: the face that spoke is still the one on screen",
          (g.screen()["stage"], g.screen()["step"], g.screen()["screen"]),
          (1, "speaking", {"kind": "face", "style": "cartoon"}))
    check("gate: and nothing counts down while it is speaking",
          (g.screen()["deadline"], g.screen()["seconds"]), (None, 60.0))
    check("gate: the voice stops, and that is what moves the state on", g.spoke(), True)
    check("gate: so the stage waits for the word rather than running a session",
          (g.screen()["step"], g.screen()["deadline"]), ("waiting", None))
    check("gate: with the STATS screen up, drawn from the session's own timeline",
          g.screen()["screen"], {"kind": "stats", "spans": timeline})
    check("gate: the attempt that failed is remembered", g.attempt, 2)
    # What the GREETER reads to tell a retry from a fresh arrival. Stage, step and
    # phase are identical in both, so the count is the only thing that separates them
    # - and reading it wrong replays Pip's 58 second introduction over the stats
    # screen of the session the participant has just missed. Both sides are pinned
    # here because the greeter's gate consumes this field.
    check("gate: the published state carries the attempt, for the greeter",
          (g.screen()["attempt"], g.screen()["stage"], g.screen()["step"]),
          (2, 1, "waiting"))
    fresh = Session(stages, 60.0, 30.0)
    fresh.step(0.0, start)
    check("gate: a run's FIRST attempt, waiting, is what may be greeted",
          (fresh.screen()["attempt"], fresh.screen()["stage"], fresh.screen()["step"]),
          (1, 1, "waiting"))
    # The second field the greeter reads. A PASSED stage waits on attempt 1, so the
    # attempt count alone cannot tell it from a fresh arrival - this tally can.
    check("gate: and the run's own tally, which the pass break is why we need",
          (fresh.screen()["sessions_done"], g.screen()["sessions_done"]), (0, 1))
    check("gate: and a stray tick starts nothing", g.step(70.0), [])
    check("gate: the participant's word starts attempt 2", g.step(80.0, word),
          [("session", 1)])
    check("gate: a full session long, from when they said it",
          g.screen()["deadline"], 140.0)
    check("gate: with the stats screen gone while they are being measured",
          g.screen()["screen"], {"kind": "face", "style": "cartoon"})
    # This fixture's stage has no pass line, so the break opens at once and silently
    # - the same screen, without a voice to wait for. The block below covers the
    # stage that HAS a line.
    check("gate: a PASS on a stage with no line opens the break at once",
          (g.step(140.0, None, passed=True, spans=timeline), g.screen()["step"]),
          ([("session_end", 1)], "waiting"))
    check("gate: on the stats screen of the session that just passed",
          g.screen()["screen"], {"kind": "stats", "spans": timeline})
    check("gate: nothing moves on while they look at it", g.step(180.0), [])
    check("gate: the word moves the run ON, because this stage already passed",
          g.step(190.0, word), [("stage", 2), ("announce", 2)])
    check("gate: and the next stage starts its own attempt count", g.attempt, 1)

    # The last stage cannot reach Success without passing either.
    solo = Session((STAGES[0],), 60.0, 30.0)
    solo.step(0.0, start)
    solo.step(1.0, word)
    check("gate: the LAST stage fails into a retry, not into Success",
          solo.step(61.0, None, passed=False),
          [("session_end", 1), ("retry", (1, 1))])
    solo.spoke()
    check("gate: and its screen is the stage, not the success screen",
          (solo.screen()["screen"], solo.screen()["step"]),
          ({"kind": "face", "style": "cartoon"}, "waiting"))
    solo.step(62.0, word)
    check("gate: a PASS on the last stage SPEAKS first, with no next stage to name",
          solo.step(122.0, None, passed=True, spans=[[0.0, 30.0, True]]),
          [("session_end", 1), ("pass", (1, None))])
    check("gate: nothing moves while that last line is being spoken",
          (solo.screen()["step"], solo.pending), ("speaking", 1))
    solo.spoke()
    check("gate: the voice stopping parks it on the stats screen", solo.step(123.0), [])
    check("gate: the last stage takes its break like the others",
          (solo.screen()["step"], solo.screen()["screen"]),
          ("waiting", {"kind": "stats", "spans": [[0.0, 30.0, True]]}))
    # The last stage ends AT STANDBY, not at the Success countdown: its pass line has
    # already said the training is complete and the stats screen was the last screen,
    # so a further "well done" screen would be a second ending (Danny, 5 November).
    check("gate: the word on the last stage ends the run", solo.step(130.0, word),
          [("done", None)])
    check("gate: and it is standby, with no success countdown to wait out",
          solo.screen(), {"phase": "standby"})
    # Success still exists for the caller with no verdict: a plain timer has nobody
    # to have spoken a closing line, and still needs a moment that says it finished.
    timer_end = Session(({"screen": {"kind": "face", "style": "cartoon"},
                          "announce": None},), 60.0, 30.0)
    timer_end.step(0.0, start)
    timer_end.step(1.0, word)
    timer_end.step(61.0)
    check("gate: a timer with no verdict still reaches the success screen",
          (timer_end.screen()["screen"], timer_end.screen()["step"]),
          ({"kind": "success"}, "success"))
    check("gate: and it ends at the deadline, as it always did",
          timer_end.step(91.0), [("done", None)])

    # A stage WITH a session-end line: the face congratulates the participant, and
    # the screen does not move on until it has finished speaking.
    lined = ({"screen": {"kind": "face", "style": "cartoon"}, "announce": None,
              "pass": "p.wav", "retry": "r.wav"},
             {"screen": {"kind": "face", "style": "illustrated"}, "announce": "b.wav"})
    ln = Session(lined, 60.0, 30.0)
    ln.step(0.0, start)
    ln.step(1.0, word)
    check("line: a passed stage SPEAKS before the screen moves on",
          ln.step(61.0, None, passed=True, spans=[[0.0, 20.0, True]]),
          [("session_end", 1), ("pass", (1, 2))])
    check("line: and the screen has not moved - stage 1's face is still up",
          (ln.screen()["stage"], ln.screen()["step"], ln.screen()["screen"]),
          (1, "speaking", {"kind": "face", "style": "cartoon"}))
    check("line: the advance waits for the participant now", ln.spoke(), True)
    check("line: the voice stopping parks the passed stage on its stats screen",
          (ln.step(62.0), ln.screen()["step"], ln.screen()["screen"]),
          ([], "waiting", {"kind": "stats", "spans": [[0.0, 20.0, True]]}))
    check("line: so stage 2 is announced when the word is said, not before",
          ln.step(70.0, word), [("stage", 2), ("announce", 2)])
    check("line: and the next stage's own face is up before its clip plays",
          (ln.screen()["screen"], ln.screen()["stage"]),
          ({"kind": "face", "style": "illustrated"}, 2))
    check("line: no verdict supplied means the timer path, and no line",
          Session(lined, 60.0, 30.0).step(0.0, start) is not None, True)
    quiet = Session(lined, 60.0, 30.0)
    quiet.step(0.0, start)
    quiet.step(1.0, word)
    check("line: a caller with no gaze bar advances on the clock alone",
          quiet.step(61.0), [("session_end", 1), ("stage", 2), ("announce", 2)])

    # No timeline, no stats screen: an empty bar is a claim about the participant,
    # and a session with no samples cannot support one.
    bare = Session(stages, 60.0, 30.0)
    bare.step(0.0, start)
    bare.step(1.0, word)
    bare.step(61.0, None, passed=False)      # no spans handed over
    bare.spoke()
    check("stats: no timeline means no stats screen - the face stays up",
          bare.screen()["screen"], {"kind": "face", "style": "cartoon"})
    check("stats: and an empty one is not a screen either",
          Session(stages, 60.0, 30.0).stats, None)

    # The operator's jump abandons the retry, stats screen and all.
    jumped = Session(stages, 60.0, 30.0)
    jumped.step(0.0, start)
    jumped.step(1.0, word)
    jumped.step(61.0, None, passed=False, spans=timeline)
    jumped.spoke()
    jumped.step(62.0, skip2)
    check("stats: the operator's jump clears it",
          (jumped.stats, jumped.screen()["screen"]),
          (None, {"kind": "face", "style": "illustrated"}))

    # The ceiling: an operator who wants a run that ends by itself sets it, and when
    # it is reached the run ENDS rather than advancing a stage that did not pass.
    capped = Session(stages, 60.0, 30.0, max_attempts=2)
    capped.step(0.0, start)
    capped.step(1.0, word)
    check("cap: attempt 1 fails into the retry line, not into a new session",
          capped.step(61.0, None, passed=False),
          [("session_end", 1), ("retry", (1, 1))])
    capped.spoke()
    capped.step(61.5, word)                  # attempt 2, deadline 121.5
    check("cap: attempt 2 failing is the last one allowed, so the run ends",
          capped.step(121.5, None, passed=False),
          [("session_end", 1), ("failed", 1)])
    check("cap: and the robot is back at standby rather than on stage 2",
          capped.screen(), {"phase": "standby"})
    check("cap: with no default ceiling - the gate decides unless asked otherwise",
          Session(stages, 60.0, 30.0).max_attempts, 0)
    one = Session(stages, 60.0, 30.0, max_attempts=1)
    one.step(0.0, start)
    one.step(1.0, word)
    check("cap: 1 is the OLD behaviour - one attempt, verdict only logged",
          one.step(61.0, None, passed=False),
          [("session_end", 1), ("failed", 1)])

    print()
    if failures:
        print("  SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    print("  SELFTEST PASSED (%d cases)" % len(ran))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Run the session: stages, timers, what is on screen.")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="the session screen file this process WRITES "
                         "(default %(default)s)")
    ap.add_argument("--command", default=DEFAULT_COMMAND,
                    help="the command outbox written by the listener "
                         "(default %(default)s)")
    ap.add_argument("--status", default=DEFAULT_STATUS,
                    help="gaze status file, read only to summarise a session "
                         "(default %(default)s)")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help="session log file (default %(default)s)")
    ap.add_argument("--mouth", default=DEFAULT_MOUTH,
                    help="face lip-sync file, for the announcements "
                         "(default %(default)s)")
    ap.add_argument("--sink", default=None,
                    help="PulseAudio sink; default from display.conf")
    ap.add_argument("--lead", type=float, default=None,
                    help="lip-sync offset; default from display.conf")
    # Shorter timers exist for TESTING a ninety-second run in ten seconds. They
    # are not a product feature, and the unit does not set them.
    ap.add_argument("--session-secs", type=float, default=SESSION_SECONDS,
                    help="length of a stage session (default %(default)s)")
    ap.add_argument("--success-secs", type=float, default=SUCCESS_SECONDS,
                    help="length of the success screen (default %(default)s)")
    ap.add_argument("--max-attempts", type=int, default=0, metavar="N",
                    help="how many times one stage's session may be repeated before "
                         "the run gives up and ends (default %(default)s = no "
                         "ceiling, so the gate decides. 1 means the old behaviour: "
                         "one attempt per stage, and the verdict only logged)")
    ap.add_argument("--poll", type=float, default=0.1,
                    help="seconds between ticks (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the whole sequence, but never play audio")
    ap.add_argument("--selftest", action="store_true",
                    help="drive the run-through with a synthetic clock and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
