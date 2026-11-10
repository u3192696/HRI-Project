# Perception — face, gaze, (later) agitation

Phase 4 of the robot: see the person, work out where they are looking, and drive
the robot's eyes. Runs on the OAK-D Lite's own VPU, so the Jetson CPU is barely
touched.

## What is here

| File | Purpose |
|---|---|
| `face_probe.py` | Capability probe: does face detection run at all? Prints the parsed result schema. |
| `gaze.py` | The gaze daemon. Detects the nearest face, estimates head yaw/pitch from landmarks, writes `gaze` into the face state file. |
| `probe-status-write.py` | Measures the torn-read rate of the two ways of writing a status file: `open("w")` + dump, against write-beside-it-and-rename. The reason both files here are written atomically. |
| `README.md` | This file — including the traps, which are the expensive part. |

Environment: the **camera venv** at `/home/jetson/robot/camera/.venv` (depthai +
depthai-nodes + numpy). Not a separate venv — the models and the interpreter are
the same, and a second 200 MB copy of depthai buys nothing.

## Running

```bash
cd /home/jetson/robot/perception

# 1. is face detection working at all?
../camera/.venv/bin/python face_probe.py --seconds 15 --fps 15

# 2. gaze, measuring but writing NOTHING (safe while tuning)
../camera/.venv/bin/python gaze.py --dry-run --fps 12 --status /tmp/gaze.json

# 3. gaze, driving the robot's eyes
../camera/.venv/bin/python gaze.py --fps 12
```

**Stop the viewer first** — DepthAI holds the camera exclusively:

```bash
sudo systemctl stop oak-viewer.service
```

## Model choice

`luxonis/scrfd-face-detection` — SCRFD-10G, 640x640, SUPERBLOB, RVC2.
Discovered via the HubAI identifier format `luxonis/<slug>`.

It is a **face detector that also emits 5 facial landmarks** (two eye corners,
nose, two mouth corners), which is exactly the minimum for head-pose estimation.
Verified landmark order by drawing them on a real frame and looking:

| index | feature |
|---|---|
| 0 | one eye corner |
| 1 | other eye corner |
| 2 | nose |
| 3 | one mouth corner |
| 4 | other mouth corner |

Measured on this device: **6.5 fps**, ~96% of frames contain the face at
640x640.

Other candidates on models.luxonis.com and why they are not used yet:

- `luxonis/mediapipe-face-landmarker` — adds **iris** points. This is the
  upgrade path for true eye-gaze rather than head-pose. The output contract
  would not change.
- `luxonis/gaze-estimation-adas` — gaze vector from a *cropped* face; needs a
  driver-monitoring style crop plus a second pipeline stage.
- `luxonis/yunet` — 640x360, smaller/faster, no landmarks. A fallback if fps
  ever matters more than head pose.

## How gaze is estimated, and why the eyes move the way they do

Face *position* alone is not enough. A person at the left edge looking straight
at the robot **is** making eye contact; a person dead centre looking away is not.
Both signals are combined:

| signal | derivation | why |
|---|---|---|
| face offset | box centre mapped to -1..1 | look toward an off-centre person |
| head yaw | `(nose_x - eye_mid_x) / eye_distance` | turning slides the nose off the eye-line midpoint |
| head pitch | `(nose_ratio - 0.396)` where `nose_ratio = (nose_y - eye_mid_y) / (mouth_mid_y - eye_mid_y)` | tipping down foreshortens the lower face, shrinking the denominator |

Dividing by the eye distance (and by the eye-to-mouth span) makes both measures
**invariant to how far away the face is**. Verified: the same pose at half the
distance gives an identical yaw.

**This is head-pose, not pupil gaze.** Worth stating plainly so nobody expects
more than it gives. It is what makes the robot look attentive, which is the goal.

## The output contract

`gaze.py` writes `gaze` into `/run/face/state.json`, which `face.py` owns:

```json
{"expression": "neutral", "gaze": {"x": 0.0, "y": 0.0}, "blink": false}
```

Rules, all of which matter:

- **Only the `gaze` key is written.** The daemon reads the file, replaces
  `gaze`, and writes it back, so a future dialogue process writing `expression`
  is not clobbered.
- **Writes are atomic** (temp file + `os.replace`). `face.py` polls by mtime and
  could otherwise read a half-written file.
