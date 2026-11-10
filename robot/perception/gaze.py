#!/usr/bin/env python3
"""gaze.py — make the robot's eyes follow the person in front of it.

WHAT THIS ACTUALLY DOES (worth being precise)
  A single RGB frame gives you a face box and 5 facial landmarks (two eye
  corners, nose, two mouth corners). That is enough to estimate **where the
  head is and which way it is turned**, which is what a social robot needs to
  make eye contact and seem attentive.

  It is NOT pupil-level gaze direction. That needs iris landmarks, which the
  SCRFD model does not produce. If true eye-gaze is wanted later, the
  `luxonis/mediapipe-face-landmarker` model provides iris points. The output
  contract here would not change, so this is a drop-in upgrade path.

  Terminology in this file: "gaze" means the head-pose estimate we send to the
  face. `yaw` = turned left/right, `pitch` = tilted up/down.

HOW GAZE IS DERIVED FROM LANDMARKS
  Face position alone is not enough - a person at the left edge looking straight
  at the robot is making eye contact, while a person dead-centre looking away is
  not. Both signals are combined:

    1. FACE OFFSET  - where the head is in frame, normalised to -1..+1.
    2. HEAD YAW     - for a frontal face the nose sits midway between the two
                      eye corners. Turning the head slides the nose toward one
                      side, so:
                          yaw ~ (nose_x - eye_mid_x) / eye_distance
                      Dividing by eye distance makes it invariant to how far
                      away the face is, so it works at any range.
    3. HEAD PITCH   - when the head tips down, the nose moves toward the eye
                      line and the eye-to-nose vertical gap shrinks. Measuring
                      that same gap in units of eye distance gives a
                      range-invariant pitch.

  Both are zero when the face is frontal, positive to one side.

OUTPUT CONTRACT — /run/face/state.json
  face.py owns this file. It re-reads it only when the mtime changes, and it
  clamps gaze to -1..1 itself. The documented shape is:

      {"expression": "neutral", "gaze": {"x": 0.0, "y": 0.0}, "blink": false}

  This process owns ONLY the `gaze` key. It reads the file, updates `gaze`, and
  writes it back, so a future dialogue process writing `expression` is not
  clobbered. Writes are atomic (temp file + rename) so the reader can never see
  a half-written file.

EXCLUSIVE ACCESS
  DepthAI holds the camera exclusively, so this cannot run while
  oak-viewer.service is up. Stop the viewer first.

WHAT COUNTS AS "LOOKING AT THE SCREEN" IS A CALIBRATION, NOT A CONSTANT
  The `looking` verdict - which the expression and the session's gaze bar both run on
  - is a threshold on the magnitude of the smoothed combined gaze. That number folds
  WHERE the head sits in the frame together with WHICH WAY it is turned, so the value
  a person produces by facing the screen belongs to the arrangement: the camera's
  position, its aim, the seat, the screen. Move the camera between the robot and the
  bench and the number moves with it.

  So the thresholds (and the subject gate) are read from look.conf beside this file,
  written by robot/perception/calibrate-look.py, and RE-READ whenever that file
  changes - a re-fit is live with no restart. With no file, the built-ins below are
  used, which are the values measured on the robot in its case. An explicit
  --look-enter/--look-exit flag beats the file and is pinned for the life of the run;
  --no-look-conf ignores the file entirely, for an A/B.

  The start-up lines in the journal state the values AND their source, so "which
  calibration is live" is answerable without reading this file.

USAGE
  .venv/bin/python gaze.py --dry-run          # measure, write NOTHING
  .venv/bin/python gaze.py                    # drive the face
  .venv/bin/python gaze.py --dump-landmarks   # print raw keypoints to verify order
  .venv/bin/python gaze.py --status /tmp/gaze.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import tempfile
import time

import depthai as dai
from depthai_nodes.node import ParsingNeuralNetwork

HERE = pathlib.Path(__file__).resolve().parent

# The "what does THIS camera call looking at the screen" calibration. Imported, not
# duplicated, so the numbers calibrate-look.py writes and the numbers this daemon
# acts on cannot drift apart. Missing is a WARNING rather than a crash: the built-in
# values are the ones measured on the robot in its case, so a device that somehow
# lacks the module keeps working - it just says so, loudly, instead of pretending it
# read a calibration it never saw.
try:
    import lookconf
except ImportError:                                      # pragma: no cover
    lookconf = None
    print("gaze: WARNING lookconf.py is missing beside gaze.py, so look.conf will "
          "NOT be read and the built-in looking thresholds stay in force. Deploy the "
          "perception directory.", flush=True)

MODEL = "luxonis/scrfd-face-detection"

# SCRFD keypoint order, verified EMPIRICALLY by drawing them on a real frame
# and looking (see README): index 0 is the eye at the SMALLER x - the viewer's
# LEFT - index 1 the larger x (viewer's right), 2 the nose, 3 and 4 the mouth
# corners. An earlier version of this comment had the sides the wrong way round.
#
# The landmark maths below uses only relationships (the eye-to-eye line, the
# nose offset from it), so it does not care which eye is which - swapping the
# two indices would not change the magnitude.
#
# The SIGN of both axes is settled by OBSERVATION, not derivation:
#   yaw:   turn to your own left  -> yaw_raw > 0
#   pitch: tip your head down     -> pitch_raw > 0
# and face.py renders positive gaze.x to the screen-right and positive gaze.y
# DOWNWARD (`ix = cx + gaze_x*ew*0.42`, `iy = cy + gaze_y*eh*0.55`, no mirroring).
# So the robot's eyes follow the direction of the turn/tilt. Correct as-is.
#
# An earlier comment claimed a downward tilt should give a NEGATIVE pitch_raw.
# That was reasoning about the intermediate signal (which way the nose ratio
# moves) instead of the end-to-end chain (ratio -> gaze.y -> iris position), and
# it was wrong. The signs above are what the hardware actually does.
KP_LEFT_EYE, KP_RIGHT_EYE, KP_NOSE, KP_MOUTH_L, KP_MOUTH_R = 0, 1, 2, 3, 4

# nose_ratio measured on a FRONTAL, level face: 0.396. Verified against a live
# session where yaw_raw was ~0.05, i.e. genuinely facing the camera.
#
# This is not a free parameter and not the midpoint of anything - it is the
# measurement that makes a level head read zero pitch.
#
# KNOWN COUPLING: turning the head also moves nose_ratio (measured: 0.341 at
# yaw_raw -0.31, 0.441 at yaw_raw +0.39). So pitch drifts as a function of yaw,
# and the drift is not symmetric. On a strongly turned head, do not trust the
# pitch figure - it is contaminated by the yaw. A yaw-compensation term is the
# fix if that matters for the agitation work; it is not needed for the eyes to
# follow a roughly-facing person.
NEUTRAL_NOSE_RATIO = 0.396


def _norm(v: float, lo: float, hi: float) -> float:
    """Map [lo, hi] to [-1, 1], clamped. Guards against a zero-width range."""
    if hi - lo <= 1e-9:
        return 0.0
    return max(-1.0, min(1.0, (v - lo) / (hi - lo) * 2.0 - 1.0))


def _unit(v: float, extent: int) -> float:
    """Return a coordinate as a 0..1 fraction.

    DepthAI's parser emits **normalised** coordinates (a 640x640 model input
    gives xmin/ymin/getCenterX/keypoints all in 0..1), NOT pixels. But an
    unnormalised source is possible, so this converts only when the value is
    clearly larger than the normalised range. Getting this wrong is not a
    subtle scaling error - treating 0.5 as "half a pixel of 640" produced a
    confident, completely bogus gaze of (-0.75, -0.93).
    """
    if extent and abs(v) > 1.5:
        return v / float(extent)
    return v


def _signed(u: float) -> float:
    """Map a 0..1 fraction to -1..1 (the face's gaze coordinate space)."""
    return max(-1.0, min(1.0, u * 2.0 - 1.0))


class Smoother:
    """Exponential smoothing with a dead band.

    A raw per-frame estimate jitters by several percent - enough to make the
    robot's eyes twitch distractingly. Two fixes:

      * EMA      - smooths the value over time.
      * deadband - if the target has barely moved, do not update at all. Without
                   it, an EMA still creeps toward sub-pixel noise, so the eyes
                   never truly hold still.

    Reset() snaps instead of easing, for when tracking is lost: easing back to
    centre looks like the robot is thinking, snapping looks like it stopped
    paying attention. Snapping is the honest signal.
    """

    def __init__(self, alpha: float, deadband: float) -> None:
        self.alpha = alpha
        self.deadband = deadband
        self.x = 0.0
        self.y = 0.0

    def update(self, x: float, y: float) -> tuple[float, float]:
        # Compare the RENDERED value against the target. Testing the raw target
        # against the (lagging) EMA would let a value converge forever.
        if (abs(x - self.x) < self.deadband
                and abs(y - self.y) < self.deadband):
            return self.x, self.y
        self.x += self.alpha * (x - self.x)
        self.y += self.alpha * (y - self.y)
        return self.x, self.y

    def reset(self) -> tuple[float, float]:
        """Snap to centre. Returns the values, for symmetry in logging."""
        self.x = 0.0
        self.y = 0.0
        return self.x, self.y


def pick_face(detections):
    """Choose the face to track: largest, then most confident.

    Largest-first rather than most-confident-first, because the nearest person
    is the one the robot is talking to. A confident small face in the
    background should not steal the robot's attention.
    """
    best = None
    best_key = None
    for det in detections:
        w, h = det.getWidth(), det.getHeight()
        area = w * h
        key = (area, det.confidence)
        if best_key is None or key > best_key:
            best, best_key = det, key
    return best


def landmarks_of(face):
    """Return [(x, y, confidence), ...] for a face, tolerant of API shape.

    `dai.Keypoint` does NOT expose .x/.y directly - it wraps a Point2f as
    `imageCoordinates`, and also carries per-point confidence. `getKeypoints2f()`
    returns bare Point2f values. Prefer the richer form and fall back, rather
    than assuming either.
    """
    out = []
    try:
        for k in face.getKeypoints():
            pt = getattr(k, "imageCoordinates", None)
            if pt is None:
                continue
            out.append((float(pt.x), float(pt.y),
                        float(getattr(k, "confidence", 1.0))))
    except Exception:  # noqa: BLE001
        out = []

    if not out:
        try:
            for p in face.getKeypoints2f():
                out.append((float(p.x), float(p.y), 1.0))
        except Exception:  # noqa: BLE001
            out = []
    return out


def estimate(face, frame_w: int, frame_h: int, yaw_sign: float):
    """Return (gaze_x, gaze_y, debug_dict) for one detected face.

    Coordinates from the parser are 0..1 fractions; the landmark ratios below
    are unit-free, so they are unaffected. Only the box centre needs care.
    """
    kps = landmarks_of(face)
    cx = _unit(face.getCenterX(), frame_w)
    cy = _unit(face.getCenterY(), frame_h)

    # Fall back to the box centre when landmarks are missing rather than
    # dropping the frame: a tracked face with no landmarks is still a face.
    if len(kps) < 5:
        return _signed(cx), _signed(cy), {
            "landmarks": False, "n_kp": len(kps),
            "note": "no keypoints, used box centre",
            "face_cx": round(cx, 4), "face_cy": round(cy, 4)}

    px = [k[0] for k in kps[:5]]
    py = [k[1] for k in kps[:5]]
    eye_mid_x = (px[KP_LEFT_EYE] + px[KP_RIGHT_EYE]) / 2.0
    eye_mid_y = (py[KP_LEFT_EYE] + py[KP_RIGHT_EYE]) / 2.0
    eye_dist = math.dist((px[KP_LEFT_EYE], py[KP_LEFT_EYE]),
                         (px[KP_RIGHT_EYE], py[KP_RIGHT_EYE]))
    mouth_mid_y = (py[KP_MOUTH_L] + py[KP_MOUTH_R]) / 2.0

    if eye_dist < 1e-6:
        return _signed(cx), _signed(cy), {
            "landmarks": True, "n_kp": len(kps),
            "note": "degenerate eye distance",
            "face_cx": round(cx, 4), "face_cy": round(cy, 4)}

    # Range-invariant pose signals (see module docstring).
    yaw = yaw_sign * (px[KP_NOSE] - eye_mid_x) / eye_dist
    # Nose-to-eyeline gap, as a fraction of the eye-line-to-mouth span.
    #
    # The DOMINANT lever here is the DENOMINATOR, not the numerator: tipping the
    # head down foreshortens the lower face, shrinking the eye->mouth span, so
    # the same nose gap becomes a LARGER fraction and pitch_raw RISES. Measured
    # on hardware: head down -> positive, head up -> negative.
    #
    # The first version of this file asserted the opposite and fixed the
    # denominator in its reasoning. Do not "correct" the sign below without
    # re-testing on a real person.
    span = max(mouth_mid_y - eye_mid_y, 1e-6)
    nose_ratio = (py[KP_NOSE] - eye_mid_y) / span
    pitch = (nose_ratio - NEUTRAL_NOSE_RATIO)

    debug = {
        "landmarks": True,
        "n_kp": len(kps),
        "eye_dist": round(eye_dist, 4),
        "yaw_raw": round(yaw, 4),
        "pitch_raw": round(pitch, 4),
        "nose_ratio": round(nose_ratio, 4),
        "face_cx": round(cx, 4),
        "face_cy": round(cy, 4),
        "box_w": round(_unit(face.getWidth(), frame_w), 4),
        "box_h": round(_unit(face.getHeight(), frame_h), 4),
        "conf": round(face.confidence, 3),
    }
    return yaw, pitch, debug


def combine(yaw: float, pitch: float, face, frame_w: int, frame_h: int,
            cfg) -> tuple[float, float]:
    """Turn head pose + face position into the face's -1..1 gaze coords."""
    # Where the head is, as a nudge toward that side - so the robot looks at a
    # person standing off to one side even while their head is frontal.
    off_x = _signed(_unit(face.getCenterX(), frame_w))
    off_y = _signed(_unit(face.getCenterY(), frame_h))
    gx = cfg.center_gain * off_x + cfg.yaw_gain * yaw
    gy = cfg.center_gain * off_y + cfg.pitch_gain * pitch
    return (max(-1.0, min(1.0, gx)), max(-1.0, min(1.0, gy)))


# --------------------------------------------------------------------------
# Expression policy
# --------------------------------------------------------------------------
# The brief, verbatim:
#   no face                                   -> neutral
#   gaze level, looking at the camera          -> happy
#   gaze not level, not looking at the camera  -> angry
#
# "Looking at the camera" is judged from the SMOOTHED combined gaze, which folds
# together head offset and head pose. A raw yaw threshold would be wrong: it
# would call a person off to one side who is staring straight at the robot
# "not looking".
#
# TWO thresholds, not one. A single threshold makes the face chatter when the
# gaze sits on the boundary, so `enter`/`exit` give it hysteresis, and a short
# dwell stops a one-frame spike from changing the expression.
LOOK_ENTER = 0.35    # inside this, we are looking at the robot
LOOK_EXIT = 0.80     # beyond this, we are definitely not


class ExpressionPolicy:
    """Map (face present, gaze magnitude) to an expression, with hysteresis."""

    def __init__(self, enter: float = LOOK_ENTER, exit_: float = LOOK_EXIT,
                 dwell: float = 0.4):
        if exit_ < enter:
            raise ValueError("look-exit must be >= look-enter, or the state "
                             "machine can oscillate")
        self.enter = enter
        self.exit = exit_
        self.dwell = dwell
        self._current = "neutral"
        self._candidate = "neutral"
        self._since = 0.0
        # THE SAME JUDGEMENT, PUBLISHED. `looking` is true exactly while the state
        # machine below would have the face smiling: someone in front of the robot
        # with their head pointed at it, with the hysteresis and dwell applied.
        # It exists because a second consumer wants that verdict for something other
        # than the face - the gaze bar the session conductor fills and scores - and
        # a second consumer must not re-derive it from raw gaze with its own
        # thresholds. Two definitions of "looking at the robot" that disagree would
        # be a bar that fills while the face disagrees, or a stage scored differently
        # from the way it was drawn.
        self.looking = False

    def _target(self, has_face: bool, mag: float) -> str:
        if not has_face:
            return "neutral"
        # Hysteresis: being happy needs mag < enter, but once happy it takes
        # mag > exit to lose it. Between the two, whatever we already are wins.
        if self._current == "happy":
            return "happy" if mag <= self.exit else "angry"
        return "happy" if mag < self.enter else "angry"

    def update(self, has_face: bool, gx: float, gy: float, now: float) -> str:
        mag = max(abs(gx), abs(gy))

        # Losing the face is INSTANT - a robot still grinning at an empty room
        # is worse than one that flickers. Everything else waits out the dwell.
        if not has_face:
            self._current = self._candidate = "neutral"
            self._since = now
            self.looking = False
            return self._current

        want = self._target(has_face, mag)
        if want != self._candidate:
            self._candidate = want
            self._since = now
        if self._candidate != self._current and now - self._since >= self.dwell:
            self._current = self._candidate
        self.looking = (self._current == "happy")
        return self._current


# --------------------------------------------------------------------------
# Subject policy: is this face someone in front of the robot, or just a face?
# --------------------------------------------------------------------------
# A detector answers "is there a face in view". That is NOT the question this
# robot needs answered. A face on a television in the next room, a photo on a
# wall, a person passing at the far end of a corridor - all produce confident,
# persistent detections. Measured on this rig with an empty room, they reached
# confidence 0.89 and persisted for tens of seconds, and one of them made the
# greeter play the entire introduction to nobody.
#
# So the size test below is really a DISTANCE test:
#   empty room (a screen across the room)   0.009 - 0.080 of the frame width
#   a person seated at conversation range   0.190 - 0.231
# A threshold at 0.12 sits about 1.5x above the worst false positive and 1.6x
# below a seated person, so the gate admits someone within roughly 2 metres.
#
# TWO thresholds, not one, for the same reason ExpressionPolicy has them: a single
# threshold chatters when someone sits on the boundary, and everything downstream
# - the eyes, the expression, the greeting - flickers along with it.

SUBJECT_MIN_W = 0.12    # enter: face must reach this fraction of frame width
SUBJECT_EXIT_W = 0.08   # exit: stay a subject until it drops below this
SUBJECT_MIN_CX = 0.10   # horizontal bounds - loose on purpose, see below
SUBJECT_MAX_CX = 0.90


def face_geometry(face, frame_w: int, frame_h: int) -> dict:
    """Box and centre of one detection, as 0..1 fractions of the frame.

    Computed here rather than inside estimate() because estimate() returns early
    when landmarks are missing - which left the box unavailable on exactly the
    frames where a box was all there was to go on.
    """
    w = _unit(face.getWidth(), frame_w)
    h = _unit(face.getHeight(), frame_h)
    return {
        "box_w": round(w, 4),
        "box_h": round(h, 4),
        "area": round(w * h, 6),
        "cx": round(_unit(face.getCenterX(), frame_w), 4),
        "cy": round(_unit(face.getCenterY(), frame_h), 4),
        "conf": round(float(face.confidence), 3),
    }


class SubjectPolicy:
    """Decide whether a detection is someone the robot should interact with.

    The horizontal bounds are loose on purpose, and there is deliberately NO
    VERTICAL bound at all. The camera sits below eye level, so a seated person's
    face lands ABOVE the frame centre - a fixed offset of about 0.12 that has
    nothing to do with being off-centre. A symmetric off-centre test therefore
    punishes the person it exists to notice: measured, a seated person reached
    offcentre 0.39 while the empty room's false positives sat at 0.22, so the two
    ranges overlap and the measure has no discriminating power at all.

    Horizontally, the false positives that matter - the LARGEST ones, up to 0.08 -
    all sat hard against the frame edge (cx 0.03-0.12), while a seated person never
    went below 0.14. That, and only that, is what the loose horizontal bound is
    for.

    Confidence is deliberately NOT used as a criterion: measured here, false
    positives and real subjects overlap almost completely (median 0.738 vs
    0.767). A confident false positive is exactly what a detector produces on a
    face-shaped thing.
    """

    def __init__(self, min_w: float = SUBJECT_MIN_W, exit_w: float = SUBJECT_EXIT_W,
                 min_cx: float = SUBJECT_MIN_CX, max_cx: float = SUBJECT_MAX_CX,
                 enabled: bool = True):
        if exit_w >= min_w:
            raise ValueError("subject-exit-w must be < subject-min-w, or the "
                             "verdict can oscillate")
        self.min_w = min_w
        self.exit_w = exit_w
        self.min_cx = min_cx
        self.max_cx = max_cx
        self.enabled = enabled
        self.active = False

    def update(self, geoms: "list[dict]") -> bool:
        """Verdict for this frame, from every detection in it.

        Takes the whole list, not one candidate: the hysteresis is a property of
        the frame's verdict, so testing each face in turn would advance the state
        machine once per face.
        """
        if not self.enabled:
            return bool(geoms)
        if not geoms:
            # Drop the hysteresis rather than keep it warm: a stale "active" would
            # let a distant speck through the moment it reappeared.
            self.active = False
            return False
        candidates = [g for g in geoms if self.min_cx <= g["cx"] <= self.max_cx]
        if not candidates:
            self.active = False
            return False
        biggest = max(g["box_w"] for g in candidates)
        limit = self.exit_w if self.active else self.min_w
        self.active = biggest >= limit
        return self.active

    def in_bounds(self, geom: dict) -> bool:
        return self.min_cx <= geom["cx"] <= self.max_cx


def write_state_atomic(path: pathlib.Path, gx: float, gy: float,
                       expression: str | None = None) -> None:
    """Merge our gaze into the shared state file without clobbering other keys.

    Read-modify-write, then rename. Renaming is atomic within a filesystem, so
    face.py's reader never observes a partial file.
    """
    data: dict = {}
    try:
        with open(path, "r") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        # Missing or malformed: face.py treats this the same way, so starting
        # from an empty dict is correct rather than an error.
        data = {}

    data["gaze"] = {"x": round(gx, 4), "y": round(gy, 4)}
    # `expression` is only touched when the caller asks for it, so gaze-only
    # operation leaves whatever face.py set alone.
    if expression is not None:
        data["expression"] = expression

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def frame_pixels(frame):
    """A frame's pixels as a numpy array, or None. No camera, no crash.

    `getCvFrame()` is the fast path and is right for the frames this pipeline
    delivers, but it is not guaranteed for every frame a device can hand over, and
    the loop above already tolerates a failure there (it falls back to the model's
    input size). So the raw bytes are the fallback: they are always host-readable.
    Returning None rather than raising is deliberate - the caller is a monitor.
    """
    if frame is None:
        return None
    try:
        img = frame.getCvFrame()
        if img is not None:
            return img
    except Exception:  # noqa: BLE001
        pass
    try:
        import cv2
        import numpy as np

        h, w = int(frame.getHeight()), int(frame.getWidth())
        stride = int(getattr(frame, "getStride", lambda: w)() or w)
        flat = np.frombuffer(frame.getData(), dtype="uint8")
        kind = frame.getType()
        if kind == dai.ImgFrame.Type.GRAY8:
            rows = flat[:h * stride].reshape(h, stride)[:, :w]
            return rows.copy()
        if kind == dai.ImgFrame.Type.BGR888i:
            rows = flat[:h * stride].reshape(h, stride)[:, :w * 3]
            return rows.reshape(h, w, 3).copy()
        if kind == dai.ImgFrame.Type.BGR888p:
            # Planar: three whole planes, not interleaved pixels.
            planes = flat[:3 * h * w].reshape(3, h, w)
            return np.ascontiguousarray(planes.transpose(1, 2, 0))
        if kind == dai.ImgFrame.Type.NV12:
            rows = flat.reshape(h * 3 // 2, stride)[:, :w]
            return cv2.cvtColor(rows.copy(), cv2.COLOR_YUV2BGR_NV12)
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------
# The operator's view of the detector
# --------------------------------------------------------------------------
# BGR, because cv2. The green is the listening pill's green: "the robot can see
# you" should look the same on the projector and in a monitor.
STREAM_SUBJECT_INK = (110, 208, 142)     # rgb(142, 208, 110)
STREAM_REJECT_INK = (60, 161, 216)       # rgb(216, 161, 60), amber
STREAM_OTHER_INK = (160, 144, 139)       # rgb(139, 144, 160)


def annotate_frame(img, dets, chosen, verdict: str, subject: bool,
                   extra: str = "") -> object:
    """Draw what the detector found onto a frame, and return it.

    AN ANNOTATED FRAME IS THE WHOLE POINT. A bare picture of the room cannot show
    whether the robot is READING it: the box says which face was taken, the five
    circles are what the head pose is derived from, and the colour is the verdict
    the rest of the robot acts on. Green for "someone is in front of me", amber
    for a face the subject gate rejected (a poster, a face on a screen, a
    passer-by at the frame edge), grey for a raw detector hit with no verdict yet.

    Coordinates from the detector are 0..1 fractions, so the same drawing is
    correct at any frame size - the frame this is handed may be the network's own
    640x640 passthrough rather than the camera's native size.
    """
    import cv2

    if img is None:
        return None
    out = img.copy()
    h, w = out.shape[:2]
    for d in dets:
        x0, y0 = int(d.xmin * w), int(d.ymin * h)
        x1, y1 = int(d.xmax * w), int(d.ymax * h)
        picked = d is chosen
        cv2.rectangle(out, (x0, y0), (x1, y1),
                      (STREAM_SUBJECT_INK if subject else STREAM_REJECT_INK)
                      if picked else STREAM_OTHER_INK,
                      2 if picked else 1)
        if picked:
            for (kx, ky, _c) in landmarks_of(d):
                cv2.circle(out, (int(kx * w), int(ky * h)), 4,
                           STREAM_SUBJECT_INK if subject else STREAM_REJECT_INK, -1)
    # The verdict, and the numbers behind it, along the top. Without a backdrop
    # this is unreadable over a bright scene, so the text is drawn twice: once as
    # a dark outline and once in the light ink.
    lines = [verdict] + ([extra] if extra else [])
    for i, text in enumerate(lines):
        org = (12, 30 + i * 26)
        for ink, thick in ((0, 4), (1, 1)):
            colour = (10, 8, 14) if ink == 0 else (227, 214, 209)
            cv2.putText(out, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour,
                        thick, cv2.LINE_AA)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fps", type=float, default=12.0,
                    help="inference rate cap (OAK-D Lite has an internal limit; "
                         "SCRFD 640 is ~7 fps measured, so asking for 30 just "
                         "queues frames)")
    ap.add_argument("--state", default=os.environ.get("FACE_STATE",
                                                     "/run/face/state.json"),
                    help="face state file to write gaze into")
    ap.add_argument("--status", default=None,
                    help="write a JSON status file here each update. NOT just a "
                         "debugging aid: the greeter, the session conductor (whose "
                         "gaze bar fills on its `looking` field) and the frame feed "
                         "all read it, and it exists so they never open the camera. "
                         "Written every frame, including when nobody is there")
    ap.add_argument("--stream", type=int,
                    default=int(os.environ.get("GAZE_STREAM") or 0),
                    metavar="PORT",
                    help="serve the frames this process is ALREADY reading, "
                         "annotated with what was detected, on 0.0.0.0:PORT for a "
                         "browser (0 = off, or $GAZE_STREAM). The OAK-D is exclusive, "
                         "so this is the only way to watch the camera DURING a run: "
                         "camera/viewer.py needs the device for itself and stops gaze "
                         "to get it. Nothing is encoded unless someone is watching, so "
                         "an unwatched robot pays nothing for this")
    ap.add_argument("--stream-fps", type=float, default=4.0,
                    help="most frames per second to publish to --stream (default "
                         "%(default)s). Detection runs faster than a monitor needs, "
                         "and the JPEG encode is this feature's only real cost)")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and log, but write NOTHING to the face state")
    ap.add_argument("--dump-landmarks", action="store_true",
                    help="print raw landmark coordinates, then exit")
    ap.add_argument("--confidence", type=float, default=0.5,
                    help="detection confidence threshold")
    # Gains: how strongly each signal moves the eyes. Defaults are deliberately
    # modest - an over-driven gaze reads as manic.
    ap.add_argument("--center-gain", type=float, default=0.9)
    ap.add_argument("--yaw-gain", type=float, default=1.6)
    ap.add_argument("--pitch-gain", type=float, default=1.2)
    ap.add_argument("--smooth", type=float, default=0.25,
                    help="EMA alpha, 0..1 (lower = smoother, laggier)")
    ap.add_argument("--deadband", type=float, default=0.015)
    ap.add_argument("--lost-grace", type=float, default=1.5,
                    help="seconds without a SUBJECT before gaze recentres")
    # Subject gate. See SubjectPolicy for why these numbers are what they are:
    # they were measured against an empty room, not guessed. `None` means "not given
    # on the command line", which is how a conf value can win over the built-in
    # without a flag silently beating it. See the resolution block below.
    ap.add_argument("--subject-min-w", type=float, default=None,
                    help="frame-width fraction a face must reach to count as "
                         "someone in front of the robot (default: look.conf, else "
                         "%s). A distance gate in disguise - see SubjectPolicy"
                         % SUBJECT_MIN_W)
    ap.add_argument("--subject-exit-w", type=float, default=None,
                    help="stay a subject until the face shrinks below this "
                         "(default: look.conf, else %s)" % SUBJECT_EXIT_W)
    ap.add_argument("--subject-min-cx", type=float, default=SUBJECT_MIN_CX,
                    help="leftmost horizontal face centre that still counts "
                         "(default %(default)s)")
    ap.add_argument("--subject-max-cx", type=float, default=SUBJECT_MAX_CX,
                    help="rightmost horizontal face centre that still counts "
                         "(default %(default)s)")
    ap.add_argument("--no-subject-gate", action="store_true",
                    help="treat EVERY detected face as a subject - the old "
                         "behaviour, kept for A/B comparison")
    ap.add_argument("--yaw-sign", type=float, default=1.0, choices=[1.0, -1.0],
                    help="flip if the eyes look the wrong way when you turn")
    # Expression policy. `--no-expression` leaves the expression key alone
    # entirely, for gaze-only operation or A/B testing. Note the `looking`
    # verdict is published either way: it is not the expression, it is the
    # judgement behind it, and the gaze bar is scored on it.
    ap.add_argument("--no-expression", action="store_true",
                    help="drive gaze only; do not touch the expression")
    # Where the looking thresholds and the subject gate come from. These belong to
    # the ARRANGEMENT - camera pose, aim, seat, screen - so they are measured per rig
    # by robot/perception/calibrate-look.py and read from a file, not fixed here.
    ap.add_argument("--look-conf",
                    default=os.environ.get("FACE_LOOK_CONF")
                    or str((lookconf.DEFAULT_CONF if lookconf else HERE / "look.conf")),
                    help="the calibration file these two thresholds and the subject "
                         "gate are read from (default %(default)s, or $FACE_LOOK_CONF). "
                         "RE-READ whenever it changes, so a re-fit is live with no "
                         "restart. Absent is fine: the built-in values below are used")
    ap.add_argument("--no-look-conf", action="store_true",
                    help="ignore the calibration file entirely and use the built-ins, "
                         "for an A/B comparison against a measured rig")
    ap.add_argument("--look-enter", type=float, default=None,
                    help="gaze magnitude at or below which we count as looking at the "
                         "robot (default: look.conf, else %s)" % LOOK_ENTER)
    ap.add_argument("--look-exit", type=float, default=None,
                    help="gaze magnitude above which we are definitely not looking "
                         "(default: look.conf, else %s)" % LOOK_EXIT)
    ap.add_argument("--expression-dwell", type=float, default=0.4,
                    help="seconds a new expression must hold before it is adopted")
    ap.add_argument("--expression-interval", type=float, default=0.6,
                    help="min seconds between expression writes. face.py eases "
                         "toward its target, and it re-reads on mtime change, so "
                         "writing faster than the transition just re-aims a "
                         "half-finished animation every time")
    args = ap.parse_args()

    state_path = pathlib.Path(args.state)

    pipeline = dai.Pipeline()
    camera = pipeline.create(dai.node.Camera).build()
    # Pass the camera NODE: ParsingNeuralNetwork reads the archive's required
    # input size (640x640) and wires the camera to it. Passing a fixed-size
    # output instead silently skips every inference.
    nn = pipeline.create(ParsingNeuralNetwork).build(camera, MODEL, fps=args.fps)

    parser = nn.getParser()
    try:
        parser.setConfidenceThreshold(args.confidence)
    except Exception as exc:  # noqa: BLE001
        print(f"  note: could not set confidence threshold: {exc}")

    result_q = nn.out.createOutputQueue()
    frame_q = nn.passthrough.createOutputQueue()

    smoother = Smoother(args.smooth, args.deadband)

    # Where the looking verdict and the subject gate actually come from: an explicit
    # flag beats the calibration file, which beats the built-in. The SOURCE is printed
    # at start-up and on every reload, because "which number is live" has to be
    # answerable from the journal without reading the unit, the conf and this file.
    def read_conf_values() -> dict:
        if args.no_look_conf or lookconf is None:
            return {}
        return lookconf.read_numbers(args.look_conf)

    def stamp(path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def resolved(key, flag, builtin):
        """flag > look.conf > built-in, and say which won.

        The precedence itself lives in lookconf (one implementation, shared with
        calibrate-look.py's reader); this wrapper only covers the case where
        lookconf.py is missing from the device entirely.
        """
        if lookconf is None:
            return ((float(flag), "flag") if flag is not None
                    else (float(builtin), "built-in"))
        return lookconf.resolve(flag, read_conf_values(), key)

    live: "dict[str, tuple[float, str]]" = {
        "LOOK_ENTER": resolved("LOOK_ENTER", args.look_enter, LOOK_ENTER),
        "LOOK_EXIT": resolved("LOOK_EXIT", args.look_exit, LOOK_EXIT),
        "SUBJECT_MIN_W": resolved("SUBJECT_MIN_W", args.subject_min_w, SUBJECT_MIN_W),
        "SUBJECT_EXIT_W": resolved("SUBJECT_EXIT_W", args.subject_exit_w, SUBJECT_EXIT_W),
    }
    # A key given as a flag is PINNED for the life of the process: a reload must not
    # quietly retune a value someone set on the command line for an A/B.
    pinned = {k for k in live if live[k][1] == "flag"}

    def apply_values():
        """Push the resolved values onto the two policies."""
        policy.enter = live["LOOK_ENTER"][0]
        policy.exit = live["LOOK_EXIT"][0]
        if not args.no_subject_gate:
            subject_policy.min_w = live["SUBJECT_MIN_W"][0]
            subject_policy.exit_w = live["SUBJECT_EXIT_W"][0]

    policy = ExpressionPolicy(live["LOOK_ENTER"][0], live["LOOK_EXIT"][0],
                              args.expression_dwell)
    subject_policy = SubjectPolicy(live["SUBJECT_MIN_W"][0],
                                   live["SUBJECT_EXIT_W"][0],
                                   args.subject_min_cx, args.subject_max_cx,
                                   enabled=not args.no_subject_gate)
    conf_state = {"stamp": stamp(args.look_conf)}

    def reload_conf(now) -> None:
        """Re-read look.conf when it changes, so a re-fit needs no restart.

        The whole reason the values are in a file that is WATCHED rather than in the
        unit file: calibrate-look.py writes the new numbers, and gaze applies them at
        the next check. A value that is written but not applied is indistinguishable
        from one that was never written, which is how a freshly measured rig ends up
        being tested against the old thresholds.
        """
        lc = lookconf
        if args.no_look_conf or lc is None:
            return
        if now - conf_state.get("at", 0.0) < 1.0:
            return
        conf_state["at"] = now
        fresh = stamp(args.look_conf)
        if fresh == conf_state["stamp"]:
            return
        conf_state["stamp"] = fresh
        values = read_conf_values()
        changes = []
        for key in live:
            if key in pinned:
                continue
            value = float(values.get(key, lc.BUILTIN[key]))
            if value != live[key][0]:
                changes.append("%s %g -> %g"
                               % (key.lower().replace("_", "-"), live[key][0], value))
                live[key] = (value, "conf" if key in values else "built-in")
        if not changes:
            return
        old_enter, old_exit = policy.enter, policy.exit
        try:
            lc.validate(live["LOOK_ENTER"][0], live["LOOK_EXIT"][0])
            if live["SUBJECT_EXIT_W"][0] >= live["SUBJECT_MIN_W"][0]:
                raise ValueError("subject-exit-w must stay below subject-min-w")
        except ValueError as exc:
            # Refuse the whole file rather than half of it: a conf that inverts one
            # pair would make that verdict oscillate, and "some of it took" is the
            # worst state to debug from.
            live["LOOK_ENTER"] = (old_enter, live["LOOK_ENTER"][1])
            live["LOOK_EXIT"] = (old_exit, live["LOOK_EXIT"][1])
            print(f"gaze: look.conf REJECTED ({exc}) - keeping "
                  f"{old_enter:g}/{old_exit:g}", flush=True)
            return
        apply_values()
        print(f"gaze: look.conf reloaded (no restart) - {'; '.join(changes)}; now "
              f"look-enter {policy.enter:g} look-exit {policy.exit:g} "
              f"subject-min-w {subject_policy.min_w:g}", flush=True)

    stop = {"now": False}

    def on_signal(signum, _frame):
        print(f"\nsignal {signum}: recentring gaze and stopping", flush=True)
        stop["now"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    pipeline.start()
    print(f"gaze: model={MODEL} fps<={args.fps:g} "
          f"USB={pipeline.getDefaultDevice().getUsbSpeed().name}")
    print(f"gaze: state={'DRY RUN - not writing' if args.dry_run else state_path}")
    # Which numbers the looking verdict is actually running on, and where each came
    # from. Without this line the journal cannot answer "is the calibration live?",
    # and a measured rig silently tested against the built-ins looks like a rig that
    # measures wrong.
    print("gaze: looking - look-enter %g (%s)  look-exit %g (%s)  subject-min-w %g (%s)"
          % (live["LOOK_ENTER"][0], live["LOOK_ENTER"][1],
             live["LOOK_EXIT"][0], live["LOOK_EXIT"][1],
             live["SUBJECT_MIN_W"][0], live["SUBJECT_MIN_W"][1]))
    print("gaze: look.conf %s%s (watched: a re-fit applies without a restart)"
          % (args.look_conf, "" if os.path.exists(args.look_conf) else "  ABSENT - "
             "running on the built-ins above"))

    # The frame feed. Lazily imported and wrapped, because a monitoring feature
    # must never be able to take the eyes down: if the port is busy or anything
    # else about it fails, gaze carries on and says so once.
    feed = None
    stream_error_shown = False
    last_stream_t = 0.0
    if args.stream:
        try:
            import framefeed
            feed = framefeed.start(args.stream, args.status)
            print(f"gaze: serving annotated frames and the status on "
                  f"0.0.0.0:{args.stream} (at most {args.stream_fps:g} fps, and only "
                  f"while somebody is watching)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"gaze: NOT serving frames on port {args.stream}: {exc}",
                  flush=True)

    # The newest frame the loop has drained, as pixels. WHY THIS CACHE EXISTS: the
    # device sends the image and the detections as SEPARATE messages, and this loop
    # pops the image queue on EVERY spin - it has to, or the queue backs up - so by
    # the time a detection result is in hand, the frame it was computed from has
    # usually already been popped and dropped by an earlier spin. A publisher that
    # went back to frame_q would find it empty. One frame of lag at worst, and in
    # practice none: the image is sent first, so the cached pixels are the detection's
    # own frame.
    latest = {"img": None}

    def note_frame(frame) -> None:
        """Keep a drained frame's pixels, if anyone is watching.

        Called on every spin, so the copy is gated on the same `wanted()` test as the
        encode: an unwatched robot does no work here at all.
        """
        if feed is None or frame is None or not feed.wanted():
            return
        img = frame_pixels(frame)
        if img is not None:
            latest["img"] = img.copy()      # a host copy: the device recycles buffers
            try:
                latest["seq"] = frame.getSequenceNum()
            except Exception:  # noqa: BLE001
                pass

    def publish_frame(dets, chosen, verdict: str, subject: bool,
                      expr: str, extra: str = "") -> None:
        """Hand an annotated frame to the feed, if anyone is watching.

        Skipped entirely when nobody is: the JPEG encode is this feature's only
        real cost, and a robot with no operator watching should not pay it. Errors
        are reported once and then swallowed, for the same reason the import is
        wrapped - watching the robot must not be able to break it.
        """
        nonlocal last_stream_t, stream_error_shown
        if feed is None or not feed.wanted():
            return
        now_m = time.monotonic()
        if now_m - last_stream_t < 1.0 / max(args.stream_fps, 0.2):
            return
        last_stream_t = now_m
        try:
            img = latest["img"]
            if img is None:
                # No pixels yet. Counted rather than silent: a stream that connects
                # and carries nothing is the failure that looks like success.
                feed.store.set_meta(skipped=feed.store.meta().get("skipped", 0) + 1)
                return
            import cv2
            out = annotate_frame(img, dets, chosen, verdict, subject, extra)
            if out is None:
                return
            ok, buf = cv2.imencode(".jpg", out,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            if ok:
                feed.publish(buf.tobytes(), verdict=verdict, subject=subject,
                             faces=len(dets), expression=expr,
                             frame_seq=latest.get("seq"))
        except Exception as exc:  # noqa: BLE001
            if not stream_error_shown:
                stream_error_shown = True
                line = f"frame feed dropped a frame: {exc}"
                print(f"gaze: {line}; still running", flush=True)
                feed.store.set_meta(error=line[:200])



    # Time of the last frame in which there was someone to look at. Decaying from
    # the last SUBJECT, not the last detection, is what stops the eyes lingering
    # on a face on a screen after the person in front has walked away.
    last_subject_t = time.monotonic()
    last_write_t = 0.0
    last_expr_write_t = 0.0
    last_expr = "__unset__"      # sentinel: forces one expression write at start
    frames = 0
    hits = 0
    subject_hits = 0
    t0 = time.monotonic()
    dumped = False

    def commit(gx: float, gy: float, expr: str, now: float) -> None:
        """Publish gaze and/or expression to the face, each throttled separately.

        Gaze wants to be responsive, so it goes out at ~10 Hz. The expression
        does not: face.py *eases* toward its target over roughly half a second,
        and it re-reads the file whenever the mtime changes. Rewriting an
        unchanged expression 10 times a second therefore re-aims a half-finished
        transition on every write, and the animation never completes. So the
        expression is written only when it actually changes, and no faster than
        --expression-interval.
        """
        if args.dry_run:
            return
        nonlocal last_write_t, last_expr_write_t, last_expr

        want_expr = None if args.no_expression else expr
        gaze_due = (now - last_write_t) > 0.1
        expr_due = (want_expr is not None
                    and want_expr != last_expr
                    and (now - last_expr_write_t) > args.expression_interval)
        if not (gaze_due or expr_due):
            return

        if gaze_due:
            last_write_t = now
        if expr_due:
            last_expr_write_t = now
            last_expr = want_expr
        write_state_atomic(state_path, gx, gy, want_expr)

    def emit_status(gx: float, gy: float, n_faces: int, dbg: dict | None,
                    has_face: bool, expr: str, subject: bool = False,
                    geom: dict | None = None, looking: bool = False) -> None:
        """Write the status file, ALWAYS - including when no face is present.

        Skipping the write when there is no face leaves the file showing the
        last face seen, which is indistinguishable from a live reading. During
        calibration that is actively misleading, so `has_face` and a fresh `t`
        are written on every state.

        Key naming matters here: `yaw_raw` is the UNSMOOTHED measurement and the
        smoothed result is the top-level `gaze`. The two are exposed separately
        so neither can be mistaken for the other.

        `looking` is the strict verdict the session conductor's gaze bar runs on -
        someone in front of the robot with their head pointed at it, i.e. exactly
        the condition that makes the expression `happy`. See ExpressionPolicy.
        """
        if not args.status:
            return
        try:
            # ATOMIC: write beside it, then rename. A reader can catch this file
            # mid-write - the conductor polls it ~10x a second and treats an
            # unreadable one as "no data" - and a plain open("w") TRUNCATES first, so
            # the reader sees half a JSON document and drops the sample. That is rare
            # (one read in a few hundred, measured 5 November) and it costs a tenth of
            # a second of score, but it is the only torn read left in the run. Renaming
            # a complete file is atomic within a filesystem, and this process is the
            # only writer, so a fixed temporary name is enough.
            tmp = args.status + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({
                    "t": time.time(),
                    "has_face": has_face,
                    # A face was detected SOMEWHERE. `subject` is the question
                    # the rest of the robot actually asks: is someone in front of
                    # it? Consumers should prefer `subject`; `has_face` remains the
                    # raw detector verdict.
                    "subject": subject,
                    # ... and `looking` is the narrower question again: someone in
                    # front of it AND looking at it. Published here, from the one
                    # state machine that decides it, so no consumer has to invent
                    # thresholds of its own.
                    "looking": bool(looking),
                    "faces": n_faces,
                    # First-class geometry, so a consumer never has to read a
                    # field called `debug` to find out how big or where a face is.
                    # Present whenever has_face is, including on frames with no
                    # landmarks.
                    "face": geom,
                    "gaze": {"x": round(gx, 4), "y": round(gy, 4)},
                    "gaze_mag": round(max(abs(gx), abs(gy)), 4),
                    "expression": None if args.no_expression else expr,
                    "expression_enabled": not args.no_expression,
                    # Convenience copies of the two headline measurements, so
                    # `jq .yaw_raw` works without knowing the nesting.
                    "yaw_raw": round(dbg["yaw_raw"], 4) if dbg else None,
                    "pitch_raw": round(dbg["pitch_raw"], 4) if dbg else None,
                    "debug": dbg,
                    "fps_avg": round(frames / max(time.monotonic() - t0, 1e-6), 2),
                    "hit_rate": round(hits / max(frames, 1), 3),
                    "subject_rate": round(subject_hits / max(frames, 1), 3),
                    "dry_run": args.dry_run,
                }, fh, indent=1)
            os.replace(tmp, args.status)
        except OSError:
            pass

    try:
        while not stop["now"]:
            now = time.monotonic()
            reload_conf(now)
            result = result_q.tryGet()
            frame = frame_q.tryGet()
            note_frame(frame)

            if result is None:
                time.sleep(0.002)
                continue

            frames += 1
            dets = list(getattr(result, "detections", []) or [])

            if args.dump_landmarks and dets and not dumped:
                dumped = True
                d = pick_face(dets)
                print(f"\nraw face: box=({d.xmin:.3f},{d.ymin:.3f})-"
                      f"({d.xmax:.3f},{d.ymax:.3f}) conf={d.confidence:.3f}"
                      f"   (coordinates are 0..1 fractions of the frame)")
                for i, (x, y, c) in enumerate(landmarks_of(d)):
                    print(f"  kp[{i}] = ({x:.4f}, {y:.4f})  conf={c}")

            if not dets:
                subject_policy.update([])
                if now - last_subject_t > args.lost_grace:
                    gx, gy = smoother.reset()
                    expr = policy.update(False, gx, gy, now)
                    commit(gx, gy, expr, now)
                else:
                    gx, gy = smoother.x, smoother.y
                    expr = policy.update(False, gx, gy, now)
                emit_status(gx, gy, 0, None, has_face=False, expr=expr,
                            looking=policy.looking)
                publish_frame([], None, "nobody in front", False, expr)
                continue

            hits += 1

            fw, fh = 0, 0
            if frame is not None:
                try:
                    fh, fw = frame.getCvFrame().shape[:2]
                except Exception:  # noqa: BLE001
                    pass
            if not fw:
                # Fall back to the model's own input size; only used for
                # normalising the face centre, so any consistent value works.
                fw, fh = 640, 640

            pairs = [(d, face_geometry(d, fw, fh)) for d in dets]
            subject = subject_policy.update([g for _, g in pairs])

            if subject:
                # Look at the biggest face that is inside the horizontal bounds, so
                # a large face on a screen at the frame edge cannot steal the eyes
                # from the person actually sitting in front of the robot.
                in_bounds = [p for p in pairs if subject_policy.in_bounds(p[1])]
                face, geom = max(in_bounds or pairs, key=lambda p: p[1]["area"])
                last_subject_t = now
                subject_hits += 1
                yaw, pitch, dbg = estimate(face, fw, fh, args.yaw_sign)
                gx, gy = combine(yaw, pitch, face, fw, fh, args)
                gx, gy = smoother.update(gx, gy)
                expr = policy.update(True, gx, gy, now)
                commit(gx, gy, expr, now)
                emit_status(gx, gy, len(dets), dbg, has_face=True, subject=True,
                            geom=geom, expr=expr, looking=policy.looking)
                # gaze_mag is defined exactly as the status file defines it, so a
                # number read off the video matches the number in the JSON. The
                # looking verdict is on the frame because it is what the gaze bar
                # fills on, and a bar that is not filling is otherwise
                # indistinguishable from a bar nobody is watching.
                publish_frame(dets, face, "SUBJECT - someone in front", True,
                              expr, f"gaze {gx:+.2f} {gy:+.2f}  "
                                    f"mag {max(abs(gx), abs(gy)):.2f}  "
                                    f"{len(dets)} face(s)  "
                                    f"{'LOOKING' if policy.looking else 'not looking'}")
                continue

            # A face, but not someone in front of the robot: a face on a screen
            # across the room, a poster, a passer-by at distance. Treat it as
            # nobody. Hold the eyes briefly in case it is really a dropout, then
            # look forward and go neutral - because "a face exists somewhere in the
            # world" is no reason to greet an empty room, nor to stare past whoever
            # is not there.
            face, geom = max(pairs, key=lambda p: p[1]["area"])
            if now - last_subject_t > args.lost_grace:
                gx, gy = smoother.reset()
            else:
                gx, gy = smoother.x, smoother.y
            expr = policy.update(False, gx, gy, now)
            commit(gx, gy, expr, now)
            emit_status(gx, gy, len(dets), None, has_face=True, subject=False,
                        geom=geom, expr=expr, looking=policy.looking)
            publish_frame(dets, face, "face seen, gate rejected it", False,
                          expr, f"{len(dets)} face(s), none in front")

    finally:
        # Always release the camera, and leave the face looking forward rather
        # than frozen mid-glance.
        try:
            pipeline.stop()
        except Exception:  # noqa: BLE001
            pass
        if feed is not None:
            # Stop serving before the process goes away, so the port is free for
            # the next start rather than held by a dying thread.
            feed.stop()
        if not args.dry_run:
            try:
                # Reset the EXPRESSION too, not just the gaze. The daemon is
                # going away, so nobody is being looked at - leaving "happy"
                # behind parks a grin on a robot with no camera running, which
                # is the same failure as grinning at an empty room except
                # permanent. Caught by testing: the first version wrote gaze
                # only, and the state file kept expression "happy" after exit.
                write_state_atomic(state_path, 0.0, 0.0,
                                   None if args.no_expression else "neutral")
            except Exception:  # noqa: BLE001
                pass

    dt = max(time.monotonic() - t0, 1e-6)
    print(f"gaze: {frames} results in {dt:.1f}s = {frames / dt:.1f} fps, "
          f"{hits} with a face ({hits / max(frames, 1) * 100:.0f}%)")
    if feed is not None:
        print(f"gaze: the frame feed published {feed.store.published} frames, "
              f"served {feed.served} stream(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
