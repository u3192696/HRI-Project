#!/usr/bin/env python3
"""probe-annotate.py — verify the frame annotation by measuring the pixels.

Runs in the camera venv (cv2 + numpy) and needs NO camera: it builds a synthetic
frame and synthetic detections shaped like the ones the detector produces, calls
gaze.annotate_frame() on them, encodes to JPEG exactly as gaze does, decodes it
back, and then measures the result. Drawing code that "looks right" is not
evidence; the pixel positions of the box, the landmark dots and the verdict text
are.

    /home/jetson/robot/camera/.venv/bin/python probe-annotate.py
"""
import sys

import cv2
import numpy as np

sys.path.insert(0, "/home/jetson/robot/perception")
import gaze  # noqa: E402

W = H = 640
failures = []


def check(name, got, want=True):
    ok = got == want
    print("  %-62s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(name)


class Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Kp:
    def __init__(self, x, y, c=0.9):
        self.imageCoordinates = Pt(x, y)
        self.confidence = c


class Det:
    """Shaped like the detector's ImgDetection: 0..1 box, conf, keypoints."""

    def __init__(self, xmin, ymin, xmax, ymax, conf=0.8, kps=()):
        self.xmin, self.ymin, self.xmax, self.ymax = xmin, ymin, xmax, ymax
        self.confidence = conf
        self._kps = list(kps)

    def getKeypoints(self):
        return self._kps

    def getKeypoints2f(self):
        return [k.imageCoordinates for k in self._kps]


def near(c, want, tol=40):
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(c, want))


def count_near(img, x0, y0, x1, y1, want, tol=60):
    """How many pixels in a region match a colour. Used instead of sampling one
    pixel on an edge: cv2's 1-px rectangle stroke lands on one column, and which
    column moves with the coordinate rounding."""
    region = img[max(0, y0):y1, max(0, x0):x1]
    if region.size == 0:
        return 0
    diff = np.abs(region.astype("int16") - np.array(want[::-1], dtype="int16"))
    return int((diff.max(axis=2) <= tol).sum())


def px(img, x, y):
    b, g, r = img[y, x][:3]
    return (int(r), int(g), int(b))


def rgb_of(bgr):
    return (bgr[2], bgr[1], bgr[0])


GREEN = rgb_of(gaze.STREAM_SUBJECT_INK)
AMBER = rgb_of(gaze.STREAM_REJECT_INK)
GREY = rgb_of(gaze.STREAM_OTHER_INK)


def frame():
    img = np.zeros((H, W, 3), dtype="uint8")
    img[:, :] = (30, 20, 15)          # a dark room
    img[200:440, 220:460] = (150, 170, 180)
    return img


def jpeg_roundtrip(img):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    assert ok, "encode failed"
    blob = buf.tobytes()
    out = cv2.imdecode(np.frombuffer(blob, dtype="uint8"), cv2.IMREAD_COLOR)
    return blob, out


def main() -> int:
    kps = [Kp(0.36, 0.42), Kp(0.44, 0.42), Kp(0.40, 0.50), Kp(0.36, 0.58),
           Kp(0.45, 0.58)]
    subject = Det(0.34, 0.38, 0.47, 0.62, 0.91, kps)
    other = Det(0.80, 0.20, 0.86, 0.28, 0.55)      # a face on a screen, far right

    print("== a SUBJECT, drawn green ==")
    out = gaze.annotate_frame(frame(), [subject, other], subject,
                              "SUBJECT - someone in front", True,
                              "0.91  gaze +0.02 -0.01  mag 0.02  2 face(s)")
    blob, img = jpeg_roundtrip(out)
    check("the annotated frame is a valid JPEG",
          gaze.__dict__ is not None and blob[:2] == b"\xff\xd8" and blob[-2:] == b"\xff\xd9")
    # The box: its top edge is at ymin*H, spanning xmin*W..xmax*W. Sample the middle
    # of each edge from OUTSIDE the drawn rectangle's stroke (cv2 draws inside).
    y_top, y_bot = int(0.38 * H), int(0.62 * H)
    x_left, x_right = int(0.34 * W), int(0.47 * W)
    for label, (x, y) in (("top edge", ((x_left + x_right) // 2, y_top + 1)),
                          ("bottom edge", ((x_left + x_right) // 2, y_bot - 1)),
                          ("left edge", (x_left + 1, (y_top + y_bot) // 2)),
                          ("right edge", (x_right - 1, (y_top + y_bot) // 2))):
        got = px(img, x, y)
        print("    %-12s at (%3d,%3d) = rgb%s (want ~%s)" % (label, x, y, got, GREEN))
        check("  the subject box's %s is green" % label, near(got, GREEN, 60))
    # A landmark dot, drawn filled at the keypoint.
    kx, ky = int(0.36 * W), int(0.42 * H)
    got = px(img, kx, ky)
    print("    landmark dot at (%3d,%3d) = rgb%s (want ~%s)" % (kx, ky, got, GREEN))
    check("  the landmark dot is green", near(got, GREEN, 60))
    # The second detection is drawn in grey, not the verdict colour. Counted over
    # its outline rather than sampled at one pixel.
    n_grey = count_near(img, int(0.78 * W), int(0.18 * H), int(0.88 * W),
                        int(0.30 * H), GREY)
    print("    grey pixels on the unchosen box's outline: %d (want ~%s)"
          % (n_grey, GREY))
    check("  an unchosen detection is grey", n_grey > 40)
    check("  and the unchosen box is not the verdict colour",
          count_near(img, int(0.78 * W), int(0.18 * H), int(0.88 * W),
                     int(0.30 * H), GREEN, 40) == 0)
    # The verdict text: light ink in the top band, over the dark outline.
    band = img[0:56, 0:W]
    light = int((band.min(axis=2) > 150).sum())
    print("    light pixels in the top band: %d" % light)
    check("  the verdict text is drawn", light > 200)

    print("\n== a REJECTED face, drawn amber ==")
    out = gaze.annotate_frame(frame(), [other], other,
                              "face seen, gate rejected it", False,
                              "1 face(s), none in front")
    _, img = jpeg_roundtrip(out)
    got = px(img, int(0.80 * W) + 1, int(0.24 * H))
    print("    the box at (%3d,%3d) = rgb%s (want ~%s)"
          % (int(0.80 * W) + 1, int(0.24 * H), got, AMBER))
    check("  a rejected face is amber, not green", near(got, AMBER, 60))
    check("  and it is not the green", not near(got, GREEN, 30))

    print("\n== nothing detected ==")
    out = gaze.annotate_frame(frame(), [], None, "nobody in front", False)
    _, img = jpeg_roundtrip(out)
    band = img[0:56, 0:W]
    print("    light pixels in the top band: %d" % int((band.min(axis=2) > 150).sum()))
    check("  the verdict still says so on screen",
          int((band.min(axis=2) > 150).sum()) > 100)
    check("  no box is drawn anywhere",
          not any(near(px(img, x, y), GREEN, 30) or near(px(img, x, y), AMBER, 30)
                  for x in range(0, W, 7) for y in range(0, H, 7)))

    print("\n== the degenerate cases ==")
    check("  no frame, no crash (returns None)",
          gaze.annotate_frame(None, [subject], subject, "x", True) is None)
    check("  no detections, no crash",
          gaze.annotate_frame(frame(), [], None, "x", False) is not None)
    check("  a detection with no keypoints still draws a box",
          gaze.annotate_frame(frame(), [Det(0.1, 0.1, 0.2, 0.2)], None, "x",
                              False) is not None)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("PASSED: the box, the landmarks and the verdict are where they should be, "
          "in the colour the verdict says.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
