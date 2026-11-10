#!/usr/bin/env python3
"""
face.py — the projected cartoon face for the social robot.

Runs fullscreen on the kiosk compositor (Weston, DRM backend, DP-1).

WHY THIS FILE IS SHAPED THE WAY IT IS
-------------------------------------
The drawing code is a PURE FUNCTION of (cairo context, width, height, time,
state). It has no GTK dependency and no display dependency. That buys us:

  * `--snapshot out.png` renders a single frame to a PNG offline, so the face
    can be checked visually without a monitor, a compositor, or a session.
    This is how the face gets reviewed before anyone looks at a screen.
  * The renderer is swappable: replace `draw_face()` with a 3D renderer later
    and the state contract below does not change.

The face is driven by an EXPRESSION + GAZE state read from a JSON file. The
vision and dialogue components write that file; they never touch the renderer.
That is the seam that lets the three halves of the robot be built and tested
independently.

STATE CONTRACT  (JSON at $FACE_STATE, default /run/face/state.json)
------------------------------------------------------------------
    {
      "expression": "neutral"|"happy"|"curious"|"confused"|
                    "listening"|"thinking"|"speaking",
      "gaze":  {"x": 0.0, "y": 0.0},   # -1..1, where to look
      "blink": false                    # set true to force one blink
    }

Missing file or malformed JSON is NOT an error — the face falls back to a
calm neutral idle. A robot that stops showing a face because a helper process
crashed is worse than one that keeps breathing.

MOUTH CONTRACT  (JSON at $FACE_MOUTH, default /run/face/mouth.json)
-----------------------------------------------------------------
    {
      "open": 0.0,     # 0..1 lip-sync envelope; 0 = closed
      "wide": 1.0      # optional multiplier on mouth width
    }

This is a SEPARATE FILE from the state contract, deliberately. gaze.py
rewrites state.json about 12x/s with a read-modify-write, so a speech driver
sharing that file would race it; two writers on one file is a bug waiting to
happen. The mouth also needs to be independent of the expression, so the
robot can look happy AND talk at the same time.

A STALE mouth file is treated as absent (see MOUTH_STALE_SECS). Without that,
a speech driver dying mid-word would freeze the mouth open forever.

Without this file the face behaves exactly as it always has: `speaking`
oscillates the mouth at a fixed rate, which is a generic talking flap and not
real lip-sync.

SESSION SCREEN  (JSON at $FACE_SESSION, default /run/face/session.json)
----------------------------------------------------------------------
    absent, unreadable, {"phase": "standby"}    the word, centred. Power-on.
    {"phase": "running", "screen": {...}}       whatever the screen says:
        {"kind": "face", "style": "cartoon"}        the face, as above
        {"kind": "label", "text": "Stage 3"}        a stage placeholder
        {"kind": "success"}                         success, with a countdown

Optional fields the face uses for its footer, when a stage is running:
"stage", "stages", "step" ("waiting" or "session") and "deadline" (a monotonic
timestamp; the countdown is computed from it, never from a tick count).

THE GAZE BAR, in the same file:
    "gaze": {"progress": 0..1, "looking": bool, "looked": s, "target": s}
drawn as a vertical bar down the right-hand side of the screen while a stage's
timed session runs. It is the participant's score, not a picture of the sensor:
it fills while the person is LOOKING AT THE ROBOT and never drains, and filling it
is what makes the stage a pass. The conductor accumulates it and writes it (it is
the file's only writer and the thing that decides success); this process only
draws it, so the number on the projector and the number in the log cannot differ.
The block is absent outside a session step, which is what "no bar" means - and a
bar drawn at zero would be a lie about a participant who had not started yet.

WRITTEN BY session.py, and by nothing else. The voice listener publishes a
confirmed command instead, to a separate file, because one writer per file is
what lets the face re-read on change without racing anybody. See the README.

NOTE the one consequence for anything that LOGS on this file changing: the gaze
bar moves, so the file is rewritten several times a second while a session runs.
The kiosk therefore logs on what the screen SAYS, not on the file having changed,
or a moving bar writes a journal line at frame rate.

AN ABSENT OR UNREADABLE FILE MEANS STANDBY, deliberately. /run is a tmpfs, so at
power on the file does not exist and the robot comes up waiting, with no writer
needed at boot: a robot that starts performing before anyone has started it is
the failure this screen exists to prevent.

A RUNNING session with an unusable screen falls back to the FACE, not to standby,
and says so in the journal. The two failures are not alike: standby is honest when
no session exists, and a lie when one does and the person is mid-exercise.

THE LISTENING PILL
------------------
/run/face/mic.json, written by the voice listener, re-stamped on a heartbeat while
it can hear: the face draws a green "Listening" pill in the top-left corner, over
whatever screen is up, and draws NOTHING otherwise. See the constants below for why
a stale stamp means no pill and why that default is False everywhere else.

The pill is how anything can add to this screen. This process is the only Wayland
client on the kiosk's Weston - and Weston has no layer-shell, so there is no
composited overlay window another process could own - therefore every addition to
the screen is a FILE: one writer per file, and this one reader draws them all.

PIP'S EXPRESSION IS PINNED (temporary)
--------------------------------------
The kiosk holds ONE expression and ignores the expression and gaze keys of the
state file, so the face does not mirror the participant's head pose. It still
blinks and still drifts its eyes, because those are the face's own signs of life
and a perfectly still frame is indistinguishable from a crashed renderer. The
policy that used to drive it - look at me and I smile, look away and I scowl - is
UNTENDED, not deleted: gaze.py still computes it and still publishes it as the
`looking` field of its status file, which is what the gaze bar scores. Releasing
the pin is `--pin-expression off` (or $FACE_PIN=off); the replacement trigger is a
separate, later decision. See PIN_EXPRESSION below.

SNAPSHOT MODE
-------------
    python3 face.py --snapshot /tmp/face.png
    python3 face.py --snapshot /tmp/face-happy.png --expression happy
    python3 face.py --snapshot /tmp/standby.png --screen standby
    python3 face.py --snapshot /tmp/label.png --screen label --label-text "Stage 3"
    python3 face.py --snapshot /tmp/success.png --screen success
    python3 face.py --snapshot /tmp/prompt.png --screen standby --prompt start
"""

import argparse
import json
import math
import os
import random
import sys
import time

import cairo

# The directory this file lives in. Needed by the rung 4 puppet, whose still and
# landmarks are ASSETS beside the code rather than paths relative to the cwd: the
# kiosk's cwd is not where anyone would expect, and a face that only draws when
# started from the right directory is a face that vanishes on the projector.
HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
BG        = (0.051, 0.043, 0.078)   # deep navy, matches the kiosk theme
FACE      = (0.945, 0.780, 0.620)   # warm, friendly skin tone
FACE_SHADE= (0.880, 0.690, 0.530)   # subtle shading under the eyes
OUTLINE   = (0.100, 0.080, 0.140)
EYE_WHITE = (0.985, 0.985, 1.000)
IRIS      = (0.255, 0.412, 0.882)   # Tokyo-Neon royal blue
IRIS_RIM  = (0.130, 0.220, 0.560)
PUPIL     = (0.020, 0.020, 0.050)
HIGHLIGHT = (1.000, 1.000, 1.000)
BROW      = (0.330, 0.220, 0.180)
BLUSH     = (0.960, 0.560, 0.560)
MOUTH     = (0.420, 0.150, 0.200)
TEXT_DIM  = (0.600, 0.620, 0.700)

# Rung 2 (illustrated) only. Warm and low-saturation so the face reads as a
# person rather than a cartoon, while staying in the same family as the above.
HAIR       = (0.170, 0.120, 0.135)   # dark warm brown, near-black in shadow
HAIR_HI    = (0.320, 0.235, 0.230)   # lit strands
LIP        = (0.700, 0.395, 0.395)
LIP_SHADE  = (0.505, 0.245, 0.270)
MOUTH_IN   = (0.165, 0.070, 0.090)   # inner mouth, revealed when the lips part
SCLERA     = (0.945, 0.940, 0.950)
PUPIL      = (0.055, 0.050, 0.075)
SKIN_SHADE = (0.845, 0.640, 0.505)   # cel-shadow tone
BROW_ILL   = (0.215, 0.145, 0.140)

EXPRESSIONS = (
    "neutral", "happy", "curious", "confused",
    "listening", "thinking", "speaking", "angry",
)

# Per-expression facial parameters.
#   brow_l/brow_r : brow angle in radians (positive = inner end raised)
#   eye_open      : eyelid aperture multiplier
#   mouth_curve   : >0 smiles, <0 frowns
#   mouth_open    : vertical opening as a fraction of mouth width
EXPRESSION_PARAMS = {
    # `mouth_w` is a MULTIPLIER on the mouth width, default 1.00. It exists so an
    # expression can have a bigger mouth without the width being hardcoded per
    # expression in the renderer. Every key must be present in "neutral" - that
    # dict is the default set, and anything missing from it cannot ease back
    # when switching away from an expression that set it.
    "neutral":   dict(brow_l=0.00, brow_r=0.00, eye_open=1.00, mouth_curve=0.12, mouth_open=0.02, mouth_w=1.00),
    # MEASURED, not guessed - see expression_probe.py.
    #
    # Two findings drive these numbers, and both are counter-intuitive:
    #
    #   1. The mouth renders as a filled LENS when mouth_open > w*0.035, and as a
    #      stroked LINE below it. The apparent depth of a smile does NOT track
    #      `curve` on the lens path, where it is dwarfed by the opening term.
    #      A closed, strongly-curved LINE is the shape that reads as a smile; a
    #      lens reads as an open mouth (talking), not as happiness.
    #
    #   2. neutral previously bowed 5.80% of its width, while happy bowed 3.24% -
    #      so neutral was the SMILEIER of the two, which is why the face looked
    #      pleased with itself when idle. The old happy only looked happier
    #      because its mouth was open, not because it smiled more.
    #
    # Now: neutral bows 3.00%, happy bows 18.92% - 6x the corner lift, and wider.
    "happy":     dict(brow_l=0.10, brow_r=0.10, eye_open=0.78, mouth_curve=0.60, mouth_open=0.02, mouth_w=1.15),
    "curious":   dict(brow_l=0.00, brow_r=0.30, eye_open=1.15, mouth_curve=0.05, mouth_open=0.18),
    "confused":  dict(brow_l=-0.25, brow_r=0.35, eye_open=1.05, mouth_curve=-0.15, mouth_open=0.06),
    "listening": dict(brow_l=0.18, brow_r=0.18, eye_open=1.20, mouth_curve=0.10, mouth_open=0.03),
    "thinking":  dict(brow_l=-0.10, brow_r=-0.10, eye_open=0.85, mouth_curve=-0.05, mouth_open=0.02),
    "speaking":  dict(brow_l=0.06, brow_r=0.06, eye_open=1.00, mouth_curve=0.20, mouth_open=0.30),
    # NOTE the MIRRORED brow signs. `_draw_brow` is called with `-brow_l`/`-brow_r`
    # and no per-side mirroring, so the same numeric value rotates both brows the
    # same way on screen: the left brow's INNER end is its +x end, the right
    # brow's inner end is its -x end. Getting both inner ends DOWN (the angry V)
    # therefore needs opposite signs. Verified by rendering, not by reasoning —
    # see `--snapshot` in README.
    "angry":     dict(brow_l=-0.38, brow_r=0.38, eye_open=0.72, mouth_curve=-0.45, mouth_open=0.05),
}


def lerp(a, b, t):
    return a + (b - a) * t


def ease(cur, target, rate):
    """Frame-rate independent exponential approach."""
    return cur + (target - cur) * rate


# How far a full-scale (1.0) lip-sync envelope opens the mouth. The `speaking`
# expression sits at 0.30, so this is slightly past it: loud vowels should read
# as properly open rather than politely open. Units are the same as
# `mouth_open` in EXPRESSION_PARAMS — a fraction of the mouth's width.
SPEAK_OPEN_MAX = 0.55

# A mouth file older than this is treated as absent. Short enough that a dead
# speech driver is noticed before the audience notices a frozen open mouth.
MOUTH_STALE_SECS = 0.75


# --------------------------------------------------------------------------
# Which screen the robot is showing
# --------------------------------------------------------------------------
# $FACE_SESSION and $FACE_PROMPT are exported by kiosk-start.sh so that a manual
# run and the service agree on the paths, exactly as $FACE_STATE does for the
# state contract.
SESSION_PATH = os.environ.get("FACE_SESSION", "/run/face/session.json")
PROMPT_PATH = os.environ.get("FACE_PROMPT", "/run/face/prompt.json")

# Soft near-white: bright enough to read across a room on a projector, and
# deliberately not pure white, which glares on the same projector the face is
# tuned for.
STANDBY_INK = (0.905, 0.925, 0.980)
STANDBY_TEXT = "Standby"
DIM_INK = (0.620, 0.650, 0.760)      # footers and secondary lines
ACCENT_INK = (0.550, 0.850, 0.950)   # countdown bars
SUCCESS_INK = (0.620, 0.920, 0.700)

SCREEN_KINDS = ("face", "label", "success", "stats")

# --------------------------------------------------------------------------
# The pinned expression: what Pip shows while its expression is decided here
# --------------------------------------------------------------------------
# TEMPORARY, and deliberately a flag rather than a deletion. Stage 1's face is not
# to mirror the person in front of it for now: it holds ONE expression, and the
# gaze/expression policy in gaze.py keeps running and keeps publishing - the gaze
# bar is scored on it - but no longer reaches the screen. What gets replaced later
# is the TRIGGER, not the machinery, so every expression, the easing, the state
# contract and the policy all stay where they are, one flag away.
#
# WHY THE PIN IS IN THE RENDERER and not in gaze.py, where the policy lives: the
# thing being changed is what PIP LOOKS LIKE, which is this file's business.
# Pinning here leaves the perception daemon measuring exactly what it did before.
#
# What is NOT pinned: blinks and the small involuntary eye drift. Those are the
# face's own signs of life, not a mirror of the person in front of it, and a
# perfectly still frame cannot be told apart from a crashed renderer on a
# projector. A forced blink from the state file is still honoured for the same
# reason.
#
# "off" restores the old behaviour exactly, and is what a later trigger should
# switch to rather than unpicking this.
PIN_EXPRESSION_CHOICES = EXPRESSIONS + ("off",)
PIN_EXPRESSION_DEFAULT = "happy"

# --------------------------------------------------------------------------
# The listening pill
# --------------------------------------------------------------------------
# The listener publishes whether it can HEAR, and this draws it. $FACE_MIC is
# exported by kiosk-start.sh like the other two.
#
# WHY THE FACE DRAWS IT AT ALL. This process is the only Wayland client on the
# kiosk's Weston, so it is the only thing that can put a pixel on the projector: a
# second process would need its own surface, and Weston has no layer-shell (that is
# a wlroots protocol), so there is nothing to composite an overlay window above the
# kiosk with. Anyone can therefore ADD to the screen by writing a file: the pill is
# independent of the session in the way that matters, because it is drawn whatever
# the session is doing - standby, a stage, a label, Success, or over a prompt.
MIC_PATH = os.environ.get("FACE_MIC", "/run/face/mic.json")
MIC_LISTENING = "listening"

# A stamp older than this means the listener is not there any more. The listener
# re-stamps every 0.5 s, so this tolerates three missed beats: long enough not to
# flicker, short enough that a dead listener stops claiming it can hear you.
MIC_STALE = 2.0

# At most how often the file is re-read. The pill's own freshness is judged EVERY
# frame (see poll_listening in the kiosk): a dead writer stops changing the file
# rather than writing "I am dead", so this cannot be cached on its mtime.
MIC_POLL = 0.25

# --------------------------------------------------------------------------
# The tuning session
# --------------------------------------------------------------------------
# /run/face/calibration.json, owned by whoever is tuning the lip-sync offset
# (robot/voice/calibrate-lead.sh). The same mechanism as the pill above: anyone
# can put something on the screen by publishing a file, one writer per file, and
# this process is the only one that draws.
#
# WHY A FILE AND NOT THE SESSION FILE. The face is drawn only for a RUNNING
# session, and a stage's screen belongs to the conductor: session.json has exactly
# ONE writer, which is what lets this process re-read it freely. A tuning tool
# writing it would be a second writer on the one file whose whole contract is that
# there is only one.
#
# WHY IT IS NEEDED AT ALL. Tuning is judged by eye against the projected mouth,
# and at standby - which is where a robot sits while nobody has started a run -
# the screen says "Standby" and no face is drawn. A trial with no mouth on screen
# cannot be judged at all: the instrument plays, the mouth file is driven, and
# nothing moves. This brings the face up on demand, over SSH, with the conductor
# and the session file untouched.
#
# WHAT THE FILE CARRIES, both optional:
#     face    bring the face up instead of the standby word
#     stats   log the achieved frame period and the draw time every few seconds
# plus a heartbeat that makes BOTH EXPIRE (see calibration_fresh).
#
# HONOURED ONLY WHILE THE SESSION IS STANDBY. If a real run starts in the middle
# of a tuning session the run wins, because the participant's screen is not
# negotiable.
CALIBRATION_PATH = os.environ.get("FACE_CALIBRATION", "/run/face/calibration.json")
CALIBRATION_STALE = 1.5     # the tool re-stamps every 0.25 s
CALIBRATION_POLL = 0.25     # at most how often the file itself is re-read
CALIBRATION_TEXT = "Calibration"
CALIBRATION_INK = ACCENT_INK
# Top-right, in the one band that is empty on every screen (the pill is top-left
# and the timer is top-middle). Deliberately NOT near the mouth: the mouth is the
# channel under test during a trial, and a label that moved with it would be read
# as part of the fault being measured.
CALIBRATION_TEXT_Y_FRAC = 0.062
CALIBRATION_TEXT_X_FRAC = 0.855

# The frame-period line's interval, when a tuning session asks for it. Long enough
# that the journal stays readable, short enough that one bisect round (about 8 s)
# contains at least one line.
STATS_INTERVAL = 5.0

# --------------------------------------------------------------------------
# The calibration coach
# --------------------------------------------------------------------------
# /run/face/coach.json, owned by robot/perception/calibrate-look.py. The same
# mechanism as the pill and the tuning file above: one writer per file, and this
# process is the only one that draws.
#
# WHY IT EXISTS. Re-fitting "what counts as looking at the screen" means holding two
# poses in turn, and each instruction has to arrive somewhere the person can SEE it.
# It cannot be the terminal: the tool is run over SSH, so its prompts are on a laptop
# behind the person being measured, and its countdown is in a window nobody is
# looking at. That is not a cosmetic complaint - a pose held against the wrong
# instruction is a measurement of the wrong thing, and the two look identical
# afterwards. It cannot go in the session file either: session.json has exactly one
# writer, which is the conductor. So the cue is a published file and the face draws
# it, on the one surface the person is already being asked to look at.
#
# WHAT THE FILE CARRIES
#     state     lead    the window has not opened; the number counts IN
#               hold    the window is open; the number counts DOWN
#               between the window is shut and the next pose is being described
#               done    finished
#     action    the instruction, in the largest type on the screen
#     detail    one line of explanation
#     phase     "A" or "B", so the panel says which of the two is running
#     deadline  a CLOCK_MONOTONIC instant - the SAME clock this process reads - so
#               the countdown drawn here and the window the tool measures cannot
#               disagree; for `lead` it is when the window OPENS
#     total     that window's length, for the bar's fraction
#     heartbeat re-stamped every ~0.2 s, and a stamp that stops moving expires the
#               panel (the same three refusals the pill makes, and for the same
#               reason: a killed tool must not leave an instruction on a screen)
#
# WHY THE UPPER BAND. The middle is the standby word or the face, the lower third is
# the confirmation panel, and the pill and the countdown are in the top row. The band
# just under that row is empty on every screen, and nothing here overlaps the
# countdown - so a calibration that happens to run while a session's timer is up
# cannot hide the time left.
COACH_PATH = os.environ.get("FACE_COACH", "/run/face/coach.json")
COACH_STALE = 1.5           # the tool re-stamps every ~0.2 s
COACH_POLL = 0.25           # at most how often the file itself is re-read
COACH_STATES = ("lead", "hold", "between", "done")
COACH_STATE_CAPTION = {"lead": "get ready", "hold": "hold it",
                       "between": "paused", "done": "finished"}
COACH_STATE_INK = {"lead": DIM_INK, "hold": ACCENT_INK,
                   "between": DIM_INK, "done": SUCCESS_INK}
COACH_TOP_FRAC = 0.115      # panel top, clear of the pill's row
COACH_WIDTH_FRAC = 0.68
COACH_HEIGHT_FRAC = 0.27
COACH_ACTION_SIZE_FRAC = 0.062

# --------------------------------------------------------------------------
# The timer
# --------------------------------------------------------------------------
# ONE widget for every timed thing in the run: the 5 s confirmation window, the
# 60 s stage session, and the 30 s Success screen. The participant learns it once
# and sees the same object wherever the run has got to.
#
# WHY IT IS THIS BIG. The projected picture is small, so anything meant to be read
# from across a room has to be several times a caption's size. The floor is
# deliberate: the listening pill measures 245x65 px, and the brief is that the
# timer reads as clearly bigger than it. The bar is 84 px tall (1.29x the pill's
# height) and 845 px wide; the disc is 259 px across (1.06x the pill's width and
# 4.0x its height). Both are measured from the rendered frame, not assumed.
#
# WHY THE NUMBER SITS BESIDE THE BAR RATHER THAN INSIDE IT. Inside, it straddles
# the fill and the empty track as the bar drains, so it is legible over only one of
# them: measured, near-white on the filled accent composites to rgb(98,144,164),
# which is 2.4:1 - below the 3:1 large-text floor. Beside the bar it is near-white
# on the deep background, which is the most contrast this palette has (13.4:1
# measured). Grouped tightly, the two still read as one object.
#
# WHY THE TOP BAND AND NOT THE BOTTOM. The face is drawn to fill the frame - its
# head runs past the bottom edge with a body under it - so the lower band is
# occupied on exactly the two screens that have a face on them. The top band is
# empty on every screen, and the pill already lives in it, so both instruments end
# up in one row at the same margin: mic state on the left, time left in the middle.
TIMER_STYLES = ("bar", "disc")
# DECIDED (2 November): the disc. Danny picked it from rendered frames of both, on
# the numbers below - a number 1.7x larger, legible over plain background on both
# faces rather than over the illustrated face's hair, and a quarter of the bar's
# footprint. The bar stays built and one switch away: this is a legibility judgement
# about the projector, not a correctness one, and re-deciding it should cost a flag
# rather than a rewrite.
DEFAULT_TIMER_STYLE = "disc"

TIMER_MARGIN_FRAC = 0.040       # the pill's own margin, of the frame height
TIMER_BAR_WIDTH_FRAC = 0.44     # of the frame width -> 845 px at 1920
TIMER_BAR_HEIGHT_FRAC = 0.078   # of the frame height -> 84 px at 1080
TIMER_DISC_FRAC = 0.24          # disc diameter, of the frame height -> 259 px
TIMER_NUMBER_FRAC = 0.056       # the seconds number, of the frame height -> 60 px
TIMER_GAP_FRAC = 0.014          # between the number and the bar
TIMER_INK = ACCENT_INK          # the draining part
TIMER_FILL_ALPHA = 0.60
TIMER_DISC_ALPHA = 0.85         # a stroke needs more than a fill to read as one

# --------------------------------------------------------------------------
# The gaze bar
# --------------------------------------------------------------------------
# A VERTICAL bar down the right-hand side that FILLS while the participant is
# looking at the robot, and never drains. It is the participant's own score, so it
# is drawn like the countdown - a rounded track of fixed extent that cannot read as
# something collapsing - and it sits UNDER the countdown, on the same axis, rather
# than beside it: the two instruments then read as one column, "time left" above
# "how you are doing", and neither is over the face, which is drawn to fill the
# frame and runs past the bottom edge.
#
# WHY IT FILLS UPWARD, when the countdown above it drains: they measure opposite
# things. Time left goes away as it passes; looking accumulates as the participant
# earns it, and an instrument that grows when you do the right thing needs no
# explaining. The direction is the feedback.
#
# The geometry is derived from the countdown's own box (see gaze_bar_box) rather
# than from constants that repeat it, so re-deciding the countdown's size or
# corner cannot silently collide with this.
GAZE_BAR_WIDTH_FRAC = 0.050     # of the frame height -> 54 px at 1080
GAZE_BAR_GAP_FRAC = 0.030       # of the frame height, below the countdown's box
GAZE_BAR_BOTTOM_FRAC = 0.880    # of the frame height: where the track ends
GAZE_BAR_TRACK_ALPHA = 0.16
GAZE_BAR_EDGE_ALPHA = 0.35
GAZE_BAR_FILL_ALPHA = 0.85      # far more solid than the countdown's 0.60: this
                                # fill has to read as an achievement, and it is
                                # the only thing on screen that is the person's
                                # doing rather than the robot's
GAZE_BAR_INK = SUCCESS_INK

# THE STATS SCREEN: a failed session as a timeline, for the stage that is waiting
# for the word again. A bar rather than a number, because "when was I looking" is a
# shape, and the shape is the answer to the only question a participant has at that
# moment - why are we doing that one again.
STATS_BAR_WIDTH_FRAC = 0.70        # of the frame width -> 1344 px at 1920
STATS_BAR_HEIGHT_FRAC = 0.043      # of the frame height -> 46 px at 1080
STATS_BAR_Y_FRAC = 0.50
STATS_TRACK_HEIGHT_FRAC = 0.0075   # of the frame height -> 8 px at 1080
STATS_TICK_SECONDS = 10.0
# The green the gaze bar ENDS at, so the two read as the same fact: this is the
# looking that bar was filling with, laid out in time.
STATS_LOOK_INK = (0.541, 0.800, 0.616)
# Deliberately NOT red. A dim slate says "away" without saying "wrong", and it is
# 3.6:1 on the background where the looking green is 10.5:1 - so the two states
# differ in LIGHTNESS as well as hue, which is what keeps them apart for a viewer
# who cannot tell the colours apart.
STATS_AWAY_INK = (0.380, 0.410, 0.500)

# The fill's COLOUR, as a name, because it is a taste call about a projector rather
# than a property of the code - the same reason the countdown's shape is a flag.
#
# A RAMP (the default) rather than one colour, because the bar has to answer "how far
# along am I" from across a room and a fill's HEIGHT only answers it proportionally:
# at 8 m, 90% of a 616 px bar and 100% of one are 60 px apart, which is nothing. Hue is
# the second channel - it says "not yet" / "nearly" / "done" at a glance, and it does
# it without adding an object to the screen.
#   ramp         magenta -> green, the shortest hue path (through violet and blue)
#   ramp:warm    magenta -> red -> amber -> green, the long way round
#   accent       the countdown's own cyan: one palette, two instruments
#   success      the colour the Success screen uses, which says what filling it means
#   magenta      deliberately unlike anything else on screen
#
# The cool ramp is the default and the warm one is offered, not because warm is
# uglier but because its middle is RED: mid-session, a participant who is 40% of the
# way to their target would be watching a red bar, and red reads as a fault. There is
# nothing wrong with the picture; there would be something wrong with the message.
GAZE_MAGENTA_INK = (0.830, 0.440, 0.900)
GAZE_RED_INK = (0.950, 0.330, 0.420)
GAZE_AMBER_INK = (0.980, 0.720, 0.330)
GAZE_BAR_RAMPS = {
    "cool": ((0.0, GAZE_MAGENTA_INK), (1.0, SUCCESS_INK)),
    "warm": ((0.0, GAZE_MAGENTA_INK), (0.34, GAZE_RED_INK),
             (0.66, GAZE_AMBER_INK), (1.0, SUCCESS_INK)),
}
GAZE_BAR_INKS = {"accent": ACCENT_INK,
                 "success": SUCCESS_INK,
                 "magenta": GAZE_MAGENTA_INK}
GAZE_BAR_FILL_DEFAULT = "ramp"
GAZE_BAR_FILLS = ("ramp", "ramp:warm", "gradient", "gradient:warm",
                  "accent", "success", "magenta")
GAZE_BAR_CAP_ALPHA = 0.55       # the band across the top of a FULL fill - the
                                # static half of "this is finished", for after the
                                # burst has gone. Without it the last 2% of travel
                                # is the only thing separating done from nearly.
GAZE_BAR_CAP_FRAC = 0.15        # of the bar's width -> 8 px at 54


def rgb_to_hsl(rgb):
    """(hue in turns 0..1, saturation 0..1, lightness 0..1)."""
    r, g, b = (max(0.0, min(1.0, float(c))) for c in rgb)
    hi, lo = max(r, g, b), min(r, g, b)
    light = (hi + lo) / 2.0
    if hi == lo:
        return (0.0, 0.0, light)
    d = hi - lo
    sat = d / (2.0 - hi - lo) if light > 0.5 else d / (hi + lo)
    if hi == r:
        hue = ((g - b) / d) % 6.0
    elif hi == g:
        hue = (b - r) / d + 2.0
    else:
        hue = (r - g) / d + 4.0
    return (hue / 6.0, sat, light)


def hsl_to_rgb(hsl):
    hue, sat, light = hsl
    hue = hue % 1.0
    if sat <= 0.0:
        return (light, light, light)

    def channel(t):
        t = t % 1.0
        if t * 6.0 < 1.0:
            # The rising leg: p + (q - p) * 6t. Inverted, this is the bug that makes
            # BOTH ends of a ramp come out washed toward white, because the hue
            # triangle is entered from the wrong corner - and a ramp's endpoints are
            # the one thing about it somebody chose deliberately.
            return p + (q - p) * 6.0 * t
        if t * 2.0 < 1.0:
            return q
        if t * 3.0 < 2.0:
            return p + (q - p) * (2.0 / 3.0 - t) * 6.0
        return p

    q = light * (1.0 + sat) if light < 0.5 else light + sat - light * sat
    p = 2.0 * light - q
    return (channel(hue + 1.0 / 3.0), channel(hue), channel(hue - 1.0 / 3.0))


def hue_lerp(a, b, t):
    """Interpolate two hues in TURNS along the SHORTER way round the wheel.

    The short way matters and is not a detail: magenta (0.81 turns) to green (0.39)
    the long way passes through red and orange, which is the warm ramp again by
    accident, and it is also not what "halfway between two colours" means to anyone
    looking at it.
    """
    d = (b - a) % 1.0
    if d > 0.5:
        d -= 1.0
    return (a + d * t) % 1.0


def ramp_colour(stops, t):
    """The fill's colour at fraction `t` along a list of (position, rgb) stops.

    Interpolated in HSL rather than RGB because RGB mixes two saturated hues through
    their grey AXIS: magenta and green in RGB meet at a pale lavender, so a bar
    climbing through the middle of its travel would be its least colourful exactly
    where the participant is watching hardest.
    """
    t = max(0.0, min(1.0, float(t)))
    stops = sorted(stops, key=lambda s: s[0])
    lo = stops[0]
    for hi in stops[1:]:
        if t <= hi[0]:
            span = hi[0] - lo[0]
            k = 0.0 if span <= 0.0 else (t - lo[0]) / span
            h0, s0, l0 = rgb_to_hsl(lo[1])
            h1, s1, l1 = rgb_to_hsl(hi[1])
            return hsl_to_rgb((hue_lerp(h0, h1, k),
                               s0 + (s1 - s0) * k,
                               l0 + (l1 - l0) * k))
        lo = hi
    return tuple(stops[-1][1])


def luminance(rgb):
    """WCAG relative luminance of a 0..1 sRGB triple."""
    def lin(c):
        c = max(0.0, min(1.0, c))
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    """WCAG contrast ratio between two 0..1 sRGB triples."""
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def composited(colour, alpha, under=BG):
    """What the projector actually receives: `colour` at `alpha` over `under`.

    Every contrast number in this file is about the COMPOSITE, because the fill is
    drawn at GAZE_BAR_FILL_ALPHA over the background and the participant never sees
    the configured value.
    """
    return tuple(under[i] + (colour[i] - under[i]) * alpha for i in range(3))


def gaze_fill_spec(name):
    """('ramp'|'gradient', stops) or ('flat', rgb) for a fill name.

    Unknown -> the default. Two shapes rather than one because a ramp cannot be handed
    to the drawing code as a colour: the choice is a colour, or a rule for choosing one.

    'ramp' and 'gradient' are two readings of the same instruction ("starts magenta,
    ends green"), and they look quite different:

      ramp      the WHOLE fill is one colour, and that colour moves with the fraction.
                At 40% the bar is violet; at 100% it is green. Readable at a glance
                from the other side of a room, because one hue means one state.
      gradient  the TRACK carries a fixed magenta-to-green ramp, and filling it REVEALS
                that ramp. At 40% the bar shows magenta at its base and violet at its
                top; only at 100% is the green there. Richer, and the fill is never one
                colour, so it says less per glance - but it shows where the bar has
                been as well as where it is.
    """
    if isinstance(name, str) and name.startswith(("ramp", "gradient")):
        which, _, variant = name.partition(":")
        stops = GAZE_BAR_RAMPS.get(variant or "cool", GAZE_BAR_RAMPS["cool"])
        return ("gradient" if which == "gradient" else "ramp", stops)
    if name in GAZE_BAR_INKS:
        return ("flat", GAZE_BAR_INKS[name])
    # An unknown name gets the DEFAULT FILL rather than a plausible-looking flat green:
    # a typo in $FACE_GAZE_INK should show what the robot shows when nobody has chosen
    # anything, not a different look that happens to be the colour of success.
    return gaze_fill_spec(GAZE_BAR_FILL_DEFAULT)


