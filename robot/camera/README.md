# Camera — Luxonis OAK-D Lite (DepthAI v3)

Phase 3 of the robot: bring up the OAK-D Lite for gaze tracking and
stress/agitation detection. Status: **working**, streaming 1080p over USB 3.

## What is here

| File | Purpose |
|---|---|
| `probe.py` | Read-only readiness check. Proves *discovered → reachable → streaming*. `--speed-only` opens, reads the USB link, closes. |
| `capture.py` | Saves a frame as PPM (no opencv/Pillow needed). `--min-usb-speed` asserts a link floor. |
| `viewer.py` | Serves the camera as an MJPEG web stream (`/`, `/stream`, `/snapshot.jpg`, `/healthz`). |
| `oak-viewer.service` | Runs `viewer.py` at boot on port 8080. |
| `install-camera-viewer.sh` | Installs and enables the service, then *verifies* streaming. |
| `80-movidius.rules` | Vendor udev rule granting non-root access to OAK devices. |
| `install-camera-udev.sh` | Installs the above and *verifies* the node mode changed. |
| `requirements.txt` | Pins `depthai`. |

## Installing on the Jetson (already done, for reference)

```bash
sudo apt-get install -y python3-venv python3-pip
cd ~/robot/camera
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # aarch64 wheel, no compilation
sudo bash install-camera-udev.sh               # udev rule; needs root
```

DepthAI lives in a venv, **never** the system Python — it pins numpy/opencv and
would fight apt's `python3-opencv`, which the Jetson uses elsewhere.

## Using it

```bash
cd ~/robot/camera

.venv/bin/python probe.py                      # full readiness check
.venv/bin/python probe.py --speed-only         # just the negotiated USB link
.venv/bin/python capture.py --out /tmp/f.ppm   # 640x400
.venv/bin/python capture.py --size 1920x1080 --warmup 8 --out /tmp/f.ppm
.venv/bin/python capture.py --min-usb-speed SUPER   # fail if it fell back to USB2
```

Convert on the host (serenity has ffmpeg):

```bash
ffmpeg -y -i f.ppm f.png
```

`probe.py` exits non-zero on any failure, so it works as a gate, not just a
report.

## Trap 0: a 480 Mbps reading means NOTHING — and the cable is the usual cause

This cost the most time of anything in Phase 3, so it is first.

An **unopened** OAK sits in its USB 2.0 bootloader and reports 480 Mbps on the
USB2 bus *regardless of whether the port, cable and device can all do
SuperSpeed*. DepthAI uploads firmware on open, the device re-enumerates, and
only then does the link train. Luxonis state it outright:

> `lsusb -v` will show USB speed of 480 Mb/s when the device is waiting for
> connection. After connecting to the device, depthai will upload the firmware,
> and the speed will change to 5 Gb/s (if USB3 is available).

**Never diagnose cable or port from `lsusb`, `/sys/.../speed` or `lsusb -t`.**
The only authoritative check is inside an open session:

```python
device.getUsbSpeed()      # HIGH = USB2, SUPER/SUPER_PLUS = USB3
```

Measured on this device, and visible as the device *moving buses*:

| State | Tree | Speed |
|---|---|---|
| idle (bootloader) | `1-2.1` on the USB2 hub | 480M |
| session open | `2-1.1` on the USB3 hub, driver `usbfs` | 5000M |

### The actual fault was the cable

The original cable negotiated **480 Mbps after boot**. Its USB-A end had a
**white** insert; a replacement with a **blue** insert immediately gave
**SUPER (5 Gbps)**, and forcing `maxUsbSpeed=SUPER_PLUS` gives **10 Gbps**.
A blue insert in the USB-A connector is the normal marker of a USB3 cable and
is the fastest single check available.

Also worth knowing: on OAK-D Lite specifically, **flipping the USB-C plug
orientation** at the camera end has been reported to switch HIGH ↔ SUPER.

Isolate one variable at a time — swap the cable *keeping the same socket*, then
swap sockets *keeping the same cable*. Changing both at once teaches nothing.

## Trap 1: all four USB-A sockets on this devkit ARE USB3

The devkit wires its 4 USB-A ports to a pair of **onboard** Realtek hubs, one per
bus, both visible in `lsusb -t`:

| path | id | speed | role |
|---|---|---|---|
| `1-2` | `0bda:5489` | 480M | USB2 hub, 4 ports |
| `2-1` | `0bda:0489` | 10000M | USB3 hub, 4 ports |

