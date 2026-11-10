#!/usr/bin/env python3
"""probe.py — prove the OAK-D works from this host and report its USB link speed.

WHY THIS EXISTS
  A Luxonis OAK device enumerates as a USB 2.0 (High-Speed) device *until DepthAI
  loads firmware onto it*, then disconnects and re-enumerates. So `lsusb` showing
  480 Mbps proves NOTHING on its own — that is the bootloader state, and the
  Luxonis docs say so explicitly:

      "lsusb -v will show USB speed of 480 Mb/s when the device is waiting for
       connection. After connecting to the device, depthai will upload the
       firmware, and the speed will change to 5 Gb/s (if USB3 is available)."

  The only authoritative answer is what the device reports once a pipeline runs,
  which is `Device.getUsbSpeed()`. If that still says HIGH after the app has
  booted, then USB 2.0 really is the negotiated link and the cause is the cable
  or the port.

WHAT IT PROVES, IN INCREASING ORDER OF STRENGTH
  1. discovered  - DepthAI can enumerate the device
  2. reachable   - the device opens and a pipeline starts
  3. ready       - a real frame arrives from the camera

  Exits non-zero if any stage fails, so it works as a check, not just a report.

USAGE
  .venv/bin/python probe.py
  .venv/bin/python probe.py --list-only      # discovery only, do not open

DEPTHAI v3 API NOTES (learned the hard way — do not "fix" these back)
  * `Device()` takes NO pipeline argument in v3. The pipeline is built *around*
    the device:      Pipeline(Device())      ... not      Device(pipeline).
    Passing a Pipeline to Device() raises a long TypeError listing 10 valid
    constructor overloads, none of which is the v2 form.
  * `DeviceInfo` has `.deviceId` (a string), NOT `.getMxId()` — that method lives
    on `Device`. Calling getMxId() on a DeviceInfo raises AttributeError.
  * `Device.getMxId()` still works but is deprecated; `.getDeviceId()` is the v3
    spelling.
  * The device object is reachable after start via `pipeline.getDefaultDevice()`.

EXCLUSIVE ACCESS
  DepthAI takes exclusive access. Never run this while another DepthAI process
  holds the camera — both will fail.
"""

from __future__ import annotations

import argparse
import time

import depthai as dai

OK = "OK"
BAD = "FAIL"

# How each negotiated link speed should be read. Kept next to the message that
# uses it so the two cannot drift apart.
SPEED_MEANING = {
    "LOW": "USB 1.1 (1.5 Mbps)",
    "FULL": "USB 1.1 (12 Mbps)",
    "HIGH": "USB 2.0 (480 Mbps)",
    "SUPER": "USB 3.0 (5 Gbps)",
    "SUPER_PLUS": "USB 3.1 (10 Gbps)",
    "UNKNOWN": "unknown",
}


def speed_name(speed: dai.UsbSpeed) -> str:
    name = getattr(speed, "name", None) or str(speed).split(".")[-1]
    return f"{name} - {SPEED_MEANING.get(name, 'unrecognised')}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-only", action="store_true",
                    help="only enumerate devices; do not open one")
    ap.add_argument("--speed-only", action="store_true",
                    help="open just long enough to read the negotiated USB "
                         "speed, then exit; for testing cables and ports")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="seconds to wait for a frame (default 15)")
    args = ap.parse_args()

    print(f"depthai {dai.__version__}")

    # --- 1. discovered ---------------------------------------------------
    infos = dai.Device.getAllAvailableDevices()
    print(f"\n[1] discovery: {len(infos)} device(s)")

    if not infos:
        print(f"    {BAD}: DepthAI sees no device.")
        print("    If the log above said 'Insufficient permissions', the udev rule")
        print("    is missing: install /etc/udev/rules.d/80-movidius.rules, then")
        print("    replug. Otherwise check the camera is connected:")
        print("      lsusb | grep 03e7")
        return 1

    for i in infos:
        state = getattr(i.state, "name", i.state)
        print(f"    - deviceId={i.deviceId}  usb path={i.name}  state={state}")

    if args.list_only:
        print("\n(list-only: stopping before opening the device)")
        return 0

    # --- 2. reachable ----------------------------------------------------
    # v3: build the Pipeline AROUND the device. Do NOT pass the pipeline to
    # Device() — that is the v2 form and it does not exist here.
    print("\n[2] opening the device and starting a pipeline")
    pipeline = None
    try:
        pipeline = dai.Pipeline(dai.Device())
        cam = pipeline.create(dai.node.Camera).build()
        # Small on purpose: this is a readiness probe, not a workload.
        out = cam.requestOutput((640, 400))
        queue = out.createOutputQueue(maxSize=4, blocking=False)
        pipeline.start()
        print(f"    {OK}: pipeline started")

        device = pipeline.getDefaultDevice()
        device_id = device.getDeviceId()
        speed = device.getUsbSpeed()

        # --- 3. the USB link, as the DEVICE sees it ----------------------
        print("\n[3] USB link speed, reported by the booted device")
        print(f"    getUsbSpeed() = {speed_name(speed)}")

        if (getattr(speed, "name", "") or "").upper() == "HIGH":
            print()
            print("    USB 2.0 AFTER boot. The firmware is loaded, so this is the")
            print("    real negotiated link, not the bootloader state. DepthAI has")
            print("    fallen back to low-bandwidth mode: lower resolutions and")
            print("    frame rates, and some features unavailable.")
            print()
            print("    In order of likelihood:")
            print("      1. The cable is USB2-only. Check the USB-A end — a USB3")
            print("         cable usually has a BLUE insert. White/black plastic")
            print("         means it will only ever give USB2.")
            print("      2. USB-C plug orientation. On OAK-D Lite specifically,")
            print("         flipping the connector at the camera end has been")
            print("         reported to switch HIGH <-> SUPER. Free to try.")
            print("      3. The port is not USB3. Try the other USB-A ports.")

        if args.speed_only:
            print("\nRESULT: speed check only, not opening a stream")
            print(f"        deviceId:  {device_id}")
            print(f"        USB speed: {speed_name(speed)}")
            return 0

        # --- 4. a real frame --------------------------------------------
        print(f"\n[4] waiting up to {args.timeout:.0f}s for a real frame")
        frame = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            packet = queue.tryGet()
            if packet is not None:
                frame = packet
                break
            time.sleep(0.05)

        if frame is None:
            print(f"    {BAD}: no frame within {args.timeout:.0f}s")
            return 1

        arr = frame.getCvFrame()
        print(f"    {OK}: frame received, shape={arr.shape} dtype={arr.dtype}")

        print("\nRESULT: READY (discovered, reachable, streaming)")
        print(f"        deviceId:  {device_id}")
        print(f"        USB speed: {speed_name(speed)}")
        return 0

    except Exception as exc:  # noqa: BLE001 - a probe should report anything
        print(f"    {BAD}: {type(exc).__name__}: {exc}")
        print("\n    Common causes:")
        print("      - permissions: /dev/bus/usb node not writable by this user;")
        print("        install the vendor's 80-movidius.rules udev rule.")
        print("      - another DepthAI process already holds the device.")
        print("      - the device re-enumerated mid-open (cable/power).")
        return 1

    finally:
        # Always release the device, even on the failure paths above, or the
        # next run reports "no device" and looks like a different problem.
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass


if __name__ == "__main__":
    raise SystemExit(main())