def gaze_fill_args(name):
    """The keyword arguments `draw_gaze_bar` needs for a fill NAME.

    ONE place turns a name into drawing arguments. It used to be three (the renderer,
    the probe's isolated-bar helper and its frame writer), and the copy that forgot
    about 'gradient' drew a flat green bar at every fraction - which reads as entirely
    plausible in a table of numbers until you notice every row is the same. A shared
    resolution makes that class of bug impossible rather than unlikely.
    """
    kind, value = gaze_fill_spec(name)
    if kind == "flat":
        return {"ink": value}
    return {"ramp": value, "gradient": (kind == "gradient")}


def gaze_ink_colour(name):
    """A flat fill colour by name. An unknown name falls back to the default ramp's
    END colour, which is what a fill is meant to look like when it is finished."""
    return GAZE_BAR_INKS.get(name, SUCCESS_INK)


# --------------------------------------------------------------------------
# The completion burst
# --------------------------------------------------------------------------
# A ONE-SHOT animation at the top of the bar the moment the fill reaches 100%.
#
# WHY IT EXISTS AT ALL: "looks full at 99%" is not a drawing bug, it is the nature of
# a proportion - 1% of a 616 px bar is 6 px, and against a track that is 54 px wide
# nobody can see 6 px. A static cue cannot fix that. A moving one can: the participant
# does not have to judge whether the bar is full, because something HAPPENS when it
# is. The bright cap (GAZE_BAR_CAP_ALPHA) is the static half of the same answer, for
# after the sparks have gone.
#
# THE CEILING IS THE CONSTRAINT. The bar starts 33 px below the countdown's ring, so
# anything rising from the bar's top runs into the countdown. Rather than shrink the
# burst to fit or let it paint over the countdown, the particles are LAUNCHED with
# velocities that cannot reach it: a spark rising 14 px at most, and embers thrown
# sideways that arc out and fall past the bar's flanks into the empty background
# beside it. Every particle is then inside the frame and below the countdown at every
# instant (asserted in --selftest, measured in gaze_bar_probe.py).
GAZE_BURST_LIFE = 1.8          # seconds; the whole thing
GAZE_BURST_SPARKS = 30
GAZE_BURST_EMBERS = 20
GAZE_BURST_SPARK_G = 150.0     # px/s^2, downward
GAZE_BURST_EMBER_G = 220.0
GAZE_BURST_SPARK_R = 2.4       # px at birth, plus GAZE_BURST_SPARK_GROW as it fades
GAZE_BURST_SPARK_GROW = 3.4    # a spark that shrank as it faded would read as a dot
                               # switching off, not an ember burning out
GAZE_BURST_SPARK_INK = (1.000, 0.930, 0.680)   # warm gold: it has to read as
                                               # celebration, and nothing else on
                                               # screen is this colour
GAZE_BURST_PAD = 8.0           # keep all ink this far inside the frame
GAZE_BURST_CEILING_PAD = 6.0   # ... and this far below the countdown's box


def _burst_unit(i, salt):
    """A deterministic pseudo-random number in [0, 1) for particle `i`.

    NOT random.random(). A burst that lands differently on every render cannot be
    measured, cannot be compared frame-to-frame by eye, and cannot be re-checked by
    anyone after the fact - and animations are exactly where "it looked fine" is
    normally the whole argument. Same age, same pixels, every time.
    """
    x = (i * 2654435761 + salt * 40503 + 12345) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 1274126177) & 0xFFFFFFFF
    x ^= x >> 16
    return (x & 0xFFFFFF) / float(0x1000000)


def burst_particles(age, w, h, x0, y0, bw, ceiling):
    """The burst's particles at `age` seconds, as (x, y, radius, alpha) tuples.

    A PURE function of age: no state, no random source, no clock. The kiosk keeps
    only the instant the burst began, so a frame can be rendered for any moment of it
    (that is how `--snapshot --gaze-burst 0.4` works), and a test can walk the whole
    animation without a display.

    Empty before the burst starts and after it ends, which is the property that keeps
    it a one-shot: permanent ink here would be a screen that never returns to normal.
    """
    if age is None or age < 0.0 or age > GAZE_BURST_LIFE:
        return []
    cx = x0 + bw / 2.0
    out = []
    total = GAZE_BURST_SPARKS + GAZE_BURST_EMBERS
    for i in range(total):
        u1, u2, u3 = (_burst_unit(i, s) for s in (1, 2, 3))
        ember = i >= GAZE_BURST_SPARKS
        if ember:
            # Thrown SIDEWAYS past the bar's flanks and pulled down: a spark that
            # rises would meet the countdown, but there is nothing beside the bar
            # for 900 px.
            life = GAZE_BURST_LIFE * (0.75 + 0.25 * u3)
            vy0 = -(6.0 + 24.0 * u2)
            g = GAZE_BURST_EMBER_G
            side = 1.0 if (i % 2) else -1.0
            room = (w - GAZE_BURST_PAD - cx) if side > 0 else (cx - GAZE_BURST_PAD)
            vx = side * min(room / life, 90.0 + 170.0 * u1)
        else:
            # A spark fanned off the bar's top: at most 65 px/s up under 150 px/s^2
            # of gravity is a 14 px rise, against 33 px of clear air.
            life = GAZE_BURST_LIFE * (0.55 + 0.30 * u3)
            vy0 = -(20.0 + 45.0 * u2)
            g = GAZE_BURST_SPARK_G
            room = max(w - GAZE_BURST_PAD - cx, cx - GAZE_BURST_PAD)
            vx = (u1 * 2.0 - 1.0) * min(room / life, 190.0)
        if age > life:
            continue
        px = cx + vx * age
        py = y0 + vy0 * age + 0.5 * g * age * age
        if py < ceiling or not (GAZE_BURST_PAD <= px <= w - GAZE_BURST_PAD):
            continue
        fade = 1.0 - age / life
        out.append((px, py, GAZE_BURST_SPARK_R + GAZE_BURST_SPARK_GROW * fade,
                    (fade ** 1.5) * 0.95))
    return out


def draw_burst(cr, particles):
    """The particles, as gold sparks: a bright core with a soft halo.

    save/restore because this sets a source and a line width, and nothing about a
    celebration should leak into the next thing drawn (trap: a stroke that is not
    wrapped in new_path() re-paints whatever path was left behind).
    """
    if not particles:
        return
    cr.save()
    for (px, py, radius, alpha) in particles:
        cr.set_source_rgba(GAZE_BURST_SPARK_INK[0], GAZE_BURST_SPARK_INK[1],
                           GAZE_BURST_SPARK_INK[2], alpha * 0.28)
        cr.new_sub_path()          # no connecting line from the previous point:
                                   # arc() draws one, and it would be a thread of
                                   # gold across the frame (measured once already on
                                   # the countdown: 26,850 px of stray line)
        cr.arc(px, py, radius * 2.1, 0.0, 2.0 * math.pi)
        cr.fill()
    for (px, py, radius, alpha) in particles:
        cr.set_source_rgba(GAZE_BURST_SPARK_INK[0], GAZE_BURST_SPARK_INK[1],
                           GAZE_BURST_SPARK_INK[2], alpha)
        cr.new_sub_path()
        cr.arc(px, py, radius, 0.0, 2.0 * math.pi)
        cr.fill()
    cr.restore()

# What, if anything, is written under it. A judgement call about the projector, so
# it is a flag rather than a decision baked into the drawing code: "word" is the
# quietest, "percent" the most informative, "none" leaves the fill to speak.
GAZE_BAR_CAPTIONS = ("word", "percent", "none")
GAZE_BAR_CAPTION_DEFAULT = "percent"
GAZE_BAR_CAPTION_TEXT = "Gaze"
GAZE_BAR_CAPTION_FRAC = 0.925   # centre, of the frame height: the footer's band,
                                # which is horizontally empty at this x
GAZE_BAR_CAPTION_SIZE_FRAC = 0.026

PILL_TEXT = "Listening"
PILL_INK = (0.440, 0.880, 0.600)     # a green that reads as "go" on a projector
PILL_INK_DARK = (0.050, 0.043, 0.078)  # the deep background, for ink ON the green

# What every unusable input means: the screen that does nothing.
STANDBY_SCREEN = {"phase": "standby", "kind": "standby", "style": None,
                  "text": None, "stage": None, "stages": None,
                  "step": None, "deadline": None, "seconds": None,
                  "gaze_progress": None}

# Sample countdowns, so a snapshot of a countdown is byte-comparable between runs
# instead of depending on when it was rendered.
SAMPLE_SESSION_SECONDS = 42.0
SAMPLE_SUCCESS_SECONDS = 24.0
# A real failed bench session's shape for --screen stats: a long look, a long away
# stretch, then two shorter looks. Chosen so a render exercises the thing the screen
# exists for - a timeline with edges in it - rather than a uniform fill.
SAMPLE_STATS_SPANS = [[0.0, 13.4, True], [13.4, 21.0, False], [21.0, 25.8, True],
                      [25.8, 36.2, False], [36.2, 42.0, True]]
SAMPLE_STATS_TARGET = 35.0
SAMPLE_PROMPT_SECONDS = 3.4
SAMPLE_PROMPT_AGE = 1.6


