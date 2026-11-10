#!/usr/bin/env python3
"""probe-imgframe-twice.py — does converting the same ImgFrame twice raise?

The gaze loop calls frame.getCvFrame() to learn the frame size, and the new frame
publisher calls it again in the same iteration. Runs in the camera venv and builds
ImgFrames on the HOST, so it needs no camera and cannot disturb the running gaze.

    /home/jetson/robot/camera/.venv/bin/python probe-imgframe-twice.py
"""
import numpy as np
import depthai as dai

print("depthai", dai.__version__)

TYPES = [
    ("BGR888i", dai.ImgFrame.Type.BGR888i, (480, 640, 3)),
    ("BGR888p", dai.ImgFrame.Type.BGR888p, (480, 640, 3)),
    ("RGB888i", dai.ImgFrame.Type.RGB888i, (480, 640, 3)),
    ("GRAY8", dai.ImgFrame.Type.GRAY8, (480, 640)),
    ("NV12", dai.ImgFrame.Type.NV12, (int(480 * 1.5), 640)),
]

for name, typ, shape in TYPES:
    arr = np.zeros(shape, dtype="uint8")
    if len(shape) == 3:
        arr[:] = 120
    frame = dai.ImgFrame()
    try:
        frame.setCvFrame(arr, typ)
    except Exception as exc:                      # noqa: BLE001
        try:
            frame.setData(arr.tobytes())
            frame.setType(typ)
            frame.setSize(640, 480)
        except Exception as exc2:                 # noqa: BLE001
            print(f"{name:8s} could not be constructed: {exc!r} / {exc2!r}")
            continue

    # Exactly what the loop does, then what the publisher does.
    try:
        first = frame.getCvFrame()
        print(f"{name:8s} 1st conversion ok, shape {first.shape}")
    except Exception as exc:                      # noqa: BLE001
        print(f"{name:8s} 1st conversion RAISED {type(exc).__name__}: {exc}")
        continue
    try:
        second = frame.getCvFrame()
        same = getattr(second, "shape", None) == first.shape
        print(f"{name:8s} 2nd conversion ok, shape {getattr(second, 'shape', None)}, "
              f"same shape as 1st: {same}")
    except Exception as exc:                      # noqa: BLE001
        print(f"{name:8s} *** 2nd conversion RAISED {type(exc).__name__}: {exc}")
        continue
    # And the shape+copy path the annotation uses.
    try:
        out = second.copy()
        print(f"{name:8s} .copy() ok, {out.shape}, {out.dtype}")
    except Exception as exc:                      # noqa: BLE001
        print(f"{name:8s} *** .copy() RAISED {type(exc).__name__}: {exc}")