- **The status file is written the same way, and for the same reason** (5 November).
  `gaze-status.json` is what the greeter and the session conductor read — the
  conductor ~10 times a second, and it treats an unreadable file as "no data" — so a
  truncate-first `open("w")` cost it a sample now and then, which is the journal's
  `no usable gaze status file` once or twice a session. `probe-status-write.py`
  measures the difference: with a reader spinning as fast as it can, the old way
  tears 95% of reads, and renaming a complete file tears none of 277,000.
- **The daemon recentres on exit** (SIGTERM/SIGINT handled), so killing it
  leaves the robot looking forward rather than frozen mid-glance.
- `face.py` clamps to -1..1 itself, but this side clamps too — defence in depth
  is cheap here.

## Traps already hit

### 1. `ParsingNeuralNetwork` wants the CAMERA NODE, not a sized output

The obvious thing — request a nicely sized output and hand it over — produces a
**silent** failure loop. The pipeline starts, the process runs, and zero results
ever arrive:

```
[warning] Input image (640x400) does not match NN (640x640)
[error]   Input tensor 'input.1' (0) exceeds available data range
          ... - skipping inference
```

Pass the node and let it read the archive's required input size:

```python
camera = pipeline.create(dai.node.Camera).build()
nn = pipeline.create(ParsingNeuralNetwork).build(camera, "luxonis/scrfd-face-detection")
```

### 2. Parser coordinates are NORMALISED 0..1, not pixels

`xmin`, `getCenterX()`, `getWidth()` and every keypoint come back as **fractions
of the frame**, not pixels. Treating 0.5 as "half a pixel of 640" produced a
confident and completely wrong gaze of `(-0.75, -0.93)`.

The landmark *ratios* are unit-free so they were unaffected, which is exactly why
this survived the synthetic tests and had to be caught against live data.

### 3. `dai.Keypoint` has no `.x` / `.y`

It wraps a `Point2f` as `imageCoordinates`, and carries `confidence` and `label`:

```python
kps = face.getKeypoints()
x, y = kps[0].imageCoordinates.x, kps[0].imageCoordinates.y
```

`getKeypoints2f()` returns bare `Point2f` values as a fallback.

### 4. RVC2 needs the FPS capped, and models are SHAVE-compiled

The OAK-D Lite has an internal FPS limit; ask for 30 and frames queue. Cap it
(`fps=` on build / `requestOutput`).

Separately, blobs are compiled for a fixed SHAVE count. Over-compiled models
raise `Blob compiled for N shaves, but only M are available`. Fix with
`setNNArchive(archive, numShaves=N)` — the old `nn.setNumShaves(n)` was **removed
in v3** and does nothing.

### 5. Two camera outputs are fine; two *processes* are not

The probe wanted a colour preview *and* inference. Requesting a second output
from the same `Camera` node works perfectly (that is how `face_probe.py --save`
and the landmark-verification drawing were done). But the device is exclusive
across processes — see below.

## Exclusive access

DepthAI gives one process the camera at a time. `oak-viewer.service` is normally
the holder:

```bash
sudo systemctl stop oak-viewer.service     # free the camera
../camera/.venv/bin/python gaze.py
sudo systemctl start oak-viewer.service
```

The robot face does **not** use the camera, so it never contends.

## Watching the camera during a run

Exclusivity is why a *second* viewer cannot simply open the camera while the robot
is working: `camera/viewer.py` needs the device for itself, so asking for it
mid-session means stopping gaze, which stops the thing you were trying to watch.

So gaze serves the frames **it is already reading**, annotated with what it
detected, with the status file beside them:

**<http://orin.home:8080/>** — or the board's address on the USB-C gadget when
there is no LAN.

It is on in the unit (`--stream 8080`), it costs nothing while nobody is watching
(the JPEG encode is skipped entirely unless a client fetched something in the last
few seconds), and it can never take the eyes down: if the port is busy, or the
import or the encode fails, gaze prints one line and carries on.

| endpoint | what it is |
|---|---|
| `/` | HTML page: the live image and the status numbers |
| `/stream` | MJPEG, for a browser, VLC or `ffplay` |
| `/frame.jpg` | the newest frame — one JPEG, for scripts |
| `/status.json` | the status file, verbatim, as the robot is acting on it |
| `/healthz` | plain text: port, uptime, frames published, clients, verdict |

```bash
# from a laptop on the same network
curl -O http://orin.home:8080/frame.jpg     # one frame, as the detector saw it
ffplay -f mjpeg http://orin.home:8080/stream
watch -n1 'curl -s http://orin.home:8080/status.json | grep -E "subject|faces"'
```

