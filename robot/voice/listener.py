#!/usr/bin/env python3
"""
listener.py — hear a spoken command, confirm it with a pass phrase, log it.

WHY THIS IS NOT CALLED keyword.py
  It was, and it crashed. Python puts the script's own directory first on
  sys.path, so a file named keyword.py SHADOWS the standard library's `keyword`
  module. `collections` imports it (`from keyword import iskeyword`), and the
  result is a circular import that dies on the first `import argparse`:

      AttributeError: partially initialized module 're' has no attribute
      'compile' (most likely due to a circular import)

  The same trap would catch `code.py`, `types.py`, `test.py`, `parser.py` and
  anything else that collides with a stdlib top-level name. Do not rename it
  back, and if this file ever needs to import another sibling module, check the
  name against the stdlib first.

THE RITUAL
  Two stages, both spoken, both required:

    1. "system command start"      an exact trigger phrase. Arms a countdown.
    2. "marigold wombat sierra"    the pass phrase, inside CONFIRM_TIMEOUT.

  Stage 1 alone does nothing but draw a prompt and start the clock. Stage 2
  alone does nothing at all: a pass phrase with no command pending is logged as
  ORPHAN, because that means a trigger was missed. Only the pair executes.

THE PARTICIPANT'S WORD
  There is a second path, and it is deliberately NOT part of that ritual. The
  person in front of the robot starts a stage by saying ONE word, alone, with no
  prefix and no confirmation. It is not an admin command and must not feel like
  one: the participant is a person doing a gaze exercise, and asking them to say
  "system command ..." and then a pass phrase four times through a session is
  friction in the one place the design can least afford it.

  WHAT PROTECTS IT INSTEAD OF A CONFIRMATION. The word is chosen to be easy to
  say, phonetically strong, and rare in ordinary speech, so a stray word in the
  room will not fire it; and the half-duplex gate covers the robot's own voice,
  which cannot say it at all. A false positive costs a 60s stage starting early,
  which is the harmless direction. Its selection and the measurements behind it
  are recorded in keywords.conf under WORD_TRIGGER.

  Both paths share the one debounce, so each fires once per utterance and re-arms
  only after observed silence.

  The command vocabulary is CLOSED and small (start, standby, reset, skip, shutdown,
  reboot, debug), so the recogniser stays grammar-constrained the whole way
  through. That
  is the difference between this and free transcription: the decoder cannot
  invent a command, and a mishearing is confined to confusing two of five known
  words, which the confirmation prompt then shows a person to catch. The risk
  that matters is not "the trigger fired by accident", it is "the command was
  heard as a different command", and the prompt displaying the PARSED command is
  what covers it. Say "reset", see "Reset", and a mishearing is visible.

WHY A CONSTRAINED GRAMMAR, NOT TRANSCRIPTION
  A general transcriber can output any English word, so every one of them is a
  false-positive route, and open-vocabulary recognition measured 0.44-0.64 mean
  confidence on quiet speech through this microphone. Restricting the decoder to
  the phrase list (plus [unk]) removes that whole class of error and costs a
  fraction of the CPU.

WHY IT REFUSES TO START RATHER THAN DEGRADE
  The board's default capture source is internal DMA fabric that opens, reports
  success, and records exact zeros. A listener that fell back to it would run
  perfectly and never hear anything, which is indistinguishable from a quiet
  room. mic.py refuses; this inherits that refusal.

WHAT A CONFIRMED COMMAND DOES
  Two verbs have an action wired, and both are the harmless class: they PUBLISH
  the command for the session conductor to act on (see COMMAND EVENTS below).

    start     takes the robot off the standby screen and lets the greeter greet.
    standby   is the operator's reset: the conductor abandons whatever is
              running, from any state, and returns to the power-on standby. It
              exists so one stage can be tested without walking all four.

  Neither powers anything off and neither loses a clip or a setting, which is why
  they came first. Every other verb still logs "WOULD EXECUTE (executor not
  wired)". "system command shutdown" powers off the board this process runs on,
  so it stays unwired until the confirmation gate has been shown working on
  harmless verbs. The action table records that as a deliberate omission, not an
  oversight.

COMMAND EVENTS, AND WHO OWNS WHAT
  This process HEARS and PUBLISHES. It does not own the run-through. Two files,
  both written only here, both for another service to read:

    /run/face/prompt.json    the confirmation now on screen, with the monotonic
                             deadline of its countdown, so the face can draw the
                             countdown a person is meant to beat. Deleted when
                             the window closes.
    /run/face/command.json   the last command that was CONFIRMED (or the
                             participant's word), with a monotonic sequence
                             number, so a consumer can tell a new event from the
                             same one re-read.

  Its own reason: audio in, one event out. Anything that has to happen over
  seconds (stages, timers, clips, what is on screen) belongs to session.py, which
  is the ONLY writer of /run/face/session.json. Keeping that split is what stops
  the listener growing a script, and the sequence number is what lets the reader
  be a simple poller rather than a subscriber.

  A missing file means the reader's safe default, never a fault: no prompt is no
  prompt, and no command is no new command.

THE DEBOUNCE
  A trigger phrase has the same shape as any other debounced edge:
    - the ARM edge fires on a detection, and
    - the RE-ARM edge requires the phrase to be ABSENT for HOLDOFF seconds
      before the same phrase can arm again.
  Only the arm edge, and one utterance arms repeatedly off one endpoint. Only the
  re-arm edge, and a second attempt in the same breath is swallowed. Time is
  credited only while the microphone is actually being heard from: a stretch
  where the robot was talking is not observed absence.

usage:
    ./listener.py                     # listen, log events to the conf's LOG
    ./listener.py --log /tmp/v.log    # override the log path
    ./listener.py --test-file x.wav   # dry-run the whole ritual over a WAV
    ./listener.py --selftest          # exercise both state machines, no audio
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mic  # noqa: E402  (sibling module; the one place the mic is opened)

DEFAULT_CONF = HERE / "keywords.conf"
DEFAULT_MODEL = HERE / "models" / "vosk-model-small-en-us-0.15"
DEFAULT_LOG = "/tmp/voice-detection.log"
DEFAULT_MOUTH = "/run/face/mouth.json"
# What this process WRITES: the event outbox and the on-screen prompt. The
# session file is written by session.py, not here — see COMMAND EVENTS above.
DEFAULT_COMMAND = "/run/face/command.json"
DEFAULT_MIC_STATE = "/run/face/mic.json"
DEFAULT_PROMPT = "/run/face/prompt.json"

# The verb the conductor understands as "start the current stage". Not a
# user-facing admin command: it is what the participant's WORD_TRIGGER means.
STAGE_WORD_COMMAND = "begin"

# How often the listening file is re-stamped while its state is unchanged. The
# face stops drawing the pill once the stamp is MIC_STALE (2 s) old, so this only
# has to be comfortably shorter than that: it is what keeps a green pill off the
# screen after a SIGKILL, a dead arecord or a stopped service.
MIC_HEARTBEAT = 0.5

# What the listening file says. `listening` is the only state that means "say
# something and you will be heard": while the robot talks, the half-duplex gate
# drops everything, so the file says `speaking` and the face draws nothing.
MIC_LISTENING = "listening"
MIC_SPEAKING = "speaking"

READ_RATE = 16000        # what Vosk wants; the sound server resamples for us
BLOCK = 4000             # bytes per read = 125 ms of 16-bit mono at 16 kHz
EX_UNKNOWN = "[unk]"     # what a constrained decoder says when it heard neither

EX_OK, EX_DEVICE, EX_CONFIG = 0, 1, 2

# The verbs a confirmed command publishes. See COMMAND EVENTS above.
START_COMMAND = "start"
STANDBY_COMMAND = "standby"
# A whole phrase rather than a verb with an argument, because keywords.conf's rule
# is that the vocabulary stays closed: the argument to `skip` would otherwise be
# free text, and free text is what this design exists to avoid. It must match
# session.py's SKIP_STAGE2_COMMAND exactly — the conductor ignores verbs it does
# not know, silently, so a drift here would look like a command that was never
# heard. The selftest asserts the two agree.
#
# "two", not "2": the small English model's vocabulary has no digit tokens and
# drops them from the grammar with a warning, which would leave the listener
# waiting for a phrase nobody says. Spoken aloud they are the same word.
SKIP_STAGE2_COMMAND = "skip stage two"

def write_json_atomic(path, data):
    """Replace a JSON file atomically, or raise OSError with the reason.

    ATOMIC ON PURPOSE, for the readers. The face treats an unreadable prompt or
    session file as "nothing to draw", so a reader that catches a plain
    open()+write() mid-write would flicker the screen for a frame. write + fsync +
    os.replace() means a reader sees either the old content or the new, never half
    of one.
    """
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)


def publish(path, command):
    """Hand a confirmed command to the session conductor as a new event.

    The sequence number is what lets the reader be a plain poller: it compares the
    last one it acted on with the one on disk, so re-reading the file costs
    nothing and an event cannot be missed by a consumer that was busy. Monotonic
    nanoseconds, not wall clock, because this board's clock is wrong until it
    reaches the network, and not milliseconds, because two events in one
    millisecond would then look like the same event.
    """
    write_json_atomic(path, {"command": command, "seq": time.monotonic_ns()})


def write_prompt(path, command, phrase, deadline, timeout):
    """Tell the face what confirmation is on screen, and when the window shuts.

    A single write carrying a monotonic DEADLINE, rather than one tick per count
    step: the face computes the remaining seconds itself. Two processes on one
    machine share CLOCK_MONOTONIC, so the countdown cannot drift from the gate
    that will actually accept or reject the answer.

    TIMEOUT is published alongside the deadline because the face draws a bar
    against the LENGTH of the window, and a bar needs both ends. Deriving the
    length from the first read would work only if that read were instantaneous: it
    would be a few milliseconds short, differently every time.
    """
    write_json_atomic(path, {"command": command, "phrase": phrase,
                             "deadline": deadline, "timeout": timeout})


def clear_prompt(path):
    """No prompt on screen. Absence is the ordinary state, so clearing is a delete."""
    try:
        os.unlink(path)
    except OSError:
        pass


class ListeningState:
    """The "can the robot hear you right now" file the face draws a pill from.

    WHY A HEARTBEAT AND NOT JUST TRANSITIONS. The face shows a green pill while
    this file says `listening` and stops showing it once the stamp is stale, and
    that staleness is the only thing standing between "the robot is listening" and
    a green light on a robot that has died: SIGKILL, a dead arecord and a stopped
    service all leave their last state on disk, and only a stamp that stops moving
    can tell any of them apart from a quiet room. A pill that lies is worse than no
    pill, because it invites someone to talk to a robot that cannot hear them.

    A write that FAILS is not remembered: the state falls back to unknown so the
    next tick tries again, and the file simply goes stale, which reads as no pill.
    Failing closed is the whole point.
    """

    def __init__(self, path, heartbeat=MIC_HEARTBEAT):
        self.path = path
        self.heartbeat = heartbeat
        self.state = None
        self.stamped = 0.0

    def set(self, state, now):
        """Publish a state. Writes on a change, or when the stamp is due."""
        if not self.path:
            return False
        if (state == self.state and self.stamped
                and now - self.stamped < self.heartbeat):
            return False
        try:
            write_json_atomic(self.path, {"state": state, "heartbeat": now,
                                          "pid": os.getpid()})
        except OSError as exc:
            print("listener: could not write %s: %s" % (self.path, exc),
                  file=sys.stderr)
            self.state = None
            self.stamped = 0.0
            return False
        self.state = state
        self.stamped = now
        return True

    def clear(self):
        """Remove the file, so a clean stop hides the pill at once.

        Not a substitute for the heartbeat: this only covers the exits that run
        code, and the ones that do not are exactly the ones that matter.
        """
        if not self.path:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.state = None
        self.stamped = 0.0


def make_executors(command_path):
    """The action table: one entry per verb that may ACT on a confirmation.

    Built from a path rather than closing over a module-level one, so a test can
    aim it at a temporary file instead of the real outbox, and so there is exactly
    one place that decides what a confirmed verb does.

    Both wired verbs only change what is on screen, and both are reversible with
    the other one.

    Deliberately ABSENT: shutdown and reboot. They power off or restart the board
    this process runs on, so they go last, after the confirmation gate has been
    shown working on something harmless.
    """
    def publish_or_report(verb):
        """Put a confirmed verb in the outbox, or say why that failed.

        Returns False rather than raising: this runs on the confirmation path of a
        service that must keep listening, and a refused write is not a reason to
        take the microphone down.
        """
        try:
            publish(command_path, verb)
        except OSError as exc:
            print("listener: could not write %s: %s" % (command_path, exc),
                  file=sys.stderr)
            return False
        return True

    def start_session():
        """Confirmed `start`: hand it to the conductor. Entry point of a session."""
        return publish_or_report(START_COMMAND)

    def standby_session():
        """Confirmed `standby`: tell the conductor to abandon the run and reset.

        The other half of the operator's control, and the answer to "I want to test
        one stage without sitting through four": it applies in every state, so it
        is also how a run is stopped part-way.
        """
        return publish_or_report(STANDBY_COMMAND)

    def skip_to_stage_two():
        """Confirmed `skip stage 2`: put rung 2 up without walking stage 1.

        The third way in, and the one that tests whether stage 2 alone works. It
        publishes the whole phrase rather than a verb plus an argument, so the
        conductor's command vocabulary stays closed: `skip stage 2` is the thing
        the conductor knows, exactly as `start` and `standby` are.
        """
        return publish_or_report(SKIP_STAGE2_COMMAND)

    return {"start": start_session, "standby": standby_session,
            SKIP_STAGE2_COMMAND: skip_to_stage_two}


# The table for the default outbox. listen() builds its own against the path it
# resolved, and the keys are identical by construction, so `verb in EXECUTORS` is
# still the right test for "is this verb wired" wherever it is asked.
EXECUTORS = make_executors(DEFAULT_COMMAND)

_STOP = {"signum": None}


def _request_stop(signum, frame):
    """Record a stop request. Sets a flag rather than raising.

    Raising from here would break out of a blocking read instantly, at the cost
    of being able to land in the middle of a log write and leave half a line. The
    loop reads a 125 ms block of audio, so a flag is noticed within 125 ms, which
    is soon enough for a shutdown and cannot corrupt the log.
    """
    _STOP["signum"] = signum


def install_stop_handlers():
    """Take SIGTERM and SIGINT as a clean shutdown. Not optional for a service.

    `systemctl stop` sends SIGTERM, and a bare SIGTERM kills Python WITHOUT
    running the `with mic.open_stream(...)` cleanup that terminates arecord. The
    recorder then survives on its own, still holding the capture source, which is
    the orphaned-recorder fault this project already hit once.

    SIGINT is handled too, but do not expect it to arrive: a process started as a
    background job in a NON-INTERACTIVE shell inherits SIGINT set to SIG_IGN, and
    an ignored disposition survives exec. That is why an earlier attempt to stop
    this listener with `kill -INT` did nothing at all and printed no error.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass   # not the main thread, or the platform has no such signal


