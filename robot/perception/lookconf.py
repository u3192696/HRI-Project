#!/usr/bin/env python3
"""lookconf.py — "what does THIS camera call looking at the screen".

WHY THE NUMBERS ARE NOT CONSTANTS IN gaze.py
  `looking` is the verdict the face's expression and the session's gaze bar both
  run on, and it is judged from ONE number: the magnitude of the smoothed combined
  gaze, compared against a pair of thresholds. That number adds WHERE the head sits
  in the frame (`off_x`/`off_y`) to WHICH WAY it is turned (`yaw`/`pitch`), so the
  value produced by "a person facing the screen" is a property of the whole
  arrangement - where the camera is, which way it is aimed, where the screen is,
  where the seat is - and not of the robot.

  Measured on the robot in its case, 0.35/0.80 is right. Move the same camera onto
  the testing bench and the arrangement changes: the person looking at the screen
  can now sit off-centre in the frame, and/or turn their head further to face it,
  so the same intent produces a bigger number and the robot calls someone who is
  looking straight at it "not looking".

  So the two numbers live beside the module, are re-measured when the camera moves,
  and are STAMPED with the rig they were measured on. Same shape as
  robot/voice/display.conf and for the same reason: a value that belongs to a
  physical arrangement cannot be a constant in the code.

READ, NEVER SOURCE
  The file is a shell fragment of KEY=VALUE lines, so it is greppable and editable
  with the tools already in use, but nothing ever executes it: a stray `$(...)` in a
  calibration file must not run. Parsing is deliberately tolerant - an absent, empty,
  half-written or garbage file yields whatever keys could be read, and every caller
  falls back to the value it would have used without a conf at all. gaze.py re-writes
  the status file with a plain open()+json.dump(), so a reader CAN catch a partial
  file; that is why nothing here raises on bad input.

WHAT THE FILE HOLDS
  LOOK_ENTER      at or below this magnitude, this camera calls it looking at the screen
  LOOK_EXIT       above it, definitely not (the hysteresis bar; must be >= LOOK_ENTER)
  SUBJECT_MIN_W   optional: the subject (distance) gate's enter width, when measured too
  SUBJECT_EXIT_W  optional: the same gate's exit width
  RIG             free text: what the numbers were measured on, for the human reading it
  FITTED          free text: when, and by what. The record belongs next to the number.

`calibrate-look.py` measures and writes this file; gaze.py reads it. One reader path
(here) for both, because two definitions of "looking at the screen" that disagree is
exactly the bug this file exists to prevent.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONF = pathlib.Path(os.environ.get("FACE_LOOK_CONF") or (HERE / "look.conf"))
PROFILE_DIR = HERE / "looks"

# What gaze.py uses with no conf at all. These are the numbers measured on the
# robot in its case, and they remain the fallback rather than being replaced: a
# rig that has never been calibrated must keep behaving exactly as it did.
BUILTIN = {
    "LOOK_ENTER": 0.35,
    "LOOK_EXIT": 0.80,
    "SUBJECT_MIN_W": 0.12,
    "SUBJECT_EXIT_W": 0.08,
}
FLOAT_KEYS = ("LOOK_ENTER", "LOOK_EXIT", "SUBJECT_MIN_W", "SUBJECT_EXIT_W")
TEXT_KEYS = ("RIG", "FITTED")


def read(path=DEFAULT_CONF) -> dict:
    """Parse a look.conf into {key: str}. Never raises, never executes anything.

    Tolerant on purpose: a conf caught mid-write, a line with no `=`, a quoted value,
    a value that is not a number - each one drops that key and leaves the rest. The
    caller then falls back per key, which is what makes a half-written file safe.
    """
    values: dict = {}
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split("#", 1)[0].strip()          # trailing comment
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def read_numbers(path=DEFAULT_CONF) -> dict:
    """Only the keys that parse as floats. A key that does not is treated as absent."""
    out: dict = {}
    for key, raw in read(path).items():
        if key not in FLOAT_KEYS:
            continue
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def resolve(flag, conf_values: dict, key: str):
    """Return (value, source) for one key, in the order flag > conf > built-in.

    The SOURCE is returned rather than implied because it is the evidence: the
    start-up line in gaze.py's journal prints it, so "which number is live" is
    answerable without reading the unit file, the conf and the code.
    """
    if flag is not None:
        return float(flag), "flag"
    if key in conf_values:
        return float(conf_values[key]), "conf"
    return float(BUILTIN[key]), "built-in"


def validate(enter: float, exit_: float) -> None:
    """The one relation the state machine cannot survive without.

    ExpressionPolicy raises on this too; catching it here means a bad conf is
    reported at the point it was read rather than from inside the render loop.
    """
    if exit_ < enter:
        raise ValueError(f"LOOK_EXIT {exit_} < LOOK_ENTER {enter}: the looking "
                         f"verdict would oscillate")


def write(path, values: dict, header: str = "") -> None:
    """Write the conf atomically: temp file in the same directory, then os.replace.

    A reader must never see a half-written conf. gaze.py's status reader tolerates
    one, but its CONF reader is what decides the robot's behaviour, and a threshold
    that briefly reads as garbage is not a shrug - it is a robot that stops noticing
    the person in front of it.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header:
        lines.append(header.rstrip("\n"))
    for key in FLOAT_KEYS + TEXT_KEYS:
        if key in values:
            value = values[key]
            if isinstance(value, float):
                text = f"{value:g}"
            else:
                text = str(value)
            lines.append(f"{key}={text}")
    body = "\n".join(lines) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".look-conf.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def copy_profile(src, dst) -> None:
    """Make one profile the active conf, byte for byte, atomically.

    Byte-for-byte on purpose: the tuned file carries its own stamp (rig, date, the
    measured margin) in its header, and re-serialising it would keep the numbers and
    lose the reason they are what they are.
    """
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    data = src.read_bytes()
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".look-conf.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def profiles(directory=None) -> dict:
    """{rig name: path} for every archived fit, so switching rigs is a choice."""
    directory = pathlib.Path(directory or PROFILE_DIR)
    out: dict = {}
    try:
        for p in sorted(directory.glob("*.conf")):
            out[p.stem] = p
    except OSError:
        pass
    return out


def describe(path=DEFAULT_CONF) -> dict:
    """Everything a human needs to know about the active numbers, in one dict."""
    path = pathlib.Path(path)
    values = read(path)
    numbers = read_numbers(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    return {
        "path": path,
        "exists": path.exists(),
        "numbers": numbers,
        "rig": values.get("RIG"),
        "fitted": values.get("FITTED"),
        "mtime": mtime,
    }