What the drawing means: the box is the face that was taken, the five dots are what
the head pose is derived from, and the colour is the verdict everything else acts
on — green for "someone is in front of me", amber for a face the subject gate
rejected (a poster, a face on a screen, a passer-by at the edge), grey for a
detector hit with no verdict. Detector coordinates are 0..1 fractions, so the
drawing is correct whether the frame is the camera's own size or the network's
640×640 passthrough.

This is not what `oak-viewer.service` is for: that one holds the camera itself, at
full quality, and stops gaze to do it. Reach for it when you want the raw sensor
and do not need the robot's eyes; reach for this one during a session.

### Why the frames come from the loop, not from the frame queue

A publisher that goes back to `gaze.py`'s frame queue gets `None`: the device sends
the image and the detections as **separate messages**, and the loop drains the image
queue on every spin (it has to, or the queue backs up), so by the time a detection
result is in hand the frame it was computed from has already been popped. The first
live attempt at this feed published exactly zero frames that way, silently, with no
exception to log. `note_frame()` keeps the newest drained frame's pixels instead, as
a host copy, gated on the same "is anyone watching" test as the encode. There is no
lag in practice: the image is sent before the detections, so the cached pixels are
the detection's own frame.

`frame_pixels()` takes `getCvFrame()` where it works and decodes the raw bytes by
type where it does not (GRAY8, BGR888i, planar BGR888p, NV12), so a frame the device
hands over in an awkward form still reaches the page. And `/healthz` counts frames it
*skipped* for want of pixels, not just those published: a stream that connects and
carries nothing is the failure that looks like success.

To turn it off: drop `--stream 8080` from `gaze.service` and re-run the installer.
`framefeed.py` can also be run on its own — `--selftest` serves synthetic frames
and needs no camera — to check a browser's path to the board without touching
gaze.

## Tuning

Defaults are deliberately modest; an over-driven gaze reads as manic rather than
attentive.

| flag | default | effect |
|---|---|---|
| `--center-gain` | 0.9 | how much face *position* moves the eyes |
| `--yaw-gain` | 1.6 | how much head *turn* moves the eyes |
| `--pitch-gain` | 1.2 | how much head *tilt* moves the eyes |
| `--smooth` | 0.25 | EMA alpha; lower = smoother and laggier |
| `--deadband` | 0.015 | ignore changes smaller than this (stops idle jitter) |
| `--lost-grace` | 1.5 | seconds without a face before recentring |
| `--yaw-sign` | 1 | flip to -1 if the eyes look the wrong way as you turn |
| `--no-expression` | off | drive gaze only; leave the expression alone |
| `--look-enter` | look.conf, else 0.35 | gaze magnitude at or below which we count as looking at the screen |
| `--look-exit` | look.conf, else 0.80 | above which we are definitely not looking (hysteresis) |
| `--look-conf` | `look.conf` beside this file | where those two, and the subject gate, are read from. Re-read when it changes |
| `--no-look-conf` | off | ignore the calibration file and use the built-ins (A/B) |
| `--subject-min-w` | look.conf, else 0.12 | frame-width fraction a face must reach to count as someone in front of the robot |
| `--subject-exit-w` | look.conf, else 0.08 | stay a subject until the face shrinks below this |
| `--expression-dwell` | 0.4 | seconds a new expression must hold before being adopted |
| `--expression-interval` | 0.6 | min seconds between expression writes (see note below) |

Both axes are now **settled by observation**:

| you | `yaw_raw` | `pitch_raw` | eyes |
|---|---|---|---|
| turn to your left | **+** | — | screen-right, toward where you turned |
| turn to your right | **−** | — | screen-left |
| tip your head down | — | **+** | down, following the tilt |
| tip your head up | — | **−** | up |

`--yaw-sign 1` is correct and needs no change; the flag remains only as an escape
hatch if a future refactor breaks the convention.

## Pitch: how it actually works

`pitch = nose_ratio - 0.396`, where
`nose_ratio = (nose_y - eye_mid_y) / (mouth_mid_y - eye_mid_y)`.

The dominant term is the **denominator**, not the numerator. Tipping the head
down foreshortens the lower face, so the eye-to-mouth span *shrinks*; the same
nose gap then becomes a **larger fraction**, and `pitch_raw` rises. An earlier
version of this file claimed the opposite — that looking down pulls the nose
toward the eye line and shrinks the ratio — which reasoned about the numerator
while silently holding the denominator fixed. Confirmed wrong on hardware.