def stop_requested():
    return _STOP["signum"] is not None


def stop_signal():
    return _STOP["signum"]


class Hearing:
    """What a single recognition endpoint was."""

    NOTHING = "nothing"
    TRIGGER = "trigger"
    CONFIRM = "confirm"
    # The participant's word: acted on at once, with no gate in front of it.
    DIRECT = "direct"


def parse_list(text):
    """Semicolon-separated list from the config."""
    return [p.strip() for p in (text or "").split(";") if p.strip()]


def _spoken(text):
    """A phrase as the decoder writes it and the listener compares it.

    Lowercased, whitespace collapsed, and APOSTROPHES DROPPED. The participant's
    word may be written with one ("let's begin") while Vosk's lexicon emits either
    form depending on the token it picks, and a grammar string containing an
    apostrophe is not a word sequence at all. Normalising both the configured
    phrase and the recognised endpoint means the two meet in one form instead of
    the match depending on which spelling the decoder chose.
    """
    return " ".join((text or "").lower()
                    .replace("'", "").replace("\u2019", "").split())


class Vocab:
    """The closed vocabulary, and the one place a phrase becomes a meaning.

    Built from TRIGGER_PREFIX + COMMANDS for the trigger phrases, plus the one
    pass phrase. Every phrase is a whole utterance the decoder must match end to
    end; nothing here matches on substring.
    """

    def __init__(self, prefix, commands, pass_phrase, words=""):
        if isinstance(words, str):
            words = parse_list(words.replace(",", ";"))
        words = words or []
        self.prefix = (prefix or "").strip()
        self.commands = list(commands)
        self.pass_phrase = (pass_phrase or "").strip()
        # The stage word is a LIST because the script teaches more than one: the
        # stage introduction tells the participant to say "let's begin", while the
        # stage word this rig has actually measured is "moonlight". Accepting both
        # keeps the spoken script honest without discarding the word that scores.
        self.words_list = [w.strip() for w in words if w and w.strip()]
        self.word = self.words_list[0] if self.words_list else ""
        self.trigger_phrases = ["%s %s" % (self.prefix, c) for c in self.commands]
        self.phrases = list(self.trigger_phrases)
        if self.pass_phrase:
            self.phrases.append(self.pass_phrase)
        for word in self.words_list:
            self.phrases.append(_spoken(word))
        self._by_phrase = {}
        for command, phrase in zip(self.commands, self.trigger_phrases):
            self._by_phrase[_spoken(phrase)] = (Hearing.TRIGGER, command)
        if self.pass_phrase:
            self._by_phrase[_spoken(self.pass_phrase)] = (Hearing.CONFIRM, None)
        for word in self.words_list:
            # Its "command" is the verb the conductor knows for starting a stage.
            self._by_phrase[_spoken(word)] = (Hearing.DIRECT, STAGE_WORD_COMMAND)

    def classify(self, text, conf, min_conf):
        """(Hearing, command) for one endpoint. Never raises, never guesses."""
        text = _spoken(text)
        if not text or text == EX_UNKNOWN or conf < min_conf:
            return (Hearing.NOTHING, None)
        return self._by_phrase.get(text, (Hearing.NOTHING, None))

    def words(self):
        """Every word appearing in any phrase, for explaining a near miss."""
        out = set()
        for phrase in self.phrases:
            out.update(_spoken(phrase).split())
        return out

    def explain(self, text, conf, min_conf):
        """Why an endpoint did not count. For the log only, never for logic.

        This exists because Vosk in grammar mode will happily emit a SINGLE WORD
        from a multi-word phrase, at full confidence, when it hears something
        acoustically like that word. Measured on this rig: a person saying "robot"
        produced 'reboot' at conf 1.00. So the interesting line is not "nothing
        matched", it is "a grammar word came through with no phrase around it",
        and that is worth seeing on a setup with no monitor.
        """
        text = _spoken(text)
        if text in self._by_phrase:
            if conf >= min_conf:
                return "matched"
            return ("the full phrase came through but scored %.2f, below min_conf %.2f"
                    % (conf, min_conf))
        words = [w for w in text.split() if w != EX_UNKNOWN]
        if not words:
            return "nothing"
        hit = [w for w in words if w in self.words()]
        if hit:
            return ("PARTIAL: %s with no complete phrase around it - the whole "
                    "endpoint must match, so this is NOT a trigger"
                    % "/".join(repr(w) for w in hit))
        return "nothing"


