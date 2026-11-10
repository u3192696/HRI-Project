#!/usr/bin/env python3
"""capture.py — grab frames from the OAK-D and write them out as image files.

WHY THIS EXISTS
  The probe proves the camera streams. This proves the camera is *looking at
  something* — a stream of correct-shaped buffers can still be a black or dead
  sensor, and that failure is invisible in a frame count.

  The Jetson venv deliberately holds only `depthai` + `numpy` (no opencv, no
  Pillow), so this writes **PPM**: a tiny ASCII header plus raw RGB bytes. No
  dependency, and `ffmpeg`/ImageMagick can read PPM, which is already available
  on the NixOS host used for viewing.

CHANNEL ORDER
  `getCvFrame()` follows the OpenCV convention and returns **BGR**. PPM is
  **RGB**. The swap below is not optional — skip it and the picture is
  convincingly wrong (skin blue, sky orange) rather than obviously broken.

USAGE (on the Jetson, from the project's camera venv)
  .venv/bin/python capture.py --out /tmp/frame.ppm
  .venv/bin/python capture.py --size 1920x1080 --out /tmp/full.ppm
  .venv/bin/python capture.py --warmup 10 --frames 30   # also report fps

  # then, on the host:
  ffmpeg -y -i frame.ppm frame.png

EXIT STATUS
  Non-zero if no frame arrives, so a wrapper script can gate on it.
"""

from __future__ import annotations

import argparse
import time

import depthai as dai

# Ordering for --min-usb-speed. DepthAI's UsbSpeed enum is not ordered in a way
# that supports comparison, and names must be ranked explicitly.
SPEED_RANK = {
    "LOW": 0,
    "FULL": 1,
    "HIGH": 2,
    "SUPER": 3,
    "SUPER_PLUS": 4,
}


def parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except Exception as exc:  # noqa: BLE001
        raise argparse.ArgumentTypeError(
            f"expected WIDTHxHEIGHT (e.g. 1920x1080), got {text!r}"
        ) from exc


def write_ppm(path: str, rgb) -> None:
    """Write an (H, W, 3) uint8 RGB array as binary PPM (P6)."""
    h, w = rgb.shape[0], rgb.shape[1]
    with open(path, "wb") as fh:
        fh.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
        fh.write(rgb.tobytes())
    print(f"    wrote {path}  ({w}x{h}, {rgb.nbytes / 1e6:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=parse_size, default=(640, 400),
                    help="WIDTHxHEIGHT (default 640x400)")
    ap.add_argument("--out", default="/tmp/frame.ppm",
                    help="output .ppm path (default /tmp/frame.ppm)")
    ap.add_argument("--warmup", type=int, default=5,
                    help="frames to discard before saving (default 5)")
    ap.add_argument("--frames", type=int, default=1,
                    help="total frames to consume; >1 is used to time fps")
    ap.add_argument("--max-usb-speed", default=None,
                    help="LOW/FULL/HIGH/SUPER/SUPER_PLUS: REQUEST this link "
                         "speed from the device (passed to maxUsbSpeed)")
    ap.add_argument("--min-usb-speed", default=None,
                    help="LOW/FULL/HIGH/SUPER/SUPER_PLUS: ASSERT the "
                         "negotiated link is at least this fast, then exit "
                         "non-zero if not (catches a silent USB2 fallback)")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="seconds to wait for the first frame")
    args = ap.parse_args()

    width, height = args.size
    print(f"depthai {dai.__version__}  requesting {width}x{height}")

    # Build the pipeline AROUND the device (v3 API).
    request = None
    if args.max_usb_speed:
        request = getattr(dai.UsbSpeed, args.max_usb_speed.upper())
    pipeline = dai.Pipeline(dai.Device(maxUsbSpeed=request) if request
                            else dai.Device())
    cam = pipeline.create(dai.node.Camera).build()
    out = cam.requestOutput((width, height))
    queue = out.createOutputQueue(maxSize=8, blocking=False)

    try:
        pipeline.start()
        device = pipeline.getDefaultDevice()
        negotiated = device.getUsbSpeed()
        reported = negotiated.name.upper()
        print(f"device {device.getDeviceId()}  USB {reported}")

        # A FLOOR, not an equality test: asking for SUPER must still pass on a
        # 10 Gbps link. Comparing names for equality would wrongly reject it.
        if args.min_usb_speed:
            want = args.min_usb_speed.upper()
            if SPEED_RANK.get(reported, -1) < SPEED_RANK.get(want, 99):
                print(f"REFUSING: negotiated {reported}, needed at least {want}"
                      f" - the link fell back to a slower speed.")
                return 3

        saved = None
        started = time.monotonic()
        seen = 0
        first_at = None

        while seen < max(args.frames, args.warmup + 1):
            packet = queue.tryGet()
            if packet is None:
                if time.monotonic() - started > args.timeout:
                    print(f"NO FRAME within {args.timeout:.0f}s")
                    return 1
                time.sleep(0.002)
                continue

            seen += 1
            if first_at is None:
                first_at = time.monotonic()

            # Only the LAST frame is kept, so the saved image is the freshest
            # one rather than the first one to arrive while auto-exposure was
            # still settling - the usual reason a fresh capture looks dark.
            if seen >= args.warmup + 1:
                bgr = packet.getCvFrame()
                saved = bgr[:, :, ::-1]   # BGR -> RGB; see header note

        dt = time.monotonic() - (first_at or started)
        if seen > 1 and dt > 0:
            print(f"    consumed {seen} frames in {dt:.2f}s "
                  f"= {seen / dt:.1f} fps (includes dropped/ignored)")
        write_ppm(args.out, saved)
        print(f"OK  (link: {reported})")
        return 0

    finally:
        # DepthAI holds the device exclusively; never leave a handle open.
        try:
            pipeline.stop()
        except Exception:  # noqa: BLE001 - best effort
            pass


if __name__ == "__main__":
    raise SystemExit(main())