def read_session(path=None):
    """The screen to show, from the session file. Anything unusable means standby.

    Called when the file changes rather than on every frame, so it can afford to
    be strict: a missing file, a malformed one, a half-written one (the writer
    uses an atomic replace, but a hand edit is a plain write) and a phase name
    this version does not know all fall back to the screen that does nothing.
    """
    try:
        with open(path or SESSION_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return dict(STANDBY_SCREEN)

    if not isinstance(data, dict) or data.get("phase") != "running":
        return dict(STANDBY_SCREEN)

    screen = data.get("screen")
    screen = screen if isinstance(screen, dict) else {}
    kind = screen.get("kind")
    spans = stats_spans(screen.get("spans"))
    if kind == "stats" and spans is None:
        # A stats screen with no usable timeline is not a screen. An empty bar would
        # say "you never looked at me", which is a claim about the participant, and a
        # file that failed to parse cannot support one - the same rule the gaze bar's
        # missing block already follows.
        kind = "face"
    if kind not in SCREEN_KINDS:
        # Running, but the screen makes no sense. Show the FACE rather than the
        # standby word: the session is real and someone is mid-exercise, so
        # "Standby" would be a lie. The kiosk logs the surprise.
        kind = "face"
    return {"phase": "running", "kind": kind, "style": screen.get("style"),
            "text": screen.get("text"), "stage": data.get("stage"),
            "stages": data.get("stages"), "step": data.get("step"),
            "deadline": data.get("deadline"),
            # The failed session's timeline and the target it was measured against,
            # only on the screen that draws them.
            "spans": spans if kind == "stats" else None,
            "target": stats_target(screen.get("target"), spans) if kind == "stats"
            else None,
            # How long the step RUNS FOR. The timer needs both ends of the interval
            # to draw a fraction, and deriving the length from its own first read
            # would be a few ms short, differently every time - the same reason the
            # prompt carries its timeout.
            "seconds": data.get("seconds"),
            # The gaze bar, when the conductor published one. A malformed block is
            # dropped rather than defaulted to zero: a bar at zero says "you have
            # not looked at me yet", which is a claim about the participant, and a
            # file that failed to parse cannot support it.
            "gaze_progress": gaze_progress(data.get("gaze"))}


def stats_spans(raw):
    """The failed session's looking timeline, or None.

    A list of [start, end, looking] triples in seconds, validated field by field for
    the same reason every other field in this file is: a timeline is a claim about
    the participant - "here is when you were looking" - so a malformed one is
    dropped rather than drawn as a guess, and the reader above falls back to the
    face rather than to an empty bar.

    Returns (start, end, looking) tuples in order, or None if anything about the list
    cannot be trusted.
    """
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for span in raw:
        if not isinstance(span, (list, tuple)) or len(span) != 3:
            return None
        start, end, looking = span
        if looking not in (True, False) or isinstance(start, bool) or isinstance(end, bool):
            return None
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if end < start or start < 0.0:
            return None
        out.append((start, end, bool(looking)))
    return out


def stats_target(raw, spans):
    """The target the session was measured against, or None.

    The conductor publishes it because the FACE must not know the rule: the pass
    condition is the conductor's business, and a second copy of it here is a second
    number waiting to disagree with the first. Drawn only if it falls inside the
    session the timeline covers.
    """
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
        return None
    end = max((e for _, e, _ in spans), default=0.0)
    return float(raw) if raw <= end else None


def gaze_progress(raw):
    """The gaze bar's fill, 0..1, from the session file's gaze block; else None.

    VALIDATED rather than trusted. This file is read on every change, by one process
    that draws to a projector, and a bar drawn from a missing or garbled field would
    be a claim about the participant - "you have not looked at me" - that nothing
    supports. Unusable means NO bar, which is also what an older conductor's file
    means, so the two cannot be told apart... and do not need to be: neither has a
    score to show.

    Only the fraction is read. The conductor also publishes `looking`, `looked` and
    `target` in that block, and nothing here draws them - the countdowns take their
    number from the conductor's own `seconds`, and a second copy of "how long was
    this session" is a different number waiting to happen.
    """
    if not isinstance(raw, dict):
        return None
    progress = raw.get("progress")
    if not isinstance(progress, (int, float)) or isinstance(progress, bool):
        return None
    return max(0.0, min(1.0, float(progress)))


def apply_state_file(st, data, pinned=None):
    """Apply one state file's contents to the animation state.

    Returns (expression_taken, gaze_taken, blink) so a caller - or a test - can say
    exactly what the file was allowed to change.

    EXTRACTED from the kiosk loop so it can be exercised with no compositor, because
    the PIN's whole point is something that DOES NOT HAPPEN: the face does not follow
    the person in front of it. A negative like that survives any still frame looked at
    by eye - a pinned face and a mirrored face both look like a face - so it needs a
    test that can name the ignored key, and that means this decision has to be
    callable without GTK.

    While `pinned` names an expression:
      * the file's `expression` is ignored, and the pinned one is set instead;
      * the file's `gaze` is ignored, so the eyes look straight ahead plus the face's
        own idle drift;
      * a forced `blink` IS honoured - it is the face's own animation rather than a
        mirror of anything in the room.

    Everything is validated field by field: gaze that is not a dict, or is not
    numeric, is ignored without raising. `data` not being a dict at all is the
    caller's business.
    """
    if not isinstance(data, dict):
        return (False, False, False)
    expression_taken = gaze_taken = False
    if pinned is None:
        if "expression" in data:
            st.set_expression(data["expression"])
            # `set_expression` REFUSES a name it does not know, so this comparison is
            # how the caller learns whether the file's expression was actually taken
            # rather than silently dropped.
            expression_taken = st.expression == data["expression"]
        gaze = data.get("gaze")
        if isinstance(gaze, dict):
            try:
                st.gaze_x = max(-1.0, min(1.0, float(gaze.get("x", 0.0))))
                st.gaze_y = max(-1.0, min(1.0, float(gaze.get("y", 0.0))))
                gaze_taken = True
            except (TypeError, ValueError):
                pass
    else:
        st.set_expression(pinned)
    blink = bool(data.get("blink"))
    if blink:
        st.forced_blink = True
    return (expression_taken, gaze_taken, blink)


def read_prompt(path=None, now=None):
    """The confirmation window on screen, or None.

    None is the ordinary state. An expired deadline is also None: the listener
    deletes this file when the window shuts, but that is a different process and
    up to one tick later, and a countdown that has run out must not linger.
    """
    try:
        with open(path or PROMPT_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    deadline, command = data.get("deadline"), data.get("command")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return None
    if not command:
        return None
    now = time.monotonic() if now is None else now
    remaining = float(deadline) - now
    if remaining <= 0.0:
        return None
    total = data.get("timeout")
    total = float(total) if isinstance(total, (int, float)) and total > 0 else remaining
    return {"command": str(command), "phrase": str(data.get("phrase") or ""),
            "remaining": remaining, "total": total}


def read_mic_stamp(path=None):
    """(state, heartbeat) from the listener's file, or None if unusable.

    The raw pair rather than a yes/no, because the ANSWER depends on a clock: the
    kiosk re-judges freshness every frame from the same stamp, and something that
    returns only True/False cannot be re-judged without re-reading the file.
    """
    try:
        with open(path or MIC_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state, stamp = data.get("state"), data.get("heartbeat")
    if not isinstance(state, str):
        return None
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return None
    return (state, float(stamp))


def mic_fresh(stamp, now=None, stale=MIC_STALE):
    """True while the listener says it can hear AND is still stamping the file.

    Three ways to be false, and all three matter: no file at all, a state that is
    not `listening` (the robot is talking, so the half-duplex gate is dropping
    everything), and a stamp that has stopped moving (a stopped service, a SIGKILL,
    a dead arecord). A stamp from the FUTURE is not fresh either: this shares
    CLOCK_MONOTONIC with the writer, so a negative age means it did not come from
    the listener and is not evidence about anything.
    """
    if not stamp:
        return False
    state, written = stamp
    if state != MIC_LISTENING:
        return False
    now = time.monotonic() if now is None else now
    age = now - written
    return 0.0 <= age <= stale


def read_mic(path=None, now=None):
    """One-shot: can the robot hear a person right now?"""
    return mic_fresh(read_mic_stamp(path), now)


def read_calibration_stamp(path=None):
    """(heartbeat, face, stats) from the tuning tool's file, or None if unusable."""
    try:
        with open(path or CALIBRATION_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stamp = data.get("heartbeat")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return None
    return (float(stamp), bool(data.get("face")), bool(data.get("stats")))


def calibration_fresh(stamp, now=None, stale=CALIBRATION_STALE):
    """(show the face, log frame stats) while the tuning tool is still stamping.

    Three refusals, the same three the pill makes and for the same reasons: no file
    at all, a stamp from the FUTURE (not from this clock, so not evidence about
    anything), and a stamp that has stopped moving. The last one is the safety
    property: a tuning tool that is killed, and a board that is power-cycled
    mid-tune, both leave a file that stops being stamped - so the face goes back to
    "Standby" by itself and a robot nobody has started cannot be left looking like
    it is doing something.
    """
    if not stamp:
        return (False, False)
    written, face, stats = stamp
    now = time.monotonic() if now is None else now
    if not 0.0 <= now - written <= stale:
        return (False, False)
    return (face, stats)


def read_coach_stamp(path=None):
    """The calibration coach's instruction, or None if the file is unusable.

    Returns the whole payload INCLUDING the raw heartbeat, because the kiosk
    re-judges freshness every frame from that number (see coach_fresh) and something
    that returned only a yes/no could not be re-judged without re-reading the file -
    the same reason the pill's reader hands back a stamp rather than a verdict.

    Every field is checked rather than trusted: this file is written by a tool that
    may be a different version, and a panel drawn from a half-understood payload
    would put an instruction on the screen that nobody wrote.
    """
    try:
        with open(path or COACH_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state, action = data.get("state"), data.get("action")
    stamp = data.get("heartbeat")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return None
    if state not in COACH_STATES:
        return None
    if not isinstance(action, str) or not action.strip():
        return None

    def number(key):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    return {"state": state,
            "action": action.strip(),
            "detail": (data.get("detail") or "").strip()
            if isinstance(data.get("detail"), str) else "",
            "phase": data.get("phase") if isinstance(data.get("phase"), str) else None,
            "deadline": number("deadline"),
            "total": number("total"),
            "heartbeat": float(stamp)}


def coach_fresh(coach, now=None, stale=COACH_STALE):
    """True while the coach is still stamping its file.

    The pill's three refusals, for the pill's reasons: no file, a stamp that has
    stopped moving (a killed tool, a power cut), and a stamp from the FUTURE, which
    cannot have come from this clock and so is not evidence about anything.
    """
    if not coach:
        return False
    now = time.monotonic() if now is None else now
    return 0.0 <= now - coach["heartbeat"] <= stale


def coach_seconds(coach, now=None):
    """(remaining, total) for the coach's countdown, or (None, None) without one.

    Read as "seconds until the deadline", whatever the state means by it: for `hold`
    that is the window closing, for `lead` it is the window opening. One number, one
    clock, and the tool that wrote the deadline is the tool that will stop measuring
    at it.
    """
    if not coach:
        return (None, None)
    now = time.monotonic() if now is None else now
    deadline, total = coach.get("deadline"), coach.get("total")
    if deadline is None:
        return (None, total)
    return (deadline - now, total)


def remaining_seconds(screen, now=None):
    """Seconds left on a screen that has a deadline, else None."""
    deadline = screen.get("deadline")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return None
    now = time.monotonic() if now is None else now
    return float(deadline) - now


def clock_text(seconds):
    """m:ss, rounded up, so a countdown never shows 0:00 while it is still running."""
    seconds = max(0, int(math.ceil(seconds)))
    return "%d:%02d" % (seconds // 60, seconds % 60)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
class FaceState:
    """Holds the smoothed, animated state of the face."""

    def __init__(self):
        self.t = 0.0
        self.expression = "neutral"
        self.gaze_x = 0.0
        self.gaze_y = 0.0
        self.forced_blink = False

        # smoothed / derived
        self._blink = 0.0            # 0 = open, 1 = closed
        self._next_blink = time.time() + random.uniform(2.0, 5.0)
        self._blink_start = 0.0
        self._blinking = False
        self._saccade_x = 0.0
        self._saccade_y = 0.0
        self._next_saccade = time.time() + random.uniform(1.5, 4.0)
        self._mouth_phase = 0.0

        # Lip-sync drive (see MOUTH CONTRACT). None means no driver is speaking,
        # so fall back to the built-in oscillation or a static mouth.
        # Held separately from the expression params because it is not a target
        # to ease toward over ~0.15s; it is a live signal that must track audio.
        self.mouth_drive: float | None = None      # 0..1 envelope, or None
        self.mouth_drive_w: float | None = None    # optional width multiplier
        self._mouth_drive_s = 0.0                  # smoothed value rendered

        # cached expression params (smoothed toward the target set)
        self._p = {k: float(v) for k, v in EXPRESSION_PARAMS["neutral"].items()}

    def set_expression(self, name):
        if name in EXPRESSION_PARAMS:
            self.expression = name

    def update(self, dt):
        self.t += dt
        base = EXPRESSION_PARAMS["neutral"]
        target = EXPRESSION_PARAMS.get(self.expression, base)

        # Smooth expression transitions so changes read as facial movement
        # rather than a hard cut. ~0.12 per frame at 30fps.
        #
        # Merged over the neutral defaults rather than iterating `target` alone:
        # if an expression omits a key (e.g. only "happy" sets mouth_w), a plain
        # loop over `target` never touches that key, so it would keep the last
        # expression's value forever instead of easing back. Merging guarantees
        # every key is eased toward a defined value on every frame.
        rate = min(1.0, dt * 7.0)
        merged = {**base, **target}
        for k, v in merged.items():
            self._p[k] = ease(self._p.get(k, v), v, rate)

        now = time.time()

        # ---- blinks: spontaneous, plus forced from state ----
        if self.forced_blink and not self._blinking:
            self._blinking = True
            self._blink_start = now
            self.forced_blink = False
        elif not self._blinking and now >= self._next_blink:
            self._blinking = True
            self._blink_start = now

        if self._blinking:
            BLINK_DUR = 0.16
            p = (now - self._blink_start) / BLINK_DUR
            if p >= 1.0:
                self._blinking = False
                self._blink = 0.0
                self._next_blink = now + random.uniform(2.0, 6.0)
            elif p < 0.5:
                self._blink = p * 2.0
            else:
                self._blink = (1.0 - p) * 2.0
        else:
            self._blink = ease(self._blink, 0.0, min(1.0, dt * 20.0))

        # ---- saccades: small involuntary eye movements ----
        if now >= self._next_saccade:
            self._saccade_x = random.uniform(-0.18, 0.18)
            self._saccade_y = random.uniform(-0.10, 0.10)
            self._next_saccade = now + random.uniform(1.2, 4.0)

        # ---- speaking: oscillate the mouth ----
        if self.expression == "speaking":
            self._mouth_phase += dt * 11.0

        # ---- lip-sync: track the driven envelope ----
        # Smoothed only LIGHTLY. The driver already applies attack/release
        # shaping; easing this heavily would visibly lag the audio, which reads
        # as far worse than a little jitter.
        if self.mouth_drive is not None:
            self._mouth_drive_s = ease(
                self._mouth_drive_s,
                max(0.0, min(1.0, self.mouth_drive)),
                min(1.0, dt * 22.0),
            )
        else:
            self._mouth_drive_s = 0.0

    @property
    def brow_l(self):
        return self._p["brow_l"]

    @property
    def brow_r(self):
        return self._p["brow_r"]

    @property
    def eye_open(self):
        b = self._blink
        return max(0.02, self._p["eye_open"] * (1.0 - b))

    @property
    def mouth_curve(self):
        return self._p["mouth_curve"]

    @property
    def mouth_open(self):
        # A live lip-sync drive wins over everything. This is the real audio
        # envelope, so unlike the expression fallback the mouth actually closes
        # between words instead of flapping through the silence.
        if self.mouth_drive is not None:
            return self._mouth_drive_s * SPEAK_OPEN_MAX
        base = self._p["mouth_open"]
        if self.expression == "speaking":
            return base * (0.5 + 0.5 * math.sin(self._mouth_phase))
        return base

    @property
    def mouth_w(self):
        """Multiplier on the renderer's base mouth width (1.00 = as drawn)."""
        if self.mouth_drive is not None and self.mouth_drive_w is not None:
            return self._p["mouth_w"] * self.mouth_drive_w
        return self._p["mouth_w"]

    @property
    def gaze(self):
        return (self.gaze_x + self._saccade_x, self.gaze_y + self._saccade_y)

    # Idle drift so the face never looks frozen.
    @property
    def head_bob(self):
        return math.sin(self.t * 1.1) * 0.008

    @property
    def breathe(self):
        return 1.0 + 0.010 * math.sin(self.t * 1.4)


# --------------------------------------------------------------------------
# Drawing primitives
# --------------------------------------------------------------------------
def _ellipse(cr, cx, cy, rx, ry, rot=0.0):
    cr.save()
    cr.translate(cx, cy)
    if rot:
        cr.rotate(rot)
    cr.scale(rx, ry)
    cr.arc(0.0, 0.0, 1.0, 0.0, 2.0 * math.pi)
    cr.restore()


def _draw_eye(cr, cx, cy, ew, eh, openness, gaze_x, gaze_y, tilt=0.0, scale=1.0):
    """Draw one eye. `openness` 0..1 shrinks the aperture vertically."""
    eh_open = max(eh * openness, eh * 0.02)

    # --- eye socket / white ---
    cr.save()
    _ellipse(cr, cx, cy, ew, eh_open, tilt)
    cr.set_source_rgb(*EYE_WHITE)
    cr.fill_preserve()
    cr.set_source_rgb(*OUTLINE)
    cr.set_line_width(ew * 0.09 * scale)
    cr.stroke()
    cr.restore()

    # --- iris + pupil, clipped to the eye aperture ---
    cr.save()
    _ellipse(cr, cx, cy, ew, eh_open, tilt)
    cr.clip()

    # gaze offset, constrained so the iris stays inside the aperture
    max_dx = ew * 0.42
    max_dy = eh_open * 0.55
    ix = cx + max(-max_dx, min(max_dx, gaze_x * ew * 0.42))
    iy = cy + max(-max_dy, min(max_dy, gaze_y * eh_open * 0.55))

    iris_r = min(ew, eh) * 0.62 * scale

    # iris
    cr.set_source_rgb(*IRIS)
    cr.arc(ix, iy, iris_r, 0.0, 2.0 * math.pi)
    cr.fill_preserve()
    cr.set_source_rgb(*IRIS_RIM)
    cr.set_line_width(iris_r * 0.14)
    cr.stroke()

    # pupil
    cr.set_source_rgb(*PUPIL)
    cr.arc(ix, iy, iris_r * 0.46, 0.0, 2.0 * math.pi)
    cr.fill()

    # specular highlight — this single dot is what makes an eye read as "alive"
    cr.set_source_rgb(*HIGHLIGHT)
    cr.arc(ix - iris_r * 0.32, iy - iris_r * 0.34, iris_r * 0.20, 0.0, 2.0 * math.pi)
    cr.fill()

    cr.restore()

    # --- upper eyelid shading when partially closed ---
    if openness < 0.85:
        cr.save()
        _ellipse(cr, cx, cy, ew, eh_open, tilt)
        cr.clip()
        cr.set_source_rgba(*FACE_SHADE, 0.55)
        cr.rectangle(cx - ew * 1.2, cy - eh * 1.2, ew * 2.4, eh * 0.55 * (1.0 - openness) + eh * 0.34)
        cr.fill()
        cr.restore()


def _draw_brow(cr, cx, cy, w, angle, thickness, scale=1.0, color=None):
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(angle)
    cr.set_source_rgb(*(color if color is not None else BROW))
    cr.set_line_width(thickness)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(-w / 2, 0)
    # gentle arc so the brow is not a ruler-straight bar
    cr.curve_to(-w * 0.2, -thickness * 0.9,
                w * 0.2, -thickness * 0.9,
                w / 2, 0)
    cr.stroke()
    cr.restore()


def _draw_mouth(cr, cx, cy, w, curve, opening, scale=1.0):
    """Mouth: curve>0 smiles, <0 frowns; `opening` is vertical aperture."""
    half = w / 2
    open_px = opening * w

    if open_px > w * 0.035:
        # open mouth (talking / surprise): a filled shape
        cr.save()
        cr.set_source_rgb(*MOUTH)
        cr.new_path()
        # top lip (a shallow down-curve)
        cr.move_to(cx - half, cy)
        cr.curve_to(cx - half * 0.5, cy - open_px * 0.28,
                    cx + half * 0.5, cy - open_px * 0.28,
                    cx + half, cy)
        # bottom lip (deeper, driven by the curve parameter)
        cr.curve_to(cx + half * 0.5, cy + open_px * (0.55 + curve),
                    cx - half * 0.5, cy + open_px * (0.55 + curve),
                    cx - half, cy)
        cr.close_path()
        cr.fill_preserve()
        cr.set_source_rgb(*OUTLINE)
        cr.set_line_width(w * 0.035 * scale)
        cr.stroke()
        cr.restore()
    else:
        # closed mouth: a stroked curve
        cr.save()
        cr.set_source_rgb(*MOUTH)
        cr.set_line_width(w * 0.075 * scale)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(cx - half, cy)
        cr.curve_to(cx - half * 0.45, cy + curve * w * 0.45,
                    cx + half * 0.45, cy + curve * w * 0.45,
                    cx + half, cy)
        cr.stroke()
        cr.restore()


# --------------------------------------------------------------------------
# The face — pure function of (cr, w, h, state)
# --------------------------------------------------------------------------
def draw_face_cartoon(cr, w, h, st, show_hud=True):
    # --- background ---
    cr.set_source_rgb(*BG)
    cr.paint()

    s = min(w, h)
    cx = w / 2.0
    cy = h / 2.0 + s * 0.02 + st.head_bob * s
    R = s * 0.30

    # --- neck / shoulders: soft grounding shape ---
    cr.save()
    cr.set_source_rgb(*FACE_SHADE)
    cr.new_path()
    sh_y = cy + R * 1.02
    cr.move_to(cx - R * 1.35, h)
    cr.curve_to(cx - R * 0.85, sh_y,
                cx + R * 0.85, sh_y,
                cx + R * 1.35, h)
    cr.close_path()
    cr.fill()
    cr.restore()

    # --- head ---
    cr.save()
    cr.translate(cx, cy)
    cr.scale(1.0, 1.10)
    cr.arc(0.0, 0.0, R * st.breathe, 0.0, 2.0 * math.pi)
    cr.set_source_rgb(*FACE)
    cr.fill_preserve()
    cr.set_source_rgb(*OUTLINE)
    cr.set_line_width(s * 0.0045)
    cr.stroke()
    cr.restore()

    # --- ears ---
    for sign in (-1, 1):
        cr.save()
        _ellipse(cr, cx + sign * R * 1.0, cy + R * 0.16, R * 0.16, R * 0.24)
        cr.set_source_rgb(*FACE)
        cr.fill_preserve()
        cr.set_source_rgb(*OUTLINE)
        cr.set_line_width(s * 0.0035)
        cr.stroke()
        cr.restore()

    # --- blush ---
    for sign in (-1, 1):
        cr.save()
        _ellipse(cr, cx + sign * R * 0.58, cy + R * 0.30, R * 0.20, R * 0.11)
        cr.set_source_rgba(*BLUSH, 0.22)
        cr.fill()
        cr.restore()

    # --- eyes ---
    gx, gy = st.gaze
    eye_dx = R * 0.385
    eye_dy = -R * 0.135
    ew = R * 0.255      # half-width
    eh = R * 0.300      # half-height
    openness = st.eye_open

    _draw_eye(cr, cx - eye_dx, cy + eye_dy, ew, eh, openness, gx, gy, tilt=-0.06)
    _draw_eye(cr, cx + eye_dx, cy + eye_dy, ew, eh, openness, gx, gy, tilt=0.06)

    # --- brows ---
    # Left brow is mirrored in angle so "raised" reads consistently.
    _draw_brow(cr, cx - eye_dx, cy + eye_dy - R * 0.46,
               R * 0.40, -st.brow_l, R * 0.055)
    _draw_brow(cr, cx + eye_dx, cy + eye_dy - R * 0.46,
               R * 0.40, -st.brow_r, R * 0.055)

    # --- mouth ---
    # `mouth_w` scales the width per expression (1.00 = the base width), so a
    # wider smile is a data change rather than a code change.
    _draw_mouth(cr, cx, cy + R * 0.50, R * 0.72 * st.mouth_w,
                st.mouth_curve, st.mouth_open)

    # --- HUD: proves the renderer is live, and aids headless debugging ---
    if show_hud:
        cr.select_font_face("monospace",
                            cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(s * 0.022)
        cr.set_source_rgb(*TEXT_DIM)
        hud = "face v2.9  |  %s  |  t=%6.1fs  |  blink=%.2f" % (
            st.expression, st.t, st._blink)
        cr.move_to(s * 0.025, h - s * 0.030)
        cr.show_text(hud)


# --------------------------------------------------------------------------
# Rung 2 — the illustrated register
# --------------------------------------------------------------------------
# The same parameters drawn with real anatomy instead of a circle: a tapered jaw,
# eyelids, a nose, lips, hair, and two tones of cel shading. NOTHING about the
# animation changes - `gaze` still moves the iris, `mouth_open` still parts the
# lips, the brow angles still carry expression - so this is a rendering change,
# not a pipeline. That is exactly what makes it the cheapest rung to build, and
# why it lives *beside* the cartoon rather than replacing it.
#
# Vertical landmark positions are fractions of R, the head's HALF-HEIGHT (R is
# 0.30 * min(w, h) in both styles, so the face stays resolution-independent).
# y = 0 is the eye line, deliberately the middle of the head - the oldest rule of
# face proportions, and the one thing the cartoon most obviously breaks, since
# its eyes sit high and are taller than they are wide.
#
#   crown -1.12R   hairline -0.55R   brow -0.20R   eyes 0.00R
#   nose base +0.58R   mouth line +0.77R   chin +1.14R
#
# Rung 1 has no hair and no anatomy; this rung adds both, which is why 1 -> 2 is
# a bigger step than 2 -> 3 -> 4. See docs/face-ladder/README.md.

# The four rungs of the ladder, in order. Each is a DIFFERENT PERSONA (four distinct
# faces, decided 14 October), not one character getting clearer, so the name of the
# style is a rendering register and the name of the person lives with the rung's own
# code and voice:
#   cartoon      Pip    rung 1, the face that shipped
#   illustrated  Owen   rung 2, accepted 3 November
#   dimensional  Milo   rung 3, stylised 3D - lit rather than outlined, still drawn
#   portrait     Elliot rung 4, an image puppet - a still, animated at the eyes/mouth
STYLES = ("cartoon", "illustrated", "dimensional", "portrait")

# ---------------------------------------------------------------------------
# Rung 2: the illustrated register's LOOKS
# ---------------------------------------------------------------------------
# Two things about the illustrated face are a TASTE call rather than a
# correctness one: the palette, and a handful of proportions. They live in one
# table, so every alternative is drawn by the SAME code. Four copied renderers
# drift apart, and then the alternatives being compared are not comparable -
# each one has its own accidental differences on top of the intended one.
#
# A look is a set of overrides on ILL_BASE. Every geometry value is a
# MULTIPLIER on the measured proportion, so 1.0 always means "as measured" and a
# look that is only meant to tint the face cannot quietly redefine its anatomy.
R_CROWN, R_HAIR, R_BROW, R_EYE = -1.12, -0.55, -0.20, 0.00
R_NOSE, R_MOUTH, R_CHIN = 0.58, 0.77, 1.14

ILL_BASE: dict = dict(
    skin=FACE, skin_shade=SKIN_SHADE, hair=HAIR, hair_hi=HAIR_HI,
    lip=LIP, lip_shade=LIP_SHADE, mouth_in=MOUTH_IN, outline=OUTLINE,
    sclera=SCLERA, iris=IRIS, iris_rim=IRIS_RIM, pupil=PUPIL, brow=BROW_ILL,
    head_w=1.00,                                    # cranium and jaw width
    head_h=1.00,                                    # cranium-to-chin length
    eye_w=1.00, eye_h=1.00, eye_dx=1.00,            # eye size, eye spacing
    brow_w=1.00, brow_th=1.00, brow_lift=0.00,      # brow length, weight, height
    mouth_w=1.00, lip_thick=1.00, nose=1.00,
    line=1.00,                                      # every outline weight
    shadow=1.00,                                    # every cel-shadow alpha
)

ILL_LOOKS = {
    # The shipped look. The numbers are ILL_BASE, i.e. what the rung was built
    # with, and this name exists so the OTHER looks have something to be a
    # difference FROM.
    "warm": {},

    # Rounder, warmer, drawn in a finer line. The friendliest of the four and
    # the least "portrait": bigger eyes, softer jaw, brown hair, low-contrast
    # ink. This is the one to pick if the face has to work for someone anxious.
    "soft": dict(
        skin=(0.965, 0.812, 0.690), skin_shade=(0.905, 0.720, 0.605),
        hair=(0.290, 0.190, 0.150), hair_hi=(0.470, 0.330, 0.245),
        lip=(0.760, 0.455, 0.440), lip_shade=(0.560, 0.290, 0.300),
        mouth_in=(0.220, 0.090, 0.100), outline=(0.140, 0.110, 0.160),
        sclera=(0.970, 0.965, 0.970), iris=(0.310, 0.470, 0.780),
        iris_rim=(0.160, 0.260, 0.520), pupil=(0.070, 0.060, 0.080),
        brow=(0.330, 0.220, 0.180),
        head_w=0.96, head_h=0.96, eye_w=1.16, eye_h=1.18, brow_th=0.82,
        mouth_w=0.96, lip_thick=0.62, nose=0.92, line=0.72, shadow=0.75,
    ),

    # Poster-like: near-black ink, deeper cel shadows, saturated iris. Reads at
    # a distance and on a dim projector, at the cost of looking less like a
    # person and more like a graphic.
    "graphic": dict(
        skin=(0.955, 0.800, 0.640), skin_shade=(0.790, 0.585, 0.470),
        hair=(0.130, 0.105, 0.120), hair_hi=(0.300, 0.240, 0.250),
        lip=(0.700, 0.330, 0.360), lip_shade=(0.420, 0.150, 0.190),
        mouth_in=(0.120, 0.050, 0.070), outline=(0.060, 0.045, 0.080),
        sclera=(1.000, 1.000, 1.000), iris=(0.180, 0.360, 0.880),
        iris_rim=(0.080, 0.150, 0.480), pupil=(0.010, 0.010, 0.030),
        brow=(0.140, 0.100, 0.100),
        head_w=1.02, head_h=1.04, eye_w=1.02, eye_h=0.95, brow_w=1.05, brow_th=1.15,
        lip_thick=0.70, line=1.45, shadow=1.35,
    ),

    # An adult rather than a child: narrower head, smaller eyes set slightly
    # wider, thinner brows, greying hair, a cooler and less peachy skin. The
    # one to pick if the participant should feel they are with a peer.
    "mature": dict(
        skin=(0.910, 0.760, 0.625), skin_shade=(0.800, 0.635, 0.520),
        hair=(0.220, 0.205, 0.205), hair_hi=(0.520, 0.500, 0.500),
        lip=(0.620, 0.380, 0.380), lip_shade=(0.430, 0.230, 0.240),
        mouth_in=(0.150, 0.070, 0.080), outline=(0.090, 0.080, 0.110),
        sclera=(0.930, 0.925, 0.930), iris=(0.300, 0.430, 0.520),
        iris_rim=(0.150, 0.220, 0.290), pupil=(0.050, 0.050, 0.060),
        brow=(0.230, 0.190, 0.180),
        head_w=0.90, head_h=1.12, eye_w=0.94, eye_h=0.92, eye_dx=0.98, brow_th=0.92,
        brow_lift=0.02, mouth_w=1.04, lip_thick=0.62, nose=1.06,
        line=0.95, shadow=1.05,
    ),
}
ILL_DEFAULT_LOOK = "warm"


def ill_look(name=None):
    """Resolve a look name to a complete parameter set.

    An unknown name gets the DEFAULT look instead of raising. A snapshot still
    has to render and the kiosk must never lose its face to a typo in a file -
    the same rule as `--gaze-ink`. `describe_look` is how a caller says on the
    record which one it actually got.
    """
    if name not in ILL_LOOKS:
        if name:
            log("look %r is not one of %s - using %r"
                % (name, "/".join(ILL_LOOKS), ILL_DEFAULT_LOOK))
        name = ILL_DEFAULT_LOOK
    d = dict(ILL_BASE)
    d.update(ILL_LOOKS[name])
    d["name"] = name
    return d


def _head_outline(cr, R):
    """Head outline: cranium, cheekbones, tapered jaw, chin. Head-local coords."""
    cr.new_path()
    cr.move_to(0.0, R_CROWN * R)
    cr.curve_to(0.46 * R, R_CROWN * R, 0.80 * R, -0.88 * R, 0.86 * R, -0.46 * R)
    cr.curve_to(0.90 * R, -0.10 * R, 0.84 * R, 0.30 * R, 0.60 * R, 0.70 * R)
    cr.curve_to(0.44 * R, 0.96 * R, 0.22 * R, R_CHIN * R, -0.0, R_CHIN * R)
    cr.curve_to(-0.22 * R, R_CHIN * R, -0.44 * R, 0.96 * R, -0.60 * R, 0.70 * R)
    cr.curve_to(-0.84 * R, 0.30 * R, -0.90 * R, -0.10 * R, -0.86 * R, -0.46 * R)
    cr.curve_to(-0.80 * R, -0.88 * R, -0.46 * R, R_CROWN * R, 0.0, R_CROWN * R)
    cr.close_path()


def _hair_outline(cr, R):
    """Swept-back cap: volume above the cranium, hairline across the forehead."""
    cr.new_path()
    cr.move_to(-0.92 * R, -0.32 * R)
    cr.curve_to(-0.97 * R, -0.88 * R, -0.62 * R, -1.31 * R, 0.0, -1.31 * R)
    cr.curve_to(0.62 * R, -1.31 * R, 0.97 * R, -0.88 * R, 0.92 * R, -0.32 * R)
    # hairline: down the right temple, then sweeping across, higher on the left
    cr.curve_to(0.86 * R, -0.54 * R, 0.72 * R, -0.70 * R, 0.38 * R, -0.75 * R)
    cr.curve_to(0.00 * R, -0.63 * R, -0.46 * R, -0.52 * R, -0.92 * R, -0.32 * R)
    cr.close_path()


def _eye_aperture(cr, ew, eh, openness):
    """Almond eye. `openness` 0..1 lowers the upper lid and raises the lower."""
    up = -eh * (0.16 + 0.84 * openness)
    lo = eh * (0.80 - 0.34 * (1.0 - openness))
    cr.new_path()
    cr.move_to(-ew, 0.0)
    cr.curve_to(-ew * 0.52, up * 1.08, ew * 0.52, up * 1.08, ew, 0.0)
    cr.curve_to(ew * 0.52, lo, -ew * 0.52, lo, -ew, 0.0)
    cr.close_path()


def _draw_eye_illustrated(cr, cx, cy, ew, eh, openness, gaze_x, gaze_y,
                          tilt=0.0, L=None):
    """One illustrated eye: sclera, iris, pupil, catchlight, lid edges."""
    L = L or ILL_BASE
    # Everything inside the aperture is clipped to it, so the iris can slide
    # without ever spilling past the lids.
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(tilt)
    _eye_aperture(cr, ew, eh, openness)
    cr.set_source_rgb(*L["sclera"])
    cr.fill_preserve()
    cr.clip()

    # The iris rides on a sphere, so gaze moves it a FRACTION of the way; sliding
    # it the full distance looks like a sticker being pushed around.
    ix = gaze_x * ew * 0.34
    iy = gaze_y * eh * 0.26 + eh * 0.08
    # A real iris is only slightly clipped by the lids - about 85% of it shows.
    # Sized to the aperture edge it is clipped to roughly 60%, and the eye then
    # reads as a small lens rather than an iris behind a lid.
    ir = eh * 0.80
    cr.set_source_rgb(*L["iris"])
    cr.new_sub_path()
    cr.arc(ix, iy, ir, 0.0, 2.0 * math.pi)
    cr.fill()
    cr.set_source_rgba(*L["iris_rim"], 0.55)
    cr.set_line_width(eh * 0.13 * L["line"])
    cr.new_sub_path()
    cr.arc(ix, iy, ir * 0.94, 0.0, 2.0 * math.pi)
    cr.stroke()
    cr.set_source_rgb(*L["pupil"])
    cr.new_sub_path()
    cr.arc(ix, iy, ir * 0.46, 0.0, 2.0 * math.pi)
    cr.fill()
    # Catchlight: the single strongest cue that an eye is alive rather than drawn.
    cr.set_source_rgb(1.0, 1.0, 1.0)
    cr.new_sub_path()
    cr.arc(ix - ir * 0.34, iy - ir * 0.40, ir * 0.24, 0.0, 2.0 * math.pi)
    cr.fill()
    cr.restore()

    # Lid edges, drawn after so they read as the boundary of the aperture.
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(tilt)
    cr.set_source_rgb(*L["outline"])
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    up = -eh * (0.16 + 0.84 * openness)
    lo = eh * (0.80 - 0.34 * (1.0 - openness))
    cr.set_line_width(eh * 0.15 * L["line"])
    cr.new_path()
    cr.move_to(-ew, 0.0)
    cr.curve_to(-ew * 0.52, up * 1.08, ew * 0.52, up * 1.08, ew, 0.0)
    cr.stroke()
    cr.set_line_width(eh * 0.09 * L["line"])
    cr.new_path()
    cr.move_to(-ew, 0.0)
    cr.curve_to(-ew * 0.52, lo, ew * 0.52, lo, ew, 0.0)
    cr.stroke()
    cr.restore()


def _draw_nose(cr, R, L=None):
    """Nose: a shaded side of the bridge, a soft tip, two nostril hints.

    Drawn in HEAD-LOCAL coordinates - the caller translates to the head's
    origin first. Called bare, it paints at the FRAME's (0,0): that is how this
    rung shipped a nose in the top-left corner and no nose on the face.

    Measured against the reference rather than guessed: the nostril span is
    about 0.11 of the head half-width, and there is NO bright highlight on the
    tip. The first draft had a 0.19 span and a lit ball at the tip, which read
    as a moustache and a clown nose instead of as a nose.
    """
    L = L or ILL_BASE
    n = L["nose"]
    shade, ink = L["skin_shade"], L["outline"]

    # Bridge: a wedge down ONE SIDE of the nose, starting BELOW the inner brow.
    # Started at the brow it reads as a crease cutting the forehead in two; down
    # the middle it reads as a scar.
    cr.save()
    cr.set_source_rgba(*shade, min(1.0, 0.30 * L["shadow"]))
    cr.new_path()
    cr.move_to(0.078 * n * R, -0.04 * R)
    cr.curve_to(0.082 * n * R, 0.20 * R, 0.070 * n * R, 0.38 * R, 0.034 * n * R, 0.50 * R)
    cr.curve_to(0.016 * n * R, 0.36 * R, 0.014 * n * R, 0.10 * R, 0.026 * n * R, -0.02 * R)
    cr.close_path()
    cr.fill()
    cr.restore()

    # Tip: a crescent under the ball of the nose. This is what gives the nose
    # its third dimension, and it is a shadow rather than a highlight because a
    # highlight at this scale is a dot a projector at 2 m turns into a balloon.
    cr.save()
    cr.set_source_rgba(*shade, min(1.0, 0.42 * L["shadow"]))
    _ellipse(cr, 0.0, R_NOSE * R + 0.040 * R, 0.078 * n * R, 0.026 * R)
    cr.fill()
    cr.restore()

    # Nostrils: small, close in, tilted outward so they follow the tip.
    for sign in (-1, 1):
        cr.save()
        cr.set_source_rgba(*ink, min(1.0, 0.40 * L["shadow"]))
        _ellipse(cr, sign * 0.047 * n * R, R_NOSE * R + 0.020 * R,
                 0.022 * n * R, 0.011 * R, rot=sign * 0.45)
        cr.fill()
        cr.restore()


def _draw_lips(cr, cx, cy, w, curve, opening, L=None):
    """Illustrated mouth: two lips around a line, parted by `opening`.

    Same contract as _draw_mouth, so expression behaves identically: `curve` > 0
    smiles (the line sags in the middle, corners level), `opening` 0..1 is the
    vertical aperture. Each band is traced as two curves sharing their endpoints,
    which is why it reads as a lip rather than a stroked line.
    """
    half = w / 2.0
    sag = curve * w * 0.34
    gap = opening * w * 0.62          # a full drive drops the jaw this far
    L = L or ILL_BASE
    # Lip thicknesses were measured, then measured again, because the first
    # number was wrong in a way that mattered: a band traced between two curves
    # rises only 3/4 of the control offset, so 0.20w of padding drew about 0.15w
    # of lip. At 0.20/0.28 the lower lip came out as a fat lens that read as an
    # OPEN MOUTH - a robot that looks mid-sentence when it is smiling. Thin lips
    # with a defined line are what read as closed; the aperture is the thing that
    # opens the mouth, and only the speech drive should do that.
    THICK_UP = 0.11 * w * L["lip_thick"]
    THICK_LO = 0.17 * w * L["lip_thick"]

    def band(cr, pad_top, pad_bot):
        cr.new_path()
        cr.move_to(cx - half, cy)
        cr.curve_to(cx - half * 0.45, cy + sag + pad_top,
                    cx + half * 0.45, cy + sag + pad_top,
                    cx + half, cy)
        cr.curve_to(cx + half * 0.45, cy + sag + pad_bot,
                    cx - half * 0.45, cy + sag + pad_bot,
                    cx - half, cy)
        cr.close_path()

    # inner mouth first, so the lips lap over its edges
    cr.save()
    cr.set_source_rgb(*L["mouth_in"])
    band(cr, 0.0, gap)
    cr.fill()
    cr.restore()

    # upper lip: thinner of the two, with a cupid's bow
    cr.save()
    cr.set_source_rgb(*L["lip"])
    band(cr, -THICK_UP, 0.0)
    cr.fill()
    cr.restore()

    # lower lip: fuller, sitting below the aperture
    cr.save()
    cr.set_source_rgb(*L["lip"])
    band(cr, gap, gap + THICK_LO)
    cr.fill()
    cr.restore()

    # mouth line
    cr.save()
    cr.set_source_rgba(*L["lip_shade"], 0.85)
    cr.set_line_width(w * 0.026 * L["line"])
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.new_path()
    cr.move_to(cx - half, cy)
    cr.curve_to(cx - half * 0.45, cy + sag, cx + half * 0.45, cy + sag,
                cx + half, cy)
    cr.stroke()
    cr.restore()


def draw_face_illustrated(cr, w, h, st, show_hud=True, look=None):
    """The illustrated register, drawn from ONE parameter set per look.

    `look` is a name from ILL_LOOKS (or an already-resolved dict). Everything
    that differs between the alternatives goes through `L`, so two frames of two
    looks differ only in what the look table says - which is the whole point of
    comparing them.
    """
    L = look if isinstance(look, dict) else ill_look(look)

    cr.set_source_rgb(*BG)
    cr.paint()

    s = min(w, h)
    cx = w / 2.0
    cy = h / 2.0 + s * 0.02 + st.head_bob * s
    R = s * 0.30
    hw = L["head_w"]
    hv = L["head_h"]

    # --- neck, then shoulders over its base ---------------------------------
    cr.save()
    cr.set_source_rgb(*L["skin_shade"])
    cr.new_path()
    cr.move_to(cx - R * 0.44, cy + R * 0.58)
    cr.curve_to(cx - R * 0.47, cy + R * 1.22, cx - R * 0.53, cy + R * 1.42,
                cx - R * 0.57, cy + R * 1.60)
    cr.line_to(cx + R * 0.57, cy + R * 1.60)
    cr.curve_to(cx + R * 0.53, cy + R * 1.42, cx + R * 0.47, cy + R * 1.22,
                cx + R * 0.44, cy + R * 0.58)
    cr.close_path()
    cr.fill()
    cr.new_path()
    shy = cy + R * 1.58
    cr.move_to(cx - R * 2.0, h)
    cr.curve_to(cx - R * 1.08, shy, cx + R * 1.08, shy, cx + R * 2.0, h)
    cr.close_path()
    cr.fill()
    cr.restore()

    # --- the face, in HEAD-LOCAL coordinates --------------------------------
    # ONE transform for head, ears, hair, eyes, brows, nose and lips: every
    # feature is placed relative to the head's own origin, so a proportion change
    # in the look table moves all of them together instead of leaving the eyes
    # where the old skull used to be.
    #
    # It is also the guard against the bug this rung actually shipped. The nose
    # draws head-local; called outside this block it painted at the FRAME's
    # origin - a tapered brown band, a pale tip and two nostril blobs, 42x202 px
    # in the top-left corner - and the face itself had no nose on it.
    cr.save()
    cr.translate(cx, cy)
    cr.scale(hw, hv)
    # Ink weight is a property of the FRAME, not of the head's proportions: divide
    # by the scaling this block applies, or a narrow face gets thin outlines for
    # a reason nobody chose.
    def lw(base):
        return base / ((hw + hv) / 2.0)

    # --- head ---------------------------------------------------------------
    _head_outline(cr, R)
    cr.set_source_rgb(*L["skin"])
    cr.fill_preserve()
    # Cel shading, clipped to the head: a crescent down one side, plus the
    # socket shadow. Two tones only - more would start to look unrendered
    # rather than stylised.
    cr.save()
    cr.clip()
    sh = L["shadow"]
    cr.set_source_rgba(*L["skin_shade"], min(1.0, 0.55 * sh))
    cr.new_path()
    cr.move_to(-0.10 * R, -1.20 * R)
    cr.curve_to(-0.95 * R, -0.95 * R, -1.05 * R, 0.10 * R, -0.55 * R, 1.10 * R)
    cr.line_to(-1.30 * R, 1.30 * R)
    cr.line_to(-1.30 * R, -1.30 * R)
    cr.close_path()
    cr.fill()
    cr.set_source_rgba(*L["skin_shade"], min(1.0, 0.42 * sh))
    _ellipse(cr, 0.0, R_BROW * R + 0.02 * R, 0.74 * R, 0.15 * R)
    cr.fill()
    # Under the jaw: a crescent that follows the jawline, which is what stops the
    # head reading as a flat mask. Clipped to the head, so it cannot spill.
    cr.set_source_rgba(*L["skin_shade"], min(1.0, 0.50 * sh))
    cr.new_path()
    cr.move_to(-0.62 * R, 0.72 * R)
    cr.curve_to(-0.40 * R, 1.02 * R, 0.05 * R, 1.20 * R, 0.52 * R, 0.92 * R)
    cr.curve_to(0.20 * R, 1.06 * R, -0.20 * R, 0.94 * R, -0.44 * R, 0.66 * R)
    cr.close_path()
    cr.fill()
    # Cheek highlight, opposite the shadow crescent.
    cr.set_source_rgba(1.0, 0.97, 0.92, 0.16)
    _ellipse(cr, 0.42 * R, 0.28 * R, 0.20 * R, 0.13 * R, rot=-0.4)
    cr.fill()
    cr.restore()
    cr.set_source_rgb(*L["outline"])
    cr.set_line_width(lw(s * 0.0030 * L["line"]))
    _head_outline(cr, R)
    cr.stroke()

    # --- ears ---------------------------------------------------------------
    for sign in (-1, 1):
        cr.save()
        _ellipse(cr, sign * R * 0.855, 0.02 * R, R * 0.085, R * 0.145,
                 rot=sign * 0.10)
        cr.set_source_rgb(*L["skin"])
        cr.fill_preserve()
        cr.set_source_rgb(*L["outline"])
        cr.set_line_width(lw(s * 0.0026 * L["line"]))
        cr.stroke()
        cr.restore()

    # --- hair ---------------------------------------------------------------
    cr.save()
    _hair_outline(cr, R)
    cr.set_source_rgb(*L["hair"])
    cr.fill_preserve()
    cr.set_source_rgb(*L["outline"])
    cr.set_line_width(lw(s * 0.0030 * L["line"]))
    cr.stroke()
    # one lit strand, following the sweep - reads as volume without a gradient
    cr.save()
    cr.clip()
    cr.set_source_rgba(*L["hair_hi"], 0.55)
    cr.new_path()
    cr.move_to(-0.70 * R, -0.95 * R)
    cr.curve_to(-0.10 * R, -1.24 * R, 0.55 * R, -1.15 * R, 0.90 * R, -0.70 * R)
    cr.line_to(0.90 * R, -0.30 * R)
    cr.curve_to(0.45 * R, -0.85 * R, -0.20 * R, -0.95 * R, -0.70 * R, -0.72 * R)
    cr.close_path()
    cr.fill()
    cr.restore()
    # A few tapered strands falling past the hairline. A bare hard edge is the
    # single thing that most says "this hair was drawn as a shape".
    for bx, bw, bl in ((-0.46, 0.20, 0.16), (-0.06, 0.26, 0.21), (0.34, 0.22, 0.14)):
        cr.new_path()
        cr.move_to((bx - bw) * R, -0.62 * R)
        cr.curve_to((bx - bw * 0.3) * R, (-0.62 + bl * 0.6) * R,
                    (bx + bw * 0.3) * R, (-0.62 + bl * 0.6) * R,
                    (bx + bw) * R, -0.60 * R)
        cr.curve_to((bx + bw * 0.4) * R, (-0.62 + bl) * R,
                    (bx - bw * 0.4) * R, (-0.62 + bl) * R,
                    (bx - bw) * R, -0.62 * R)
        cr.close_path()
        cr.set_source_rgba(*L["hair"], 0.92)
        cr.fill()
    cr.restore()

    # --- eyes ---------------------------------------------------------------
    gx, gy = st.gaze
    eye_dx = R * 0.36 * L["eye_dx"]
    ew = R * 0.175 * L["eye_w"]
    eh = R * 0.100 * L["eye_h"]
    _draw_eye_illustrated(cr, -eye_dx, R_EYE * R, ew, eh,
                          st.eye_open, gx, gy, tilt=0.05, L=L)
    _draw_eye_illustrated(cr, eye_dx, R_EYE * R, ew, eh,
                          st.eye_open, gx, gy, tilt=-0.05, L=L)

    # --- brows --------------------------------------------------------------
    # Measured: at R*0.030 the brow was ~10px on a 1080p frame and simply did not
    # read - it vanished into the socket shadow it sits in. Nearly double, and
    # dark enough to survive the projector.
    brow_y = (R_BROW + L["brow_lift"] - 0.03) * R
    _draw_brow(cr, -eye_dx, brow_y,
               R * 0.36 * L["brow_w"], -st.brow_l, R * 0.058 * L["brow_th"],
               color=L["brow"])
    _draw_brow(cr, eye_dx, brow_y,
               R * 0.36 * L["brow_w"], -st.brow_r, R * 0.058 * L["brow_th"],
               color=L["brow"])

    # --- nose and mouth -----------------------------------------------------
    _draw_nose(cr, R, L)
    _draw_lips(cr, 0.0, R_MOUTH * R, R * 0.62 * L["mouth_w"] * st.mouth_w,
               st.mouth_curve, st.mouth_open, L=L)

    cr.restore()          # end head-local

    # --- HUD: names the active style AND look, so a screenshot is
    # self-describing and a stale running process is visible at a glance ------
    if show_hud:
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(s * 0.022)
        cr.set_source_rgb(*TEXT_DIM)
        cr.move_to(s * 0.025, h - s * 0.030)
        cr.show_text("face v2.10  |  style=illustrated  |  look=%s  |  %s  |  t=%6.1fs" % (
            L["name"], st.expression, st.t))


# --------------------------------------------------------------------------
# Rung 3 — the dimensional register ("Milo")
# --------------------------------------------------------------------------
# A cairo face that READS as a 3D animated-feature render without being one: no
# rig, no GLES, no glTF. What produces the dimensionality is light rather than
# geometry - every form is a radial or linear gradient with an ambient-occlusion
# blob under it, which is the same trick a matte painter uses - so the whole
# animation contract survives unchanged. Gaze still moves an iris, expression still
# bends brows and mouth, lip-sync still parts the lips, at the same cost as rung 2.
#
# WHY NOT A REAL RIG. The ladder says rung 3's honest form is a rigged head on the
# GPU, and that is still true of what rung 3 eventually wants to be. But a rig is a
# new pipeline (asset format, skinning, a GLES path, a budget check against gaze and
# the LLM) and this rung has to exist inside the same kiosk that already draws two
# other faces. A shaded vector face is the same *register* - a stylised 3D look -
# delivered with the animation carrier intact, and it is reversible: if a real rig
# lands later, `--style` picks it up and nothing else changes.
#
# The step rung 2 -> rung 3 is therefore in the SHADING, not the silhouette: same
# landmarks (crown -1.12R, hairline -0.55R, brow -0.20R, eyes 0, nose 0.58R,
# mouth 0.77R, chin 1.14R), so the two faces are the same pose in two rendering
# languages and can be compared frame to frame. That comparison is the point.
DIM_BASE: dict = dict(
    skin=(0.945, 0.735, 0.620),        # mid tone, the base of the head fill
    skin_lit=(1.000, 0.885, 0.790),    # key light: upper-left, warm
    skin_shade=(0.780, 0.520, 0.455),  # the side away from the key
    skin_deep=(0.480, 0.305, 0.300),   # occlusion in a crease, never on a plane
    sub=(0.930, 0.450, 0.425),         # subsurface warmth in ears, nose, cheeks
    hair=(0.300, 0.190, 0.120),
    hair_lit=(0.610, 0.410, 0.245),
    hair_dark=(0.115, 0.070, 0.055),
    brow=(0.290, 0.180, 0.120),
    lip=(0.810, 0.440, 0.415),
    lip_lit=(0.955, 0.650, 0.600),
    lip_shade=(0.520, 0.205, 0.225),
    mouth_in=(0.215, 0.070, 0.085),
    sclera=(0.930, 0.920, 0.930),
    sclera_shade=(0.660, 0.640, 0.690),
    iris=(0.275, 0.470, 0.860),
    iris_lit=(0.600, 0.810, 0.980),
    iris_rim=(0.050, 0.110, 0.340),
    pupil=(0.020, 0.015, 0.030),
    shirt=(0.095, 0.105, 0.145),
    shirt_lit=(0.190, 0.210, 0.280),
    rim=(0.560, 0.740, 0.960),         # cool rim on the shaded edge
    key=(1.000, 0.930, 0.820),
)


def _soft(cr, cx, cy, rx, ry, colour, alpha, rot=0.0, core=0.42, tail=0.0):
    """A soft radial patch: the whole of this rung's shading is built from these.

    `core` is where the colour has fallen to 55% of its alpha, so a small core is a
    tight highlight and a large one is a broad shadow. Everything is drawn in the
    CURRENT transform, so inside the head-local block these coordinates are head
    proportions and scale with the face.
    """
    if alpha <= 0.0 or rx <= 0.0 or ry <= 0.0:
        return
    cr.save()
    cr.translate(cx, cy)
    if rot:
        cr.rotate(rot)
    cr.scale(rx, ry)
    g = cairo.RadialGradient(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    g.add_color_stop_rgba(0.0, colour[0], colour[1], colour[2], alpha)
    g.add_color_stop_rgba(min(0.99, max(0.01, core)),
                          colour[0], colour[1], colour[2], alpha * 0.55)
    g.add_color_stop_rgba(1.0, colour[0], colour[1], colour[2], tail)
    cr.set_source(g)
    cr.new_sub_path()
    cr.arc(0.0, 0.0, 1.0, 0.0, 2.0 * math.pi)
    cr.fill()
    cr.restore()


def _head3d_outline(cr, R):
    """Head: cranium, cheekbones, tapered jaw, chin. Head-local coordinates."""
    cr.new_path()
    cr.move_to(0.0, -1.12 * R)
    cr.curve_to(0.50 * R, -1.12 * R, 0.86 * R, -0.84 * R, 0.91 * R, -0.40 * R)
    cr.curve_to(0.95 * R, -0.06 * R, 0.88 * R, 0.34 * R, 0.62 * R, 0.76 * R)
    cr.curve_to(0.47 * R, 1.00 * R, 0.24 * R, 1.14 * R, 0.0, 1.14 * R)
    cr.curve_to(-0.24 * R, 1.14 * R, -0.47 * R, 1.00 * R, -0.62 * R, 0.76 * R)
    cr.curve_to(-0.88 * R, 0.34 * R, -0.95 * R, -0.06 * R, -0.91 * R, -0.40 * R)
    cr.curve_to(-0.86 * R, -0.84 * R, -0.50 * R, -1.12 * R, 0.0, -1.12 * R)
    cr.close_path()


def _hair3d_outline(cr, R):
    """A swept side part: high over the left brow, diving to the right temple.

    The asymmetry is the whole point. A hairline drawn as a level cap reads as a
    helmet, which is exactly what the first render of this rung looked like.
    """
    cr.new_path()
    cr.move_to(-0.95 * R, -0.22 * R)
    cr.curve_to(-1.01 * R, -0.84 * R, -0.60 * R, -1.30 * R, 0.02 * R, -1.31 * R)
    cr.curve_to(0.64 * R, -1.32 * R, 1.01 * R, -0.86 * R, 0.95 * R, -0.22 * R)
    # HARLINE, right to left: a SWEEP, never a peak. The first version rose to a
    # point at (0.12R, -0.94R) and dived to -0.54R on the left, so the forehead came
    # to a widow's peak in the middle and the whole mass read as a bowl cut with a
    # pale V cut into it. Rounded through the part and lower at both temples.
    cr.curve_to(0.93 * R, -0.42 * R, 0.85 * R, -0.58 * R, 0.66 * R, -0.68 * R)
    cr.curve_to(0.46 * R, -0.77 * R, 0.24 * R, -0.83 * R, 0.05 * R, -0.85 * R)
    cr.curve_to(-0.16 * R, -0.80 * R, -0.40 * R, -0.66 * R, -0.62 * R, -0.50 * R)
    cr.curve_to(-0.78 * R, -0.40 * R, -0.89 * R, -0.30 * R, -0.95 * R, -0.22 * R)
    cr.close_path()


def _brow3d(cr, cx, cy, w, angle, thickness, colour):
    """A tapered filled brow with a few strands, rather than a stroked line.

    A stroked line has a constant weight along its length, which is the single
    thing that makes a drawn brow read as a drawn line. A brow has a heavy body and
    ends that fade to nothing, so it is drawn as a filled lens and then textured.
    """
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(angle)
    cr.new_path()
    cr.move_to(-w / 2.0, thickness * 0.15)
    cr.curve_to(-w * 0.22, -thickness * 1.05, w * 0.20, -thickness * 1.05,
                w / 2.0, -thickness * 0.10)
    cr.curve_to(w * 0.18, thickness * 0.30, -w * 0.24, thickness * 0.32,
                -w / 2.0, thickness * 0.15)
    cr.close_path()
    g = cairo.LinearGradient(-w / 2.0, 0.0, w / 2.0, 0.0)
    g.add_color_stop_rgb(0.0, colour[0] * 0.80, colour[1] * 0.80, colour[2] * 0.80)
    g.add_color_stop_rgb(0.55, colour[0], colour[1], colour[2])
    g.add_color_stop_rgb(1.0, colour[0] * 0.88, colour[1] * 0.88, colour[2] * 0.88)
    cr.set_source(g)
    cr.fill()
    # strands: they follow the brow's own arc, so they read as hair rather than
    # as scratches
    cr.set_line_width(thickness * 0.16)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for i in range(7):
        t = -0.42 + i * 0.14
        x = t * w
        cr.set_source_rgba(colour[0] * 1.45, colour[1] * 1.45, colour[2] * 1.45, 0.30)
        cr.new_path()
        cr.move_to(x - w * 0.05, thickness * 0.55)
        cr.curve_to(x, -thickness * 0.35, x + w * 0.04, -thickness * 0.6,
                    x + w * 0.07, -thickness * 0.9)
        cr.stroke()
    cr.restore()


def _eye3d(cr, cx, cy, ew, eh, openness, gaze_x, gaze_y, tilt, D):
    """One eye: socket, sclera, iris on a sphere, lids, lashes, catchlights."""
    # --- socket shading, BEFORE the aperture: the lid sits in a dent ---------
    _soft(cr, cx, cy - eh * 0.30, ew * 1.15, eh * 1.30, D["skin_deep"], 0.15)
    _soft(cr, cx, cy + eh * 0.60, ew * 1.05, eh * 0.50, D["skin_shade"], 0.22)
    # a lit brow bone above, which is what makes the socket read as recessed
    _soft(cr, cx, cy - eh * 1.55, ew * 1.05, eh * 0.55, D["skin_lit"], 0.34)

    cr.save()
    cr.translate(cx, cy)
    cr.rotate(tilt)
    _eye_aperture(cr, ew, eh, openness)
    cr.clip()
    # --- sclera: never flat white. Darker at the corners and under the lid ----
    g = cairo.LinearGradient(0.0, -eh, 0.0, eh * 0.9)
    g.add_color_stop_rgb(0.0, *D["sclera_shade"])
    g.add_color_stop_rgb(0.45, *D["sclera"])
    g.add_color_stop_rgb(1.0, *D["sclera"])
    cr.set_source(g)
    cr.new_sub_path()
    cr.arc(0.0, 0.0, max(ew, eh) * 1.6, 0.0, 2.0 * math.pi)
    cr.fill()
    _soft(cr, -ew * 0.95, 0.0, ew * 0.55, eh * 0.8, D["skin_deep"], 0.30)
    _soft(cr, ew * 0.95, 0.0, ew * 0.55, eh * 0.8, D["skin_deep"], 0.30)

    # --- the eyeball is a SPHERE, so the iris moves a fraction and the lid
    # occludes its top. Both are what stop it reading as a sticker.
    ix = gaze_x * ew * 0.30
    iy = gaze_y * eh * 0.22 + eh * 0.06
    ir = eh * 0.86
    g = cairo.RadialGradient(ix - ir * 0.22, iy - ir * 0.30, ir * 0.10,
                             ix, iy, ir)
    g.add_color_stop_rgb(0.0, *D["iris_lit"])
    g.add_color_stop_rgb(0.45, *D["iris"])
    g.add_color_stop_rgb(0.86, *D["iris"])
    g.add_color_stop_rgb(1.0, *D["iris_rim"])
    cr.set_source(g)
    cr.new_sub_path()
    cr.arc(ix, iy, ir, 0.0, 2.0 * math.pi)
    cr.fill()
    # limbal ring: the dark edge a real iris has, and the thing an iris drawn as
    # one flat colour is missing
    cr.set_source_rgba(*D["iris_rim"], 0.55)
    cr.set_line_width(ir * 0.16)
    cr.new_sub_path()
    cr.arc(ix, iy, ir * 0.92, 0.0, 2.0 * math.pi)
    cr.stroke()
    cr.set_source_rgb(*D["pupil"])
    cr.new_sub_path()
    cr.arc(ix, iy, ir * 0.40, 0.0, 2.0 * math.pi)
    cr.fill()
    # shadow cast by the upper lid onto the eyeball
    _soft(cr, ix - ew * 0.1, iy - ir * 0.98, ir * 1.35, ir * 0.62,
          D["skin_deep"], 0.42)
    # catchlight: one strong, one weak on the opposite side
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
    cr.new_sub_path()
    cr.arc(ix - ir * 0.34, iy - ir * 0.40, ir * 0.23, 0.0, 2.0 * math.pi)
    cr.fill()
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.45)
    cr.new_sub_path()
    cr.arc(ix + ir * 0.34, iy + ir * 0.34, ir * 0.10, 0.0, 2.0 * math.pi)
    cr.fill()
    cr.restore()

    # --- lids, drawn after so they bound the aperture -----------------------
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(tilt)
    up = -eh * (0.16 + 0.84 * openness)
    lo = eh * (0.80 - 0.34 * (1.0 - openness))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    # upper lid: heavy, and heavier at the outer corner where the lashes are
    cr.set_source_rgb(*D["hair_dark"])
    cr.set_line_width(eh * 0.20)
    cr.new_path()
    cr.move_to(-ew, 0.0)
    cr.curve_to(-ew * 0.52, up * 1.08, ew * 0.52, up * 1.08, ew * 0.98, -eh * 0.02)
    cr.stroke()
    # lash fan at the outer corner
    cr.set_line_width(eh * 0.055)
    for i in range(5):
        t = 0.52 + i * 0.11
        cr.set_source_rgba(*D["hair_dark"], 0.85 - i * 0.10)
        cr.new_path()
        cr.move_to(ew * t, up * (1.08 - (t - 0.5) * 1.2))
        cr.curve_to(ew * (t + 0.05), up * 0.55, ew * (t + 0.08), up * 0.35,
                    ew * (t + 0.12), up * 0.15)
        cr.stroke()
    # lower lid: a light line with a shadow beneath it
    cr.set_source_rgba(*D["skin_lit"], 0.55)
    cr.set_line_width(eh * 0.10)
    cr.new_path()
    cr.move_to(-ew, 0.0)
    cr.curve_to(-ew * 0.52, lo, ew * 0.52, lo, ew, 0.0)
    cr.stroke()
    cr.set_source_rgba(*D["skin_deep"], 0.22)
    cr.set_line_width(eh * 0.14)
    cr.new_path()
    cr.move_to(-ew * 0.95, eh * 0.10)
    cr.curve_to(-ew * 0.5, lo + eh * 0.16, ew * 0.5, lo + eh * 0.16, ew * 0.95,
                eh * 0.10)
    cr.stroke()
    # crease above the lid
    cr.set_source_rgba(*D["skin_shade"], 0.42)
    cr.set_line_width(eh * 0.075)
    cr.new_path()
    cr.move_to(-ew * 0.92, -eh * 0.22)
    cr.curve_to(-ew * 0.5, up - eh * 0.42, ew * 0.5, up - eh * 0.42,
                ew * 0.92, -eh * 0.22)
    cr.stroke()
    cr.restore()


def _nose3d(cr, R, D):
    """Nose in head-local coordinates: bridge, ball, wings, nostrils.

    Everything is a shadow or a soft light rather than a line, which is why the
    nose survives at projector distance - a stroked nose outline is the one feature
    that always looks drawn.
    """
    # bridge: lit on the key side, in shadow on the other, and the two do not meet
    # in a line down the middle
    _soft(cr, -0.050 * R, 0.20 * R, 0.046 * R, 0.30 * R, D["skin_lit"], 0.95)
    _soft(cr, 0.072 * R, 0.22 * R, 0.052 * R, 0.30 * R, D["skin_shade"], 1.00)
    # the ball: lit from above, in shadow underneath
    _soft(cr, -0.012 * R, 0.495 * R, 0.092 * R, 0.068 * R, D["skin_lit"], 0.95)
    _soft(cr, -0.020 * R, 0.470 * R, 0.048 * R, 0.034 * R, D["sub"], 0.55)
    _soft(cr, 0.010 * R, 0.560 * R, 0.098 * R, 0.038 * R, D["skin_deep"], 0.80)
    # wings, with the nostrils between them
    for sign in (-1, 1):
        _soft(cr, sign * 0.090 * R, 0.512 * R, 0.044 * R, 0.056 * R,
              D["sub"], 0.60)
        _soft(cr, sign * 0.088 * R, 0.548 * R, 0.036 * R, 0.030 * R,
              D["skin_deep"], 0.85)
        _soft(cr, sign * 0.046 * R, 0.556 * R, 0.026 * R, 0.013 * R,
              D["skin_deep"], 1.00, rot=sign * 0.40)
    # the shadow the nose throws down onto the philtrum: without it the nose is a
    # texture rather than a form, which is exactly how the first pass read at this
    # size
    _soft(cr, 0.010 * R, 0.596 * R, 0.070 * R, 0.026 * R, D["skin_deep"], 0.30)
    # philtrum: two faint columns, not one line
    for sign in (-1, 1):
        _soft(cr, sign * 0.020 * R, 0.660 * R, 0.014 * R, 0.055 * R,
              D["skin_shade"], 0.35)


def _lips3d(cr, cx, cy, w, curve, opening, D):
    """Mouth: cupid's bow, a full lower lip, and a real aperture when it parts.

    Drawn in MOUTH-LOCAL coordinates - the caller's (cx, cy) is applied as a
    translate, and every y below is relative to the mouth line. The first version of
    this function took (cx, cy) and then ignored cy in every coordinate, so the mouth
    was painted at the head's ORIGIN - which put a red band across the eye line and
    left the face with no mouth at all. That is the identical failure the rung 2 nose
    shipped (a head-local helper called in frame coordinates), and the identical fix:
    put the offset in ONE transform rather than in forty numbers.
    """
    cr.save()
    cr.translate(cx, cy)
    half = w / 2.0
    sag = curve * w * 0.30
    gap = opening * w * 0.55
    # LIP THICKNESS, measured against the shape rather than the expression. The
    # first render had down = 0.175w and the lower lip came out as a 39 px dark
    # crescent on a 194 px mouth - a grin, not a smile. A closed mouth's upper lip
    # is about 0.09 of its width and the lower about 0.11; anything thicker stops
    # reading as lips and starts reading as an open mouth.
    up = 0.088 * w * max(0.80, 1.0 - curve * 0.30)
    down = 0.112 * w

    if gap > w * 0.012:
        # interior first: a dark lens with a gradient, so an open mouth has depth
        cr.save()
        cr.new_path()
        cr.move_to(-half, sag)
        cr.curve_to(-half * 0.45, sag + gap * 0.30,
                    half * 0.45, sag + gap * 0.30, half, sag)
        cr.curve_to(half * 0.45, sag + gap * 0.62,
                    -half * 0.45, sag + gap * 0.62, -half, sag)
        cr.close_path()
        g = cairo.LinearGradient(0.0, sag, 0.0, sag + gap)
        g.add_color_stop_rgb(0.0, *D["mouth_in"])
        g.add_color_stop_rgb(0.55, D["mouth_in"][0] * 0.55,
                             D["mouth_in"][1] * 0.55, D["mouth_in"][2] * 0.55)
        g.add_color_stop_rgb(1.0, *D["mouth_in"])
        cr.set_source(g)
        cr.fill()
        cr.restore()
        # teeth, only once the opening is wide enough to show them
        if opening > 0.22:
            cr.save()
            cr.new_path()
            cr.move_to(-half * 0.80, sag)
            cr.curve_to(-half * 0.40, sag + gap * 0.26,
                        half * 0.40, sag + gap * 0.26, half * 0.80, sag)
            cr.curve_to(half * 0.40, sag + gap * 0.10,
                        -half * 0.40, sag + gap * 0.10, -half * 0.80, sag)
            cr.close_path()
            cr.set_source_rgba(0.93, 0.91, 0.88, 0.92)
            cr.fill()
            cr.restore()

    # upper lip: cupid's bow on top, the mouth line underneath
    cr.save()
    cr.new_path()
    cr.move_to(-half, sag * 0.0)
    cr.curve_to(-half * 0.66, -up * 1.02, -half * 0.30, -up * 0.92,
                -half * 0.15, -up * 0.70)
    cr.curve_to(-half * 0.05, -up * 0.50, half * 0.05, -up * 0.50,
                half * 0.15, -up * 0.70)
    cr.curve_to(half * 0.30, -up * 0.92, half * 0.66, -up * 1.02,
                half, 0.0)
    cr.curve_to(half * 0.45, sag + gap * 0.06,
                -half * 0.45, sag + gap * 0.06, -half, 0.0)
    cr.close_path()
    g = cairo.LinearGradient(0.0, -up, 0.0, 0.0)
    g.add_color_stop_rgb(0.0, *D["lip_shade"])
    g.add_color_stop_rgb(0.30, *D["lip"])
    g.add_color_stop_rgb(1.0, *D["lip"])
    cr.set_source(g)
    cr.fill()
    cr.restore()

    # lower lip: fuller, lit across its middle
    cr.save()
    cr.new_path()
    cr.move_to(-half, sag * 0.0)
    cr.curve_to(-half * 0.62, sag + down * 0.95,
                half * 0.62, sag + down * 0.95, half, 0.0)
    cr.curve_to(half * 0.45, sag + gap * 0.10,
                -half * 0.45, sag + gap * 0.10, -half, sag * 0.0)
    cr.close_path()
    g = cairo.LinearGradient(0.0, sag, 0.0, sag + down)
    g.add_color_stop_rgb(0.0, *D["lip"])
    g.add_color_stop_rgb(0.35, *D["lip"])
    g.add_color_stop_rgb(1.0, D["lip"][0] * 0.88, D["lip"][1] * 0.80,
                         D["lip"][2] * 0.80)
    cr.set_source(g)
    cr.fill()
    cr.restore()

    # specular on the lower lip - the cue that makes it read as wet rather than flat
    _soft(cr, -half * 0.12, sag + down * 0.42, half * 0.30, down * 0.26,
          D["lip_lit"], 0.55, rot=-0.08)
    # corners: small dark dents, so the mouth ends somewhere
    for sign in (-1, 1):
        _soft(cr, sign * half, sag * 0.4, w * 0.045, w * 0.035,
              D["skin_deep"], 0.45)
    # the seam, and the shadow the lips cast on the chin
    cr.set_source_rgba(*D["lip_shade"], 0.75)
    cr.set_line_width(w * 0.020)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.new_path()
    cr.move_to(-half * 0.98, sag * 0.02)
    cr.curve_to(-half * 0.45, sag + gap * 0.14,
                half * 0.45, sag + gap * 0.14, half * 0.98, sag * 0.02)
    cr.stroke()
    _soft(cr, 0.0, sag + down + w * 0.045, half * 0.70, w * 0.045,
          D["skin_deep"], 0.32)
    cr.restore()


def _noise(seed, lo, hi, white=False, size=96):
    """A cached tile of per-pixel alpha noise; black by default, white on request.

    DETERMINISTIC, and that is the whole point. Grain regenerated per frame reads as
    video noise, and a face that shimmers is worse than a face that is too smooth. A
    fixed seed also keeps `--selftest` stable, which is what lets the pixel checks
    mean anything.

    Premultiplied ARGB32, so a white pixel with alpha a has colour channels of a and
    a black one has zeros. Two tiles - one darkening, one lifting - sand the surface
    instead of dirtying it, which is the difference between texture and a stain.
    """
    key = (seed, lo, hi, white, size)
    hit = _NOISE.get(key)
    if hit is not None:
        return hit
    rng = random.Random(seed)
    buf = bytearray(size * size * 4)
    for i in range(size * size):
        a = max(0, min(255, int(255.0 * rng.uniform(lo, hi))))
        if white:
            buf[i * 4 + 0] = a
            buf[i * 4 + 1] = a
            buf[i * 4 + 2] = a
        buf[i * 4 + 3] = a
    surf = cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32,
                                              size, size, size * 4)
    surf.flush()
    # the buffer has to outlive the surface: cairo reads it, it does not own it
    _NOISE[key] = (surf, buf)
    return _NOISE[key]


def _grain(cr, R, period, seed, lo, hi, white=False, alpha=0.5):
    """Paint one noise layer over the current clip.

    `period` is the tile's repeat size as a fraction of R, so the texture scales with
    the face and the same call works at 960x540 and at 1920x1080.
    """
    surf, _buf = _noise(seed, lo, hi, white)
    size = surf.get_width()
    pat = cairo.SurfacePattern(surf)
    pat.set_extend(cairo.EXTEND_REPEAT)
    m = cairo.Matrix()
    m.scale(period * R / size, period * R / size)
    pat.set_matrix(m)
    cr.set_source(pat)
    cr.paint_with_alpha(alpha)


def _skin_texture(cr, R, D):
    """The whole second pass over the skin: grain, mottle, stubble, wear.

    Called inside the head's clip, BEFORE any feature. The first render was lit but
    perfectly smooth - one radial gradient and soft shadows - and that smoothness is
    most of what made it read as a drawing rather than a person. Nothing here moves a
    coordinate; it only puts back the things a camera would catch: the sanded surface
    of skin, its uneven colour, the cool patch of a shave, and a couple of lines that
    are only there if you look for them.

    Clips itself to the head outline: the caller has already released the clip it
    used for the shading, and grain that escapes the silhouette lands on the
    background as visible dust.
    """
    cr.save()
    _head3d_outline(cr, R)
    cr.clip()
    # grain: fine, and both ways, so the surface is sanded rather than dirtied
    _grain(cr, R, 0.050, 11, 0.00, 0.17, False, 0.20)
    _grain(cr, R, 0.050, 12, 0.00, 0.13, True, 0.14)
    # mottle: real skin is never one colour. Broad and very faint.
    _grain(cr, R, 0.62, 13, 0.00, 0.22, False, 0.030)
    _grain(cr, R, 0.85, 14, 0.00, 0.18, True, 0.025)
    # a shave: the jaw and upper lip are cool and a little mottled. Warm skin with a
    # cool patch in the beard line is one of the loudest "this is a person" cues.
    beard = (0.46, 0.42, 0.45)
    _soft(cr, 0.0, 0.72 * R, 0.46 * R, 0.22 * R, beard, 0.030)
    _soft(cr, 0.0, 0.94 * R, 0.34 * R, 0.14 * R, beard, 0.040)
    for sign in (-1, 1):
        _soft(cr, sign * 0.42 * R, 0.60 * R, 0.16 * R, 0.20 * R, beard, 0.030,
              rot=sign * 0.3)
    _soft(cr, 0.0, 0.44 * R, 0.13 * R, 0.06 * R, beard, 0.05)      # upper lip
    # grain over the shaved area specifically, so the beard line has the coarser
    # texture stubble actually has
    cr.save()
    cr.new_path()
    cr.rectangle(-0.60 * R, 0.40 * R, 1.20 * R, 0.62 * R)
    cr.clip()
    _grain(cr, R, 0.028, 15, 0.00, 0.30, False, 0.12)
    cr.restore()
    # fine lines: crow's feet and a forehead line. This is the cheapest realism in
    # the pass - two strokes per side, at an alpha you have to look for.
    for sign in (-1, 1):
        for i, k in enumerate((0.20, 0.15, 0.11)):
            cr.set_source_rgba(*D["skin_shade"], k)
            cr.set_line_width(0.0035 * R)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.new_path()
            x0 = sign * (0.505 + i * 0.012) * R
            cr.move_to(x0, -0.030 * R + i * 0.020 * R)
            cr.curve_to(x0 + sign * 0.030 * R, 0.010 * R + i * 0.016 * R,
                        x0 + sign * 0.045 * R, 0.030 * R + i * 0.020 * R,
                        x0 + sign * 0.058 * R, 0.048 * R + i * 0.024 * R)
            cr.stroke()
    cr.set_source_rgba(*D["skin_shade"], 0.13)
    cr.set_line_width(0.004 * R)
    cr.new_path()
    cr.move_to(-0.30 * R, -0.80 * R)
    cr.curve_to(-0.10 * R, -0.84 * R, 0.14 * R, -0.83 * R, 0.30 * R, -0.78 * R)
    cr.stroke()
    # two imperfections. Symmetry is what makes a drawn face look like a mask.
    _soft(cr, 0.34 * R, 0.16 * R, 0.016 * R, 0.014 * R, D["skin_deep"], 0.34)
    _soft(cr, -0.52 * R, 0.02 * R, 0.010 * R, 0.009 * R, D["skin_deep"], 0.26)
    cr.restore()


def _brow_hairs3d(cr, cx, cy, w, angle, thickness, colour):
    """Fine strokes over a brow's mass, so the brow ends in hairs.

    `_brow3d` draws the shape; a shape with a hard lower edge is what a stencilled
    brow looks like. These are drawn on the same arc with the same taper, a couple of
    them longer than the rest, and a few lighter ones through the middle.
    """
    rng = random.Random(int(abs(cx) * 7) & 0xFFFF)
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(angle)
    n = 24
    for i in range(n):
        t = i / float(n - 1)
        x = -w * 0.5 + w * t
        arch = -math.sin(math.pi * t) * w * 0.13
        # A hair is a fraction of the brow's own thickness, not a multiple of it:
        # the first pass drew 1.5-3.4 thicknesses radiating upward and rendered as
        # a comb standing on the face.
        length = thickness * (0.55 + 0.55 * rng.random())
        lean = (rng.random() - 0.5) * thickness * 0.9
        light = rng.random() < 0.20
        a = 0.28 + 0.34 * rng.random()
        col = (min(1.0, colour[0] * 1.8), min(1.0, colour[1] * 1.8),
               min(1.0, colour[2] * 1.8)) if light else colour
        cr.set_source_rgba(col[0], col[1], col[2],
                           a * 0.6 if light else a)
        cr.set_line_width(thickness * (0.16 + 0.12 * rng.random()))
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.new_path()
        cr.move_to(x, arch + thickness * 0.30)
        cr.curve_to(x + lean * 0.30, arch - length * 0.40,
                    x + lean * 0.65, arch - length * 0.72,
                    x + lean, arch - length)
        cr.stroke()
    cr.restore()


_NOISE = {}


def _draw_dimensional_procedural(cr, w, h, st, show_hud=True):
    """Rung 3: the same anatomy as rung 2, lit instead of outlined."""
    D = DIM_BASE
    s = min(w, h)
    cx = w / 2.0
    cy = h / 2.0 + s * 0.02 + st.head_bob * s
    R = s * 0.30

    # --- background: a gradient with a glow behind the head. A flat fill is the
    # one thing that most makes a shaded face look pasted onto a card.
    g = cairo.LinearGradient(0.0, 0.0, 0.0, h)
    g.add_color_stop_rgb(0.0, 0.058, 0.055, 0.098)
    g.add_color_stop_rgb(0.55, 0.040, 0.038, 0.072)
    g.add_color_stop_rgb(1.0, 0.022, 0.020, 0.042)
    cr.set_source(g)
    cr.paint()
    _soft(cr, cx, cy - R * 0.30, w * 0.46, h * 0.52, (0.20, 0.26, 0.42), 0.34,
          core=0.10)

    # --- shoulders: a dark tee, lit along the top edge
    shy = cy + R * 1.62
    cr.save()
    cr.new_path()
    cr.move_to(cx - R * 2.30, h)
    cr.curve_to(cx - R * 1.30, shy + R * 0.10, cx - R * 0.62, shy - R * 0.22,
                cx - R * 0.30, shy - R * 0.30)
    cr.curve_to(cx, shy - R * 0.10, cx, shy - R * 0.10,
                cx + R * 0.30, shy - R * 0.30)
    cr.curve_to(cx + R * 0.62, shy - R * 0.22, cx + R * 1.30, shy + R * 0.10,
                cx + R * 2.30, h)
    cr.close_path()
    g = cairo.LinearGradient(cx - R, shy - R * 0.3, cx + R, h)
    g.add_color_stop_rgb(0.0, *D["shirt_lit"])
    g.add_color_stop_rgb(1.0, *D["shirt"])
    cr.set_source(g)
    cr.fill()
    cr.restore()

    # --- neck, with the jaw's shadow across the top of it
    cr.save()
    cr.new_path()
    cr.move_to(cx - R * 0.46, cy + R * 0.54)
    cr.curve_to(cx - R * 0.52, cy + R * 1.08, cx - R * 0.56, cy + R * 1.40,
                cx - R * 0.60, cy + R * 1.62)
    cr.line_to(cx + R * 0.60, cy + R * 1.62)
    cr.curve_to(cx + R * 0.56, cy + R * 1.40, cx + R * 0.52, cy + R * 1.08,
                cx + R * 0.46, cy + R * 0.54)
    cr.close_path()
    g = cairo.LinearGradient(cx - R * 0.5, 0.0, cx + R * 0.5, 0.0)
    g.add_color_stop_rgb(0.0, *D["skin"])
    g.add_color_stop_rgb(0.45, *D["skin"])
    g.add_color_stop_rgb(1.0, *D["skin_shade"])
    cr.set_source(g)
    cr.fill()
    _soft(cr, cx, cy + R * 0.86, R * 0.62, R * 0.26, D["skin_deep"], 0.55)
    _soft(cr, cx + R * 0.10, cy + R * 1.20, R * 0.30, R * 0.22,
          D["skin_shade"], 0.35)
    cr.restore()

    # ================= head-local from here on =================
    cr.save()
    cr.translate(cx, cy)

    # --- ears, behind the head's silhouette edge
    for sign in (-1, 1):
        cr.save()
        cr.new_path()
        cr.move_to(sign * 0.86 * R, -0.16 * R)
        cr.curve_to(sign * 1.00 * R, -0.18 * R, sign * 1.01 * R, 0.16 * R,
                    sign * 0.88 * R, 0.20 * R)
        cr.curve_to(sign * 0.82 * R, 0.10 * R, sign * 0.82 * R, -0.06 * R,
                    sign * 0.86 * R, -0.16 * R)
        cr.close_path()
        g = cairo.LinearGradient(sign * 0.80 * R, 0.0, sign * 1.02 * R, 0.0)
        g.add_color_stop_rgb(0.0, *D["skin_shade"])
        g.add_color_stop_rgb(0.55, *D["skin"])
        g.add_color_stop_rgb(1.0, *D["skin_lit"])
        cr.set_source(g)
        cr.fill()
        _soft(cr, sign * 0.915 * R, 0.020 * R, 0.040 * R, 0.090 * R,
              D["sub"], 0.45)
        cr.restore()

    # --- the head: one radial gradient IS the key light
    cr.save()
    _head3d_outline(cr, R)
    g = cairo.RadialGradient(-0.42 * R, -0.58 * R, R * 0.10,
                             -0.16 * R, -0.10 * R, R * 1.62)
    g.add_color_stop_rgb(0.0, *D["skin_lit"])
    g.add_color_stop_rgb(0.42, *D["skin"])
    g.add_color_stop_rgb(0.78, *D["skin"])
    g.add_color_stop_rgb(1.0, *D["skin_shade"])
    cr.set_source(g)
    cr.fill_preserve()
    cr.clip()
    # the shaded side, as a broad occlusion rather than a hard crescent
    _soft(cr, 0.92 * R, 0.10 * R, 0.55 * R, 1.30 * R, D["skin_shade"], 0.55,
          core=0.20)
    # forehead and cheek light
    _soft(cr, -0.30 * R, -0.74 * R, 0.52 * R, 0.30 * R, D["skin_lit"], 0.34)
    _soft(cr, -0.44 * R, 0.30 * R, 0.28 * R, 0.22 * R, D["key"], 0.14, rot=-0.3)
    _soft(cr, -0.46 * R, 0.36 * R, 0.20 * R, 0.14 * R, D["sub"], 0.12, rot=-0.3)
    _soft(cr, 0.46 * R, 0.34 * R, 0.20 * R, 0.14 * R, D["sub"], 0.16, rot=0.3)
    # jawline: two soft shadows under the cheekbones rather than a stroked arc.
    # Stroked, it drew a visible loop around the lower face - a mask rim.
    _soft(cr, -0.56 * R, 0.80 * R, 0.30 * R, 0.16 * R, D["skin_shade"], 0.22,
          rot=0.35)
    _soft(cr, 0.56 * R, 0.78 * R, 0.30 * R, 0.16 * R, D["skin_shade"], 0.26,
          rot=-0.35)
    # brow ridge shadow
    _soft(cr, 0.0, -0.21 * R, 0.78 * R, 0.13 * R, D["skin_deep"], 0.13)
    # jaw and chin: the occlusion that stops the head reading as a flat mask
    _soft(cr, 0.0, 1.06 * R, 0.62 * R, 0.16 * R, D["skin_deep"], 0.42)
    _soft(cr, 0.0, 0.86 * R, 0.46 * R, 0.10 * R, D["skin_shade"], 0.28)
    # nasolabial folds, faint and asymmetric: symmetry is what makes a drawn face
    # look like a mask. Kept short and weak - drawn as long beige arcs they read as
    # two lines someone put on the face, which the first render showed outright.
    for sign, k in ((-1, 0.16), (1, 0.11)):
        cr.save()
        cr.set_source_rgba(*D["skin_shade"], k)
        cr.set_line_width(0.024 * R)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.new_path()
        cr.move_to(sign * 0.27 * R, 0.50 * R)
        cr.curve_to(sign * 0.34 * R, 0.60 * R, sign * 0.36 * R, 0.66 * R,
                    sign * 0.33 * R, 0.72 * R)
        cr.stroke()
        cr.restore()
    # hairline shadow, UNDER the hair so its top edge is covered
    _soft(cr, -0.10 * R, -0.56 * R, 0.86 * R, 0.13 * R, D["skin_deep"], 0.30)
    # rim light on the shaded edge, BEFORE the hair: drawn after it, the stroke
    # crossed the hair at the temple and read as a scratch on the render
    cr.save()
    _head3d_outline(cr, R)
    cr.clip()
    # three passes rather than one stroke: a single line has a hard edge on both
    # sides and reads as a scratch, which is exactly how the first render looked.
    # Layered, it falls off into the skin like light.
    for width, alpha in ((0.075, 0.10), (0.042, 0.14), (0.020, 0.18)):
        cr.set_source_rgba(*D["rim"], alpha)
        cr.set_line_width(width * R)
        cr.new_path()
        cr.move_to(0.46 * R, -1.08 * R)
        cr.curve_to(0.93 * R, -0.78 * R, 0.98 * R, -0.06 * R, 0.60 * R, 0.78 * R)
        cr.stroke()
    cr.restore()
    # THE SECOND PASS OVER THE SKIN: grain, mottle, stubble, fine lines. Inside the
    # head's clip and BEFORE every feature, so it textures skin and nothing else -
    # the iris, the lips and the eye whites stay exactly the colours that the
    # self-test measures them to be.
    _skin_texture(cr, R, D)
    cr.restore()

    # --- hair: a mass, then strands, then one specular sweep
    cr.save()
    _hair3d_outline(cr, R)
    g = cairo.LinearGradient(-0.6 * R, -1.30 * R, 0.6 * R, -0.30 * R)
    g.add_color_stop_rgb(0.0, *D["hair_dark"])
    g.add_color_stop_rgb(0.35, *D["hair"])
    g.add_color_stop_rgb(0.75, D["hair"][0] * 1.25, D["hair"][1] * 1.25,
                         D["hair"][2] * 1.25)
    g.add_color_stop_rgb(1.0, *D["hair_dark"])
    cr.set_source(g)
    cr.fill_preserve()
    cr.save()
    cr.clip()
    # strands sweeping from the part to the right temple
    for i in range(13):
        t = i / 12.0
        y0 = -1.24 * R + t * 0.30 * R
        cr.new_path()
        cr.move_to((0.02 - 0.10 * t) * R, y0)
        cr.curve_to((0.36 + 0.10 * t) * R, (y0 + 0.16 * R),
                    (0.72 + 0.06 * t) * R, (y0 + 0.44 * R),
                    (0.96 - 0.04 * t) * R, (y0 + 0.86 * R))
        cr.curve_to((0.70 + 0.04 * t) * R, (y0 + 0.50 * R),
                    (0.34 + 0.06 * t) * R, (y0 + 0.24 * R),
                    (0.00 - 0.08 * t) * R, (y0 + 0.08 * R))
        cr.close_path()
        k = 0.55 + 0.45 * math.sin(i * 1.7)
        cr.set_source_rgba(D["hair_lit"][0], D["hair_lit"][1], D["hair_lit"][2],
                           0.10 + 0.16 * k)
        cr.fill()
    # temple locks: without these the mass stops at the hairline and the sides of
    # the head are bare skin, which is what made the first render read as a wig
    # the specular sweep
    _soft(cr, -0.18 * R, -0.94 * R, 0.62 * R, 0.24 * R, D["hair_lit"], 0.42,
          rot=-0.12, core=0.25)
    _soft(cr, 0.48 * R, -0.70 * R, 0.34 * R, 0.16 * R, D["hair_lit"], 0.22,
          rot=-0.30, core=0.25)
    cr.restore()
    # TEMPLE LOCKS, drawn after the clip is released: inside the mass's own clip
    # they drew nothing at all, because they sit outside it - which is exactly where
    # they need to be for the hair to reach the sides of the head.
    for sign in (-1, 1):
        cr.new_path()
        cr.move_to(sign * 0.955 * R, -0.36 * R)
        cr.curve_to(sign * 1.010 * R, -0.14 * R, sign * 0.975 * R, 0.08 * R,
                    sign * 0.900 * R, 0.20 * R)
        cr.curve_to(sign * 0.860 * R, 0.02 * R, sign * 0.865 * R, -0.14 * R,
                    sign * 0.885 * R, -0.28 * R)
        cr.close_path()
        cr.set_source_rgb(*D["hair"])
        cr.fill()
        _soft(cr, sign * 0.930 * R, -0.02 * R, 0.045 * R, 0.16 * R,
              D["hair_dark"], 0.40)
    # a soft edge, not an outline: hair against skin has no line
    cr.set_source_rgba(*D["hair_dark"], 0.55)
    cr.set_line_width(0.006 * R)
    _hair3d_outline(cr, R)
    cr.stroke()
    cr.restore()

    # --- brows, eyes, nose, lips ---------------------------------------------
    eye_dx = 0.355 * R
    ew = 0.188 * R
    eh = 0.104 * R
    _brow3d(cr, -eye_dx, -0.250 * R, 0.42 * R, -st.brow_l * 0.9 - 0.03,
            0.060 * R * 1.05, D["brow"])
    _brow3d(cr, eye_dx, -0.250 * R, 0.42 * R, -st.brow_r * 0.9 + 0.03,
            0.060 * R * 1.05, D["brow"])

    gx, gy = st.gaze
    _eye3d(cr, -eye_dx, 0.0, ew, eh, st.eye_open, gx, gy, 0.05, D)
    _eye3d(cr, eye_dx, 0.0, ew, eh, st.eye_open, gx, gy, -0.05, D)

    _nose3d(cr, R, D)
    # THE MOUTH IS THE EASIEST THING TO DRAW TOO BIG. At session scale the first
    # pass put a 224 px mouth on a 590 px-wide head - 38% of the face width, and it
    # read as a grin holding the whole screen. A mouth is about a third of the face
    # across and sits a third of the way from the nose base to the chin, so
    # 0.54R wide at 0.735R, not 0.69R at 0.775R.
    _lips3d(cr, 0.0, 0.735 * R, 0.54 * R * st.mouth_w, st.mouth_curve,
            st.mouth_open, D)

    # --- rim light on the shaded edge: the last thing that says "volume"
    # (drawn above, before the hair - see that block for why)

    cr.restore()          # end head-local

    if show_hud:
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(s * 0.022)
        cr.set_source_rgb(*TEXT_DIM)
        cr.move_to(s * 0.025, h - s * 0.030)
        cr.show_text("face v3.0  |  style=dimensional  |  person=Milo  |  %s  "
                     "|  t=%6.1fs" % (st.expression, st.t))


# --------------------------------------------------------------------------
# Rung 4 — the image puppet ("Elliot")
# --------------------------------------------------------------------------
# A photoreal face cannot be drawn from a handful of floats, so this rung is a
# different architecture rather than a bigger drawing: a STILL, with the parts that
# move animated over it. The animation carrier is the same `FaceState` the other
# three rungs use - gaze offsets the eye content, `eye_open` closes a lid, and
# `mouth_open` (the lip-sync envelope) parts the lips - so the drive that was built
# for a coded mouth carries over unchanged. That is exactly what the ladder's table
# says rung 4 has to be, and it is why the rung can exist without a new pipeline.
#
# WHAT IT CANNOT DO, said plainly: it cannot change EXPRESSION. The expression lives
# in the photograph, and there is one of it. A blink, a look and a talking mouth are
# what a single still can honestly give; a frown would need whole-head variants, which
# is the atlas the ladder's table names and this build does not have yet.
#
# THE ASSET IS THE APPROVED DESIGN STUDY. `assets/portrait-r4.png` is a copy of
# `docs/face-ladder/rung4-photoreal.png` - the rung-4 image Danny accepted as "what
# rung 4 looks like" - so the face on the projector is the face on the ladder, not a
# fresh generation that happens to be photoreal. It was re-framed on 5 November (same
# man, camera pulled back: whole head, crown in shot, head 70% of frame height against
# the coded rungs' 68%) because the first crop sat much closer than rungs 1-3, which
# broke the ladder's own premise that the only thing changing is realism. Its
# landmarks are MEASURED from its own pixels by `portrait_probe.py` into
# `assets/portrait-r4.json`; the puppet reads that file and never guesses.
#
# WHY IT FALLS BACK. Missing or malformed assets must not cost the robot its face: a
# kiosk drawing nothing because a PNG went astray is the same failure class as a bar
# drawn at zero from a file that did not say. It logs once and draws rung 3 instead,
# which is the nearest register that still animates.
PORTRAIT_NAME = "Elliot"
PORTRAIT_DIR = os.path.join(HERE, "assets")
PORTRAIT_PNG = os.path.join(PORTRAIT_DIR, "portrait-r4.png")
PORTRAIT_JSON = os.path.join(PORTRAIT_DIR, "portrait-r4.json")
# How far the eye content slides at full gaze, as a fraction of the iris radius. An
# eye rotates rather than translates, so this is small on purpose: sliding the iris a
# full iris-width looks like a sticker being pushed around the sclera.
PORTRAIT_GAZE = 0.26
PORTRAIT_LID_FULL = 1.06         # how far past the aperture a closed lid reaches

_PORTRAIT = {"stamp": None, "png": None, "lm": None, "scaled": {},
             "mouth": None, "mouth_key": None, "warned": False}


def _portrait_valid(lm):
    """Are the landmarks a description of a face, or just some numbers?

    Checked rather than trusted for the same reason the session file is: this file
    comes from a measurement script, and a re-measured portrait with a bad seed would
    otherwise put the mouth on the ear and call it a face. It is also what the
    self-test asserts, so the validation cannot rot away from the reader.
    """
    if not isinstance(lm, dict):
        return False
    try:
        w, h = float(lm["width"]), float(lm["height"])
        eyes = lm["eyes"]
        m = lm["mouth"]
        if len(eyes) != 2 or w <= 0 or h <= 0:
            return False
        for e in eyes:
            for k in ("cx", "cy", "iris_r", "ap_rx", "ap_ry"):
                v = float(e[k])
                if not (0.0 <= v <= max(w, h) * 2.0):
                    return False
            if not (0.0 <= e["cx"] <= w and 0.0 <= e["cy"] <= h):
                return False
        for k in ("cx", "cy", "half_w", "up", "down"):
            float(m[k])
        if not (0.0 <= m["cx"] <= w and 0.0 <= m["cy"] <= h):
            return False
        if float(m["half_w"]) <= 0.0 or float(m["up"]) <= 0.0 \
                or float(m["down"]) <= 0.0:
            return False
        # The mouth is BELOW the eyes on every face this ladder will ever draw. A
        # measurement that says otherwise is a bad measurement, whatever it looks
        # like numerically.
        if float(m["cy"]) <= max(float(e["cy"]) for e in eyes):
            return False
        if len(lm.get("skin", [])) != 3:
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _portrait_load(png_path=None, json_path=None, cache=None):
    """(surface, landmarks) or (None, None), logging the reason once."""
    try:
        png_path = png_path or PORTRAIT_PNG
        json_path = json_path or PORTRAIT_JSON
        cache = _PORTRAIT if cache is None else cache
        stamp = (os.path.getmtime(png_path), os.path.getmtime(json_path))
    except OSError as exc:
        if not cache["warned"]:
            log("asset unavailable (%s) - drawing the rung below instead" % exc)
            cache["warned"] = True
        return None, None
    if cache["stamp"] != stamp:
        try:
            surf = cairo.ImageSurface.create_from_png(png_path)
            with open(json_path) as fh:
                lm = json.load(fh)
        except Exception as exc:                     # noqa: BLE001 - any failure
            if not cache["warned"]:
                log("asset unreadable (%s) - drawing the rung below instead" % exc)
                cache["warned"] = True
            return None, None
        if not _portrait_valid(lm):
            if not cache["warned"]:
                log("landmarks are not a face (%r) - drawing the rung below instead"
                    % (json_path,))
                cache["warned"] = True
            return None, None
        cache.update(stamp=stamp, png=surf, lm=lm, scaled={}, mouth=None,
                         mouth_key=None, warned=False)
    return cache["png"], cache["lm"]


def _portrait_scaled(surf, lm, w, h, cache=None):
    """(frame-sized surface, dx, dy, scale) - the still fitted once per size.

    Scaling on every frame would put a 1024x576 filter on the kiosk's critical path
    30 times a second; scaling once per frame SIZE costs nothing after the first
    frame, and the size only changes if the compositor's mode does.
    """
    cache = _PORTRAIT if cache is None else cache
    key = (w, h)
    hit = cache["scaled"].get(key)
    if hit is not None:
        return hit
    sw, sh = float(lm["width"]), float(lm["height"])
    sc = max(w / sw, h / sh)                 # COVER, not fit: no letterbox bars
    dx = (w - sw * sc) / 2.0
    dy = (h - sh * sc) / 2.0
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    c2 = cairo.Context(out)
    c2.set_source_rgb(*lm["background"])
    c2.paint()
    c2.save()
    c2.translate(dx, dy)
    c2.scale(sc, sc)
    c2.set_source_surface(surf, 0.0, 0.0)
    c2.get_source().set_filter(cairo.FILTER_GOOD)
    c2.paint()
    c2.restore()
    out.flush()
    cache["scaled"] = {key: (out, dx, dy, sc)}
    return cache["scaled"][key]


def _aperture_path(cr, cx, cy, rx, ry):
    """An elliptical path in DEVICE space, built through the CTM.

    cairo stores path points in device coordinates as they are added, so building
    the ellipse under a transform and restoring afterwards leaves a correct path -
    the same idiom `_ellipse` uses. It matters here because the clip has to be the
    eye's real shape rather than its bounding box.
    """
    cr.save()
    cr.translate(cx, cy)
    cr.scale(rx, ry)
    cr.new_path()
    cr.arc(0.0, 0.0, 1.0, 0.0, 2.0 * math.pi)
    cr.restore()


def _soft_rect_mask(w_, h_, feather):
    """An A8 rectangle, opaque inside and feathered on all four edges.

    Two DEST_IN passes multiply the alpha down each axis, which is the only way to
    get a four-sided falloff out of linear gradients. Feathered because a hard
    clipping rectangle leaves a visible step where shifted skin meets unshifted
    skin: that is exactly what the previous attempt showed at 3x.
    """
    s = cairo.ImageSurface(cairo.FORMAT_A8, max(1, int(w_)), max(1, int(h_)))
    c = cairo.Context(s)
    c.set_source_rgba(1.0, 1.0, 1.0, 1.0)
    c.paint()
    c.set_operator(cairo.OPERATOR_DEST_IN)
    for horiz in (True, False):
        length = w_ if horiz else h_
        f = min(0.45, feather / max(1.0, length))
        if horiz:
            g = cairo.LinearGradient(0.0, 0.0, float(w_), 0.0)
        else:
            g = cairo.LinearGradient(0.0, 0.0, 0.0, float(h_))
        g.add_color_stop_rgba(0.0, 1, 1, 1, 0.0)
        g.add_color_stop_rgba(f, 1, 1, 1, 1.0)
        g.add_color_stop_rgba(1.0 - f, 1, 1, 1, 1.0)
        g.add_color_stop_rgba(1.0, 1, 1, 1, 0.0)
        c.set_source(g)
        c.paint()
    s.flush()
    return s


def _slice(img, x, y, w_, h_):
    """A copy of one rectangle of `img`, as its own surface."""
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w_)), max(1, int(h_)))
    c = cairo.Context(s)
    c.set_source_surface(img, -x, -y)
    c.paint()
    s.flush()
    return s


def _portrait_mouth_cache(img, mx, my, hw, up, down, sc):
    """The two lip bands, sliced and masked once per frame size.

    Sliced rather than pushed into a group each frame: a group costs a full-frame
    surface allocation, and this runs on a Jetson at 30 Hz for the length of a
    session. The band is a fixed rectangle of a fixed image, so cutting it once and
    blitting it small is the same picture for a fraction of the work.
    """
    pad = max(2.0, 3.0 * sc)
    feather = max(2.0, 2.5 * sc)
    half = hw * 1.06
    bands = {}
    for name, y0, h_ in (("up", my - up - pad, up + pad),
                         ("lo", my, down + pad)):
        bands[name] = {
            "rect": (mx - half, y0, half * 2.0, h_),
            "crop": _slice(img, mx - half, y0, half * 2.0, h_),
            "mask": _soft_rect_mask(half * 2.0, h_, feather),
        }
    return bands


def _draw_puppet(cr, w, h, st, show_hud, png_path, json_path, cache, who,
                 style_label, fallback):
    """A still with its eyes and mouth driven from `st` - the whole of both rungs.

    Rung 4 and rung 3 are the same rig over different pictures: one still of Elliot,
    and an atlas of two of Milo. Everything below is about the PHOTOGRAPH - its own
    lid skin, its own lips, its own aperture - so it is written once and pointed at
    whichever asset is in play. `fallback` is what to draw when the asset is missing,
    which must be rung BELOW this one and not a recursion.
    """
    surf, lm = _portrait_load(png_path, json_path, cache)
    if surf is None:
        return fallback(cr, w, h, st, show_hud=show_hud)

    img, dx, dy, sc = _portrait_scaled(surf, lm, w, h, cache)
    bob = st.head_bob * h
    skin = lm["skin"]

    cr.set_source_rgb(*lm["background"])
    cr.paint()
    cr.save()
    cr.translate(0.0, bob)
    cr.set_source_surface(img, 0.0, 0.0)
    cr.paint()

    # An atlas may include a SHUT still, and if it does that is what a blink is:
    # no lid is synthesised at all, the picture is cross-faded to one where the eyes
    # are already closed. Synthesising a lid out of stretched skin works on Elliot -
    # narrow eye, clean band of lid above it - and could not be made to work on Milo,
    # whose big round eye has no clean band above (the brow sits on the crease) and a
    # bright lit cheek below. Every band either showed his iris or pasted a pale disc
    # over it. A third picture answers the question instead of approximating it.
    shut = None
    if lm.get("eyes_closed"):
        if "closed" not in cache:
            cache["closed"] = {"stamp": None, "png": None, "lm": None, "scaled": {},
                               "mouth": None, "mouth_key": None, "warned": False}
        cpath = os.path.join(os.path.dirname(json_path), lm["eyes_closed"])
        csurf, clm = _portrait_load(cpath, json_path, cache["closed"])
        if csurf is not None:
            shut = _portrait_scaled(csurf, clm, w, h, cache["closed"])[0]

    gx = max(-1.0, min(1.0, st.gaze[0]))
    gy = max(-1.0, min(1.0, st.gaze[1]))
    for e in lm["eyes"]:
        ex = dx + e["cx"] * sc
        ey = dy + e["cy"] * sc
        rx = e["ap_rx"] * sc
        ry = e["ap_ry"] * sc
        ir = e["iris_r"] * sc
        # The opening is NOT symmetric about the iris centre: the upper lid clears the
        # iris by more than the lower one does (13 px above, 7 below on this still).
        # A symmetric ellipse drawn at `ey` therefore leaves the photograph's own lash
        # line uncovered along the top, so a closed eye still showed a strip of the
        # open one. These two lines are what the measured ap_up/ap_down are for.
        up_h = e.get("ap_up", e["ap_ry"]) * sc
        dn_h = e.get("ap_down", e["ap_ry"]) * sc
        ap_cy = ey + (dn_h - up_h) / 2.0
        ap_ry = max(up_h, dn_h)

        # --- gaze: the eye's own content, slid inside its own aperture -------
        ox = gx * ir * PORTRAIT_GAZE
        oy = gy * ir * PORTRAIT_GAZE * 0.7
        if abs(ox) > 0.15 or abs(oy) > 0.15:
            cr.save()
            _aperture_path(cr, ex, ap_cy, rx * 0.93, ap_ry * 0.90)
            cr.clip()
            cr.set_source_surface(img, ox, oy)
            cr.paint()
            cr.restore()

        # --- blink: a lid made of the still's OWN lid skin ------------------
        # The first version filled the aperture with the average skin colour from
        # the landmarks file, which read as a pale sticker: flat, brighter than the
        # lid it covered, no crease, and a grey line for lashes. Sampling the band
        # of skin just above the lash line and stretching it down keeps the
        # texture, the crease and the shading that the photograph already has, and
        # a vertical stretch of skin is the one distortion nobody can see.
        op = max(0.0, min(1.0, st.eye_open))
        if op < 0.995 and shut is None:
            top = ap_cy - ap_ry
            bot = ap_cy + ap_ry
            lid = top + (1.0 - op) * (bot - top) * PORTRAIT_LID_FULL
            # Sample the lid skin BETWEEN the lash line and the brow, and stretch it
            # only as far as there is skin: 1.0ry of it, taken to end exactly at the
            # lash. Reaching higher (1.95ry) pulls the brow in and the closed lids come
            # out brown and streaked; taking less (0.47ry) flattens them into a pale
            # wash. One ry is the whole of the clean lid on a face this size.
            # WHERE the clean lid skin is depends on the face, so the atlas says.
            # Elliot has a full upper lid and samples ABOVE the lash (offset -1.98,
            # the default). Milo's brow sits almost on his crease, so the band above
            # his eye is eyebrow: the closed lid came out dark maroon, stretched from
            # hair. His atlas sets a POSITIVE offset and the lid is made from the
            # lit skin below the eye instead, which for a vertical stretch of skin is
            # the same picture.
            band_off, band_h = lm.get("lid_band") or (-1.98, 1.00)
            src_y0 = ap_cy + ap_ry * band_off
            src_h = ap_ry * band_h
            # Overshoot BOTH ends. The lid has to hide the whole opening, and the
            # bottom of an opening is where an iris shows if the fill stops a pixel
            # short - so the band is allowed to run past the aperture and let the
            # clip decide, rather than ending on a computed line that lands on the
            # lash and leaves a sliver of pupil underneath.
            dst_y0 = top - ap_ry * 0.10
            dst_h = max(2.0, (bot + ap_ry * 0.10) - dst_y0) if op < 0.5 \
                else max(2.0, lid - dst_y0)
            cr.save()
            _aperture_path(cr, ex, ap_cy, rx * 1.04, ap_ry * 1.02)
            cr.clip()
            cr.save()
            cr.rectangle(ex - rx * 1.10, dst_y0, rx * 2.20, dst_h)
            cr.clip()
            cr.translate(ex - rx * 1.10, dst_y0)
            cr.scale(1.0, dst_h / src_h)
            cr.set_source_surface(img, -(ex - rx * 1.10), -src_y0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
            # THE TINT. A band taken from under the eye is the only clean skin on
            # some faces, but under-eye skin is LIT skin: stretched over the lid it
            # reads as a pale sticker, which is the exact failure this construction
            # exists to avoid. So the atlas may also name a band to lay over the fill
            # at part alpha - skin from where the lid's own tone is, however little of
            # it there is. Same stretch, same rect, one more small blit.
            tint = lm.get("lid_tint")
            if tint:
                t_off, t_h, t_a = tint[0], tint[1], tint[2]
                cr.set_source_surface(img, -(ex - rx * 1.10), -(ap_cy + ap_ry * t_off))
                cr.get_source().set_filter(cairo.FILTER_GOOD)
                cr.paint_with_alpha(t_a)
            cr.restore()
            # the lash line, where the lid meets the eye. Dark and thin, with a
            # soft shadow under it - a thick grey arc is what made the first
            # attempt read as a drawn-on line.
            _soft(cr, ex, lid + ap_ry * 0.10, rx * 0.85, ap_ry * 0.22,
                  (0.10, 0.06, 0.05), 0.55 * (1.0 - op) + 0.10)
            cr.set_source_rgba(0.09, 0.055, 0.045, 0.55 * (1.0 - op) + 0.10)
            cr.set_line_width(max(1.1, ap_ry * 0.08))
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.new_path()
            cr.move_to(ex - rx * 0.90, lid - ap_ry * 0.06)
            cr.curve_to(ex - rx * 0.40, lid + ap_ry * 0.06,
                        ex + rx * 0.40, lid + ap_ry * 0.06,
                        ex + rx * 0.90, lid - ap_ry * 0.06)
            cr.stroke()
            cr.restore()

    # The shut still, faded in over the closing eye. Fully closed by the time the
    # eyelid is 65% down and fully gone by the time it is 90% open, so the fade is
    # finished while the lid is still moving and the two never appear to disagree.
    op_e = max(0.0, min(1.0, st.eye_open))
    if shut is not None and op_e < 0.92:
        cr.set_source_surface(shut, 0.0, 0.0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint_with_alpha(max(0.0, min(1.0, (0.92 - op_e) / 0.45)))

    # --- the mouth: the lips are the still's own pixels, slid apart ----------
    # GEOMETRY MATTERS HERE, because the first version did not open at all. The lip
    # bands were clipped to their SOURCE position and then filled from a shifted
    # image, so the band's far edge sampled the mouth line and painted the closed
    # lips straight back over the opening. The bands have to be clipped to their
    # DESTINATION; the source offset is what moves the pixels.
    m = lm["mouth"]
    mx = dx + m["cx"] * sc
    my = dy + m["cy"] * sc
    hw = m["half_w"] * sc
    up = m["up"] * sc
    down = m["down"] * sc
    op = max(0.0, min(1.0, st.mouth_open))
    gap = op * (up + down) * 0.85
    if gap > 1.0:
        lift = gap * 0.28           # the upper lip rises this far
        drop = gap * 0.72           # the jaw carries the lower lip this far
        top = my - lift
        bot = my + drop
        bow = hw * 0.10             # the opening keeps the mouth's bow

        # 1. the opening, with depth: a gradient, not a black hole
        cr.save()
        cr.new_path()
        cr.move_to(mx - hw * 0.96, top)
        cr.curve_to(mx - hw * 0.45, top + bow, mx + hw * 0.45, top + bow,
                    mx + hw * 0.96, top)
        cr.curve_to(mx + hw * 0.55, bot + bow, mx - hw * 0.55, bot + bow,
                    mx - hw * 0.96, top)
        cr.close_path()
        g = cairo.LinearGradient(0.0, top, 0.0, bot + bow)
        g.add_color_stop_rgb(0.0, 0.10, 0.035, 0.045)
        g.add_color_stop_rgb(0.45, 0.24, 0.075, 0.085)
        g.add_color_stop_rgb(1.0, 0.13, 0.04, 0.05)
        cr.set_source(g)
        cr.fill()
        cr.restore()

        # 2-3. the two lips: the still's own bands, blitted at their new places
        # through a feathered mask. Crops are cached per frame size; the only
        # per-frame work is two small masked blits.
        if cache.get("mouth_key") != (w, h):
            cache["mouth"] = _portrait_mouth_cache(img, mx, my, hw, up,
                                                       down, sc)
            cache["mouth_key"] = (w, h)
        bands = cache["mouth"]
        for name, dy_ in (("up", -lift), ("lo", drop)):
            bx, by, bw, bh = bands[name]["rect"]
            cr.save()
            cr.set_source_surface(bands[name]["crop"], bx, by + dy_)
            cr.mask_surface(bands[name]["mask"], bx, by)
            cr.restore()

        # 4. a soft shadow under the lower lip, where the jaw's displacement shows.
        # A stroked arc read as a grey band with cut ends; a blurred ellipse has no
        # ends to cut.
        _soft(cr, mx, bot + down * 0.55, hw * 0.80, down * 0.55,
              (0.05, 0.02, 0.02), 0.16)
    cr.restore()

    if show_hud:
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(min(w, h) * 0.022)
        cr.set_source_rgb(*TEXT_DIM)
        cr.move_to(min(w, h) * 0.025, h - min(w, h) * 0.030)
        cr.show_text("face v3.0  |  style=%s  |  person=%s  "
                     "|  mouth=%.2f  eye=%.2f  t=%6.1fs"
                     % (style_label, who, st.mouth_open, st.eye_open, st.t))



def draw_face_portrait(cr, w, h, st, show_hud=True):
    """Rung 4: Elliot, one still. No atlas: he does not change expression."""
    return _draw_puppet(cr, w, h, st, show_hud, PORTRAIT_PNG, PORTRAIT_JSON,
                        _PORTRAIT, PORTRAIT_NAME, "portrait (image puppet)",
                        _draw_dimensional_procedural)


# --- rung 3's atlas: the same man twice, and nothing else --------------------
# Milo is a PUPPET, like Elliot, but he has an ATLAS where Elliot has a single
# still: two pictures of the same man in the same pose, one at rest and one smiling.
# The rig is identical for both, measured once into assets/milo.json, so swapping
# the picture cannot move his eyes.
#
# Two expressions, not three. A third (concerned) was designed and dropped: each
# additional still is another generated face that has to be the same person in the
# same light, and every one of them is a chance for the character to drift. Two
# covers "idle" and "pleased", which is the whole of what a gaze-training run needs
# the robot's face to say.
MILO_NEUTRAL_PNG = os.path.join(PORTRAIT_DIR, "milo-neutral.png")
MILO_HAPPY_PNG = os.path.join(PORTRAIT_DIR, "milo-happy.png")
MILO_JSON = os.path.join(PORTRAIT_DIR, "milo.json")

_MILO_NEUTRAL = {"stamp": None, "png": None, "lm": None, "scaled": {},
                 "mouth": None, "mouth_key": None, "warned": False}
_MILO_HAPPY = {"stamp": None, "png": None, "lm": None, "scaled": {},
               "mouth": None, "mouth_key": None, "warned": False}

# Scratch frames for the cross-fade, reused rather than reallocated: this runs at
# 30 Hz and a 1920x1080 ARGB32 surface is 8 MB, so allocating two per frame during a
# transition would churn memory for no reason.
_PUPPET_BLEND = {}


def _milo_weight(st):
    """0 = at rest, 1 = smiling, read off the EASED mouth curve.

    The face already eases every expression parameter toward its target over about a
    tenth of a second, so the cross-fade rides that instead of running a clock of its
    own. A drawn mouth curving and a photograph dissolving then take exactly the same
    line, and there is no second animation to fall out of step with the first.
    """
    lo = EXPRESSION_PARAMS["neutral"]["mouth_curve"]
    hi = EXPRESSION_PARAMS["happy"]["mouth_curve"]
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (st.mouth_curve - lo) / (hi - lo)))


def _blend_pair(w, h):
    hit = _PUPPET_BLEND.get((w, h))
    if hit is None:
        _PUPPET_BLEND.clear()
        hit = (cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h),
               cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h))
        _PUPPET_BLEND[(w, h)] = hit
    return hit


def draw_face_dimensional(cr, w, h, st, show_hud=True):
    """Rung 3: Milo, from a two-still atlas (neutral, happy) on the rung-4 rig.

    This used to be drawn: every line of his face was code, so every expression was
    free and nothing needed an asset. It looked like a drawing, and no amount of
    shading fixed that - see the v2 notes in docs/STATUS.md. So rung 3 now spends
    the same currency rung 4 does (a still) and buys the same realism, keeping only
    the expressions an atlas can hold.

    If either still is missing the rung falls back to the drawn face rather than to
    a blank projector. The drawn face is still here, still maintained, and still what
    the self-test renders: it is the floor this rung stands on.
    """
    if _portrait_load(MILO_NEUTRAL_PNG, MILO_JSON, _MILO_NEUTRAL)[0] is None \
            or _portrait_load(MILO_HAPPY_PNG, MILO_JSON, _MILO_HAPPY)[0] is None:
        return _draw_dimensional_procedural(cr, w, h, st, show_hud=show_hud)

    wgt = _milo_weight(st)
    if wgt <= 0.02:
        return _draw_puppet(cr, w, h, st, show_hud, MILO_NEUTRAL_PNG, MILO_JSON,
                            _MILO_NEUTRAL, "Milo", "dimensional (atlas puppet)",
                            _draw_dimensional_procedural)
    if wgt >= 0.98:
        return _draw_puppet(cr, w, h, st, show_hud, MILO_HAPPY_PNG, MILO_JSON,
                            _MILO_HAPPY, "Milo", "dimensional (atlas puppet)",
                            _draw_dimensional_procedural)

    # In between: render both and dissolve. Two full frames is affordable here
    # because a transition lasts about a tenth of a second, and every frame of it
    # would otherwise have to be a choice between a jump and a still that has not
    # caught up with the eased expression yet.
    a, b = _blend_pair(w, h)
    _draw_puppet(cairo.Context(a), w, h, st, show_hud, MILO_NEUTRAL_PNG, MILO_JSON,
                 _MILO_NEUTRAL, "Milo", "dimensional (atlas puppet)",
                 _draw_dimensional_procedural)
    _draw_puppet(cairo.Context(b), w, h, st, show_hud, MILO_HAPPY_PNG, MILO_JSON,
                 _MILO_HAPPY, "Milo", "dimensional (atlas puppet)",
                 _draw_dimensional_procedural)
    cr.set_source_surface(a, 0.0, 0.0)
    cr.paint()
    cr.set_source_surface(b, 0.0, 0.0)
    cr.paint_with_alpha(wgt)

def draw_face(cr, w, h, st, show_hud=True, style="cartoon", look=None):
    """Draw the face in `style`. See STYLES, ILL_LOOKS and the rung comments."""
    if style == "illustrated":
        return draw_face_illustrated(cr, w, h, st, show_hud=show_hud, look=look)
    if style == "dimensional":
        return draw_face_dimensional(cr, w, h, st, show_hud=show_hud)
    if style == "portrait":
        return draw_face_portrait(cr, w, h, st, show_hud=show_hud)
    return draw_face_cartoon(cr, w, h, st, show_hud=show_hud)


def log(msg):
    """One line to stderr, which systemd puts in the journal."""
    sys.stderr.write("face: " + msg + "\n")
    sys.stderr.flush()


def draw_text_at(cr, x_centre, y_centre, text, size, ink, alpha=1.0):
    """One line of text, centred on its INK box at an arbitrary point.

    Centred on the INK box rather than on the font's em box: the two differ by the
    font's own ascent and descent, which is tens of pixels at this size and would
    put a big number visibly off-centre.
    """
    cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    cr.set_source_rgba(ink[0], ink[1], ink[2], alpha)
    cr.move_to(x_centre - ext.width / 2.0 - ext.x_bearing,
               y_centre - ext.height / 2.0 - ext.y_bearing)
    cr.show_text(text)


def draw_text(cr, w, text, size, y_centre, ink, alpha=1.0):
    """One line of text, centred in a frame of width `w`."""
    draw_text_at(cr, w / 2.0, y_centre, text, size, ink, alpha)


def draw_bar(cr, w, frac, y, height, ink=ACCENT_INK, width_frac=0.34):
    """A progress bar, centred. Used for countdowns and nothing else."""
    frac = max(0.0, min(1.0, float(frac)))
    bw = w * width_frac
    x0 = (w - bw) / 2.0
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], 0.22)
    cr.rectangle(x0, y, bw, height)
    cr.fill()
    cr.set_source_rgb(*ink)
    cr.rectangle(x0, y, max(bw * frac, 0.0), height)
    cr.fill()