class Trigger:
    """Fire once per utterance; re-arm only after observed silence.

    Pure: give it a monotonic timestamp and a tri-state reading, and it returns
    "fire" on exactly the tick a detection should be acted on, else None. No I/O,
    no clock reads, no sleeping inside it, so it can be driven by a synthetic
    clock in a test.

    `heard` is tri-state, and the third state is not decoration:
      True   the phrase was recognised in this block
      False  the block was heard and contained no keyword
      None   nothing was observed: the stream stalled, or the robot is speaking
             so its own voice is being suppressed
    Time is credited to the re-arm window only in the False case, because that is
    the only case where the absence was actually observed.
    """

    WAIT, HOLD = "wait", "hold"

    # Cap on what one tick may credit. If the process is starved or the machine
    # suspends, one enormous dt would otherwise credit silence nothing observed.
    MAX_TICK = 2.0

    def __init__(self, holdoff):
        self.holdoff = holdoff
        self.phase = self.WAIT
        self.absent_for = 0.0
        self.prev = None

    def step(self, now, heard):
        dt = 0.0 if self.prev is None else min(now - self.prev, self.MAX_TICK)
        self.prev = now

        if heard is True:
            self.absent_for = 0.0
            if self.phase == self.WAIT:
                self.phase = self.HOLD
                return "fire"
            return None

        if heard is False:
            self.absent_for += dt
            if self.phase == self.HOLD and self.absent_for >= self.holdoff:
                self.phase = self.WAIT
                self.absent_for = 0.0
            return None

        # heard is None: knowledge is paused. Neither fire nor re-arm.
        return None


class CommandGate:
    """Two stages: a trigger arms a countdown, the pass phrase confirms.

    Pure, so the whole ritual is testable on a synthetic clock with no audio.

    Events returned by step(), each with a command payload where one makes sense:
      PROMPT    a trigger armed the countdown. Show the parsed command.
      EXECUTE   the pass phrase arrived inside the window. Actuate.
      TIMEOUT   the window closed with no pass phrase. Nothing happens.
      REPLACED  a trigger for a DIFFERENT command arrived mid-countdown. The
                pending command changed, so the prompt must be redrawn.
      ORPHAN    the pass phrase arrived with nothing pending, which means a
                trigger was missed. Safe to ignore, worth seeing.

    Note the ordering inside step(): the expiry is evaluated BEFORE this tick's
    hearing is considered. A pass phrase landing on exactly the deadline is late,
    and a pass phrase landing on the same tick as a fresh trigger belongs to the
    OLD countdown, so it must not execute the new command.
    """

    IDLE, ARMED = "idle", "armed"

    PROMPT, EXECUTE, TIMEOUT, REPLACED, ORPHAN = (
        "prompt", "execute", "timeout", "replaced", "orphan")

    def __init__(self, timeout):
        self.timeout = timeout
        self.state = self.IDLE
        self.pending = None
        self.deadline = 0.0

    def remaining(self, now):
        return max(0.0, self.deadline - now) if self.state == self.ARMED else 0.0

    def step(self, now, hearing, command=None):
        events = []

        if self.state == self.ARMED and now >= self.deadline:
            events.append((self.TIMEOUT, self.pending))
            self._clear()

        if hearing == Hearing.TRIGGER:
            if self.state == self.ARMED and command != self.pending:
                events.append((self.REPLACED, command))
            events.append((self.PROMPT, command))
            self.state = self.ARMED
            self.pending = command
            self.deadline = now + self.timeout

        elif hearing == Hearing.CONFIRM:
            if self.state == self.ARMED:
                events.append((self.EXECUTE, self.pending))
                self._clear()
            else:
                events.append((self.ORPHAN, None))

        return events

    def _clear(self):
        self.state = self.IDLE
        self.pending = None
        self.deadline = 0.0


