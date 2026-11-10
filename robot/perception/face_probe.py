#!/usr/bin/env python3
"""face_probe.py — prove the OAK-D Lite can detect + landmark faces on-device.

GAZE STEP 1 of the HRI robot. This is a *capability probe*, not the gaze loop:
it answers one question — does face detection work on this RVC2 chip at a usable
rate, with the model's own parser wired up correctly?

WHY THIS EXISTS BEFORE THE GAZE LOGIC
  Everything downstream depends on a face box and its landmarks. If the detector
  is slow, wrong, or unparseable, no amount of gaze maths will help. So prove
  the detector first, on real frames, and print what it actually returns.

THE v3 PATTERN (do not "simplify" back to v2)
  Pipeline -> Camera -> ParsingNeuralNetwork(model_id) -> out queue.
  The parsers live in the separate `depthai-nodes` package, not `depthai`.

  `ParsingNeuralNetwork.build()` accepts an **NN Archive** or a **HubAI model
  identifier**. The identifier format is `luxonis/<slug>` — verified working by
  this script downloading `luxonis/scrfd-face-detection` -> RVC2 archive.

RVC2 / OAK-D LITE CONSTRAINTS THAT BITE
  * FPS must be capped explicitly. The OAK-D Lite has an internal FPS limit and
    will not silently do the right thing.
  * Blobs are compiled for a fixed SHAVE count. If a model was built for more
    SHVEs than the device has, the pipeline raises a RuntimeError. Fix with
    `setNNArchive(archive, numShaves=N)` — NOT the removed `setNumShaves`.
  * Only one inference process may hold the camera at a time.

USAGE
  .venv/bin/python face_probe.py                 # scrfd-face-detection
  .venv/bin/python face_probe.py --model mediapipe-face-landmarker
  .venv/bin/python face_probe.py --seconds 20 --fps 15
  .venv/bin/python face_probe.py --save /tmp/faces   # write annotated PPMs

EXIT
  Non-zero if no inference result arrives, so it works as a gate.
"""

from __future__ import annotations

import argparse
import pathlib
import time

import depthai as dai
from depthai_nodes.node import ParsingNeuralNetwork

# HubAI identifiers, format luxonis/<slug>. Confirmed available for RVC2 on
# models.luxonis.com; see the camera README for the full candidate list.
MODELS = {
    "face": "luxonis/scrfd-face-detection",
    "landmarks": "luxonis/mediapipe-face-landmarker",
    "gaze": "luxonis/gaze-estimation-adas",
}


def describe(obj, depth: int = 0, seen: set | None = None) -> None:
    """Print a parsed detection object without assuming its exact schema."""
    if seen is None:
        seen = set()
    pad = "  " * depth
    if id(obj) in seen:
        print(f"{pad}<same object again>")
        return
    seen.add(id(obj))

    if depth > 3:
        print(f"{pad}{type(obj).__name__}(...)")
        return

    if isinstance(obj, (str, int, float, bool)) or obj is None:
        print(f"{pad}{obj!r}")
        return

    if isinstance(obj, (list, tuple)):
        print(f"{pad}{type(obj).__name__}[{len(obj)}]")
        for i, item in enumerate(obj[:4]):
            print(f"{pad}  [{i}]")
            describe(item, depth + 2, seen)
        if len(obj) > 4:
            print(f"{pad}  ... {len(obj) - 4} more")
        return

    attrs = [a for a in dir(obj)
             if not a.startswith("_") and not callable(getattr(obj, a, None))]
    if attrs:
        print(f"{pad}{type(obj).__name__}:")
        for a in attrs[:14]:
            try:
                v = getattr(obj, a)
            except Exception as exc:  # noqa: BLE001
                print(f"{pad}  {a} -> <{type(exc).__name__}>")
                continue
            if isinstance(v, (str, int, float, bool, type(None))):
                print(f"{pad}  {a} = {v!r}")
            else:
                print(f"{pad}  {a} = {type(v).__name__}")
    else:
        print(f"{pad}{type(obj).__name__} (no simple attributes)")