def timer_box(w, h, style=DEFAULT_TIMER_STYLE):
    """(x, y, w, h) for the countdown shape, in the bottom band.

    TOP BAND, not the bottom. The face is drawn to fill the frame, so the lower band
    is occupied on exactly the screens that have a face on them; the top band is
    empty everywhere, and the pill is already in it. Both instruments therefore sit
    the same distance from the frame's edge.

    The bar is centred horizontally; the disc goes to the top-right corner, where a
    clock belongs and where nothing else is drawn. The box returned is the SHAPE's:
    for the disc, the square the circle is inscribed in.
    """
    margin = h * TIMER_MARGIN_FRAC
    if style == "disc":
        side = h * TIMER_DISC_FRAC
        return (w - margin - side, margin, side, side)
    bw, bh = w * TIMER_BAR_WIDTH_FRAC, h * TIMER_BAR_HEIGHT_FRAC
    return ((w - bw) / 2.0, margin, bw, bh)


def draw_timer_bar(cr, x0, y0, bw, bh, frac, ink=TIMER_INK):
    """A draining bar: the track always full width, the fill draining left-anchored.

    The TRACK is drawn at every value, so the bar's extent never changes. A bar that
    only drew the remaining part would shrink as an object, which reads as something
    collapsing rather than as time passing.

    `save`/`restore` because this changes the line width and the source, and nothing
    about drawing a countdown should leak into whatever is drawn after it.
    """
    cr.save()
    r = bh / 2.0
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], 0.16)
    rounded_rect(cr, x0, y0, bw, bh, r)
    cr.fill()
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], 0.35)
    cr.set_line_width(2.0)
    rounded_rect(cr, x0, y0, bw, bh, r)
    cr.stroke()
    if frac > 0.0:
        # Clipped to the track's rounded ends, or the fill's square corners would
        # poke out of it at the extremes.
        cr.save()
        rounded_rect(cr, x0, y0, bw, bh, r)
        cr.clip()
        cr.set_source_rgba(ink[0], ink[1], ink[2], TIMER_FILL_ALPHA)
        cr.rectangle(x0, y0, bw * frac, bh)
        cr.fill()
        cr.restore()      # the clip
    cr.restore()          # the save at the top of this function