def robot_is_speaking(mouth_path, stale_seconds):
    """True while a clip is playing, judged by the face's mouth file being fresh.

    lipsync.py rewrites that file ~60x a second for the duration of a clip, and
    stops when the clip ends, so a recent mtime means audio is going out of the
    robot's own speaker - straight into the microphone beside it. An absent or
    stale file means nothing is playing, which is not a fault.
    """
    try:
        age = time.time() - os.path.getmtime(mouth_path)
    except OSError:
        return False
    return age <= stale_seconds


def mean_conf(result):
    """Mean word confidence from a Vosk result dict. 1.0 when it has no words."""
    words = result.get("result") or []
    if not words:
        return 1.0
    return sum(w.get("conf", 0.0) for w in words) / len(words)


def write_line(path, text):
    """Append one line, flush immediately.

    Flushing matters more than it looks: a listener that buffers its indication
    looks like a listener that is not working, and the whole point of the log is
    to be watched while someone speaks.
    """
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
    with open(path, "a") as fh:
        fh.write("%s  %s\n" % (stamp, text))
        fh.flush()


def load_model(model_path):
    if not pathlib.Path(model_path).is_dir():
        raise SystemExit(
            "listener: no Vosk model at %s\n"
            "         Run ./fetch-model.sh to download it (needs network).\n"
            "         The lab has no LAN, so do this before leaving home."
            % model_path)
    from vosk import Model, SetLogLevel
    SetLogLevel(-1)
    return Model(str(model_path))


def make_recognizer(model, phrases, rate):
    from vosk import KaldiRecognizer
    # The grammar IS the accuracy strategy: the decoder cannot output a word
    # that is not in this list.
    grammar = json.dumps(list(phrases) + [EX_UNKNOWN])
    rec = KaldiRecognizer(model, rate, grammar)
    rec.SetWords(True)
    return rec


def describe(event, command, conf, vocab, timeout):
    """The one log line an event produces.

    On a PROMPT this line is also the text the face will eventually draw, which is
    why it names the command in title case: it is the human-facing name.
    """
    label = command.title() if command else ""
    if event == CommandGate.PROMPT:
        return ('PROMPT    "%s"  conf %.2f   -> on screen: say "%s" within %.1fs'
                % (label, conf, vocab.pass_phrase, timeout))
    if event == CommandGate.EXECUTE:
        wired = command in EXECUTORS
        return ('CONFIRMED "%s"  conf %.2f   -> %s'
                % (label, conf,
                   "EXECUTING" if wired else "WOULD EXECUTE (executor not wired)"))
    if event == CommandGate.TIMEOUT:
        return ('TIMEOUT   "%s"   no pass phrase within %.1fs - nothing happened'
                % (label, timeout))
    if event == CommandGate.REPLACED:
        return ('REPLACED  "%s"   the pending command changed before confirmation'
                % label)
    if event == CommandGate.ORPHAN:
        return ("ORPHAN    pass phrase heard with nothing pending - a trigger was missed?")
    return str(event)


def read_vocab(conf, args):
    """The vocabulary from config, or None with a reason printed."""
    prefix = conf.get("TRIGGER_PREFIX", "system command")
    commands = parse_list(conf.get("COMMANDS"))
    pass_phrase = conf.get("PASS_PHRASE", "")
    word = args.word if getattr(args, "word", None) is not None else conf.get("WORD_TRIGGER", "")
    if not commands:
        print("listener: no COMMANDS in %s" % args.conf, file=sys.stderr)
        return None
    if not pass_phrase.strip():
        print("listener: no PASS_PHRASE in %s - a trigger with no confirmation "
              "is not a two-stage ritual" % args.conf, file=sys.stderr)
        return None
    return Vocab(prefix, commands, pass_phrase, word)


def listen(args):
    conf = mic.read_conf(args.conf)
    vocab = read_vocab(conf, args)
    if vocab is None:
        return EX_CONFIG
    log_path = args.log or conf.get("LOG") or DEFAULT_LOG
    model_path = args.model or conf.get("MODEL") or str(DEFAULT_MODEL)
    holdoff = args.holdoff if args.holdoff is not None else float(conf.get("HOLDOFF", "2.0"))
    min_conf = args.min_conf if args.min_conf is not None else float(conf.get("MIN_CONF", "0.5"))
    timeout = (args.confirm_timeout if args.confirm_timeout is not None
               else float(conf.get("CONFIRM_TIMEOUT", "5.0")))
    mouth = args.mouth or conf.get("MOUTH") or DEFAULT_MOUTH
    stale = float(conf.get("MOUTH_STALE", "0.3"))
    # What this process WRITES: the event outbox, and the prompt the face draws.
    # Both live in keywords.conf beside MOUTH, because all three are files shared
    # with other processes rather than private state.
    command_path = args.command or conf.get("COMMAND") or DEFAULT_COMMAND
    prompt_path = args.prompt or conf.get("PROMPT") or DEFAULT_PROMPT
    listen_path = args.mic_state or conf.get("MIC_STATE") or DEFAULT_MIC_STATE
    executors = make_executors(command_path)
    listening = ListeningState(listen_path)

    model = load_model(model_path)
    rec = make_recognizer(model, vocab.phrases, READ_RATE)
    trigger = Trigger(holdoff)
    gate = CommandGate(timeout)

    write_line(log_path, "--- listening for %s then \"%s\" within %.1fs; "
                         "participant word %s (holdoff %.1fs, min_conf %.2f)"
               % (", ".join('"%s"' % p for p in vocab.trigger_phrases),
                  vocab.pass_phrase, timeout,
                  ", ".join('"%s"' % w for w in vocab.words_list), holdoff, min_conf))
    print("listener: triggers   %s" % ", ".join(vocab.trigger_phrases))
    print("listener: pass phrase %r within %.1fs" % (vocab.pass_phrase, timeout))
    if vocab.words_list:
        print("listener: stage words %s - spoken ALONE by the participant, no "
              "confirmation, starts the current stage"
              % ", ".join('"%s"' % w for w in vocab.words_list))
    print("listener: log %s" % log_path)
    print("listener: wired actions: %s   (anything else logs WOULD EXECUTE)"
          % (", ".join(sorted(executors)) or "none"))
    if not args.no_half_duplex:
        print("listener: suppressing while %s is fresh (half-duplex gate)" % mouth)
    if listen_path:
        print("listener: saying whether it can hear in %s (the pill)" % listen_path)

    suppressed_logged = False
    mic_conf = args.mic_conf or str(mic.CONF)
    install_stop_handlers()
    try:
        with mic.open_stream(conf_path=mic_conf, rate=READ_RATE, channels=1) as proc:
            while not stop_requested():
                data = proc.stdout.read(BLOCK)
                now = time.monotonic()

                if not data:
                    # The stream ended. arecord dying is a device fault, not a
                    # quiet moment, so say so and stop rather than spinning.
                    err = proc.stderr.read().decode(errors="replace").strip()
                    print("listener: audio stream ended (%s)" % (err or "no detail"),
                          file=sys.stderr)
                    return EX_DEVICE

                speaking = (not args.no_half_duplex
                            and robot_is_speaking(mouth, stale))
                # Publish whether a person can be heard, for the pill. BEFORE the
                # `continue` below, so the robot's own speech is published as
                # `speaking` instead of being skipped over: the pill must go out
                # while the half-duplex gate is dropping everything.
                listening.set(MIC_SPEAKING if speaking else MIC_LISTENING, now)
                if speaking:
                    if not suppressed_logged:
                        print("listener: robot is speaking - suppressing "
                              "(half-duplex gate)")
                        suppressed_logged = True
                    # Drop what the recogniser had buffered, so the robot's own
                    # words cannot complete a phrase once we resume. The gate is
                    # still ticked, so a countdown that expires while the robot
                    # talks still expires and is still logged.
                    rec.Reset()
                    trigger.step(now, None)
                    for event, command in gate.step(now, Hearing.NOTHING):
                        line = describe(event, command, 0.0, vocab, timeout)
                        write_line(log_path, line)
                        print("listener: %s" % line)
                        if event in (CommandGate.TIMEOUT, CommandGate.REPLACED):
                            clear_prompt(prompt_path)
                    continue
                suppressed_logged = False

                # What this block was, carried to the gate's call for the same
                # tick so the log line and the decision cannot disagree.
                hearing, command, conf_now = Hearing.NOTHING, None, 0.0
                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = (res.get("text") or "").strip()
                    conf_now = mean_conf(res)
                    hearing, command = vocab.classify(text, conf_now, min_conf)
                    if args.verbose and text:
                        print("listener: heard %r conf %.2f -> %s"
                              % (text, conf_now, hearing))
                    if hearing == Hearing.NOTHING and text:
                        why = vocab.explain(text, conf_now, min_conf)
                        if why != "nothing":
                            write_line(log_path, 'IGNORED   %-34r conf %.2f  %s'
                                       % (text, conf_now, why))

                # One debounce in front of BOTH paths, so each fires once per
                # utterance and re-arms only after observed silence.
                heard = hearing in (Hearing.TRIGGER, Hearing.DIRECT)
                if heard and trigger.step(now, True) != "fire":
                    hearing, command = Hearing.NOTHING, None
                elif not heard:
                    trigger.step(now, False)

                if hearing == Hearing.DIRECT:
                    # The participant's word: acted on at once, and NOT fed to the
                    # gate, because it is not an admin command and needs no
                    # confirmation. The gate is still ticked below, so a prompt
                    # that happens to be open still expires on time.
                    line = ('STAGE     "%s"  conf %.2f   -> starting the stage'
                            % (command, conf_now))
                    write_line(log_path, line)
                    print("listener: %s" % line)
                    try:
                        publish(command_path, command)
                    except OSError as exc:
                        write_line(log_path, "STAGE     could not publish: %s" % exc)
                        print("listener: could not write %s: %s"
                              % (command_path, exc), file=sys.stderr)
                    hearing, command = Hearing.NOTHING, None

                for event, payload in gate.step(now, hearing, command):
                    line = describe(event, payload, conf_now, vocab, timeout)
                    write_line(log_path, line)
                    print("listener: %s" % line)
                    # Keep the screen in step with the gate. The prompt a person
                    # beats with the pass phrase is the only thing on screen that
                    # tells them what is happening, so it is written when the
                    # window opens and deleted the moment it closes.
                    if event == CommandGate.PROMPT:
                        try:
                            write_prompt(prompt_path, payload, vocab.pass_phrase,
                                         now + timeout, timeout)
                        except OSError as exc:
                            print("listener: could not write %s: %s"
                                  % (prompt_path, exc), file=sys.stderr)
                    elif event in (CommandGate.EXECUTE, CommandGate.TIMEOUT,
                                   CommandGate.REPLACED):
                        clear_prompt(prompt_path)
                    if event == CommandGate.EXECUTE:
                        run = executors.get(payload)
                        if run is not None:
                            write_line(log_path, "EXECUTING %s" % payload)
                            run()
        # The loop exits only on a stop request, so the `with` block has already
        # released the microphone: arecord was terminated by the context manager,
        # not orphaned. This line is the proof it happened in the log.
        signum = stop_signal()
        try:
            name = signal.Signals(signum).name
        except (ValueError, TypeError):
            name = "signal %s" % signum
        write_line(log_path, "--- listener stopped (%s) - microphone released" % name)
        print("listener: stopped (%s)" % name)
        return EX_OK
    except KeyboardInterrupt:
        write_line(log_path, "--- listener stopped (SIGINT)")
        return EX_OK
    except mic.DeviceError as exc:
        print("listener: %s" % exc, file=sys.stderr)
        return EX_DEVICE
    finally:
        # The pill must not outlive the listener. This covers the exits that RUN
        # code; the ones that do not (SIGKILL, a killed arecord) are covered by the
        # heartbeat going stale, which is why both exist.
        listening.clear()


