#!/usr/bin/env python3
"""probe-annotate-shapes.py — run the publisher's body against EVERY array shape
getCvFrame() can return, and print the exception when one fails.

probe-annotate.py only ever fed it 3-channel BGR. getCvFrame() returns a 2-D array
for GRAY8 (measured), and cv2's drawing calls are depth-sensitive, so the shape
that never got tested is the one worth testing. No camera needed.

    /home/jetson/robot/camera/.venv/bin/python probe-annotate-shapes.py
"""
import sys
import traceback

import cv2
import numpy as np

sys.path.insert(0, "/home/jetson/robot/perception")
import gaze  # noqa: E402


class Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Kp:
    def __init__(self, x, y, c=0.9):
        self.imageCoordinates = Pt(x, y)
        self.confidence = c


class Det:
    def __init__(self, xmin, ymin, xmax, ymax, conf=0.9, kps=()):
        self.xmin, self.ymin, self.xmax, self.ymax = xmin, ymin, xmax, ymax
        self.confidence = conf
        self._kps = list(kps)

    def getKeypoints(self):
        return self._kps


KPS = [Kp(0.36, 0.42), Kp(0.44, 0.42), Kp(0.40, 0.50), Kp(0.36, 0.58), Kp(0.45, 0.58)]
SUBJECT = Det(0.34, 0.38, 0.47, 0.62, 0.91, KPS)

SHAPES = [
    ("gray 2-D (what GRAY8 gives)", np.full((480, 640), 120, dtype="uint8")),
    ("gray 2-D, uint8, non-contiguous",
     np.full((480, 1280), 120, dtype="uint8")[:, ::2]),
    ("BGR 3-ch (what BGR888i/NV12 give)",
     np.full((480, 640, 3), 120, dtype="uint8")),
    ("BGRA 4-ch (belt and braces)", np.full((480, 640, 4), 120, dtype="uint8")),
]


def publisher_body(img):
    """Exactly what gaze.publish_frame does between getCvFrame() and publish()."""
    out = gaze.annotate_frame(img, [SUBJECT], SUBJECT,
                              "SUBJECT - someone in front", True,
                              "gaze +0.02 -0.01  mag 0.02  1 face(s)")
    if out is None:
        return "returned None (would publish nothing)"
    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    if not ok:
        return "imencode said no"
    return f"published {len(buf)} bytes"


for label, arr in SHAPES:
    try:
        print(f"{label:38s} -> {publisher_body(arr)}")
    except Exception:  # noqa: BLE001
        print(f"{label:38s} -> *** RAISED ***")
        traceback.print_exc(limit=3)

print()
print("and the frame the loop actually hands over, converted twice first:")
import depthai as dai  # noqa: E402

for label, typ in (("GRAY8", dai.ImgFrame.Type.GRAY8),
                   ("BGR888i", dai.ImgFrame.Type.BGR888i),
                   ("BGR888p", dai.ImgFrame.Type.BGR888p)):
    shape = (480, 640) if label == "GRAY8" else (480, 640, 3)
    frame = dai.ImgFrame()
    frame.setCvFrame(np.full(shape, 120, dtype="uint8"), typ)
    try:
        fh, fw = frame.getCvFrame().shape[:2]      # the loop, line 863
        print(f"  {label:8s} loop's conversion -> {fw}x{fh}, "
              f"then publisher's -> {publisher_body(frame.getCvFrame())}")
    except Exception:  # noqa: BLE001
        print(f"  {label:8s} *** RAISED ***")
        traceback.print_exc(limit=3)