A USB3 device in any socket appears under **both** trees. The USB3 hub sits
legitimately *empty* while only USB2 devices are attached — that is **not**
evidence of a dead port. `/etc/udev/rules.d/99-nv-l4t-usb-host-config.rules`
references exactly these ids (`0bda:5489`, `0bda:0489`) plus variants
(`0bda:5411`, `0bda:0411`), which is how to confirm they are onboard NVIDIA
wiring and not an external hub.

Confirm the USB3 side independently by plugging in **any** known USB3 peripheral
(a USB3 flash drive or SATA bridge); if it trains at 10000M, port and controller
are fine.

Low-level tool if ports need inspecting: `sudo uhubctl -l 2-1` (USB3 hub),
`sudo uhubctl -l 1-2` (USB2 hub). `Rx.Detect` on all four USB3 ports while the
camera sits on the USB2 hub means the SuperSpeed lanes never trained.

## Trap 2: "Insufficient permissions" is not "no device"

Before the udev rule, the device node was `root:root 0664` and DepthAI reported:

```
[warn] Insufficient permissions to communicate with X_LINK_UNBOOTED
device having name "1.2.1". Make sure udev rules are set
[1] discovery: 0 device(s)
```

"having name 1.2.1" means bus 1, port 2, port 1 — it enumerated fine and was
telling us the access was the problem. After installing the rule the node is
`666` and discovery returns 1 device.

## Trap 3: DepthAI v3 is not v2 (API differences)

Writing v2 examples from memory produces confusing `TypeError`s.

```python
# v2 — does NOT work
# device = dai.Device(pipeline)

# v3 — the pipeline is built AROUND the device
pipeline = dai.Pipeline(dai.Device())
cam = pipeline.create(dai.node.Camera).build()
out = cam.requestOutput((640, 400))
queue = out.createOutputQueue(maxSize=4, blocking=False)
pipeline.start()
device = pipeline.getDefaultDevice()
```

- `Device(pipeline)` raises a `TypeError` listing 10 valid overloads, none of
  which is the v2 form.
- `DeviceInfo` exposes `.deviceId` (string), `.name` (USB path), `.state`.
  `getMxId()` lives on **`Device`**, not `DeviceInfo` — wrong object raises
  `AttributeError`.