def test_file(args):
    """Dry-run the whole ritual over a WAV, as if the microphone had produced it.

    Two jobs. Point it at the greeter's own clip to confirm the vocabulary does
    NOT fire on the robot's own words. And point it at a recording of a person to
    see what the ritual would actually have done, endpoints and all, without
    needing the room.

    NOTHING IS ACTED ON. EXECUTE here is the GATE's decision, as the summary line
    says; no executor runs, so a confirmed `start` in a test file does not swing
    the session phase. The actions themselves are covered by --selftest.

    GIVE THE FILE TRAILING SILENCE. Vosk emits a whole utterance only when it
    hears a gap after it, so a clip that stops the instant the speaker does
    yields its last phrase in FinalResult instead — at the end of the stream,
    where the countdown has already expired. That looks exactly like a person who
    spoke too slowly, and it is not.

    The gate is driven here exactly as in listen(), so a timeout is a timeout and
    the same ordering rules apply.
    """
    conf = mic.read_conf(args.conf)
    vocab = read_vocab(conf, args)
    if vocab is None:
        return EX_CONFIG
    model_path = args.model or conf.get("MODEL") or str(DEFAULT_MODEL)
    min_conf = args.min_conf if args.min_conf is not None else float(conf.get("MIN_CONF", "0.5"))
    timeout = (args.confirm_timeout if args.confirm_timeout is not None
               else float(conf.get("CONFIRM_TIMEOUT", "5.0")))
    model = load_model(model_path)

    src = pathlib.Path(args.test_file)
    if not src.is_file():
        print("listener: no such file: %s" % src, file=sys.stderr)
        return EX_CONFIG
    # Normalise to what the live path feeds Vosk: 16 kHz mono 16-bit.
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tf:
        raw = tf.name
    try:
        subprocess.run(["sox", str(src), "-t", "raw", "-r", str(READ_RATE),
                        "-c", "1", "-b", "16", raw], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print("listener: could not convert %s (sox needed): %s" % (src, exc),
              file=sys.stderr)
        return EX_CONFIG

    rec = make_recognizer(model, vocab.phrases, READ_RATE)
    gate = CommandGate(timeout)
    events, endpoints = [], []
    with open(raw, "rb") as fh:
        while True:
            data = fh.read(BLOCK)
            if not data:
                break
            now = fh.tell() / (2.0 * READ_RATE)
            hearing, command, conf_now = Hearing.NOTHING, None, 0.0
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                text = (res.get("text") or "").strip()
                conf_now = mean_conf(res)
                if text:
                    endpoints.append((now, text, conf_now))
                    print("  %7.2fs  %-34r conf %.2f" % (now, text, conf_now))
                hearing, command = vocab.classify(text, conf_now, min_conf)
                if hearing == Hearing.DIRECT:
                    # The participant's word is a DETECTION, not a gate decision,
                    # and it is reported separately for one reason: a candidate
                    # word has to be measurable against real audio before anyone
                    # is asked to say it in the room. This is that instrument.
                    events.append((now, "stage", command, conf_now))
            for event, payload in gate.step(now, hearing, command):
                events.append((now, event, payload, conf_now))
    res = json.loads(rec.FinalResult())
    tail = (res.get("text") or "").strip()
    if tail:
        conf_now = mean_conf(res)
        endpoints.append((0.0, tail, conf_now))
        print("  %7s  %-34r conf %.2f" % ("end", tail, conf_now))
        hearing, command = vocab.classify(tail, conf_now, min_conf)
        if hearing == Hearing.DIRECT:
            events.append((float("inf"), "stage", command, conf_now))
        for event, payload in gate.step(1e9, hearing, command):  # let it expire
            events.append((float("inf"), event, payload, conf_now))
    os.unlink(raw)

    print("\n=== %s ===" % src.name)
    print("  %d endpoint result(s), %d event(s)" % (len(endpoints), len(events)))
    for t, event, payload, conf_now in events:
        label = payload.title() if payload else ""
        when = "expiry" if t == float("inf") else "%6.2fs" % t
        print("  %s  %-9s %s" % (when, event.upper(), label))
    if events:
        print("  (dry run: these are the GATE's decisions and the DETECTIONS. No")
        print("   action was run, nothing was published; the actions themselves")
        print("   are covered by --selftest.)")
    if not events:
        print("  no trigger, no prompt, nothing executed")
    return EX_OK


def selftest():
    """Both state machines and the half-duplex gate, on a synthetic clock. No audio."""
    failures, ran = [], []

    def check(name, got, want):
        ran.append(name)
        ok = got == want
        print("  %-62s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    def drive(holdoff, events, tick=0.1):
        """Drive Trigger: events are (duration_s, heard). Returns fire times."""
        t = Trigger(holdoff)
        now, fired = 0.0, []
        for dur, heard in events:
            end = now + dur
            while now < end:
                now += tick
                if t.step(now, heard) == "fire":
                    fired.append(round(now, 2))
            now = end
        return fired

    def drive_gate(timeout, events, tick=0.1):
        """Drive CommandGate: events are (duration_s, hearing, command).

        Returns [(event, command)] so the test reads as the ritual, not as times.
        """
        g = CommandGate(timeout)
        now, out = 0.0, []
        for dur, hearing, command in events:
            end = now + dur
            while now < end:
                now += tick
                out.extend(g.step(now, hearing, command))
            now = end
        return out

    # ---------------------------------------------------------------- Trigger
    # 1. one detection fires once
    check("a single detection fires once", drive(2.0, [(0.1, False), (0.1, True)]), [0.2])
    # 2. the phrase continuing does NOT fire again (only the arm edge)
    check("detection continuing -> exactly one fire",
          drive(2.0, [(0.1, False), (5.0, True)]), [0.2])
    # 3. heard nothing for longer than the holdoff -> can fire again.
    #    Derived from the requirement, NOT read off the code's output: the
    #    detection lands at t=0.1, the holdoff needs 2.0 s of OBSERVED silence, so
    #    it re-arms at 2.1, and the next detection tick is 3.2. The first version
    #    of this test said 3.3, which was my arithmetic slip, not a code bug.
    check("silence past the holdoff -> fires again",
          drive(2.0, [(0.1, True), (3.0, False), (0.1, True)]), [0.1, 3.2])
    # 4. heard nothing for LESS than the holdoff -> does not fire again
    check("silence shorter than the holdoff -> does NOT fire again",
          drive(2.0, [(0.1, True), (1.0, False), (0.1, True), (3.0, False)]), [0.1])
    # 5. nothing ever detected -> never fires
    check("no detection at all -> never fires", drive(2.0, [(30.0, False)]), [])
    # 6. unknown data is neither a detection nor observed silence
    check("unknown data -> never fires", drive(2.0, [(30.0, None)]), [])
    # 7. and unknown does NOT advance the re-arm timer: after firing, a long
    #    unknown stretch followed by one quiet tick must not have re-armed.
    check("unknown does not count as observed silence",
          drive(2.0, [(0.1, True), (10.0, None), (0.1, False), (0.1, True)]), [0.1])
    # 8. and once observed silence does exceed it, it re-arms
    check("observed silence advances the re-arm even after unknown",
          drive(2.0, [(0.1, True), (10.0, None), (3.0, False), (0.1, True)]),
          [0.1, 13.2])

    # ----------------------------------------------------------- CommandGate
    T, C, N = Hearing.TRIGGER, Hearing.CONFIRM, Hearing.NOTHING
    # 9. a trigger arms the countdown and does nothing else
    check("gate: trigger -> prompt, and no execution",
          drive_gate(5.0, [(0.1, T, "reset")]), [("prompt", "reset")])
    # 10. trigger then pass phrase inside the window -> executes
    check("gate: trigger then pass phrase -> execute",
          drive_gate(5.0, [(0.1, T, "reset"), (1.0, N, None), (0.1, C, None)]),
          [("prompt", "reset"), ("execute", "reset")])
    # 11. THE ONE THAT MATTERS: trigger, window expires, pass phrase is too late
    late = drive_gate(5.0, [(0.1, T, "reset"), (6.0, N, None), (0.1, C, None)])
    check("gate: pass phrase AFTER the window -> no execute",
          [e for e, _ in late if e == "execute"], [])
    check("gate: and the late pass phrase is reported as an orphan",
          [e for e, _ in late], ["prompt", "timeout", "orphan"])
    # 12. pass phrase with nothing pending does nothing at all
    check("gate: pass phrase alone -> orphan, no execute",
          drive_gate(5.0, [(0.1, C, None)]), [("orphan", None)])
    # 13. a different command mid-countdown replaces the pending one, and it is
    #     the NEW command that gets confirmed
    check("gate: a second, different command replaces the pending one",
          drive_gate(5.0, [(0.1, T, "reset"), (1.0, N, None), (0.1, T, "skip"),
                           (1.0, N, None), (0.1, C, None)]),
          [("prompt", "reset"), ("replaced", "skip"), ("prompt", "skip"),
           ("execute", "skip")])
    # 14. the SAME command again re-prompts but is not a replacement
    check("gate: the same command again -> no replaced event",
          [e for e, _ in drive_gate(5.0, [(0.1, T, "reset"), (1.0, N, None),
                                          (0.1, T, "reset")])],
          ["prompt", "prompt"])
    # 15. a fresh trigger restarts the clock, so a slow confirmation still lands.
    #     Without the restart this would have timed out at t=5.1.
    check("gate: a fresh trigger extends the window",
          drive_gate(5.0, [(0.1, T, "reset"), (4.0, N, None), (0.1, T, "reset"),
                           (3.9, N, None), (0.1, C, None)]),
          [("prompt", "reset"), ("prompt", "reset"), ("execute", "reset")])
    # 16. the deadline is a deadline: 4.8 s of silence still confirms, 5.0 s does not
    check("gate: confirmed just inside the window",
          [e for e, _ in drive_gate(5.0, [(0.1, T, "reset"), (4.8, N, None),
                                          (0.1, C, None)])],
          ["prompt", "execute"])
    check("gate: confirmed just outside the window",
          [e for e, _ in drive_gate(5.0, [(0.1, T, "reset"), (5.0, N, None),
                                          (0.1, C, None)])],
          ["prompt", "timeout", "orphan"])

    # ------------------------------------------------------------------ Vocab
    v = Vocab("system command", ["reset", "skip", "shutdown"], "marigold wombat sierra")
    check("vocab: builds one trigger phrase per command",
          v.trigger_phrases,
          ["system command reset", "system command skip", "system command shutdown"])
    check("vocab: the grammar is the triggers plus the pass phrase",
          v.phrases,
          ["system command reset", "system command skip", "system command shutdown",
           "marigold wombat sierra"])
    check("vocab: a trigger phrase classifies as its command",
          v.classify("system command skip", 0.9, 0.5), (Hearing.TRIGGER, "skip"))
    check("vocab: the pass phrase classifies as a confirmation",
          v.classify("marigold wombat sierra", 0.9, 0.5), (Hearing.CONFIRM, None))
    check("vocab: the bare command word is NOT a trigger",
          v.classify("reset", 1.0, 0.5), (Hearing.NOTHING, None))
    check("vocab: the prefix alone is NOT a trigger",
          v.classify("system command", 1.0, 0.5), (Hearing.NOTHING, None))
    check("vocab: [unk] is nothing", v.classify("[unk]", 1.0, 0.5), (Hearing.NOTHING, None))
    check("vocab: empty text is nothing", v.classify("", 1.0, 0.5), (Hearing.NOTHING, None))
    check("vocab: below min_conf is nothing",
          v.classify("system command reset", 0.2, 0.5), (Hearing.NOTHING, None))
    check("vocab: matching is case-insensitive",
          v.classify("System Command Reset", 0.9, 0.5), (Hearing.TRIGGER, "reset"))
    check("vocab: [unk] padding breaks a match, it does not substring",
          v.classify("[unk] reset [unk]", 0.99, 0.5), (Hearing.NOTHING, None))
    # explain() exists because Vosk in grammar mode emits bare words at full
    # confidence, so the log has to distinguish the three ways a non-match happens.
    check("vocab: explain calls a bare grammar word a PARTIAL",
          "PARTIAL" in v.explain("skip", 0.88, 0.5), True)
    check("vocab: explain calls a matched full phrase matched",
          v.explain("system command skip", 0.9, 0.5), "matched")
    check("vocab: explain reports a full phrase under the floor",
          "below min_conf" in v.explain("system command skip", 0.2, 0.5), True)
    check("vocab: explain calls ordinary speech nothing",
          v.explain("the quick brown fox", 0.9, 0.5), "nothing")
    check("vocab: explain calls pure [unk] nothing",
          v.explain("[unk] [unk]", 0.9, 0.5), "nothing")
    check("parse_list splits and strips",
          parse_list(" reset ; skip;;reboot "), ["reset", "skip", "reboot"])
    check("parse_list on empty text is empty", parse_list(""), [])

    # -------------------------------------------------------------------- gate
    # The half-duplex gate. "Do not listen while Pip is talking" is a claim about
    # four states of one file, so test all four rather than asserting it works.
    with tempfile.TemporaryDirectory() as d:
        m = os.path.join(d, "mouth.json")
        check("gate: no mouth file at all -> not speaking",
              robot_is_speaking(m, 0.3), False)
        with open(m, "w") as fh:
            fh.write('{"open": 0.7, "wide": 0.2}')
        check("gate: mouth file just written -> speaking",
              robot_is_speaking(m, 0.3), True)
        old = time.time() - 5.0
        os.utime(m, (old, old))
        check("gate: mouth file stale -> not speaking",
              robot_is_speaking(m, 0.3), False)
        os.utime(m, (time.time() - 0.1, time.time() - 0.1))
        check("gate: mouth file 0.1 s old, inside the window -> speaking",
              robot_is_speaking(m, 0.3), True)

    # ------------------------------------------------------------ stop handling
    # A service is stopped with SIGTERM, and the handler must record rather than
    # raise, because raising inside a log write would truncate the line.
    _STOP["signum"] = None
    check("stop: nothing requested at startup", stop_requested(), False)
    install_stop_handlers()
    _request_stop(signal.SIGTERM, None)
    check("stop: SIGTERM is recorded as a request", stop_requested(), True)
    check("stop: and the signal number is kept for the log",
          stop_signal(), int(signal.SIGTERM))
    _STOP["signum"] = None

    # ------------------------------------------------------------- execution
    # The action table is the last gate before something happens, so assert its
    # SHAPE and then actually drive it. This case used to assert the table was
    # EMPTY; a confirmed `start` now leaves standby, so it asserts precisely what
    # is wired and what must stay unwired instead.
    check("executors: only the harmless verbs are wired",
          sorted(EXECUTORS), ["skip stage two", "standby", "start"])
    # "skip stage two" belongs in this list because it changes nothing but the
    # screen, and `standby` reverses it: like `start`, the worst case is a stage
    # being shown that nobody asked for.
    check("executors: shutdown stays unwired", "shutdown" in EXECUTORS, False)
    check("executors: reboot stays unwired", "reboot" in EXECUTORS, False)

    # The reset is only wired if it actually publishes: an entry in the table that
    # does nothing is worse than an absent one, because the log would say
    # EXECUTING while the robot stayed exactly where it was.
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "command.json")
        ex = make_executors(out)
        ok = ex["standby"]()
        with open(out) as fh:
            payload = json.load(fh)
        check("executors: a confirmed standby publishes it",
              (ok, payload.get("command")), (True, "standby"))
        check("executors: with a sequence number, so the conductor sees a new event",
              isinstance(payload.get("seq"), int), True)

    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "command.json")
        ex = make_executors(out)
        check("executors: no event before anything is confirmed",
              os.path.exists(out), False)
        check("executors: start succeeds", ex["start"](), True)
        with open(out) as fh:
            first = json.load(fh)
        check("executors: start publishes the start command",
              first.get("command"), "start")
        check("executors: and stamps it with a sequence number",
              isinstance(first.get("seq"), int), True)
        check("executors: and leaves no temp file behind",
              os.listdir(d), ["command.json"])
        check("executors: a second start is a NEW event", ex["start"](), True)
        with open(out) as fh:
            second = json.load(fh)
        check("executors: with a later sequence number than the first",
              second["seq"] > first["seq"], True)
        # "The command was published" and "the write silently failed" must not
        # look the same in the log, so an unwritable path has to report failure.
        check("executors: an unwritable path is reported, not swallowed",
              make_executors(os.path.join(d, "nope", "command.json"))["start"](),
              False)

        # The prompt the face draws during the countdown, and its clearing.
        p = os.path.join(d, "prompt.json")
        write_prompt(p, "start", "marigold wombat sierra", 12.5, 5.0)
        with open(p) as fh:
            pr = json.load(fh)
        check("prompt: carries the command, the phrase and the deadline",
              (pr["command"], pr["phrase"], pr["deadline"]),
              ("start", "marigold wombat sierra", 12.5))
        clear_prompt(p)
        check("prompt: clearing removes the file, because absence is no prompt",
              os.path.exists(p), False)
        clear_prompt(p)
        check("prompt: clearing twice is harmless", os.path.exists(p), False)

    # ------------------------------------------------- the participant's word
    v = Vocab("system command", ["start", "skip"], "marigold wombat sierra",
              "moonlight")
    check("word: is a phrase in the grammar", "moonlight" in v.phrases, True)
    check("word: spoken alone is a DIRECT detection",
          v.classify("moonlight", 1.0, 0.5), (Hearing.DIRECT, STAGE_WORD_COMMAND))
    check("word: is not a trigger phrase",
          v.classify("moonlight", 1.0, 0.5)[0] == Hearing.TRIGGER, False)
    # The gate reacts only to TRIGGER and CONFIRM, which is exactly why a DIRECT
    # word cannot open a countdown: there is nothing for it to confirm.
    g2 = CommandGate(5.0)
    check("word: cannot open a confirmation window",
          g2.step(0.0, *v.classify("moonlight", 1.0, 0.5)), [])
    check("word: and a prefixed verb still does",
          [e for e, _ in g2.step(0.0, Hearing.TRIGGER, "start")], ["prompt"])
    check("word: below min_conf is nothing",
          v.classify("moonlight", 0.4, 0.5), (Hearing.NOTHING, None))
    v3 = Vocab("system command", ["start"], "marigold wombat sierra")
    check("word: no WORD_TRIGGER configured means no word path",
          v3.phrases, ["system command start", "marigold wombat sierra"])
    check("word: and the bare word is then not recognised at all",
          v3.classify("moonlight", 1.0, 0.5), (Hearing.NOTHING, None))

    # ... and the SECOND stage word, which the stage introductions teach.
    v2 = Vocab("system command", ["start", "skip"], "marigold wombat sierra",
               "moonlight; let's begin")
    check("word: both configured words are in the grammar, apostrophe dropped",
          [p for p in v2.phrases if p in ("moonlight", "lets begin")],
          ["moonlight", "lets begin"])
    check("word: the scripted phrase is a DIRECT detection",
          v2.classify("let's begin", 1.0, 0.5), (Hearing.DIRECT, STAGE_WORD_COMMAND))
    # The decoder may emit it without the apostrophe, or with a typographic one.
    # Normalising both sides is what makes the match independent of that choice,
    # so all three spellings have to land on the same meaning.
    check("word: and so is the spelling without an apostrophe",
          v2.classify("lets begin", 1.0, 0.5), (Hearing.DIRECT, STAGE_WORD_COMMAND))
    check("word: and the typographic apostrophe",
          v2.classify("let\u2019s begin", 1.0, 0.5),
          (Hearing.DIRECT, STAGE_WORD_COMMAND))
    check("word: the measured word still works alongside it",
          v2.classify("moonlight", 1.0, 0.5), (Hearing.DIRECT, STAGE_WORD_COMMAND))
    # The half of it that matters most: "begin" on its own is not the phrase. The
    # intros say the word at the end of a sentence, so a bare verb must stay a
    # near miss that is explained and NOT acted on.
    check("word: the bare verb is not the phrase",
          v2.classify("begin", 1.0, 0.5), (Hearing.NOTHING, None))
    check("word: and a sentence containing it is not either",
          v2.classify("shall we begin", 1.0, 0.5), (Hearing.NOTHING, None))
    check("word: but it is explained as a PARTIAL, not as silence",
          v2.explain("shall we begin", 1.0, 0.5).startswith("PARTIAL"), True)

    # --------------------------------------------------- the two new triggers
    # Built from the SHIPPED config rather than a fixture, because the thing that
    # can go wrong here is the conf: a typo or a lost semicolon would silently
    # produce a phrase nobody can say.
    shipped = mic.read_conf(DEFAULT_CONF)
    live = Vocab(shipped.get("TRIGGER_PREFIX"), parse_list(shipped.get("COMMANDS")),
                 shipped.get("PASS_PHRASE"), shipped.get("WORD_TRIGGER"))
    check("skip stage 2: the shipped conf builds the phrase",
          "system command skip stage two" in live.trigger_phrases, True)
    check("skip stage 2: and it classifies as a TRIGGER, so it gets confirmation",
          live.classify("system command skip stage two", 1.0, 0.5),
          (Hearing.TRIGGER, "skip stage two"))
    check("skip stage 2: the bare verb is a different phrase, and still parses",
          live.classify("system command skip", 1.0, 0.5), (Hearing.TRIGGER, "skip"))
    # The half-phrase is the near miss an operator will actually produce, and it
    # must not be mistaken for the whole command.
    check("skip stage 2: 'skip' without the number fires the WRONG verb, not this one",
          live.classify("system command skip", 1.0, 0.5)[1] != "skip stage two", True)
    check("skip stage 2: the shipped conf carries BOTH stage words",
          live.words_list, ["moonlight", "let's begin"])
    check("skip stage 2: and the shipped conf accepts the scripted phrase",
          live.classify("let's begin", 1.0, 0.5), (Hearing.DIRECT, STAGE_WORD_COMMAND))
    check("skip stage 2: it is wired, so a confirmation acts",
          "skip stage two" in EXECUTORS, True)
    check("skip stage 2: the executor publishes the whole phrase",
          [k for k in EXECUTORS if k == "skip stage two"], ["skip stage two"])
    # The conductor and the listener must agree on the STRING, because the
    # conductor silently ignores a verb it does not know: a drift here would look
    # like a command that was never heard, at exactly the moment it is needed.
    try:
        import session as session_mod
        check("skip stage 2: listener and conductor agree on the phrase",
              session_mod.SKIP_STAGE2_COMMAND, SKIP_STAGE2_COMMAND)
    except ImportError:
        print("  %-62s %s" % ("skip stage 2: conductor not importable, skipped", "ok"))

    # ------------------------------------------------------- the listening file
    # What the green pill is drawn from. Three things have to hold: it says what
    # the loop decided, it is re-stamped on a heartbeat even when that decision
    # does not change (or a dead listener would leave the pill green), and a write
    # that fails is NOT remembered as a state we are publishing.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mic.json")
        st = ListeningState(path)
        check("listen: nothing on disk before the first write",
              os.path.exists(path), False)
        check("listen: the first state is written", st.set(MIC_LISTENING, 100.0), True)
        with open(path) as fh:
            first = json.load(fh)
        check("listen: it says listening, stamped with the caller's clock",
              (first.get("state"), first.get("heartbeat")), ("listening", 100.0))
        check("listen: the process id goes with it, for diagnosis",
              first.get("pid"), os.getpid())
        check("listen: an unchanged state inside the heartbeat writes nothing",
              st.set(MIC_LISTENING, 100.1), False)
        check("listen: but it IS re-stamped once the heartbeat is due",
              st.set(MIC_LISTENING, 100.0 + MIC_HEARTBEAT), True)
        check("listen: a state change is written at once",
              st.set(MIC_SPEAKING, 100.6), True)
        with open(path) as fh:
            check("listen: so the robot's own speech is visible to the face",
                  json.load(fh).get("state"), "speaking")
        st.clear()
        check("listen: clearing removes the file, so a clean stop hides the pill",
              os.path.exists(path), False)
        # A write that cannot happen must not leave the object claiming a state:
        # the file goes stale instead, which the face reads as no pill.
        broken = ListeningState(os.path.join(d, "not-a-dir", "mic.json"))
        check("listen: a write that fails reports failure",
              broken.set(MIC_LISTENING, 1.0), False)
        check("listen: and does not leave the state claimed", broken.state, None)
        check("listen: and no file appeared", os.path.exists(path), False)

    print()
    if failures:
        print("  SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    print("  SELFTEST PASSED (%d cases)" % len(ran))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Hear a spoken command, confirm it with a pass phrase, log it.")
    ap.add_argument("--conf", default=str(DEFAULT_CONF), help="config (default %(default)s)")
    ap.add_argument("--mic-conf", default=None, help="mic.conf override")
    ap.add_argument("--model", default=None, help="Vosk model directory")
    ap.add_argument("--log", default=None, help="where detections are appended")
    ap.add_argument("--mouth", default=None, help="face mouth file (half-duplex gate)")
    ap.add_argument("--command", default=None,
                    help="outbox for confirmed commands, read by the session "
                         "conductor (default from keywords.conf: %s)" % DEFAULT_COMMAND)
    ap.add_argument("--prompt", default=None,
                    help="on-screen confirmation prompt (default from "
                         "keywords.conf: %s)" % DEFAULT_PROMPT)
    ap.add_argument("--mic-state", default=None,
                    help="the 'can the robot hear you' file the face draws its "
                         "pill from; re-stamped on a heartbeat so the pill goes out "
                         "if this process dies (default from keywords.conf: %s)"
                         % DEFAULT_MIC_STATE)
    ap.add_argument("--word", default=None,
                    help="the participant's stage word(s): spoken alone, with no "
                         "prefix and no confirmation. Semicolon-separated if more "
                         "than one is accepted (default from keywords.conf: "
                         "WORD_TRIGGER). Empty means no stage word")
    ap.add_argument("--no-half-duplex", action="store_true",
                    help="do not suppress while the robot is speaking. For "
                         "measuring how badly the robot hears itself")
    ap.add_argument("--holdoff", type=float, default=None,
                    help="seconds of silence before the same trigger may arm again")
    ap.add_argument("--confirm-timeout", type=float, default=None,
                    help="seconds the confirmation prompt stays open")
    ap.add_argument("--min-conf", type=float, default=None,
                    help="minimum mean word confidence to count as a detection")
    ap.add_argument("--test-file", help="dry-run the ritual over a WAV")
    ap.add_argument("--verbose", action="store_true", help="log every recognition")
    ap.add_argument("--selftest", action="store_true",
                    help="exercise both state machines with a synthetic clock, no audio")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.test_file:
        return test_file(args)
    return listen(args)


if __name__ == "__main__":
    sys.exit(main())