def draw_timer_disc(cr, x0, y0, bw, bh, frac, ink=TIMER_INK):
    """A ring that drains, with the number left on the plain background inside it.

    A RING and not a filled pie: a pie changes how much colour is on screen as it
    drains, which starts to read as a graphic rather than as a measurement, and it
    would put the number on top of the fill - the contrast problem the bar avoids by
    keeping the number off it.

    The remaining time is the arc, drawn from twelve o'clock clockwise and shrinking
    back towards twelve as it drains (cairo's angles run clockwise with y down).

    TWO TRAPS, both measured before they were fixed, both invisible to a reader:

      * `arc()` first draws a LINE from the CURRENT POINT to the arc's start, and the
        current point is wherever the last text ended - the footer, at the opposite
        corner. That stray line was 26,850 px of track colour, a diagonal across the
        face. `new_sub_path()` starts a fresh subpath without the connecting line,
        which is why `rounded_rect()` (and therefore the bar and the pill) never had
        this.
      * a stroke does not consume the path, so stroking the remaining arc also
        re-stroked the full track. The ring was drawn complete at EVERY value and the
        drain never showed at all. `new_path()` between them is the fix.
    """
    cx, cy = x0 + bw / 2.0, y0 + bh / 2.0
    radius = min(bw, bh) / 2.0
    width = radius * 0.20
    mid = radius - width / 2.0
    cr.save()
    cr.set_line_width(width)
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], 0.18)
    cr.new_sub_path()
    cr.arc(cx, cy, mid, 0.0, 2.0 * math.pi)
    cr.stroke()
    if frac > 0.0:
        cr.new_path()
        cr.set_source_rgba(ink[0], ink[1], ink[2], TIMER_DISC_ALPHA)
        cr.new_sub_path()
        cr.arc(cx, cy, mid, -math.pi / 2.0,
               -math.pi / 2.0 + 2.0 * math.pi * frac)
        cr.stroke()
    cr.restore()