`0.396` is not a free parameter: it is the measured `nose_ratio` of a level,
frontal face (live session, `yaw_raw ≈ 0.05`). Getting it right matters — at the
old `0.45`, a level head read `-0.054` and the eyes sat visibly too high.

**Known coupling: pitch drifts with yaw.** Turning the head also moves
`nose_ratio` (measured: `0.341` at `yaw_raw -0.31`, `0.441` at `+0.39`), so on a
strongly turned head the pitch figure is contaminated. Tolerable for driving the
eyes at a roughly-facing person; needs a compensation term if the agitation work
depends on accurate pitch from an off-axis person.

Pitch is also inherently the weaker axis: the nose and mouth points compress
toward the eye line from both directions, so the signal is noisier than yaw's.
Expect less precision.

## Expression policy

The face now reacts to whether you are looking at it:

| condition | expression | how it is judged |
|---|---|---|
| no face detected | `neutral` | instant — no dwell |
| looking at the robot | `happy` | `max(\|gaze.x\|, \|gaze.y\|) < 0.35` |
| not looking | `angry` | `> 0.80`, with hysteresis |

"Looking at the camera" is judged from the **smoothed combined gaze**, not raw
yaw. A raw yaw threshold would call a person standing off to one side who is
staring straight at the robot "not looking" — the combined signal already folds
head position and head pose together.

**Two thresholds, plus a dwell.** A single threshold chatters when the gaze sits
on the boundary, so `happy` needs `< 0.35` to enter but `> 0.80` to leave; in
between, the current expression holds. On top of that a new expression must
persist for `--expression-dwell` (0.4 s) before being adopted, so a one-frame
detection spike cannot flip the face. Losing the face is the one exception and
is instant — a robot still grinning at an empty room is worse than one that
flickers.

The expression is written **only when it changes**, and no more often than
`--expression-interval` (0.6 s). This is not an optimisation: `face.py` *eases*
toward its target over roughly half a second and re-reads the file whenever the
mtime changes, so rewriting an unchanged expression 10 times a second re-aims a
half-finished transition on every write and the animation never completes.

`face.py` gained the `angry` expression for this — it had no negative expression
at all. The brow angles are **mirrored** (`brow_l=-0.38, brow_r=+0.38`) because
`_draw_brow` is called with `-brow_l`/`-brow_r` and no per-side mirroring, so
identical values rotate both brows the same way *on screen*. Verified by
rendering `--snapshot --expression angry` and looking at it.

## What counts as "looking at the screen": a per-rig calibration

The two thresholds above are **not robot constants**. `looking` is
`max(|gaze.x|, |gaze.y|)` compared against them, and that magnitude adds *where the
head sits in the frame* to *which way it is turned* — so the value a person produces
by facing the screen belongs to the arrangement: where the camera is, which way it is
aimed, where the seat is, where the screen is. Measured on the robot in its case,
0.35/0.80 is right. Put the same camera on the testing bench and the number moves
with the geometry, and the robot starts calling someone who is looking straight at it
"not looking".

They therefore live in **`look.conf`**, beside this file, written by
`calibrate-look.py`, and **re-read whenever the file changes** — a re-fit is live with
no service restart (gaze logs every reload). With no file, the built-ins apply, which
are the values measured on the robot; a rig that has never been calibrated behaves
exactly as it did before.

```bash
# on the board, with gaze.service running and publishing its status file
ssh -t orin 'cd ~/robot/perception && python3 ./calibrate-look.py --rig bench'

  phase A: sit where you will sit and look at the screen   (8 s)
  phase B: same seat, look away                            (8 s)
  -> fits LOOK_ENTER/LOOK_EXIT to the gap, shows the margin, asks once, writes
```

### Being told which pose to hold

The run is done by someone sitting in front of the camera, so this terminal — and the
person reading it — are on opposite sides of their head. Each window is therefore
announced **three ways**:

| cue | carried by | reaches you when |
| --- | --- | --- |
| the instruction panel | `/run/face/coach.json`, drawn by `face-kiosk.service` | you are facing the screen — i.e. phase A |
| the boundary tones | a rising pair to open a window, a falling pair to shut it | you are looking away — i.e. phase B, which is the whole point of having them |
| the terminal lines | this tool, printed as it goes | you are the operator watching |

Every window is preceded by `--lead` seconds of *get ready* (default 3 s), so the pose
is **held** when the measurement starts rather than moved into, and phase B is preceded
by `--between` seconds (default 4 s) with its instruction on screen and readable before
you turn away. The panel's countdown is drained from the same `CLOCK_MONOTONIC`
deadline the tool measures against, so "it said 8 s" and "it measured 8 s" are the same
number rather than two clocks that happen to agree.

