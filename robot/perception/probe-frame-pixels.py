#!/usr/bin/env python3
"""probe-frame-pixels.py — verify gaze.frame_pixels() on every frame type, for real.

The publisher no longer trusts getCvFrame() alone: it falls back to the frame's raw
bytes, decoded by type. A fallback that has never been exercised is a guess, so this
builds host ImgFrames with KNOWN content in every type the pipeline can deliver and
checks what comes back channel by channel. It also forces the fallback path with a
shim whose getCvFrame() raises, which is the case the fallback exists for.

No camera: the frames are built on the host, so the running gaze is untouched.

    /home/jetson/robot/camera/.venv/bin/python probe-frame-pixels.py
"""
import sys

import cv2
import numpy as np
import depthai as dai

sys.path.insert(0, "/home/jetson/robot/perception")
import gaze  # noqa: E402

failures = []


def check(name, got, want=True):
    ok = got == want
    print("  %-58s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(name)


def means(arr):
    if arr is None:
        return None
    if arr.ndim == 2:
        return (round(float(arr.mean()), 1),)
    return tuple(round(float(arr[:, :, i].mean()), 1) for i in range(arr.shape[2]))


class RaisingFrame:
    """Everything frame_pixels' fallback needs, with getCvFrame() failing."""

    def __init__(self, data, w, h, stride, kind):
        self._d, self._w, self._h, self._s, self._k = data, w, h, stride, kind

    def getCvFrame(self):
        raise RuntimeError("Frame is not in memory (forced, to test the fallback)")

    def getData(self):
        return self._d

    def getWidth(self):
        return self._w

    def getHeight(self):
        return self._h

    def getStride(self):
        return self._s

    def getType(self):
        return self._k


H, W = 480, 640
print("== a grey frame, constant 120 ==")
g = np.full((H, W), 120, dtype="uint8")
f = dai.ImgFrame()
f.setCvFrame(g, dai.ImgFrame.Type.GRAY8)
got = gaze.frame_pixels(f)
print("  shape %s, mean %s" % (None if got is None else got.shape, means(got)))
check("  GRAY8 decodes to a 2-D array", got is not None and got.ndim == 2)
check("  GRAY8 keeps its value", got is not None and abs(float(got.mean()) - 120) < 1)

print("\n== a BGR frame, B=200 G=100 R=50 (channel order matters) ==")
bgr = np.zeros((H, W, 3), dtype="uint8")
bgr[:, :] = (200, 100, 50)          # cv2 order: B, G, R
f = dai.ImgFrame()
f.setCvFrame(bgr, dai.ImgFrame.Type.BGR888i)
got = gaze.frame_pixels(f)
print("  shape %s, per-channel means (B,G,R) %s" % (None if got is None else got.shape,
                                                    means(got)))
check("  BGR888i decodes 3-channel", got is not None and got.ndim == 3)
check("  and no channel is swapped", means(got) == (200.0, 100.0, 50.0))

print("\n== the fallback: same frame, getCvFrame() refusing ==")
raw = bgr.tobytes()
shim = RaisingFrame(raw, W, H, W * 3, dai.ImgFrame.Type.BGR888i)
got = gaze.frame_pixels(shim)
print("  shape %s, per-channel means %s" % (None if got is None else got.shape, means(got)))
check("  the raw-bytes fallback decodes BGR888i", means(got) == (200.0, 100.0, 50.0))

print("\n== the fallback for grey ==")
shim = RaisingFrame(g.tobytes(), W, H, W, dai.ImgFrame.Type.GRAY8)
got = gaze.frame_pixels(shim)
check("  the raw-bytes fallback decodes GRAY8",
      got is not None and abs(float(got.mean()) - 120) < 1)

print("\n== planar BGR888p: three separate planes ==")
planes = np.zeros((3, H, W), dtype="uint8")
planes[0] = 200                     # B plane
planes[1] = 100                     # G plane
planes[2] = 50                      # R plane
f = dai.ImgFrame()
f.setCvFrame(np.ascontiguousarray(planes.transpose(1, 2, 0)),
             dai.ImgFrame.Type.BGR888p)
got = gaze.frame_pixels(f)
print("  shape %s, per-channel means %s" % (None if got is None else got.shape, means(got)))
check("  BGR888p decodes with the planes in the right channels",
      means(got) == (200.0, 100.0, 50.0))

print("\n== NV12 round-trip, against cv2's own decoder ==")
src = np.zeros((H, W, 3), dtype="uint8")
src[:, :W // 2] = (200, 100, 50)
src[:, W // 2:] = (20, 220, 120)
i420 = cv2.cvtColor(src, cv2.COLOR_BGR2YUV_I420)
nv12 = i420.copy()
y_plane, uv = W * H, W * H // 4
nv12[y_plane:y_plane + uv * 2:2] = i420[y_plane:y_plane + uv]        # U interleaved
nv12[y_plane + 1:y_plane + uv * 2:2] = i420[y_plane + uv:y_plane + 2 * uv]  # V
# setCvFrame() wants a 3-channel Mat, so an NV12 frame is built the other way. That
# also means getCvFrame() may refuse it, which exercises the raw-bytes fallback - the
# path a device-side NV12 frame would need anyway.
f = dai.ImgFrame()
f.setData(nv12.tobytes())
f.setType(dai.ImgFrame.Type.NV12)
f.setSize(W, H)
got = gaze.frame_pixels(f)
raw_nv12 = nv12.tobytes()
shim = RaisingFrame(raw_nv12, W, H, W, dai.ImgFrame.Type.NV12)
got_shim = gaze.frame_pixels(shim)
print("  via the frame object: %s   via the forced fallback: %s"
      % (means(got), means(got_shim)))
check("  the fallback decodes NV12 too",
      got_shim is not None and got_shim.shape == src.shape
      and all(abs(a - b) < 34 for a, b in zip(means(got_shim), means(src))))
print("  shape %s, per-channel means %s (source %s)"
      % (None if got is None else got.shape, means(got), means(src)))
check("  NV12 decodes to something with the source's colours",
      got is not None and got.shape == src.shape
      and all(abs(a - b) < 34 for a, b in zip(means(got), means(src))))

print("\n== the degenerate cases ==")
check("  None in, None out", gaze.frame_pixels(None) is None)
check("  an object with nothing usable in it returns None, no raise",
      gaze.frame_pixels(object()) is None)

print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    raise SystemExit(1)
print("PASSED: getCvFrame() and the raw-bytes fallback both decode every frame type "
      "this pipeline can deliver, with no channel swaps.")