- `Device.getMxId()` works but is deprecated; use `getDeviceId()`.
- `ImgFrame.getCvFrame()` returns **BGR** (per its own docstring: "ImgFrame is
  converted to color BGR interleaved"). Swap with `arr[:, :, ::-1]` for RGB.
- Always `pipeline.stop()` in a `finally`. DepthAI holds the device
  exclusively, and a leaked handle makes the next run report "no device" —
  which looks like an entirely different problem.

## Trap 5: `VideoEncoder` accepts ONLY NV12 (or GRAY8)

Feed it BGR and it drops every frame, forever, with no hard error:

```
[warning] Arrived frame type (2) is not either NV12 or YUV400p (8-bit Gray)
```

repeated once per frame. The pipeline looks alive, the process runs, the port
listens — and **no JPEG is ever produced**. Diagnosing this from the outside is
painful, so always confirm frames actually arrive (`viewer.py --self-test`, or
`curl /healthz`).

The fix is to request NV12 from the camera:

```python
out = cam.requestOutput((w, h), type=dai.ImgFrame.Type.NV12, fps=fps)
```

## Trap 6: `enable --now` does not restart an already-running service

After editing a unit or the code it runs, `systemctl enable --now` leaves the
**existing process** untouched, so the old code keeps running and every check
still reports the old behaviour. This wasted a round trip: the fps fix appeared
not to work because the process had ~226 s of uptime.

Always `sudo systemctl restart <unit>` after a change. Confirm with uptime, not
with hope:

```bash
curl -s http://127.0.0.1:8080/healthz | grep uptime
```

The same trap makes a **health check fail on a healthy service**: opening an OAK
takes ~5 s (firmware upload + re-enumeration), so a fixed `sleep 3` before
probing reports a false failure. Poll until `frames=` appears instead.

## Trap 7: `StartLimit*` belongs in `[Unit]`, not `[Service]`

systemd silently ignores these keys in `[Service]`:

```
Unknown key name 'StartLimitIntervalSec' in section 'Service', ignoring.
```

The unit looks configured but has **no rate limit**, so a crash-looping service
restarts forever. Put `StartLimitIntervalSec` / `StartLimitBurst` in `[Unit]`.

## Trap 8: DepthAI `fps` must be set on the camera output, not just the encoder

`VideoEncoder.build(..., frameRate=fps)` is only an upper bound. Set the encoder's
rate alone and the camera still streams at the full sensor rate — measured 30 fps
when 15 was requested, doubling LAN bandwidth for nothing.

Set it where frames are requested:

```python
out = cam.requestOutput((w, h), type=dai.ImgFrame.Type.NV12, fps=fps)
```

## Trap 9: the first frame after opening is often near-black

Auto-exposure has not settled. `capture.py --warmup N` discards N frames and
saves the last. A real measurement of the first frame gave channel means
B=6.7 / G=8.5 / R=3.1 — essentially black. A dark image is usually exposure
settling, not a fault.

Corollary: do not try to settle channel order by eyeballing a near-neutral
indoor scene. If R/G/B means are within a few percent (e.g. 102/110/106), no
observer — human or model — can tell which way round it is. Read the API
docstring instead.

## Measured performance (USB3 SUPER unless noted)

| Mode | Result |
|---|---|
| 640x400 | ~33–40 fps |
| 1920x1080 | ~34 fps, 6.2 MB/frame (~210 MB/s — impossible on USB2) |
| Device id | `194430100199667E00` |

## Live viewing (MJPEG over HTTP)

`viewer.py` is installed as `oak-viewer.service` and starts at boot:

**<http://orin.home:8080/>**

> **During a session, use the gaze feed instead.** This viewer holds the camera
> for itself, so starting it stops gaze (and vice versa) — which means it cannot
> show you what the robot is seeing while the robot is working. `gaze.py --stream`
> serves the same port with the frames gaze is already reading, annotated with
> what it detected. See `perception/README.md`, "Watching the camera during a
> run". This page covers the raw sensor at full quality, when you do not need the
> robot's eyes.

| endpoint | what it is |
|---|---|
| `/` | HTML page with the live image and a status readout |
| `/stream` | the MJPEG stream itself — open in any viewer (VLC, ffplay) |
| `/snapshot.jpg` | one JPEG, for scripts (`curl -O`) |
| `/healthz` | plain-text status: size, usb, fps, frames, uptime |

```bash
curl -O http://orin.home:8080/snapshot.jpg        # grab one frame
ffplay -f mjpeg http://orin.home:8080/stream      # watch outside a browser
curl http://orin.home:8080/healthz                # is it healthy?
```

### Why MJPEG and not H.264

The OAK encodes JPEG **on the device** (its `VideoEncoder` has an MJPEG
profile), so the Jetson only forwards bytes — measured at **2.5% of one core**,
against 73.5% for the robot face. Browsers render `multipart/x-mixed-replace`
natively in a plain `<img>` tag, so there is no player, plugin, or JavaScript on
the client either. This is the cheapest camera-to-eyeball path available, which
matters on a board that also drives the face and will later run an LLM.

Measured cost at 1280×720:

| fps | bandwidth | frame size |
|---|---|---|
| 30 | ~2222 KB/s | ~75 KB |
| 15 | ~1207 KB/s | ~82 KB |

### Changing the stream

Edit `ExecStart` in `oak-viewer.service` (`--fps`, `--quality`, `--size`), install
it, then **restart**:

```bash
sudo systemctl restart oak-viewer.service   # REQUIRED - see trap 6
```

### Security

The stream is **unauthenticated** and bound to `0.0.0.0`, so anything on the LAN
can watch. That was a deliberate choice for a bench prototype. Do not port-forward
it to the internet without adding authentication.

## Exclusive access

Only one DepthAI process can hold the camera at a time; two will both fail.
While `oak-viewer.service` is running it **is** the holder, so `probe.py`,
`capture.py` and any future gaze-tracking loop must either run while the viewer
is stopped, or replace it:

```bash
sudo systemctl stop oak-viewer.service     # free the camera
.venv/bin/python probe.py
sudo systemctl start oak-viewer.service
```

The robot face does not use the camera, so it never contends.

## Next

- Stereo depth + the RGB stream together (the OAK-D Lite has RGB + 2 mono).
- Gaze / agitation detection on the device or on the host.
- Decide what feeds the dialogue loop, and from which process.

## References

- Luxonis skills: <https://github.com/luxonis/skills>
- OAK USB deployment guide (udev rule, cable and speed guidance):
  <https://docs.luxonis.com/hardware/platform/deploy/usb-deployment-guide>
- DepthAI install: <https://docs.luxonis.com/software-v3/depthai/installation/>
- Orin Nano USB3 ports regression/port-specificity report (stock devkit, same
  480M fallback, resolved by the physical port):
  <https://forums.developer.nvidia.com/t/oak-1-lite-stuck-in-usb-2-0-high-speed-mode-on-jetson-orin-nano-usb-3-2-ports/369485>
- USB3 does work on this devkit (JMicron bridge negotiated 10000M):
  <https://forums.developer.nvidia.com/t/usb-2-0-instead-of-usb3-2-on-jetson-orin-nano-super-dev-kit/357813>