The panel is written with a heartbeat and **removed on every exit path** — a refusal, a
`Ctrl-C`, an exception. A killed tool must not leave an instruction on a screen, and
neither may a half-finished one: if the file stops being re-stamped, `face.py` expires
the panel after 1.5 s and goes back to ignoring it. Preflight says what will actually
reach you: whether `face-kiosk.service` is up at all, and whether the sink named in
`voice/display.conf` is present on this board (this board's *default* sink is an analog
output nobody has plugged in, so "played without error" and "was audible" are different
claims). A bench rig with neither a display nor audio is a legitimate arrangement —
`--no-coach`, `--no-tones`, or both — and then the terminal's phase B text, which tells
you what to do *before* you turn away, is the cue that matters.

```bash
./calibrate-look.py --lead 5 --between 6   # more time to settle / to read and turn
./calibrate-look.py --no-tones             # screen cue only
./calibrate-look.py --no-coach             # terminal only
```

* It **reads the status file gaze already publishes** and never opens the camera: the
  OAK-D is exclusive, and stopping gaze to calibrate gaze's own verdict is the one
  thing that must not happen.
* The fit uses the **tails**, not the cluster middles — the *worst* pose you still
  call looking (p95 of phase A) against the *best* pose you call not-looking (p5 of
  phase B). If those two overlap it refuses to write anything and says so, printing
  the components (framing vs pose) so the next move is a decision rather than a guess.
* Samples where the camera could not see anyone are **excluded, not counted as "not
  looking"**: a lost face leaves a recentred `0.0` on disk, and `0.0` is the most
  looking-like number there is.
* The subject (distance) gate has the same problem for the same reason. The report
  always measures `box_w` on both phases and says so when the gate, not the
  threshold, is what is rejecting the person.

```bash
./calibrate-look.py --show             # active numbers, stamp, live reading, saved rigs
./calibrate-look.py --watch            # one live line: magnitude, verdict, age
./calibrate-look.py --use bench        # make a saved fit active (live, no restart)
./calibrate-look.py --dry-run          # measure and report; write nothing
./calibrate-look.py --selftest         # the fit, the guards and the cues; no camera
```

Each fit is stamped with the rig it was measured on and archived as
`looks/<rig>.conf`, so switching between the robot and the bench is `--use <rig>`
rather than a re-measure. The stamp is the point: a number without the arrangement it
belongs to is a number nobody can re-check.

## Running as a service

`gaze.service` owns the OAK-D from boot; the face reacts to you with no manual
step. The viewer is now **on demand** rather than always-on.

```bash
sudo bash install-gaze-service.sh      # from ~/robot/perception, or /tmp
```

**The camera is exclusive, so the two units cannot both run.** Both carry a
`Conflicts=` line, which makes the swap automatic and symmetric:

```bash
sudo systemctl start oak-viewer.service   # gaze stops; browse http://orin.home:8080/
sudo systemctl stop  oak-viewer.service   # hand the camera back to gaze
```

Without those lines the loser dies with `X_LINK_DEVICE_ALREADY_IN_USE`, which
reads like a hardware fault rather than a scheduling conflict.

The unit also declares `After=`/`Wants=face-kiosk.service`, because `/run/face`
is created *and removed* by that unit's `RuntimeDirectory=face` and this service
writes `/run/face/gaze-status.json` into it.

## Status

- [x] Face detection running on-device (SCRFD, 6.5 fps, 96% hit rate)
- [x] Landmark order verified visually against real pixels
- [x] Head yaw/pitch derived, scale-invariance and sign conventions unit-tested
- [x] Atomic, merge-safe writes into the face state contract
- [x] **Yaw sign confirmed by observation** (turn left → `yaw_raw` +, eyes follow)
- [x] **Pitch sign confirmed by observation** (head down → `pitch_raw` +); neutral constant set from a real frontal face (0.396)
- [x] Expression policy: neutral / happy / angry with hysteresis and dwell
- [x] `angry` face added to `face.py`, verified by rendering
- [x] Run as a systemd service at boot
- [ ] **Agitation detection** (phase 2, after gaze is signed off)

## References

- Model zoo: <https://models.luxonis.com/>
- Inference guide (v3 pattern): <https://docs.luxonis.com/software-v3/ai-inference/inference.md>
- DepthAI nodes / parsers: <https://github.com/luxonis/depthai-nodes>
- Face state contract: `../face/README.md` and the docstring in `face.py`