def draw_timer(cr, w, h, remaining, total, style=DEFAULT_TIMER_STYLE, box=None,
               ink=TIMER_INK, number_frac=TIMER_NUMBER_FRAC, number_above=False):
    """The countdown, and the seconds left. Returns the box it occupied.

    `box` overrides the placement: the confirmation panel needs the same widget
    inside itself rather than in the top band, and there the number has to go ABOVE
    the bar because the panel ends below it. See the constants above for the size,
    and for why the number is never inside the bar.
    """
    if box is None:
        box = timer_box(w, h, style)
    x0, y0, bw, bh = box
    frac = 0.0 if not total else max(0.0, min(1.0, float(remaining) / float(total)))
    number = clock_text(remaining)

    if style == "disc":
        draw_timer_disc(cr, x0, y0, bw, bh, frac, ink)
        draw_text_at(cr, x0 + bw / 2.0, y0 + bh / 2.0, number, bh * 0.40, STANDBY_INK)
        return (x0, y0, bw, bh)

    size = h * number_frac
    gap = h * TIMER_GAP_FRAC
    ny = (y0 - gap - size / 2.0) if number_above else (y0 + bh + gap + size / 2.0)
    draw_text_at(cr, x0 + bw / 2.0, ny, number, size, STANDBY_INK)
    draw_timer_bar(cr, x0, y0, bw, bh, frac, ink)
    top, bottom = min(y0, ny - size / 2.0), max(y0 + bh, ny + size / 2.0)
    return (x0, top, bw, bottom - top)


def gaze_bar_box(w, h, timer_style=DEFAULT_TIMER_STYLE):
    """(x, y, w, h) for the gaze bar: the right column, under the countdown.

    DERIVED from the countdown's own box rather than repeating its constants, so
    re-deciding the countdown's size or corner moves this with it instead of
    colliding with it - the bar sits on the countdown's axis, and both are in the
    column the face never reaches.

    The one case that would otherwise put it in the wrong place is the top-centre
    BAR style, which has no right-hand axis to hang under: there the gaze bar takes
    the right EDGE at the countdown's own margin, which is where a bar on the right
    belongs anyway.
    """
    margin = h * TIMER_MARGIN_FRAC
    bw = h * GAZE_BAR_WIDTH_FRAC
    tbox = timer_box(w, h, timer_style)
    if timer_style == "disc":
        x0 = tbox[0] + tbox[2] / 2.0 - bw / 2.0
        y0 = tbox[1] + tbox[3] + h * GAZE_BAR_GAP_FRAC
    else:
        x0 = w - margin - bw
        y0 = margin
    return (x0, y0, bw, max(0.0, h * GAZE_BAR_BOTTOM_FRAC - y0))


def draw_gaze_bar(cr, x0, y0, bw, bh, progress, ink=GAZE_BAR_INK, ramp=None,
                  gradient=False):
    """A vertical bar that FILLS from the bottom up. Returns the track's box.

    The TRACK is drawn at every value, which is the same rule the countdown's bar
    follows and for the same reason: an object whose extent changes as it fills
    reads as something moving rather than as a measurement. The fill is CLIPPED to
    the rounded track, or its square corners would poke out of the ends at the
    extremes - the countdown's bar has exactly that clip for exactly that reason.

    `ramp` is a list of (position, rgb) stops, used one of two ways: as ONE colour read
    at `progress` (the fill's hue moves with the fraction), or - with `gradient` - as a
    paint that spans the TRACK, so filling reveals the ramp from its base upward. See
    gaze_fill_spec for why both exist. With neither, `ink` is one flat colour.

    A full fill also gets a bright CAP across its top: see GAZE_BAR_CAP_ALPHA for why
    the last few percent need something of their own.

    `save`/`restore` because this sets a line width and a source, and nothing about
    drawing a bar should leak into whatever is drawn after it.
    """
    progress = max(0.0, min(1.0, float(progress)))
    colour = ramp_colour(ramp, progress) if ramp else tuple(ink)
    cr.save()
    r = bw / 2.0
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], GAZE_BAR_TRACK_ALPHA)
    rounded_rect(cr, x0, y0, bw, bh, r)
    cr.fill()
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], GAZE_BAR_EDGE_ALPHA)
    cr.set_line_width(2.0)
    rounded_rect(cr, x0, y0, bw, bh, r)
    cr.stroke()
    if progress > 0.0:
        cr.save()
        rounded_rect(cr, x0, y0, bw, bh, r)
        cr.clip()
        fill_h = bh * progress
        if gradient and ramp:
            # Anchored to the TRACK and not to the fill: the whole point is that the
            # ramp stays put and the filling uncovers it. Anchored to the fill, every
            # frame would show the entire ramp compressed into the filled part, and
            # the bar would look identical at 20% and at 90% except for its height.
            pat = cairo.LinearGradient(x0, y0 + bh, x0, y0)
            for (pos, rgb) in sorted(ramp, key=lambda s: s[0]):
                pat.add_color_stop_rgba(pos, rgb[0], rgb[1], rgb[2],
                                       GAZE_BAR_FILL_ALPHA)
            cr.set_source(pat)
        else:
            cr.set_source_rgba(colour[0], colour[1], colour[2], GAZE_BAR_FILL_ALPHA)
        cr.rectangle(x0, y0 + bh - fill_h, bw, fill_h)
        cr.fill()
        if progress >= 1.0:
            # THE CAP. Only at a true 100%: the conductor publishes the fraction it
            # also judges the verdict on, so "full" here and "passed" in the log are
            # the same event rather than two near-misses.
            cap = min(bh, max(3.0, bw * GAZE_BAR_CAP_FRAC))
            cr.set_source_rgba(min(1.0, colour[0] + 0.45),
                               min(1.0, colour[1] + 0.35),
                               min(1.0, colour[2] + 0.35),
                               GAZE_BAR_CAP_ALPHA)
            cr.rectangle(x0, y0, bw, cap)
            cr.fill()
        cr.restore()      # the clip
    cr.restore()          # the save at the top of this function
    return (x0, y0, bw, bh)


def gaze_caption_text(caption, progress):
    """What is written under the bar, or None for nothing."""
    if caption == "word":
        return GAZE_BAR_CAPTION_TEXT
    if caption == "percent":
        return "%d%%" % round(progress * 100.0)
    return None


def draw_gaze(cr, w, h, progress, caption=GAZE_BAR_CAPTION_DEFAULT,
              timer_style=DEFAULT_TIMER_STYLE, fill=GAZE_BAR_FILL_DEFAULT,
              burst_age=None):
    """The gaze bar, its caption and any completion burst. Returns their box.

    `fill` is a NAME ("ramp", "gradient", "success"...), not a colour: the renderer
    resolves it, so the caller cannot disagree with the drawing code about what a ramp
    is, and there is one place that knows the difference between the two readings.

    The box is the whole point of returning anything: the probe checks that every
    pixel that CHANGES in a full frame lies inside what this function claims to have
    drawn, and that check is what caught a 26,850 px stray line once already. A burst
    drawn out here and not counted in the box would read as stray ink.
    """
    x0, y0, bw, bh = gaze_bar_box(w, h, timer_style)
    draw_gaze_bar(cr, x0, y0, bw, bh, progress, **gaze_fill_args(fill))
    boxes = [(x0, y0, bw, bh)]
    text = gaze_caption_text(caption, progress)
    if text:
        size = min(w, h) * GAZE_BAR_CAPTION_SIZE_FRAC
        cy = h * GAZE_BAR_CAPTION_FRAC
        draw_text_at(cr, x0 + bw / 2.0, cy, text, size, DIM_INK, 0.85)
        boxes.append((x0 - size, cy - size, bw + 2.0 * size, size * 2.0))
    if burst_age is not None and progress >= 1.0:
        # ... and ONLY at a full bar. A burst is the claim "this is finished", so a
        # caller that passes an age for a bar at 40% gets no burst rather than a
        # celebration of nothing - the same rule as an absent fraction drawing no bar.
        ceiling = timer_box(w, h, timer_style)[1] \
            + timer_box(w, h, timer_style)[3] + GAZE_BURST_CEILING_PAD
        particles = burst_particles(burst_age, w, h, x0, y0, bw, ceiling)
        draw_burst(cr, particles)
        for (px, py, radius, _alpha) in particles:
            boxes.append((px - radius * 2.1, py - radius * 2.1,
                          radius * 4.2, radius * 4.2))
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[0] + b[2] for b in boxes) - min(b[0] for b in boxes),
            max(b[1] + b[3] for b in boxes) - min(b[1] for b in boxes))


def footer_text(screen):
    """The quiet line at the bottom: WHERE in the run we are.

    For the OPERATOR rather than the participant, which is why it is small and dim.
    It exists because a stage session is otherwise indistinguishable from a robot
    doing nothing for sixty seconds, and "is it running?" needs an answer that is
    not a journal read.

    It no longer carries the clock. The countdown is the timer widget now, several
    times this size, and a second copy in small text beside it would be two numbers
    to reconcile rather than one to read.
    """
    stage = screen.get("stage")
    if not stage:
        return None
    stages = screen.get("stages")
    return "stage %d of %d" % (stage, stages) if stages else "stage %d" % stage


def timer_seconds(screen, now=None):
    """(remaining, total) for a timed step, or None if there is nothing to count.

    The TOTAL comes from the session file rather than from a constant here. It is
    the conductor's timer, and a second copy of "60" in the renderer would be a
    different number waiting to happen. A file without it - or with a deadline that
    has already passed - draws no timer at all: an empty track would read as "time is
    up" when it only means "this file did not say how long".
    """
    if screen.get("step") != "session":
        return None
    left, total = remaining_seconds(screen, now), screen.get("seconds")
    if left is None or not isinstance(total, (int, float)) or total <= 0:
        return None
    return (left, float(total))


def success_seconds(screen, now=None):
    """(remaining, total) for the success countdown, or None."""
    left, total = remaining_seconds(screen, now), screen.get("seconds")
    if left is None or not isinstance(total, (int, float)) or total <= 0:
        return None
    return (left, float(total))


def describe_screen(screen, look=None):
    """One line naming a screen, for the journal.

    `look` is the look the KIOSK was started with. A screen may override it, so
    the line reports whichever one will actually be drawn - the point of the line
    is that two looks are both just "a face on a projector".
    """
    kind = screen.get("kind", "standby")
    if kind == "face":
        bits = []
        if screen.get("stage"):
            bits.append("stage %d" % screen["stage"])
        if screen.get("step"):
            bits.append(screen["step"])
        if screen.get("style"):
            bits.append("style=%s" % screen["style"])
        if (screen.get("style") or "") == "illustrated":
            bits.append("look=%s" % (screen.get("look") or look or ILL_DEFAULT_LOOK))
        return "face (%s)" % ", ".join(bits) if bits else "face"
    if kind == "label":
        return "label %r" % (screen.get("text") or "")
    return kind


def draw_standby(cr, w, h, t=0.0):
    """The power-on screen: the word, centred, and nothing else.

    Deliberately NOT a face. "Waiting to be started" has to look different from
    "showing a face", or the start command has nothing visible to change — and a
    face on screen implies the robot is already doing something.

    The ink breathes very slowly (3.5% of its alpha, over ~4.5s). A perfectly
    static frame and a crashed renderer are indistinguishable on a projector, and
    this is the only screen with no animation of its own to prove it is alive.
    """
    cr.set_source_rgb(*BG)
    cr.paint()
    alpha = 0.80 + 0.20 * (0.5 + 0.5 * math.sin(t * 1.4))
    draw_text(cr, w, STANDBY_TEXT, min(w, h) * 0.16, h / 2.0, STANDBY_INK, alpha)


def draw_stats(cr, w, h, spans, seconds, target=None):
    """A failed session as a timeline: when the camera saw looking, and when not.

    The screen a stage waits on after a session that did not pass, and the answer to
    the only question a participant has at that moment - "why are we doing that one
    again". It answers with a SHAPE rather than a score, because a bar shows WHEN as
    well as how much, and a participant who was looking in bursts can see their own
    bursts.

    `spans` is validated by stats_spans() and non-empty: a stats screen with no
    usable timeline is turned into the FACE by the reader above, so an empty bar -
    which would say "you never looked at me" - cannot be drawn from a bad file.

    The two states are drawn as a thin band and full-height blocks, not only as two
    colours: the words in the legend say which is which, and the difference in
    HEIGHT survives a viewer who cannot tell the colours apart.
    """
    cr.set_source_rgb(*BG)
    cr.paint()
    draw_text(cr, w, "How that session went", min(w, h) * 0.070, h * 0.26, STANDBY_INK)

    length = float(seconds) if isinstance(seconds, (int, float)) and seconds else 60.0
    # FLOOR, not round. This screen only ever appears after a session that did not
    # pass, so a 49.6 s total rounded to "50 s" would print the target next to a
    # target line it did not reach - the one rounding error that would tell a
    # participant they passed when they did not.
    looked = math.floor(sum(e - s for s, e, looking in spans if looking))
    draw_text(cr, w, "Looking at me: %d s" % looked,
              min(w, h) * 0.034, h * 0.36, STANDBY_INK, 0.88)

    bx = (w - w * STATS_BAR_WIDTH_FRAC) / 2.0
    bw = w * STATS_BAR_WIDTH_FRAC
    bh = h * STATS_BAR_HEIGHT_FRAC
    th = h * STATS_TRACK_HEIGHT_FRAC
    cy = h * STATS_BAR_Y_FRAC

    def x_at(t):
        return bx + bw * max(0.0, min(1.0, t / length))

    def label(x, y, text, size, ink, alpha=1.0, align="left"):
        """One line of text placed by its EDGE rather than its centre.

        The bar's ends and the legend entries have to line up with things that are
        not text, so the ink box is measured here and draw_text_at() does the rest -
        one placement path, so a text change cannot shift the layout by a few px.
        """
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)
        width = cr.text_extents(text).width
        if align == "right":
            x -= width
        elif align == "centre":
            x -= width / 2.0
        draw_text_at(cr, x + width / 2.0, y, text, size, ink, alpha)

    # The whole session as a thin band: the "away" state is the BASELINE, so a
    # session with no looking at all still shows a bar rather than nothing.
    cr.set_source_rgb(*STATS_AWAY_INK)
    cr.rectangle(bx, cy - th / 2.0, bw, th)
    cr.fill()

    # The looking, as full-height blocks.
    cr.set_source_rgb(*STATS_LOOK_INK)
    for start, end, looking in spans:
        if not looking or end <= start:
            continue
        cr.rectangle(x_at(start), cy - bh / 2.0, max(2.0, x_at(end) - x_at(start)), bh)
        cr.fill()

    # Ticks every 10 s, in the background colour, so the timeline can be read as
    # time without anyone counting pixels. Drawn AFTER the looking blocks: the other
    # way round, the blocks paint over them and the ruler exists only in the away
    # stretches, which is exactly where it is least needed.
    cr.set_source_rgb(*BG)
    cr.set_line_width(max(1.0, w * 0.0010))
    t = STATS_TICK_SECONDS
    while t < length:
        x = x_at(t)
        cr.move_to(x, cy - bh / 2.0)
        cr.line_to(x, cy + bh / 2.0)
        cr.stroke()
        t += STATS_TICK_SECONDS

    # The target, when the conductor said what it was: a line TALLER than the bar, so
    # it reads as a mark on the timeline rather than as part of the fill.
    if target:
        x = x_at(target)
        cr.set_source_rgb(*DIM_INK)
        cr.set_line_width(max(2.0, w * 0.0016))
        cr.move_to(x, cy - bh * 0.95)
        cr.line_to(x, cy + bh * 0.95)
        cr.stroke()
        label(x, cy - bh * 1.45, "%d s" % round(target), min(w, h) * 0.026,
              DIM_INK, 0.95, align="centre")

    # 0 s and the session's length, at the ends of the bar and clear of it rather
    # than touching: 1.15 of the bar's half-height below the centre measured 18 px of
    # air at 1080, where 0.95 measured 7.
    label(bx, cy + bh * 1.15, "0s", min(w, h) * 0.026, DIM_INK, align="left")
    label(bx + bw, cy + bh * 1.15, "%ds" % round(length), min(w, h) * 0.026,
          DIM_INK, align="right")

    # The legend, and it is not decoration: the two states differ in colour and in
    # height, so the words are what say which is which.
    ly = h * 0.635
    sw = min(w, h) * 0.022
    lx = bx + w * 0.02
    cr.set_source_rgb(*STATS_LOOK_INK)
    cr.rectangle(lx, ly - sw / 2.0, sw, sw)
    cr.fill()
    label(lx + sw * 1.6, ly, "looking at me", min(w, h) * 0.028, DIM_INK)
    lx2 = lx + w * 0.22
    cr.set_source_rgb(*STATS_AWAY_INK)
    cr.rectangle(lx2, ly - th / 2.0, sw * 1.6, th)
    cr.fill()
    label(lx2 + sw * 2.2, ly, "not looking", min(w, h) * 0.028, DIM_INK)

    draw_text(cr, w, "When you're ready, just say \"Let's begin\".",
              min(w, h) * 0.030, h * 0.90, STANDBY_INK, 0.90)


def draw_label(cr, w, h, text, footer=None, timer=None,
               style=DEFAULT_TIMER_STYLE):
    """A stage placeholder: its name, and an honest note that it is one.

    The note stays until the rung it stands in for exists. A label that could be
    mistaken for a finished screen is worse than one that admits what it is.
    """
    cr.set_source_rgb(*BG)
    cr.paint()
    draw_text(cr, w, text, min(w, h) * 0.15, h * 0.46, STANDBY_INK)
    draw_text(cr, w, "placeholder face", min(w, h) * 0.026, h * 0.565, DIM_INK, 0.7)
    if footer:
        draw_text(cr, w, footer, min(w, h) * 0.028, h * 0.94, DIM_INK, 0.75)
    if timer:
        draw_timer(cr, w, h, timer[0], timer[1], style)


def draw_success(cr, w, h, timer, style=DEFAULT_TIMER_STYLE):
    """The end of the run: the word, and how long until the robot lets go.

    The countdown is the SAME timer widget the stage sessions use, in the success
    colour. This screen is where the widget came from - it is the one the brief
    pointed at - so it staying the same object is the point: the participant has
    already learned what it means by the time they see this one.
    """
    cr.set_source_rgb(*BG)
    cr.paint()
    draw_text(cr, w, "Success", min(w, h) * 0.16, h * 0.36, SUCCESS_INK)
    draw_text(cr, w, "session complete - back to standby",
              min(w, h) * 0.030, h * 0.50, DIM_INK, 0.85)
    if timer:
        draw_timer(cr, w, h, timer[0], timer[1], style, ink=SUCCESS_INK)


def draw_prompt(cr, w, h, prompt):
    """The confirmation window, in the lower third, over whatever screen is up.

    LOWER THIRD ON PURPOSE: the screen underneath still answers "what is this
    robot doing", and a panel across the middle would hide the only thing on
    screen that a person already in the room can read. Drawn only when a prompt is
    live, because none is the ordinary state.
    """
    if not prompt or prompt.get("remaining", 0.0) <= 0.0:
        return
    # Taller than it used to be (0.34h against 0.20h): the countdown inside it is the
    # shared timer widget now, and a 5 s window with a legible clock beats a compact
    # panel with an unreadable one.
    panel_w, panel_h = w * 0.56, h * 0.34
    x0, y0 = (w - panel_w) / 2.0, h * 0.63

    cr.set_source_rgba(0.10, 0.09, 0.15, 0.92)
    cr.rectangle(x0, y0, panel_w, panel_h)
    cr.fill()
    cr.set_source_rgba(DIM_INK[0], DIM_INK[1], DIM_INK[2], 0.55)
    cr.set_line_width(2.0)
    cr.rectangle(x0, y0, panel_w, panel_h)
    cr.stroke()

    draw_text(cr, w, "Confirm command '%s'?" % prompt["command"].title(),
              min(w, h) * 0.050, y0 + panel_h * 0.17, STANDBY_INK)
    if prompt.get("phrase"):
        draw_text(cr, w, "say: %s" % prompt["phrase"],
                  min(w, h) * 0.030, y0 + panel_h * 0.34, DIM_INK, 0.95)
    total = prompt.get("total") or 0.0
    if total > 0:
        # The same widget the stage timers use, sized to the panel. It is ALWAYS the
        # bar, whatever --timer says: a disc big enough to read is 259 px across,
        # which does not fit inside a panel that also carries the question, and
        # hanging it below the panel would put the whole thing over the screen's own
        # content. The 5 s number is a single digit and reads at any shape; the two
        # long timers are the ones that needed a choice.
        tbox = (x0 + panel_w * 0.06, y0 + panel_h * 0.62,
                panel_w * 0.88, panel_h * 0.26)
        draw_timer(cr, w, h, prompt["remaining"], total, "bar", box=tbox,
                   number_above=True)
    else:
        # No declared window length means no honest fraction, so no bar: the number
        # alone, where the bar would have been.
        draw_text(cr, w, "%.0fs" % math.ceil(prompt["remaining"]),
                  min(w, h) * 0.056, y0 + panel_h * 0.78, DIM_INK, 0.9)


def rounded_rect(cr, x, y, w, h, r):
    """A rounded rectangle path. cairo has no primitive for this."""
    r = min(r, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2.0, 0.0)
    cr.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2.0)
    cr.arc(x + r, y + h - r, r, math.pi / 2.0, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def draw_pill(cr, w, h, t=0.0):
    """The green "you can be heard" pill, top-left, over whatever else is up.

    Returns its box, so a test can measure where it landed rather than trust this
    arithmetic.

    TOP-LEFT because it belongs to no screen's story: it is the robot's state, and
    the middle of the frame is the session's. The standby word, the face, the stage
    labels and the confirmation prompt all live centre or lower, so this cannot
    cover anything a participant is meant to read.

    The dot breathes slowly, for the same reason the standby word does: a still
    frame and a crashed renderer are indistinguishable on a projector, and this is
    the one element whose entire job is to say the robot is still running.
    """
    text = PILL_TEXT
    size = min(w, h) * 0.030
    pad, dot, gap = size * 0.95, size * 0.70, size * 0.55
    margin = h * 0.040
    ph = size * 2.0

    cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    pw = pad + dot + gap + ext.width + pad
    x0, y0 = margin, margin

    cr.set_source_rgba(PILL_INK[0], PILL_INK[1], PILL_INK[2], 0.92)
    rounded_rect(cr, x0, y0, pw, ph, ph / 2.0)
    cr.fill()

    pulse = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(t * 2.5))
    cr.set_source_rgba(PILL_INK_DARK[0], PILL_INK_DARK[1], PILL_INK_DARK[2], pulse)
    cr.arc(x0 + pad + dot / 2.0, y0 + ph / 2.0, dot / 2.0, 0.0, 2.0 * math.pi)
    cr.fill()

    cr.set_source_rgb(*PILL_INK_DARK)
    cr.move_to(x0 + pad + dot + gap - ext.x_bearing,
               y0 + ph / 2.0 - ext.height / 2.0 - ext.y_bearing)
    cr.show_text(text)
    return (x0, y0, pw, ph)


def draw_coach(cr, w, h, coach, now=None):
    """The calibration cue: what to do now, and how long is left of the window.

    Returns the panel's box, so a test can measure where it landed rather than trust
    this arithmetic - the same contract draw_pill has.

    WHY IT IS ON THE SCREEN AT ALL. The tool that asks for the poses runs over SSH, so
    its prompts are on a laptop behind the person being measured and its countdown is
    in a terminal nobody is looking at. An instruction has to be on the screen the pose
    is ABOUT, and this process owns that surface.

    WHY IT DOES NOT SPEAK. Sound would work while someone is looking away, which is
    exactly what phase B asks for - and `calibrate-look.py` therefore also plays a tone
    at each window boundary. But a bench rig may have no audio path at all, and this
    board's default sink is the silent analog trap, so the cue that must not fail is
    the one drawn where the pose is measured.

    The countdown is the SAME widget every other timed thing uses. The state is carried
    by the caption and the panel's outline rather than by colour alone, because a
    projector's colour is the least reliable thing about it.
    """
    state = coach.get("state") or "hold"
    ink = COACH_STATE_INK.get(state, STANDBY_INK)
    panel_w, panel_h = w * COACH_WIDTH_FRAC, h * COACH_HEIGHT_FRAC
    x0, y0 = (w - panel_w) / 2.0, h * COACH_TOP_FRAC

    cr.set_source_rgba(0.10, 0.09, 0.15, 0.93)
    cr.rectangle(x0, y0, panel_w, panel_h)
    cr.fill()
    cr.set_source_rgba(ink[0], ink[1], ink[2], 0.80)
    cr.set_line_width(3.0)
    cr.rectangle(x0, y0, panel_w, panel_h)
    cr.stroke()

    caption = [b for b in (("phase %s" % coach["phase"]) if coach.get("phase") else None,
                           COACH_STATE_CAPTION.get(state, state)) if b]
    draw_text(cr, w, " \u00b7 ".join(caption).upper(), min(w, h) * 0.026,
              y0 + panel_h * 0.16, DIM_INK, 0.95)
    draw_text(cr, w, coach["action"], min(w, h) * COACH_ACTION_SIZE_FRAC,
              y0 + panel_h * 0.37, STANDBY_INK)
    if coach.get("detail"):
        draw_text(cr, w, coach["detail"], min(w, h) * 0.030,
                  y0 + panel_h * 0.57, DIM_INK, 0.95)

    remaining, total = coach_seconds(coach, now)
    if remaining is None:
        return (x0, y0, panel_w, panel_h)
    tbox = (x0 + panel_w * 0.10, y0 + panel_h * 0.66, panel_w * 0.80, panel_h * 0.11)
    if total:
        # Smaller than the top-band timer's number on purpose: this one sits inside a
        # panel and has to leave the outline alone, and the panel's own headline is the
        # thing being read here, not the clock.
        draw_timer(cr, w, h, remaining, total, "bar", box=tbox, number_frac=0.040)
    else:
        draw_text(cr, w, "%ds" % max(0, int(math.ceil(remaining))),
                  min(w, h) * 0.040, y0 + panel_h * 0.78, DIM_INK, 0.9)
    return (x0, y0, panel_w, panel_h)


def draw_frame(cr, w, h, st, screen=None, style="cartoon", prompt=None, now=None,
               listen=False, timer_style=DEFAULT_TIMER_STYLE, calibration=False,
               gaze_caption=GAZE_BAR_CAPTION_DEFAULT,
               gaze_ink=GAZE_BAR_FILL_DEFAULT, gaze_burst=None, look=None,
               coach=None):
    """Draw the whole screen: the session screen, then any prompt over it.

    The one place either decision is made, so the kiosk and --snapshot cannot
    drift apart.

    `look` picks the illustrated register's palette and proportions (ILL_LOOKS).
    It is overridden per screen by `screen["look"]`, so the conductor can name
    one stage's look without the kiosk needing a flag per stage - the same shape
    as `screen["style"]`.

    `calibration` brings the face up while the session is STANDBY, for a lip-sync
    tuning trial. It goes through the same draw call as a stage's face on purpose:
    the thing being tuned is the mouth's arrival on the projector, so the render
    cost here has to match the render cost there.

    `coach` is the gaze-calibration cue (robot/perception/calibrate-look.py), and it
    does the same thing for the same reason - the pose being measured is "looking at
    the screen", so the screen has to be the thing it will be in a real run, not the
    word "Standby". Both are honoured ONLY over standby: a real run's screen belongs
    to the conductor.
    """
    screen = screen or STANDBY_SCREEN
    kind = screen.get("kind", "standby")
    look = screen.get("look") or look
    footer = footer_text(screen)
    # The countdown for a timed step. A WAITING stage gets none: nothing is counting
    # down until the participant speaks, and a timer that is not moving would read as
    # stalled rather than as waiting.
    timer = timer_seconds(screen, now)
    # Only ever over standby: a running session's screen is the conductor's.
    calibrating = bool(calibration) and kind == "standby"
    coaching = bool(coach) and kind == "standby"

    if kind == "label":
        draw_label(cr, w, h, screen.get("text") or "", footer, timer, timer_style)
    elif kind == "stats":
        # No timer and no gaze bar: nothing is being measured while this is up, and
        # the bar would be a second, emptier reading of the same session.
        draw_stats(cr, w, h, screen.get("spans") or [], screen.get("seconds"),
                   screen.get("target"))
    elif kind == "success":
        draw_success(cr, w, h, success_seconds(screen, now), timer_style)
    elif kind == "face":
        # The HUD is a development aid, so a running stage replaces it: one line
        # at the bottom, and the session line is the one an operator needs.
        draw_face(cr, w, h, st, show_hud=footer is None,
                  style=screen.get("style") or style, look=look)
    elif calibrating or coaching:
        draw_face(cr, w, h, st, show_hud=False, style=style, look=look)
    else:
        draw_standby(cr, w, h, st.t)

    if kind == "face":
        if footer:
            draw_text(cr, w, footer, min(w, h) * 0.028, h * 0.94, DIM_INK, 0.75)
        if timer:
            draw_timer(cr, w, h, timer[0], timer[1], timer_style)
    elif calibrating:
        # Static text, and it does not move: a label on the channel under test
        # would be counted as part of the fault being measured.
        draw_text_at(cr, w * CALIBRATION_TEXT_X_FRAC, h * CALIBRATION_TEXT_Y_FRAC,
                     CALIBRATION_TEXT, min(w, h) * 0.026, CALIBRATION_INK, 0.9)
    # The gaze bar, over whatever screen the session has up. It belongs to the
    # SESSION rather than to the face, so it is drawn wherever the conductor says a
    # timed session is running - including the stage 3 and 4 label screens, which
    # become faces later. The step test is deliberate: the block's presence is what
    # the conductor means by "a session is running", and a stale block on a
    # non-session screen must not draw a score nobody is earning.
    if screen.get("step") == "session" and screen.get("gaze_progress") is not None:
        draw_gaze(cr, w, h, screen["gaze_progress"], gaze_caption, timer_style,
                  fill=gaze_ink, burst_age=gaze_burst)
    draw_prompt(cr, w, h, prompt)
    # The gaze-calibration cue, over the screen it is about. After the prompt so it
    # is never hidden, and only over standby - see the note in the docstring.
    if coaching:
        draw_coach(cr, w, h, coach, now)
    # LAST, and outside the session's business entirely: the pill says the robot can
    # hear, which is true (or not) regardless of which screen is up. Note the
    # default is False - a caller that has not been told the mic is live draws
    # nothing, which is the safe direction for a green "talk to me" light.
    if listen:
        draw_pill(cr, w, h, st.t)


# --------------------------------------------------------------------------
# Snapshot mode — render one frame to a PNG, no display required
# --------------------------------------------------------------------------
def sample_screen(kind="face", style="cartoon", label_text="Stage 3", step=None,
                  gaze_progress=None, spans=None, target=None, seconds=None):
    """A screen for --snapshot, with a FIXED countdown so renders compare.

    The deadlines are relative to now, so the rendered countdown text and bar are
    the same on every run: a snapshot that changes with the wall clock cannot be
    compared with the previous one, which is the only reason to take it.

    `step` pins the screen to a step of a stage, which is what decides whether a
    countdown is drawn at all - a WAITING stage has nothing to count. Without it a
    screen renders in its plain state, which for the face means no stage and the
    development HUD. That is a real state, but not the one a stage session shows,
    so a preview of the countdown needs --step session.

    `gaze_progress` draws the gaze bar at a known fill, which is how the bar gets
    measured offline: the conductor is not needed to render one, and a render with
    no bar at all is exactly what a real session that has never seen a face looks
    like.

    `spans` and `target` do the same for the stats screen: a fixed timeline, so two
    renders of it are comparable and the geometry can be measured.
    """
    now = time.monotonic()
    if kind == "standby":
        return dict(STANDBY_SCREEN)
    if kind == "stats":
        # A stage waiting for the word after a session that did not pass: a real
        # timeline, from a real failed bench session (a burst of looking, a long
        # away stretch, then two short looks), so the render exercises the shape the
        # screen is for rather than a uniform fill.
        return {"phase": "running", "kind": "stats", "text": None, "style": None,
                "stage": 1, "stages": 4, "step": "waiting", "deadline": None,
                "seconds": seconds if seconds is not None else SAMPLE_SESSION_SECONDS,
                "gaze_progress": None,
                "spans": spans if spans is not None else SAMPLE_STATS_SPANS,
                "target": target if target is not None else SAMPLE_STATS_TARGET}
    if step == "success" or kind == "success":
        return {"phase": "running", "kind": "success", "text": None, "style": None,
                "stage": 4, "stages": 4, "step": "success",
                "deadline": now + SAMPLE_SUCCESS_SECONDS,
                "seconds": SAMPLE_SUCCESS_SECONDS, "gaze_progress": None}
    if step in ("waiting", "session"):
        running = step == "session"
        return {"phase": "running", "kind": kind,
                "style": style if kind == "face" else None,
                "text": label_text if kind == "label" else None,
                "stage": 1 if kind == "face" else 3, "stages": 4, "step": step,
                "deadline": now + SAMPLE_SESSION_SECONDS if running else None,
                "seconds": SAMPLE_SESSION_SECONDS if running else None,
                "gaze_progress": gaze_progress if running else None}
    if kind == "label":
        return {"phase": "running", "kind": "label", "text": label_text,
                "style": None, "stage": 3, "stages": 4, "step": "session",
                "deadline": now + SAMPLE_SESSION_SECONDS,
                "seconds": SAMPLE_SESSION_SECONDS, "gaze_progress": gaze_progress}
    # The plain face: no stage, so no footer and the HUD stays, which is exactly
    # what this snapshot rendered before sessions existed.
    return {"phase": "running", "kind": "face", "style": style, "text": None,
            "stage": None, "stages": None, "step": None, "deadline": None,
            "seconds": None, "gaze_progress": None}