def write_ppm(path: pathlib.Path, rgb) -> None:
    h, w = rgb.shape[0], rgb.shape[1]
    with open(path, "wb") as fh:
        fh.write(f"P6\n{w} {h}\n255\n".encode())
        fh.write(rgb.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=MODELS["face"],
                    help=f"HubAI id or one of {list(MODELS)}")
    ap.add_argument("--fps", type=float, default=15.0,
                    help="FPS cap (OAK-D Lite has an internal limit)")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--size", default="640x400")
    ap.add_argument("--save", default=None,
                    help="directory to write annotated PPM frames into")
    args = ap.parse_args()

    model_id = MODELS.get(args.model, args.model)
    w, h = (int(v) for v in args.size.lower().split("x"))
    outdir = pathlib.Path(args.save) if args.save else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    print(f"depthai {dai.__version__}  model={model_id}  {w}x{h} @ {args.fps:g}fps")

    pipeline = dai.Pipeline()
    camera = pipeline.create(dai.node.Camera).build()

    # Pass the CAMERA NODE, not a pre-sized output. `ParsingNeuralNetwork`
    # reads the archive's required input size and wires/reshapes the camera
    # output to match. Handing it a fixed-size output instead produces a silent
    # failure loop:
    #
    #   [warning] Input image (640x400) does not match NN (640x640)
    #   [error]   Input tensor 'input.1' (0) exceeds available data range
    #             ... - skipping inference
    #
    # The pipeline starts, the process runs, and ZERO results ever arrive.
    # scrfd-face-detection is a 640x640 SUPERBLOB, so any other size is wrong.
    try:
        nn = pipeline.create(ParsingNeuralNetwork).build(
            camera, model_id, fps=args.fps)
    except TypeError:
        # Older/newer signature without fps: fall back, and note it so the
        # slowdown is not mistaken for a device limit later.
        print("  (build(fps=) unsupported; building without an explicit cap)")
        nn = pipeline.create(ParsingNeuralNetwork).build(camera, model_id)
    except Exception as exc:  # noqa: BLE001
        print(f"build failed: {type(exc).__name__}: {exc}")
        return 1

    print("  ParsingNeuralNetwork built")
    for attr in ("getParser", "out", "passthrough"):
        print(f"    has {attr}: {hasattr(nn, attr)}")

    # Queue BOTH the parsed results and the passthrough frames, so a detection
    # can be tied to the exact image it came from.
    try:
        result_q = nn.out.createOutputQueue()
    except Exception as exc:  # noqa: BLE001
        print(f"out queue failed: {exc}")
        return 1
    try:
        frame_q = nn.passthrough.createOutputQueue()
    except Exception as exc:  # noqa: BLE001
        print(f"passthrough queue failed (continuing without frames): {exc}")
        frame_q = None

    pipeline.start()
    print(f"  started; USB {pipeline.getDefaultDevice().getUsbSpeed().name}")

    frames = 0
    with_det = 0
    t0 = time.monotonic()
    last_print = 0.0
    saved = 0

    try:
        while time.monotonic() - t0 < args.seconds:
            try:
                result = result_q.tryGet()
            except Exception as exc:  # noqa: BLE001
                print(f"  result error: {type(exc).__name__}: {exc}")
                break

            frame = None
            if frame_q is not None:
                frame = frame_q.tryGet()

            if result is not None:
                frames += 1
                dets = getattr(result, "detections", None)
                n = len(dets) if dets is not None else -1
                if n:
                    with_det += 1
                    if time.monotonic() - last_print > 1.0:
                        last_print = time.monotonic()
                        print(f"\n  --- result with {n} detection(s) ---")
                        describe(result)
                        if dets:
                            describe(dets[0])

                if outdir and frame is not None and n:
                    try:
                        rgb = frame.getCvFrame()[:, :, ::-1]
                        write_ppm(outdir / f"face_{saved:03d}.ppm", rgb)
                        saved += 1
                    except Exception:  # noqa: BLE001
                        pass
            else:
                time.sleep(0.002)

        dt = time.monotonic() - t0
        print(f"\ninference results: {frames} in {dt:.1f}s = {frames / dt:.1f} fps")
        print(f"results containing >=1 detection: {with_det}")
        if outdir:
            print(f"frames saved: {saved}")
        print("RESULT:", "WORKING" if frames else "NO RESULTS")
        return 0 if frames else 1

    finally:
        try:
            pipeline.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