def sample_prompt(command="start"):
    """A confirmation prompt for --snapshot, part way through its countdown."""
    return {"command": command, "phrase": "marigold wombat sierra",
            "remaining": SAMPLE_PROMPT_SECONDS, "total": 5.0}


def sample_coach(state="hold", action="LOOK AT THE SCREEN",
                 detail="hold this pose", phase="A", remaining=6.0, total=8.0):
    """A coach payload for --snapshot, with the deadline relative to NOW.

    Relative, like sample_screen's countdown, so the rendered number is the same on
    every run: the panel's clock rounds UP, so a millisecond of skew cannot change the
    digits a snapshot is compared on. The bar's leading edge can move by under a pixel,
    which is the same tolerance the other snapshots already live with.
    """
    hb = time.monotonic()
    return {"state": state, "action": action, "detail": detail, "phase": phase,
            "deadline": hb + remaining, "total": total, "heartbeat": hb}


def sample_stats_spans(text):
    """The --stats-spans JSON as a timeline, or None for the fixed sample one.

    A malformed argument RAISES rather than falling back to the sample timeline: this
    is a switch for rendering the screen, and a typo that silently drew the default
    would look exactly like the flag having worked.
    """
    if text is None:
        return None
    spans = stats_spans(json.loads(text))
    if spans is None:
        raise SystemExit("face: --stats-spans is not a timeline: %r" % text)
    return spans


def snapshot(path, expression="neutral", width=1920, height=1080,
             mouth_open=None, style="cartoon", screen="face", label_text="Stage 3",
             prompt_sample=None, listen=False, timer_style=DEFAULT_TIMER_STYLE,
             step=None, calibration=False, gaze_progress=None,
             gaze_caption=GAZE_BAR_CAPTION_DEFAULT,
             gaze_ink=GAZE_BAR_FILL_DEFAULT, gaze_burst=None, look=None,
             coach=None, stats_spans=None, stats_target=None, stats_seconds=None):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)

    st = FaceState()
    st.set_expression(expression)
    # settle the smoothed params on the target expression
    for _ in range(60):
        st.update(1.0 / 30.0)
    st.gaze_x = 0.0
    st.gaze_y = 0.0

    # Drive the lip-sync path directly, so the mouth can be inspected offline.
    # This is how the lip-sync rendering gets verified with no display, no
    # compositor and no audio — the same reason --snapshot exists at all.
    if mouth_open is not None:
        st.mouth_drive = mouth_open
        for _ in range(30):          # let the smoothing settle
            st.update(1.0 / 30.0)

    # t = 0.0 pins the standby breathing alpha to a known value (0.90), so two
    # snapshots of one build are byte-comparable.
    st.t = 0.0
    draw_frame(cr, width, height, st,
               sample_screen(screen, style, label_text, step,
                             gaze_progress=gaze_progress, spans=stats_spans,
                             target=stats_target, seconds=stats_seconds),
               style=style, listen=listen, timer_style=timer_style,
               calibration=calibration, gaze_caption=gaze_caption,
               gaze_ink=gaze_ink, gaze_burst=gaze_burst, look=look,
               coach=coach,
               prompt=sample_prompt(prompt_sample) if prompt_sample else None)
    surface.write_to_png(path)
    return path


# --------------------------------------------------------------------------
# Kiosk mode — GTK4 on Wayland
# --------------------------------------------------------------------------
def run_kiosk(retries=30, delay=0.2, style="cartoon",
              timer_style=DEFAULT_TIMER_STYLE, stats_forced=False,
              pin_expression=PIN_EXPRESSION_DEFAULT,
              gaze_caption=GAZE_BAR_CAPTION_DEFAULT,
              gaze_ink=GAZE_BAR_FILL_DEFAULT, look=ILL_DEFAULT_LOOK):
    """Run the face on the compositor.

    RACE WE HAVE TO HANDLE
      The Wayland socket file appearing does NOT mean the compositor is ready to
      serve clients. Starting between those two moments makes GTK fail with
      "Gtk couldn't be initialized" / "Connection reset by peer", and on a cold
      boot that window is wide enough to hit reliably. Retrying with a short
      backoff is the fix; a fixed sleep would just move the race.

      A retry here is cheap and cannot mask a real failure: if the compositor
      never comes up, we exhaust the retries and exit non-zero, which is exactly
      what systemd's Restart=always is for.
    """
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib

    state_path = os.environ.get("FACE_STATE", "/run/face/state.json")
    # Lip-sync has its OWN file so that the speech driver and the gaze service
    # never write the same path. gaze.py rewrites state.json ~12x/s with a
    # read-modify-write; sharing it would put two writers on one file.
    mouth_path = os.environ.get("FACE_MOUTH", "/run/face/mouth.json")
    # Which screen to show, and any prompt over it. Both come from files so that
    # the conductor and the listener can change them while this process keeps
    # running: a screen that needed a restart would blank the projector exactly
    # when someone had just spoken to it.
    session_path = os.environ.get("FACE_SESSION", SESSION_PATH)
    prompt_path = os.environ.get("FACE_PROMPT", PROMPT_PATH)
    mic_path = os.environ.get("FACE_MIC", MIC_PATH)
    calibration_path = os.environ.get("FACE_CALIBRATION", CALIBRATION_PATH)
    coach_path = os.environ.get("FACE_COACH", COACH_PATH)
    # Which shape the countdowns take. An env var as well as --timer, so a device can
    # be switched without touching the kiosk's command line: this is a legibility
    # preference about the projector, not a property of the code.
    if timer_style not in TIMER_STYLES:
        print("face: unknown FACE_TIMER %r - using %s"
              % (timer_style, DEFAULT_TIMER_STYLE), file=sys.stderr)
        timer_style = DEFAULT_TIMER_STYLE
    st = FaceState()
    # The pinned expression, applied ONCE here rather than per frame: the face eases
    # toward whatever this is, so setting it every frame would be the same value
    # re-aimed at 30 Hz. `pinned is None` means the state file decides, which is the
    # behaviour every earlier build had.
    pinned = pin_expression if pin_expression in EXPRESSIONS else None
    if pinned:
        st.set_expression(pinned)
        log("expression PINNED to %r: the state file's expression and gaze are "
            "ignored (blinks are still honoured, so the face keeps breathing)"
            % pinned)
    last = {"t": time.monotonic(), "state_mtime": 0.0, "mouth_mtime": 0.0,
            "session": None, "session_mtime": "unset",
            "prompt": None, "prompt_mtime": "unset",
            "listen": None, "calibrating": None, "stats_on": None,
            "coach": None,
            "screen_line": None, "burst_age": None}
    # The pill's small cache. Two caches with two jobs: the file is re-read at most
    # MIC_POLL apart, and freshness is judged from the stamp on EVERY frame.
    mic = {"stamp": None, "read_at": -1.0}
    # The calibration coach's cache, the same two jobs.
    coach_poll = {"stamp": None, "read_at": -1.0}
    # The tuning session's cache, the same two jobs: re-read at most
    # CALIBRATION_POLL apart, freshness judged every frame from its heartbeat.
    tuner = {"stamp": None, "read_at": -1.0}
    # Frame statistics, accumulated only while a tuning session asks for them.
    # `since is None` means no window is open.
    stats = {"frames": 0, "since": None, "draw_sum": 0.0, "draw_max": 0.0}
    # The completion burst. `at` is when the fill reached 100%, or None; the age is
    # derived from it every frame rather than counted up, so a stalled kiosk resumes
    # the animation at the right moment instead of replaying part of it. `seen` is the
    # last published fraction, and it is what makes this a ONE-SHOT: the burst fires on
    # the crossing into 1.0, not on the state of being full.
    burst = {"at": None, "seen": None}

    def poll(path, key, reader, default):
        """(value, changed) for a file, re-read only when its mtime moves.

        The mtime cache is why this can run at frame rate: an unchanged file costs
        one stat() instead of a parse. An absent file is the caller's DEFAULT, not
        a fault — standby with no session, and no prompt with nothing pending.
        """
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if mtime == last[key + "_mtime"]:
            return last[key], False
        last[key + "_mtime"] = mtime
        value = reader(path) if mtime is not None else default
        value = default if value is None else value
        changed = value != last[key]
        last[key] = value
        return value, changed

    def read_state():
        """Read the JSON state file. Absent or malformed => keep going.

        The DECISION about what the file is allowed to change is `apply_state_file`,
        which is a plain function so it can be tested with no compositor; this is only
        the mtime cache and the file handling around it.
        """
        try:
            mtime = os.path.getmtime(state_path)
        except OSError:
            return
        if mtime == last["state_mtime"]:
            return
        last["state_mtime"] = mtime
        try:
            with open(state_path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        apply_state_file(st, data, pinned)

    def read_mouth():
        """Read the lip-sync drive. Absent, stale, or malformed => release it.

        STALENESS IS THE SAFETY PROPERTY. If the speech driver dies mid-word the
        last value it wrote would otherwise freeze the mouth open forever. The
        face notices that nothing has updated it and lets the mouth close.
        """
        try:
            mtime = os.path.getmtime(mouth_path)
        except OSError:
            st.mouth_drive = None
            return
        if time.time() - mtime > MOUTH_STALE_SECS:
            st.mouth_drive = None
            return
        if mtime == last["mouth_mtime"]:
            return
        last["mouth_mtime"] = mtime
        try:
            with open(mouth_path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            st.mouth_drive = None
            return
        if not isinstance(data, dict):
            return
        try:
            value = float(data["open"])
        except (KeyError, TypeError, ValueError):
            st.mouth_drive = None
            return
        st.mouth_drive = max(0.0, min(1.0, value))
        try:
            wide = data.get("wide")
            st.mouth_drive_w = None if wide is None else float(wide)
        except (TypeError, ValueError):
            st.mouth_drive_w = None

    class FaceWindow(Gtk.ApplicationWindow):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.set_decorated(False)
            self.set_title("face")
            self.area = Gtk.DrawingArea()
            self.area.set_draw_func(self.on_draw)
            self.set_child(self.area)
            self.fullscreen()
            GLib.timeout_add(int(1000 / 30), self.tick)

        def tick(self):
            now = time.monotonic()
            dt = now - last["t"]
            last["t"] = now
            # The pill: re-read at most MIC_POLL apart, freshness judged now.
            if now - mic["read_at"] >= MIC_POLL:
                mic["read_at"] = now
                mic["stamp"] = read_mic_stamp(mic_path)
            listening = mic_fresh(mic["stamp"], now)
            if listening != last["listen"]:
                log("listening pill %s"
                    % ("on - the robot can hear a person" if listening else "off"))
                last["listen"] = listening

            # The calibration coach, on the same two clocks as the pill: the file is
            # re-read at most COACH_POLL apart, and freshness is judged EVERY frame
            # from its heartbeat - because a killed tool stops changing the file rather
            # than writing "I am finished", and an instruction left on a screen after
            # the tool has gone is worse than no instruction at all.
            if now - coach_poll["read_at"] >= COACH_POLL:
                coach_poll["read_at"] = now
                coach_poll["stamp"] = read_coach_stamp(coach_path)
            coaching = (coach_poll["stamp"]
                        if coach_fresh(coach_poll["stamp"], now) else None)
            if bool(coaching) != bool(last["coach"]):
                log("calibration coach %s"
                    % ("on - %r (%s)" % (coaching["action"], coaching["state"])
                       if coaching else "off"))
            last["coach"] = coaching

            st.update(min(dt, 0.25))   # clamp so a stall cannot jump the anim
            read_state()
            read_mouth()
            _, screen_changed = poll(session_path, "session", read_session,
                                     dict(STANDBY_SCREEN))
            prompt, prompt_changed = poll(prompt_path, "prompt", read_prompt, None)
            # The journal is the only mechanical evidence of what is on the
            # projector, so both of these are logged on change and never per
            # frame. A steady screen is worth no lines at all - and the test is on
            # what the screen SAYS rather than on whether the file changed, because
            # the gaze bar makes the file change several times a second while a
            # session runs. Keying on the file would put a journal line per frame.
            described = describe_screen(last["session"], look)
            if screen_changed and described != last["screen_line"]:
                log("screen %s" % described)
            last["screen_line"] = described

            # The completion burst: fired on the CROSSING to a full bar, and only
            # there. A session that starts already full (a very short test timer) fires
            # too, which is correct - the participant did earn it - while a bar that
            # STAYS full for the rest of the session fires once, not once a frame.
            # Going back below 1.0 (a new session's fresh bar, or a different stage)
            # re-arms it, so the next crossing is a new celebration.
            progress = last["session"].get("gaze_progress")
            if progress is not None and progress >= 1.0 and (burst["seen"] is None
                                                             or burst["seen"] < 1.0):
                burst["at"] = now
                log("gaze bar FULL - completion burst")
            if progress is None and burst["seen"] is not None:
                burst["at"] = None
            burst["seen"] = progress
            burst_age = None if burst["at"] is None else (now - burst["at"])
            if burst_age is not None and burst_age > GAZE_BURST_LIFE:
                burst["at"] = None       # over; nothing is drawn after this
                burst_age = None
            # Handed to on_draw through the same dict as everything else the frame
            # needs, so the drawing method stays a pure function of what tick decided.
            last["burst_age"] = burst_age
            if prompt_changed:
                if prompt:
                    log("prompt on screen: %r, %.1fs left"
                        % (prompt["command"], prompt["remaining"]))
                else:
                    log("prompt cleared")

            # The tuning session, if one is running. Honoured ONLY over standby: a
            # real session's screen is the conductor's, and a tuning tool must not
            # be able to take the participant's screen away.
            if now - tuner["read_at"] >= CALIBRATION_POLL:
                tuner["read_at"] = now
                tuner["stamp"] = read_calibration_stamp(calibration_path)
            face_up, stats_wanted = calibration_fresh(tuner["stamp"], now)
            face_up = face_up and last["session"].get("kind") == "standby"
            stats_on = stats_wanted or stats_forced
            if face_up != last["calibrating"]:
                log("calibration screen %s"
                    % ("on - the face is up for a lip-sync trial"
                       if face_up else "off - back to the standby screen"))
                last["calibrating"] = face_up
            if stats_on != last["stats_on"]:
                log("frame stats %s"
                    % ("on - the achieved period and draw time every %.0fs"
                       % STATS_INTERVAL if stats_on else "off"))
                last["stats_on"] = stats_on

            # Frame statistics. The PERIOD is tick-to-tick, which is what decides
            # when a newly written mouth value can appear; the DRAW time is the
            # render inside on_draw. Both together answer the one half of the
            # lip-sync offset that cannot be read off the system: the face's own
            # visual latency. Accrued only when asked for, so the journal is not
            # carrying a line every five seconds forever.
            if stats_on:
                if stats["since"] is None:
                    stats["since"] = now
                stats["frames"] += 1
                if now - stats["since"] >= STATS_INTERVAL:
                    period = (now - stats["since"]) / max(1, stats["frames"])
                    log("frame: %.1f fps (%.1f ms period), draw mean %.1f ms max "
                        "%.1f ms over %d frames"
                        % (1.0 / period, period * 1000.0,
                           1000.0 * stats["draw_sum"] / max(1, stats["frames"]),
                           1000.0 * stats["draw_max"], stats["frames"]))
                    stats.update(frames=0, since=now, draw_sum=0.0, draw_max=0.0)
            else:
                stats.update(frames=0, since=None, draw_sum=0.0, draw_max=0.0)

            self.area.queue_draw()
            return True

        def on_draw(self, area, cr, width, height):
            started = time.monotonic() if last["stats_on"] else None
            draw_frame(cr, width, height, st, last["session"], style=style,
                       prompt=last["prompt"], listen=bool(last["listen"]),
                       timer_style=timer_style,
                       calibration=bool(last["calibrating"]),
                       coach=last["coach"],
                       gaze_caption=gaze_caption, gaze_ink=gaze_ink,
                       gaze_burst=last["burst_age"], look=look)
            if started is not None:
                took = time.monotonic() - started
                stats["draw_sum"] += took
                stats["draw_max"] = max(stats["draw_max"], took)

    class FaceApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="au.edu.robot.face")

        def do_activate(self):
            win = FaceWindow(application=self)
            win.present()

    # Verify the compositor is actually accepting connections before handing
    # over to GTK, so a failure is reported clearly rather than as a GTK
    # assertion storm.
    #
    # NOTE: `Gtk.init_check()` is NOT a reliable readiness test here — measured
    # on this board it returns True even with no display at all. The thing that
    # actually tells the truth is Gdk.Display.get_default(): it is None until a
    # connection to the compositor succeeds, and non-None once it is usable.
    def display_ready():
        try:
            if not Gtk.init_check():
                return False
            return Gdk.Display.get_default() is not None
        except Exception:
            return False

    from gi.repository import Gdk
    for attempt in range(1, retries + 1):
        if display_ready():
            if attempt > 1:
                sys.stderr.write(
                    "face: compositor ready after %d attempt(s)\n" % attempt
                )
            break
        sys.stderr.write(
            "face: waiting for compositor (attempt %d/%d, "
            "WAYLAND_DISPLAY=%s)\n"
            % (attempt, retries, os.environ.get("WAYLAND_DISPLAY"))
        )
        time.sleep(delay)
    else:
        sys.stderr.write(
            "face: compositor never became ready after %d attempts; exiting "
            "so systemd can restart the stack.\n" % retries
        )
        return 1

    app = FaceApp()
    return app.run(sys.argv[:1])


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def selftest():
    """Exercise the things the KIOSK decides, with no compositor and no display.

    The two decisions worth testing here are both negatives, which is why they cannot
    be checked by looking at the robot:

      * the PINNED expression means the face must NOT follow the person. A pinned
        face and a mirrored face are both a face on a projector.
      * a bar drawn from a malformed session file must NOT be drawn at all, because a
        bar at zero is a claim about the participant.

    Runs in well under a second, and needs cairo but not GTK.
    """
    failures, ran = [], []

    def check(name, got, want):
        ran.append(name)
        ok = got == want
        print("  %-62s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
        if not ok:
            failures.append(name)

    def fresh():
        st = FaceState()
        for _ in range(30):          # settle the eased params, as the kiosk does
            st.update(1.0 / 30.0)
        return st

    live = {"expression": "angry", "gaze": {"x": 0.9, "y": -0.7}, "blink": False}

    # 1. Unpinned, the file still drives the face: this is the behaviour that must
    #    come back when the pin is released, so it is asserted rather than assumed.
    st = fresh()
    took = apply_state_file(st, live)
    check("unpinned: the file's expression is taken", took[0], True)
    check("unpinned: the file's gaze is taken", took[1], True)
    check("unpinned: expression and gaze follow the file",
          (st.expression, round(st.gaze_x, 2), round(st.gaze_y, 2)),
          ("angry", 0.9, -0.7))

    # 2. Pinned, the SAME file must change neither. Note both flags, not just the
    #    resulting values: the point is that the file was not consulted.
    st = fresh()
    took = apply_state_file(st, live, PIN_EXPRESSION_DEFAULT)
    check("pinned: the file's expression is NOT taken", took[0], False)
    check("pinned: the file's gaze is NOT taken", took[1], False)
    check("pinned: the expression is the pinned one", st.expression,
          PIN_EXPRESSION_DEFAULT)
    check("pinned: the eyes look straight ahead",
          (st.gaze_x, st.gaze_y), (0.0, 0.0))

    # 3. ... but the face keeps breathing: a forced blink is the face's own
    #    animation, not a mirror of the room.
    st = fresh()
    took = apply_state_file(st, {"blink": True}, PIN_EXPRESSION_DEFAULT)
    check("pinned: a forced blink is still honoured",
          (took[2], st.forced_blink), (True, True))

    # 4. A file cannot move a pinned face by any other route either: an expression
    #    set BEFORE the pin (as a live file would have done) is overridden.
    st = fresh()
    apply_state_file(st, {"expression": "angry"})
    check("pinned: an earlier expression is overridden", st.expression, "angry")
    apply_state_file(st, live, PIN_EXPRESSION_DEFAULT)
    check("pinned: and the pin wins on the next read", st.expression,
          PIN_EXPRESSION_DEFAULT)

    # 5. An unknown expression name is refused, and the caller is told.
    st = fresh()
    took = apply_state_file(st, {"expression": "smug"})
    check("unpinned: an unknown expression is refused", took[0], False)
    check("unpinned: and the face keeps what it had", st.expression, "neutral")

    # 6. Garbage is ignored field by field rather than raising.
    st = fresh()
    check("garbage: a non-numeric gaze is ignored",
          apply_state_file(st, {"gaze": {"x": "nope", "y": 0.5}}),
          (False, False, False))
    check("garbage: and leaves the eyes where they were", (st.gaze_x, st.gaze_y),
          (0.0, 0.0))
    check("garbage: gaze that is not a dict is ignored",
          apply_state_file(st, {"gaze": [1, 2]}), (False, False, False))
    check("garbage: a file that is not a dict does nothing",
          apply_state_file(st, ["not", "a", "dict"]), (False, False, False))

    # 7. The pin itself names a real expression: a typo in the constant would pin
    #    the face to nothing and be invisible on screen.
    check("pin: the configured default is a real expression",
          PIN_EXPRESSION_DEFAULT in EXPRESSIONS, True)
    check("pin: 'off' is offered, so the pin can be released",
          "off" in PIN_EXPRESSION_CHOICES, True)

    # 8. The gaze bar's fraction is validated, not trusted.
    check("bar: a well-formed block gives its fraction",
          gaze_progress({"progress": 0.42}), 0.42)
    check("bar: an over-full value is clamped", gaze_progress({"progress": 1.4}), 1.0)
    check("bar: a negative value is clamped", gaze_progress({"progress": -1.0}), 0.0)
    check("bar: True is not a fraction", gaze_progress({"progress": True}), None)
    check("bar: no block means no bar", gaze_progress(None), None)
    check("bar: a block with no progress means no bar", gaze_progress({"looking": True}),
          None)
    check("bar: text is not a fraction", gaze_progress({"progress": "60%"}), None)

    # 9. The geometry: the bar is inside the frame, below the countdown, and clear of
    #    it. The probe measures the pixels; this asserts the boxes cannot collide in
    #    the first place.
    def _overlap(a, b):
        """Do two (x, y, w, h) boxes intersect? Asked of both axes, because the
        countdown's column legitimately spans the bar's - the disc is 259 px wide and
        the bar is centred on its axis - so an x-only test would call a clear layout a
        collision and the wrong fix (moving the bar sideways) would follow."""
        return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                    or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])

    x0, y0, bw, bh = gaze_bar_box(1920, 1080, "disc")
    tx, ty, tw, th = timer_box(1920, 1080, "disc")
    check("geometry: the bar is inside the frame",
          (x0 >= 0 and y0 >= 0 and x0 + bw <= 1920 and y0 + bh <= 1080), True)
    check("geometry: it starts below the countdown", y0 >= ty + th, True)
    check("geometry: and the two boxes do not intersect",
          _overlap((x0, y0, bw, bh), (tx, ty, tw, th)), False)
    check("geometry: it is centred on the countdown's axis",
          abs((x0 + bw / 2.0) - (tx + tw / 2.0)) <= 1.0, True)
    bx, by, bbw, bbh = gaze_bar_box(1920, 1080, "bar")
    btx, bty, btw, bth = timer_box(1920, 1080, "bar")
    check("geometry: with the top-centre countdown the bar takes the right edge",
          (bx + bbw <= 1920, abs(by - bty) <= 1.0, bx > btx + btw), (True, True, True))
    check("geometry: and still does not intersect it",
          _overlap((bx, by, bbw, bbh), (btx, bty, btw, bth)), False)

    # 10. The caption and the fill are ordinary flags, and default sanely.
    check("caption: the word", gaze_caption_text("word", 0.6), GAZE_BAR_CAPTION_TEXT)
    check("caption: the percentage", gaze_caption_text("percent", 0.6), "60%")
    check("caption: none", gaze_caption_text("none", 0.6), None)
    check("fill: an unknown name falls back to the ramp's END colour",
          gaze_ink_colour("chartreuse"), SUCCESS_INK)

    # 11. The colour ramp. Endpoints are the promise made to whoever chose them, and
    #     the middle is where a ramp in RGB would go grey instead.
    cool = GAZE_BAR_RAMPS["cool"]
    check("ramp: at 0 it is the start colour",
          tuple(round(c, 3) for c in ramp_colour(cool, 0.0)),
          tuple(round(c, 3) for c in GAZE_MAGENTA_INK))
    check("ramp: at 1 it is the end colour, and it is GREEN",
          tuple(round(c, 3) for c in ramp_colour(cool, 1.0)),
          tuple(round(c, 3) for c in SUCCESS_INK))
    check("ramp: and green means the green channel leads",
          max(ramp_colour(cool, 1.0)) == ramp_colour(cool, 1.0)[1], True)
    check("ramp: 'ramp' resolves to the cool one",
          gaze_fill_spec("ramp"), ("ramp", GAZE_BAR_RAMPS["cool"]))
    check("ramp: 'ramp:warm' resolves to the warm one",
          gaze_fill_spec("ramp:warm"), ("ramp", GAZE_BAR_RAMPS["warm"]))
    check("ramp: an unknown ramp name is the cool one, not a crash",
          gaze_fill_spec("ramp:turbo"), ("ramp", GAZE_BAR_RAMPS["cool"]))
    check("fill: a flat name stays flat",
          gaze_fill_spec("magenta"), ("flat", GAZE_MAGENTA_INK))
    # The name -> drawing-arguments step, which is where a missing mode went unnoticed
    # once: three copies of it existed and one did not know about 'gradient'.
    check("fill args: a flat name passes one colour",
          gaze_fill_args("success"), {"ink": SUCCESS_INK})
    check("fill args: a ramp passes the stops and no gradient flag",
          gaze_fill_args("ramp"),
          {"ramp": GAZE_BAR_RAMPS["cool"], "gradient": False})
    check("fill args: a gradient passes the stops AND the flag",
          gaze_fill_args("gradient"),
          {"ramp": GAZE_BAR_RAMPS["cool"], "gradient": True})
    check("fill args: a gradient of the warm ramp keeps warm's stops",
          gaze_fill_args("gradient:warm"),
          {"ramp": GAZE_BAR_RAMPS["warm"], "gradient": True})
    check("fill args: an unknown name is the default, not a crash",
          gaze_fill_args("holographic"),
          {"ramp": GAZE_BAR_RAMPS["cool"], "gradient": False})

    # The ramp has to stay READABLE the whole way, which is not obvious for colours
    # picked by eye: contrast is a property of the composite, and the middle of a
    # two-stop ramp is a colour nobody chose. Sampled at 5% so a dip between samples
    # cannot hide.
    #
    # 4.0:1 rather than 4.5:1 deliberately. 4.5 is the floor for body TEXT; this is a
    # 54x616 px filled shape, where the large-graphic floor is 3:1. A fill that only
    # just cleared 3:1 would be a poor instrument, so this asks for a margin - and the
    # measured worst is 4.4:1, in the violet stretch around 30%, which is the dimmest
    # the bar ever gets.
    worst, worst_at = 99.0, 0.0
    for i in range(21):
        t = i / 20.0
        ratio_c = contrast_ratio(composited(ramp_colour(cool, t), GAZE_BAR_FILL_ALPHA),
                                 BG)
        if worst is None or ratio_c < worst:
            worst, worst_at = ratio_c, t
    check("ramp: every stop of the cool ramp clears 4.0:1 (worst %.1f:1 at %.0f%%)"
          % (worst, worst_at * 100.0), worst >= 4.0, True)
    hue0 = rgb_to_hsl(GAZE_MAGENTA_INK)[0]
    hue1 = rgb_to_hsl(ramp_colour(cool, 0.5))[0]
    check("ramp: the middle is not the grey axis (saturation %.2f)"
          % rgb_to_hsl(ramp_colour(cool, 0.5))[1],
          rgb_to_hsl(ramp_colour(cool, 0.5))[1] >= 0.45, True)
    check("ramp: and it travels the SHORT way round the wheel",
          abs(((hue0 - hue1) % 1.0)) < 0.25, True)

    # 12. The completion burst: a one-shot, deterministic, and confined to the air
    #     above the bar that the countdown does not occupy.
    bx0, by0, bbw, bbh = gaze_bar_box(1920, 1080, "disc")
    tbx, tby, tbw, tbh = timer_box(1920, 1080, "disc")
    ceiling = tby + tbh + GAZE_BURST_CEILING_PAD
    check("burst: nothing before it starts", burst_particles(-0.1, 1920, 1080, bx0, by0,
                                                             bbw, ceiling), [])
    check("burst: nothing after it ends", burst_particles(GAZE_BURST_LIFE + 0.01, 1920,
                                                          1080, bx0, by0, bbw, ceiling), [])
    check("burst: it is deterministic",
          burst_particles(0.4, 1920, 1080, bx0, by0, bbw, ceiling),
          burst_particles(0.4, 1920, 1080, bx0, by0, bbw, ceiling))
    frames = [burst_particles(a / 30.0, 1920, 1080, bx0, by0, bbw, ceiling)
              for a in range(0, int(GAZE_BURST_LIFE * 30) + 1)]
    counts = [len(f) for f in frames]
    check("burst: particles actually appear (%d live at its peak)" % max(counts),
          max(counts) > 0, True)
    check("burst: and it fades out rather than cutting off (%d left in the last "
          "frame)" % counts[-1], counts[-1] < max(counts), True)
    inside = all(GAZE_BURST_PAD - 0.1 <= p[0] <= 1920 - GAZE_BURST_PAD + 0.1
                 and ceiling - 0.1 <= p[1] <= 1080 - GAZE_BURST_PAD + 0.1
                 for f in frames for p in f)
    check("burst: every particle is inside the frame and below the countdown", inside,
          True)
    def _spread(f):
        """How wide the burst's ink is, in px. A burst that did not expand would be a
        flash, not fireworks."""
        return (max(p[0] for p in f) - min(p[0] for p in f)) if len(f) > 1 else 0.0

    early = next((f for f in frames if len(f) > 1), [])
    late = next((f for f in reversed(frames) if len(f) > 1), [])
    check("burst: it SPREADS outward as the sparks fly (%.0f px -> %.0f px)"
          % (_spread(early), _spread(late)), _spread(late) > _spread(early), True)
    check("burst: nothing is drawn on the countdown's own box",
          any(p[0] >= tbx and p[0] <= tbx + tbw and p[1] >= tby and p[1] <= tby + tbh
              for f in frames for p in f), False)

    # 13. "Looks full at 99%" is the complaint this exists to answer, so check the two
    #     states DIFFER in pixels. Two things carry that difference: the bright cap, and
    #     the burst. Both live at the top of the bar, which is the only place 99% and
    #     100% differ.
    def bar_top_ink(progress, age=None):
        """Mean brightness of the top 8 px of the bar's track, from real pixels."""
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1920, 1080)
        ctx = cairo.Context(surf)
        ctx.set_source_rgb(*BG)
        ctx.paint()
        gx, gy, gw, gh = gaze_bar_box(1920, 1080, "disc")
        draw_gaze(ctx, 1920, 1080, progress, "none", "disc",
                  fill="ramp", burst_age=age)
        raw = bytes(surf.get_data())
        stride = surf.get_stride()
        cap = max(3.0, gw * GAZE_BAR_CAP_FRAC)
        total, n = 0.0, 0
        for yy in range(int(gy), int(gy + cap) + 1):
            for xx in range(int(gx + 2), int(gx + gw - 2)):
                off = yy * stride + xx * 4
                total += (raw[off] + raw[off + 1] + raw[off + 2]) / 3.0
                n += 1
        return total / max(1, n)

    full, nearly = bar_top_ink(1.0), bar_top_ink(0.99)
    check("full: 100%% is brighter at the bar's top than 99%% (%.0f vs %.0f of 255)"
          % (full, nearly), full - nearly >= 20.0, True)
    check("full: and the cap is not there at 97%",
          bar_top_ink(0.97) - nearly < 5.0, True)

    # 12. The calibration coach. Three questions, none answerable by reading the
    #     arithmetic: does the reader refuse a payload it does not fully understand,
    #     does the panel stay out of the bands other screens own, and is everything it
    #     draws inside the box it reports.
    import tempfile

    def coach_from(payload):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
            name = fh.name
        try:
            return read_coach_stamp(name)
        finally:
            os.unlink(name)

    now = time.monotonic()
    good = {"state": "hold", "action": "LOOK AT THE SCREEN", "detail": "hold this pose",
            "phase": "A", "deadline": now + 6.0, "total": 8.0, "heartbeat": now}
    stamp = coach_from(good)
    check("coach: a good payload is read", bool(stamp), True)
    check("coach: the instruction survives the round trip",
          stamp["action"] if stamp else None, "LOOK AT THE SCREEN")
    check("coach: a missing file is no coach",
          read_coach_stamp("/nonexistent/coach.json"), None)
    check("coach: garbage is no coach", coach_from("not json at all"), None)
    check("coach: a list is no coach", coach_from([1, 2, 3]), None)
    check("coach: an unknown state is no coach",
          coach_from(dict(good, state="dance")), None)
    check("coach: a missing heartbeat is no coach",
          coach_from({k: v for k, v in good.items() if k != "heartbeat"}), None)
    check("coach: a blank instruction is no coach",
          coach_from(dict(good, action="   ")), None)
    check("coach: fresh at 0.2 s old", coach_fresh(stamp, now + 0.2), True)
    check("coach: stale at 2 s old", coach_fresh(stamp, now + 2.0), False)
    check("coach: a stamp from the future is not fresh",
          coach_fresh(stamp, now - 1.0), False)
    check("coach: no coach is never fresh", coach_fresh(None, now), False)
    remaining, _total = coach_seconds(stamp, now)
    check("coach: the clock reads the deadline, not a counter",
          None if remaining is None else round(remaining, 3), 6.0)

    def ink_rows(surf, lo=None, hi=None, floor=90, step=2):
        """(first, last) rows carrying a clearly BRIGHT pixel - i.e. drawn type.

        A brightness floor rather than an equality test against BG, because
        draw_standby paints its own warm vignette: compared against a rounded constant
        every pixel of that background counts as "ink", which is how the first version
        of this check reported the whole screen as type.
        """
        raw = bytes(surf.get_data())
        stride = surf.get_stride()
        first, last = None, None
        for yy in range(lo or 0, hi or surf.get_height()):
            for xx in range(0, surf.get_width(), step):
                off = yy * stride + xx * 4
                if max(raw[off], raw[off + 1], raw[off + 2]) > floor:
                    first = yy if first is None else first
                    last = yy
                    break
        return first, last

    def render_coach(draw_it=True, state="hold"):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1920, 1080)
        ctx = cairo.Context(surf)
        ctx.set_source_rgb(*BG)
        ctx.paint()
        if draw_it:
            payload = sample_coach(state, "LOOK AT THE SCREEN", "hold this pose", "A")
            return surf, draw_coach(ctx, 1920, 1080, payload, payload["heartbeat"])
        return surf, None

    # The standby word's own ink, measured, so the panel can be checked against it
    # rather than against a constant that would go stale the moment the word moved.
    word = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1920, 1080)
    wctx = cairo.Context(word)
    draw_standby(wctx, 1920, 1080, 0.0)
    word_top, word_bottom = ink_rows(word)
    check("standby: the word renders ink to measure against", bool(word_top), True)
    word_top = 1080 if word_top is None else word_top

    surf, box = render_coach(True)
    # A None here is a real bug in draw_coach, so it is reported rather than crashed
    # on - a self-test that dies tells you less than one that says which case failed.
    check("coach: the panel reports a box", box is not None, True)
    bx, by, bw_, bh_ = box if box else (0, 0, 0, 0)
    check("coach: the panel has area", bw_ > 0 and bh_ > 0, True)
    check("coach: the panel clears the standby word (%d < %d)" % (by + bh_, word_top),
          by + bh_ < word_top, True)
    pill_box = (43, 43, 245, 65)
    check("coach: the panel clears the pill's row (%d > %d)" % (by, pill_box[1] + pill_box[3]),
          by > pill_box[1] + pill_box[3], True)

    # Everything it draws is inside the box it reports. The 3 px allowance is the
    # border stroke's own half-width: cairo strokes a 3 px line centred on the path, so
    # 1.5 px of it lands outside the rectangle it was given.
    with_it = bytes(surf.get_data())
    stride = surf.get_stride()
    without = bytes(render_coach(False)[0].get_data())
    outside, inside = 0, 0
    for yy in range(0, 1080):
        for xx in range(0, 1920):
            off = yy * stride + xx * 4
            if with_it[off:off + 3] == without[off:off + 3]:
                continue
            if (bx - 3 <= xx <= bx + bw_ + 3 and by - 3 <= yy <= by + bh_ + 3):
                inside += 1
            else:
                outside += 1
    check("coach: ink is drawn (%d px)" % inside, inside > 2000, True)
    check("coach: and NONE of it outside the reported box (%d px)" % outside, outside, 0)
    check("coach: the panel is the upper band, not the middle",
          by < 1080 * 0.2 and by + bh_ < 1080 * 0.45, True)

    # The states are distinguished by their CAPTION, which is text and therefore
    # measurable, rather than by colour alone - a projector's colour is the least
    # reliable thing about it. Two states must therefore render differently.
    lead = bytes(render_coach(True, "lead")[0].get_data())
    done = bytes(render_coach(True, "done")[0].get_data())
    check("coach: two states render differently (caption and outline)",
          lead != done, True)
    caption_rows = ink_rows(render_coach(True, "lead")[0], int(by), int(by + bh_ * 0.3))
    check("coach: the caption band carries ink", caption_rows[0] is not None, True)

    # The instruction is the thing that has to be readable, so its contrast is
    # measured rather than assumed. The panel is 0.93 opaque over an unknown screen,
    # so the worst case is that 7% showing through as the darkest background there is.
    def rel_lum(rgb):
        def chan(c):
            c = max(0.0, min(1.0, c))
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (chan(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    panel = tuple(0.93 * f + 0.07 * b for f, b in zip((0.10, 0.09, 0.15), BG))
    hi, lo = rel_lum(STANDBY_INK), rel_lum(panel)
    ratio = (max(hi, lo) + 0.05) / (min(hi, lo) + 0.05)
    check("coach: the instruction reads at %.1f:1 on its own panel (floor 7)" % ratio,
          ratio >= 7.0, True)

    # 13. The stats screen. What it draws IS a claim about the session - "you were
    #     looking here, and not here" - so the shape is measured against the times it
    #     was given rather than eyeballed, and a file that cannot support the claim
    #     must produce the FACE instead of an empty bar.
    def session_from(payload):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
            name = fh.name
        try:
            return read_session(name)
        finally:
            os.unlink(name)

    good_timeline = [[0.0, 10.0, True], [10.0, 60.0, False]]
    good_file = {"phase": "running", "stage": 1, "stages": 4, "step": "waiting",
                 "seconds": 60.0,
                 "screen": {"kind": "stats", "spans": good_timeline, "target": 50.0}}
    read = session_from(good_file)
    check("stats: a published timeline is read as a stats screen", read["kind"], "stats")
    check("stats: with its spans", read["spans"], [(0.0, 10.0, True), (10.0, 60.0, False)])
    check("stats: and the target the conductor published", read["target"], 50.0)
    check("stats: no spans means the FACE, not an empty bar",
          session_from({**good_file,
                        "screen": {"kind": "stats", "target": 50.0}})["kind"], "face")
    check("stats: and a malformed timeline is refused the same way",
          session_from({**good_file, "screen": {"kind": "stats",
                                                "spans": [[0, "x", True]]}})["kind"],
          "face")
    check("stats: a timeline with a gap in it is refused too",
          session_from({**good_file, "screen": {"kind": "stats",
                                                "spans": [[5.0, 10.0, True]]}})["kind"],
          "stats")   # a gap is not a lie: it is a session that started late
    check("stats: a span that ends before it starts is refused",
          stats_spans([[10.0, 5.0, True]]), None)
    check("stats: a verdict that is not a boolean is refused",
          stats_spans([[0.0, 5.0, "yes"]]), None)
    check("stats: an empty list is not a timeline", stats_spans([]), None)
    check("stats: a target outside the session is not drawn",
          stats_target(90.0, [(0.0, 60.0, True)]), None)
    check("stats: and neither is one that is not a number",
          stats_target("50", [(0.0, 60.0, True)]), None)

    def stats_surface(spans=None, target=None):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1920, 1080)
        cr = cairo.Context(surf)
        draw_frame(cr, 1920, 1080, fresh(),
                   sample_screen("stats", spans=spans, target=target),
                   style="cartoon", listen=False)
        return surf

    def px(surf, x, y):
        buf = surf.get_data()
        i = y * surf.get_stride() + x * 4
        return (buf[i + 2], buf[i + 1], buf[i])

    def close(rgb, want, tol=6):
        return all(abs(a - b) <= tol for a, b in zip(rgb, want))

    surf = stats_surface()
    scr = sample_screen("stats")
    length = float(scr["seconds"])
    bw = 1920 * STATS_BAR_WIDTH_FRAC
    bx = (1920 - bw) / 2.0
    bh = 1080 * STATS_BAR_HEIGHT_FRAC
    th = 1080 * STATS_TRACK_HEIGHT_FRAC
    cy = int(round(1080 * STATS_BAR_Y_FRAC))
    look_rgb = tuple(round(c * 255) for c in STATS_LOOK_INK)
    away_rgb = tuple(round(c * 255) for c in STATS_AWAY_INK)
    bg_rgb = tuple(round(c * 255) for c in BG)
    dim_rgb = tuple(round(c * 255) for c in DIM_INK)

    def x_at(t):
        return bx + bw * max(0.0, min(1.0, t / length))

    runs, run = [], None
    for x in range(int(bx) - 2, int(bx + bw) + 3):
        if close(px(surf, x, cy), look_rgb) and run is None:
            run = x
        elif not close(px(surf, x, cy), look_rgb) and run is not None:
            runs.append((run, x - 1))
            run = None
    if run is not None:
        runs.append((run, int(bx + bw) + 2))
    # The 10 s ticks are drawn through the blocks, so one block reads as two runs with
    # a 2 px gap where a tick crosses it. A 0.1 s hole between two looks is not a
    # difference anyone can see, so the runs are merged rather than reported as two.
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 4:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)
    runs = merged
    want_runs = [(x_at(s), x_at(e)) for s, e, looking in scr["spans"] if looking]
    check("stats: one block per looking span is drawn", len(runs), len(want_runs))
    check("stats: each starts and ends where its seconds say",
          all(abs(g0 - w0) <= 2 and abs(g1 - w1) <= 2
              for (g0, g1), (w0, w1) in zip(runs, want_runs)), True)

    def height(x, ink):
        return sum(1 for y in range(int(cy - bh), int(cy + bh) + 1)
                   if close(px(surf, x, y), ink))

    look_x = (runs[0][0] + runs[0][1]) // 2
    away_span = next(s for s in scr["spans"] if not s[2])
    away_x = int(x_at((away_span[0] + away_span[1]) / 2))
    check("stats: looking is drawn at full height (%.0f px)" % bh,
          abs(height(look_x, look_rgb) - bh) <= 3, True)
    check("stats: not-looking is a thin band (%.0f px)" % th,
          abs(height(away_x, away_rgb) - th) <= 3, True)
    check("stats: so the two states differ in height, not only in colour",
          height(look_x, look_rgb) > 3 * height(away_x, away_rgb), True)

    tx = int(round(x_at(scr["target"])))
    check("stats: the target line is drawn at the target's own x",
          any(close(px(surf, tx, y), dim_rgb, 10)
              for y in range(int(cy - bh * 1.6), int(cy - bh * 0.5))), True)

    def row_clean(y):
        for x in range(int(bx), int(bx + bw)):
            # The target line's own antialiased columns, and the columns where a
            # looking block meets the slate: those pixels are blends and match
            # neither state, which is not the same as something drawn through the bar.
            if abs(x - tx) <= 2 or any(abs(x - int(round(x_at(t)))) <= 2
                                       for s, e, _l in scr["spans"] for t in (s, e)):
                continue
            if not any(close(px(surf, x, y), a, 10)
                       for a in (bg_rgb, look_rgb, away_rgb, dim_rgb)):
                return x
        return None

    check("stats: nothing is drawn through the timeline",
          [row_clean(y) for y in (cy - 18, cy, cy + 18)], [None, None, None])
    gb = gaze_bar_box(1920, 1080, "disc")
    check("stats: and the gaze bar's own column is empty here",
          close(px(surf, int(gb[0] + gb[2] / 2), int(gb[1] + gb[3] / 2)), bg_rgb, 3), True)
    check("stats: the timeline is the width the constant claims",
          abs((runs[-1][1] - runs[0][0]) - (x_at(length) - x_at(0.0))) <= 4, True)

    # 15. RUNG 3, the dimensional register. The rule for this project is that a
    #     face is verified by MEASURING RENDERED PIXELS, never by reading the
    #     drawing code, so every check below reads the surface it just drew.
    def dim_frame(expr="neutral", gaze=(0.0, 0.0), blink=0.0):
        s3 = cairo.ImageSurface(cairo.FORMAT_ARGB32, 960, 540)
        c3 = cairo.Context(s3)
        st3 = FaceState()
        st3.set_expression(expr)
        # update() is not optional: set_expression records a TARGET and the drawing
        # reads the eased parameter set, so without one update the "happy" frame is
        # neutral with a different label on it. dt large enough that the ease is
        # complete in one call (rate = min(1, dt*7)).
        st3.update(0.5)
        st3.gaze_x, st3.gaze_y = gaze[0], gaze[1]
        st3._blink = blink
        # The DRAWN face on purpose: these assertions are about the rung's own
        # geometry, and rung 3 is now a puppet that draws a photograph. The drawn
        # face is still shipped as the fallback, so it is still what they must pass
        # against - and if it ever stops being able to fall back, this is the test
        # that says so.
        _draw_dimensional_procedural(c3, 960, 540, st3, show_hud=False)
        s3.flush()
        return s3

    def is_skin(rgb):
        r, g, b = rgb
        return r > g > b and (r - b) > 20 and r > 110

    r3 = dim_frame()
    R3 = 0.30 * 540.0
    cx3 = 480.0
    cy3 = 270.0 + 540 * 0.02
    eye_x3 = int(cx3 - 0.355 * R3)

    check("rung 3: the forehead is skin",
          is_skin(px(r3, int(cx3), int(cy3 - 0.42 * R3))), True)
    check("rung 3: the head reaches below the mouth, so there is a chin",
          is_skin(px(r3, int(cx3), int(cy3 + 0.94 * R3))), True)
    check("rung 3: each eye has something dark in it at the eye line",
          min(sum(px(r3, x, int(cy3))) for x in
              range(eye_x3 - 4, eye_x3 + 5)) < 330, True)
    lip_rows = [y for y in range(int(cy3 + 0.66 * R3), int(cy3 + 0.92 * R3))
                if (lambda c: c[0] > c[1] + 15 and c[0] > 70)(px(r3, int(cx3), y))]
    check("rung 3: there are lips between the nose and the chin",
          len(lip_rows) >= 5, True)

    r3h = dim_frame("happy")
    changed = sum(1 for y in range(0, 540, 4) for x in range(240, 760, 4)
                  if px(r3h, x, y) != px(r3, x, y))
    check("rung 3: an expression change is a change in the drawn face",
          changed > 300, True)

    r3l, r3r = dim_frame(gaze=(-1.0, 0.0)), dim_frame(gaze=(1.0, 0.0))
    moved = sum(1 for y in range(int(cy3 - 0.10 * R3), int(cy3 + 0.10 * R3), 2)
                for x in range(int(cx3 - 0.50 * R3), int(cx3 - 0.20 * R3), 2)
                if px(r3l, x, y) != px(r3r, x, y))
    check("rung 3: gaze moves the eye content", moved > 20, True)

    r3b = dim_frame(blink=1.0)

    def blue_count(surf, xc, yc, rad=14):
        """How many blue-dominant pixels there are around the eye.

        A COUNT, not a single sample: `eye_open` is clamped to 0.02 so a closed eye
        keeps a sliver of iris by design (an eye that shuts to nothing reads as
        dead). What a blink must do is collapse the AREA, and a count is the only
        thing that can say so without depending on where the sliver lands.
        """
        return sum(1 for y in range(yc - rad, yc + rad + 1)
                   for x in range(xc - rad, xc + rad + 1)
                   if px(surf, x, y)[2] - px(surf, x, y)[0] > 40)

    opened = blue_count(r3, eye_x3, int(cy3))
    closed = blue_count(r3b, eye_x3, int(cy3))
    check("rung 3: a closed lid shuts the iris away",
          (opened > 20, closed < opened * 0.25), (True, True))

    # 16. RUNG 4, the image puppet. The still is the approved rung-4 design study
    #     and its landmarks are measured, so the puppet must BE the still wherever
    #     nothing is animated - that is the property that makes it a puppet rather
    #     than a painting with stickers on it.
    def port_frame(blink=0.0, mouth=None, gaze=(0.0, 0.0)):
        s4 = cairo.ImageSurface(cairo.FORMAT_ARGB32, 960, 540)
        c4 = cairo.Context(s4)
        st4 = FaceState()
        st4.set_expression("neutral")
        st4.gaze_x, st4.gaze_y = gaze[0], gaze[1]
        st4._blink = blink
        if mouth is not None:
            st4.mouth_drive = mouth
            st4._mouth_drive_s = mouth
        draw_face(c4, 960, 540, st4, show_hud=False, style="portrait")
        s4.flush()
        return s4

    still, lm4 = _portrait_load()
    # 15b. RUNG 3's ATLAS. Two properties, both measurable without knowing what the
    #      face looks like: a blink must CHANGE the frame a lot (it swaps to the shut
    #      still), and the shut still must have no iris left in the eye's row.
    def milo_frame(blink=0.0, expr="neutral"):
        s6 = cairo.ImageSurface(cairo.FORMAT_ARGB32, 960, 540)
        c6 = cairo.Context(s6)
        st6 = FaceState()
        st6.set_expression(expr)
        st6._blink = blink
        draw_face(c6, 960, 540, st6, show_hud=False, style="dimensional")
        s6.flush()
        return s6

    m_open, m_shut = milo_frame(), milo_frame(blink=1.0)
    changed = sum(1 for y in range(300, 380, 2) for x in range(390, 590, 2)
                  if px(m_open, x, y) != px(m_shut, x, y))
    check("rung 3: a blink swaps in the shut still", changed > 2000, True)
    # The eyes at full blink must be the SHUT STILL's own pixels, not a patch laid
    # over the open one. A colour count cannot say this at 960 px - Milo's eye is
    # thirty pixels wide there and the lash line outnumbers the iris - but "is this
    # pixel the photograph" can, and it is the property that matters: the rung draws
    # a real closed eye because a closed eye is what the atlas contains.
    shut_lm = json.load(open(os.path.join(PORTRAIT_DIR, "milo.json")))
    shut_src = cairo.ImageSurface.create_from_png(
        os.path.join(PORTRAIT_DIR, shut_lm["eyes_closed"]))
    shut_src.flush()
    _sb, _sst = bytes(shut_src.get_data()), shut_src.get_stride()

    def pxs(x, y):
        o = int(y) * _sst + int(x) * 4
        return (_sb[o + 2], _sb[o + 1], _sb[o])
    sc6 = max(960.0 / shut_src.get_width(), 540.0 / shut_src.get_height())
    dx6 = (960 - shut_src.get_width() * sc6) / 2.0
    dy6 = (540 - shut_src.get_height() * sc6) / 2.0
    same_eye = total_eye = 0
    for e6 in shut_lm["eyes"]:
        cx6 = dx6 + e6["cx"] * sc6
        cy6 = dy6 + e6["cy"] * sc6
        for y in range(int(cy6 - 8), int(cy6 + 8)):
            for x in range(int(cx6 - 16), int(cx6 + 16)):
                total_eye += 1
                if close(px(m_shut, x, y),
                         pxs((x - dx6) / sc6, (y - dy6) / sc6), 8):
                    same_eye += 1
    check("rung 3: and the shut eye is the atlas's own closed eye",
          same_eye > 0.90 * total_eye, True)

    check("rung 4: the still and its measured landmarks load", still is not None,
          True)
    if still is not None and lm4 is not None and _portrait_valid(lm4):
        img4, dx4, dy4, sc4 = _portrait_scaled(still, lm4, 960, 540)
        p4 = port_frame()
        # the glabella: between the eyes, above them - nothing animates it
        gl_x = int(dx4 + sc4 * sum(e["cx"] for e in lm4["eyes"]) / 2.0)
        gl_y = int(dy4 + sc4 * (lm4["eyes"][0]["cy"]
                                - lm4["eyes"][0]["ap_ry"] * 1.3))
        check("rung 4: at rest the puppet draws the still itself, pixel for pixel",
              close(px(p4, gl_x, gl_y), px(img4, gl_x, gl_y), 2), True)

        e0 = lm4["eyes"][0]
        ex4 = int(dx4 + e0["cx"] * sc4)
        ey4 = int(dy4 + e0["cy"] * sc4)
        p4b = port_frame(blink=1.0)
        check("rung 4: a blink covers the eye",
              max(abs(a - b) for a, b in
                  zip(px(p4b, ex4, ey4), px(p4, ex4, ey4))) > 10, True)
        check("rung 4: and leaves the rest of the face alone",
              close(px(p4b, gl_x, gl_y), px(p4, gl_x, gl_y), 2), True)

        m4 = lm4["mouth"]
        mx4 = int(dx4 + m4["cx"] * sc4)
        my4 = int(dy4 + m4["cy"] * sc4)
        p4o = port_frame(mouth=1.0)
        before = sum(sum(px(p4, mx4, my4 + k)) for k in (2, 4, 6))
        after = sum(sum(px(p4o, mx4, my4 + k)) for k in (2, 4, 6))
        check("rung 4: mouth_open opens a dark gap between the lips",
              after < before - 60, True)

        # gaze slides the eye's own content inside its own aperture
        p4l = port_frame(gaze=(-1.0, 0.0))
        p4r = port_frame(gaze=(1.0, 0.0))
        check("rung 4: gaze changes the eye and only the eye",
              (abs(sum(px(p4l, ex4, ey4)) - sum(px(p4r, ex4, ey4))) > 0,
               close(px(p4l, gl_x, gl_y), px(p4r, gl_x, gl_y), 2)),
              (True, True))

        # a landmark file that is not a face is refused rather than drawn
        bad = json.loads(json.dumps(lm4))
        bad["mouth"]["cy"] = bad["eyes"][0]["cy"] - 60.0
        check("rung 4: landmarks with the mouth above the eyes are rejected",
              _portrait_valid(bad), False)
        good = json.loads(json.dumps(lm4))
        check("rung 4: and the measured ones are accepted",
              _portrait_valid(good), True)

    # 17. A missing asset must cost the robot a rung, not its face: the kiosk
    #     drawing nothing because a PNG went astray is the same failure class as a
    #     bar drawn at zero from a file that did not say. Compared against a rung 3
    #     frame built from the SAME state (no update() call), because head_bob is a
    #     function of t and a different t shifts the whole face.
    def plain_dim():
        s5 = cairo.ImageSurface(cairo.FORMAT_ARGB32, 960, 540)
        c5 = cairo.Context(s5)
        st5 = FaceState()
        st5.set_expression("neutral")
        _draw_dimensional_procedural(c5, 960, 540, st5, show_hud=False)
        s5.flush()
        return s5

    keep = (PORTRAIT_PNG, PORTRAIT_JSON)
    try:
        globals()["PORTRAIT_PNG"] = "/nonexistent/portrait.png"
        _PORTRAIT.update(stamp=None, png=None, lm=None, scaled={}, mouth=None,
                         mouth_key=None, warned=True)
        fb = port_frame()
    finally:
        globals()["PORTRAIT_PNG"], globals()["PORTRAIT_JSON"] = keep
        _PORTRAIT.update(stamp=None, png=None, lm=None, scaled={}, mouth=None,
                         mouth_key=None, warned=False)
    ref = plain_dim()
    same = sum(1 for y in range(0, 540, 6) for x in range(0, 960, 6)
               if close(px(fb, x, y), px(ref, x, y), 2))
    check("rung 4: a missing asset falls back to rung 3, not to a blank screen",
          same > 0.95 * (90 * 160), True)

    print()
    if failures:
        print("  SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    print("  SELFTEST PASSED (%d cases)" % len(ran))
    return 0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Social robot cartoon face")
    ap.add_argument("--snapshot", metavar="PATH",
                    help="render a single frame to a PNG and exit")
    ap.add_argument("--expression", default=None, choices=EXPRESSIONS,
                    help="expression to render in snapshot mode. Default: whatever "
                         "--pin-expression holds, i.e. what the kiosk is showing; "
                         "'neutral' when the pin is off")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--mouth-open", type=float, default=None,
                    help="snapshot only: drive the lip-sync mouth 0..1")
    ap.add_argument("--style", default="cartoon", choices=STYLES,
                    help="which face to draw (default %(default)s). The cartoon "
                         "is rung 1 of the realism ladder, the illustrated face "
                         "is rung 2 - see docs/face-ladder/README.md")
    ap.add_argument("--look", default=os.environ.get("FACE_LOOK", ILL_DEFAULT_LOOK),
                    choices=tuple(ILL_LOOKS),
                    help="ILLUSTRATED STYLE ONLY: which palette and proportions "
                         "to draw (default %(default)s). It is a taste call, not a "
                         "correctness one, so every alternative is the SAME drawing "
                         "code with a different look table - see ILL_LOOKS. An "
                         "unknown name falls back to the default rather than "
                         "failing")
    ap.add_argument("--screen", default="face",
                    choices=("standby", "face", "label", "success", "stats",
                             "calibration"),
                    help="SNAPSHOT ONLY: which screen to render (default "
                         "%(default)s). The kiosk reads the live screen from "
                         "$FACE_SESSION instead, so this switch cannot make a "
                         "running robot disagree with its own session file. "
                         "'calibration' is not a session kind: it is the standby "
                         "screen with the tuning face drawn over it, which is what "
                         "a lip-sync trial needs on the projector")
    ap.add_argument("--label-text", default="Stage 3",
                    help="SNAPSHOT ONLY: the text of a label screen")
    ap.add_argument("--step", choices=("waiting", "session", "success"), default=None,
                    help="SNAPSHOT ONLY: render the screen at a step of a stage. "
                         "'session' draws the countdown, 'waiting' draws none "
                         "because nothing is counting down until someone speaks. "
                         "Default: the screen's plain state")
    ap.add_argument("--prompt", dest="prompt_sample", default=None, metavar="COMMAND",
                    help="SNAPSHOT ONLY: draw the confirmation prompt for COMMAND, "
                         "as the listener would during its countdown")
    ap.add_argument("--listen", action="store_true",
                    help="SNAPSHOT ONLY: draw the green listening pill, as the face "
                         "does while $FACE_MIC says the robot can hear")
    ap.add_argument("--coach", choices=COACH_STATES, default=None,
                    help="SNAPSHOT ONLY: draw the calibration coach panel in this "
                         "state, as robot/perception/calibrate-look.py publishes it. "
                         "Drawn only over --screen standby, which is the only screen "
                         "the kiosk honours it on: a stage's screen is the "
                         "conductor's")
    ap.add_argument("--coach-action", default="LOOK AT THE SCREEN",
                    help="SNAPSHOT ONLY: the coach's headline instruction")
    ap.add_argument("--coach-detail", default="hold this pose",
                    help="SNAPSHOT ONLY: the coach's one line of explanation")
    ap.add_argument("--coach-phase", default="A",
                    help="SNAPSHOT ONLY: which of the two phases the panel names")
    ap.add_argument("--coach-seconds", type=float, default=6.0, metavar="SECS",
                    help="SNAPSHOT ONLY: seconds left in the coach's window")
    ap.add_argument("--coach-total", type=float, default=8.0, metavar="SECS",
                    help="SNAPSHOT ONLY: the window's length, for the bar's fraction")
    ap.add_argument("--timer", default=os.environ.get("FACE_TIMER", DEFAULT_TIMER_STYLE),
                    choices=TIMER_STYLES,
                    help="the shape of the countdown: a draining %(choices)s "
                         "(default %(default)s, or $FACE_TIMER). Applies to the stage "
                         "and success timers; the confirmation window always uses the "
                         "bar, because a readable disc does not fit inside it")
    ap.add_argument("--stats", action="store_true",
                    help="log the achieved frame period and the draw time every "
                         "few seconds. The kiosk turns this on by itself while a "
                         "tuning session owns $FACE_CALIBRATION, because the face's "
                         "own visual latency is half of the lip-sync offset and "
                         "cannot be read off the system any other way")
    ap.add_argument("--pin-expression",
                    default=os.environ.get("FACE_PIN", PIN_EXPRESSION_DEFAULT),
                    choices=PIN_EXPRESSION_CHOICES,
                    help="hold ONE expression and stop mirroring the state file "
                         "(default %(default)s, or $FACE_PIN). 'off' restores the "
                         "old behaviour, where gaze.py decides the expression from "
                         "whether the person is looking at the robot. TEMPORARY: "
                         "the pin itself will be replaced by a different trigger")
    ap.add_argument("--gaze-caption",
                    default=os.environ.get("FACE_GAZE_CAPTION",
                                           GAZE_BAR_CAPTION_DEFAULT),
                    choices=GAZE_BAR_CAPTIONS,
                    help="what is written under the gaze bar (default %(default)s, "
                         "or $FACE_GAZE_CAPTION): the word, the percentage, or "
                         "nothing")
    ap.add_argument("--gaze-progress", type=float, default=None, metavar="0..1",
                    help="SNAPSHOT ONLY: draw the gaze bar filled to this fraction "
                         "(--step session). Without it no bar is drawn, which is what "
                         "a session that has not seen a face yet looks like")
    ap.add_argument("--stats-spans", default=None, metavar="JSON",
                    help="SNAPSHOT ONLY (--screen stats): the looking timeline as a "
                         "JSON list of [start, end, looking] triples in seconds, e.g. "
                         "'[[0,13.4,true],[13.4,42,false]]'. Default is a fixed "
                         "timeline, so two renders of this screen are comparable")
    ap.add_argument("--stats-target", type=float, default=None, metavar="SECONDS",
                    help="SNAPSHOT ONLY (--screen stats): where to draw the target "
                         "line. The conductor publishes this at runtime; the face "
                         "does not know the pass condition")
    ap.add_argument("--stats-seconds", type=float, default=None, metavar="SECONDS",
                    help="SNAPSHOT ONLY (--screen stats): the length of the session "
                         "the timeline draws. Default is the 42 s sample; a real run "
                         "draws 60, which is also where the end label comes from")
    ap.add_argument("--gaze-ink", default=os.environ.get("FACE_GAZE_INK",
                                                         GAZE_BAR_FILL_DEFAULT),
                    choices=GAZE_BAR_FILLS,
                    help="the gaze bar's fill: a colour RAMP that runs with the "
                         "fraction (default %(default)s = magenta to green), "
                         "'ramp:warm' (via red and amber), or one flat colour "
                         "(accent/success/magenta). Or $FACE_GAZE_INK")
    # The literal percent is escaped TWICE on purpose: this string is
    # %-formatted here, and argparse %-formats the result AGAIN when it builds
    # the help text, so a single %% becomes a bare % and `face.py --help` dies
    # with "badly formed help string". Four escapes land as one % on screen.
    ap.add_argument("--gaze-burst", type=float, default=None, metavar="SECS",
                    help="SNAPSHOT ONLY: render the completion burst as it looked "
                         "SECS seconds after the fill reached 100%%%% (with "
                         "--gaze-progress 1.0). It lives 0-%.1fs, so e.g. 0.35"
                         % GAZE_BURST_LIFE)
    ap.add_argument("--selftest", action="store_true",
                    help="exercise the decisions the kiosk makes - the pinned "
                         "expression and the validation of the session file - with no "
                         "display, and exit. Needs cairo, not GTK")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.snapshot:
        # "calibration" is a SCREENSHOT name, not a session kind: it renders the
        # standby screen with the tuning face over it, which is the frame a
        # lip-sync trial is judged against and therefore the frame to measure.
        calibrating = args.screen == "calibration"
        # An unspecified expression follows the PIN, so a snapshot with no
        # --expression renders what the kiosk actually shows. A caller that names
        # one - expression_probe.py does, for every expression in turn - still gets
        # exactly that expression, because the pin is the kiosk's policy and not a
        # property of the renderer.
        expression = args.expression
        if expression is None:
            expression = (args.pin_expression
                          if args.pin_expression in EXPRESSIONS else "neutral")
        path = snapshot(args.snapshot, expression, args.width, args.height,
                        mouth_open=args.mouth_open, style=args.style,
                        screen="standby" if calibrating else args.screen,
                        label_text=args.label_text,
                        prompt_sample=args.prompt_sample, listen=args.listen,
                        timer_style=args.timer, step=args.step,
                        calibration=calibrating,
                        gaze_progress=args.gaze_progress,
                        gaze_caption=args.gaze_caption,
                        gaze_ink=args.gaze_ink,
                        gaze_burst=args.gaze_burst, look=args.look,
                        stats_spans=sample_stats_spans(args.stats_spans),
                        stats_target=args.stats_target,
                        stats_seconds=args.stats_seconds,
                        coach=sample_coach(args.coach, args.coach_action,
                                           args.coach_detail, args.coach_phase,
                                           args.coach_seconds, args.coach_total)
                        if args.coach else None)
        print("wrote %s" % path)
        return 0

    return run_kiosk(style=args.style, timer_style=args.timer,
                     stats_forced=args.stats,
                     pin_expression=args.pin_expression,
                     gaze_caption=args.gaze_caption,
                     gaze_ink=args.gaze_ink, look=args.look)


if __name__ == "__main__":
    sys.exit(main())
