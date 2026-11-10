# Robot face — projected cartoon face

The face the robot shows on its display. Runs fullscreen on the kiosk
compositor with no desktop, no login, and no window chrome.

## Files

| File | Purpose |
|---|---|
| `face.py` | The face app. GTK4 on Wayland, plus a headless snapshot mode. |
| `weston-kiosk.ini` | Compositor config — pins the output, kills the panel/background. |
| `kiosk-start.sh` | Launches weston + the face. Called by the systemd unit. |
| `face-kiosk.service` | systemd unit: start on boot, restart on failure. |
| `zz-nvidia-drm.conf` | **Must be installed** to `/etc/modules-load.d/`. Loads `nvidia_drm` at boot (trap 0, part 1). |
| `nvidia-drm-reload.sh` | **Must be installed** to `/usr/local/sbin/`, mode 0755 root:root. Reloads `nvidia_drm` once at boot — the step that actually makes GL work (trap 0, part 2). Root-executed, so it must not live anywhere the `jetson` user can write. |
| `nvidia-drm-reload.service` | **Must be installed** to `/etc/systemd/system/` and enabled. Runs the reload, ordered before the kiosk. |
| `drm-order-diagnostic.sh` | Diagnostic: dumps the DRM node/driver mapping before and after a reload, and reports which renderer the service then gets. |
| `gl-late-load-test.sh` | Diagnostic: proves whether `EGL_NOT_INITIALIZED` is a module-init problem. |
| `softdep-honesty-test.sh` | Diagnostic for a withdrawn theory — reads the node ordering in a rebuilt sequence. |
| `preview/` | Rendered PNGs of every expression (generated, not hand-drawn). |
| `expression_probe.py` | Measures what the renderer ACTUALLY draws, per expression. Use before tuning any expression parameter. |
| `gaze_bar_probe.py` | Measures the gaze bar from the rendered pixels: fill proportionality, stray ink, collisions, contrast. Use before believing anything about the bar's layout. |

## Expressions

Each expression is a set of **parameters**, and the parameters are *inputs* — the
rendered result is what matters, and the two are not obviously related. Measure
the output rather than guessing:

```bash
python3 expression_probe.py --report                       # measure every expression
python3 expression_probe.py --sweep 0.85/0.16/1.15         # measure candidates (curve/open/mult)
python3 expression_probe.py --band /tmp/b.png --rows \
    "0.55/0.10/1.0,0.85/0.16/1.15;0.60/0.02/1.15"          # eyeball a comparison sheet
```

Parameters (see `EXPRESSION_PARAMS`): `brow_l`/`brow_r` (radians, **mirrored** —
see below), `eye_open` (multiplier), `mouth_curve` (>0 smiles, <0 frowns),
`mouth_open` (vertical aperture as a fraction of mouth width), `mouth_w`
(multiplier on the mouth's width).

### Pip's expression is PINNED (temporary, 2 November)

**The kiosk holds one expression and does not mirror the person in front of it.**
`gaze.py` still computes the expression policy and still publishes its verdict — the
gaze bar is scored on it — but `face.py` ignores the `expression` and `gaze` keys of
the state file while the pin is set:

```bash
python3 face.py --pin-expression happy          # the default ($FACE_PIN)
python3 face.py --pin-expression off            # release it: the old mirroring returns
```

What the pin does NOT touch: **blinks and idle eye drift**, because those are the
face's own signs of life and a perfectly still frame cannot be told apart from a
crashed renderer on a projector. A forced `blink` from the state file is still
honoured for the same reason. What the pin is *for* is the mirroring — look at me and
I smiled, look away and I scowled — and the policy behind it is untended rather than
deleted, so a later trigger is a policy change plus one flag.

The decision lives in `apply_state_file()`, which is a plain function so that
`face.py --selftest` can assert the thing that is hard to see: that a pinned face
ignores a file which says otherwise. A pinned face and a mirrored face are both just
a face on a projector.

### The gaze bar

The participant's own score, drawn under the countdown while a stage's session runs:
a vertical bar that **fills** from the bottom while they are looking at the robot and
never drains. `session.py` accumulates the seconds and writes the fraction into
`/run/face/session.json`; `face.py` only draws it, so the bar on the projector and
the verdict in the log cannot disagree.

| what | value |
|---|---|
| target | `GAZE_TARGET_FRAC` in `session.py` = **50/60 of the session**, i.e. 50 s of looking inside the 60 s session |
| what counts as looking | `looking` in the gaze status file: someone in front of the robot AND head/gaze pointed at it — the same hysteresis that used to make the face smile |
| the bar | 54x616 px at x 1720, y 335 — **57.0% of the frame height**, starting 33 px below the disc and centred on its axis |
| fill measured | 25/50/75/100% of the target -> **155/309/463/617 px** of fill against 154/308/462/616 predicted (the +1 is the ink box being inclusive at both ends) |
| contrast | fill `rgb(124,189,213)`, **9.40:1** on the background; the empty track 1.26:1, like the countdown's |
| caption | `--gaze-caption word|percent|none`, `$FACE_GAZE_CAPTION`; the word, the percentage, or nothing |
| fill colour | `--gaze-ink` / `$FACE_GAZE_INK`: **`ramp`** (default), `ramp:warm`, `gradient`, `gradient:warm`, or a flat `accent`/`success`/`magenta`. A typo gets the default look, not a plausible-looking flat colour |
| full vs 99% | three cues, because one is not enough: the colour ramp, a **bright cap** across the top of a genuinely full bar (`GAZE_BAR_CAP_FRAC`, only at exactly 100%), and the **completion burst** |
| the burst | `GAZE_BURST_*`: 50 gold particles (30 sparks + 20 embers) over 1.8 s (`face.py --gaze-burst 0.4` renders a frame of it). A pure function of its age, so the kiosk stores only the instant the bar first reached full |

```bash
# measure it, don't look at the code
python3 gaze_bar_probe.py                       # fill proportionality, stray ink, collisions, contrast
python3 gaze_bar_probe.py --sweep               # just the fill table
python3 gaze_bar_probe.py --sheet /tmp/v.png    # labelled variants, to pick from pixels
python3 face.py --snapshot /tmp/bar.png --step session --gaze-progress 0.6
python3 face.py --snapshot /tmp/full.png --step session --gaze-progress 1.0 --gaze-burst 0.4
```

**The ramp is one colour per frame, not a gradient along the bar** — that reading and
the other one look quite different, so both are implemented and both are measured:

| fill | what it does |
|---|---|
| `ramp` (default) | the whole fill is one colour that shifts as it fills: magenta `rgb(172,101,201)` at 0% -> green `rgb(138,204,157)` at 100%. The colour IS the progress, so a glance at the bar reads the score |
| `gradient` | the bar uncovers a fixed magenta-at-the-bottom ramp, so the base stays magenta even at 100% and only the top arrives at green. Prettier, but a full bar is never wholly green |

The middle of either ramp is a colour nobody chose, so contrast is measured at every
sample, not at the stops: worst 4.6:1 for `ramp`, 5.3:1 for `gradient`. The floor
asserted in `--selftest` is **4.0:1** (a clearly legible large graphic), against 3:1
for large text and 2.4:1 for the countdown's number.

**The burst is bounded by geometry, not by taste.** The bar's top is 32 px below the
countdown's ring, so a celebration that rose would paint over the time left — and the
timer is the one thing on screen that must stay readable. Rather than shrink the burst
to fit, the particles are LAUNCHED with velocities that cannot reach it: sparks rise
at most 14 px, embers are thrown sideways past the bar's flanks and fall into the
empty background beside it. Measured: peak 8,842 changed pixels, never within 8 px of
the countdown, and empty again after 1.8 s.

`gaze_bar_probe.py` is the instrument: it diffs the fill against the same bar at
zero, checks that every changed pixel in a full frame lies inside the box the
renderer itself reports, and checks the bar against the countdown, the pill, the
caption and the footer. The colour hunt it does NOT do is deliberate — hunting for
an overlay by colour once missed a 26,850 px stray line on the countdown disc.

### The mouth has TWO render paths, and they fail differently

`_draw_mouth` switches shape on whether `mouth_open > w * 0.035`:

| path | trigger | what it looks like |
|---|---|---|
| **line** | `mouth_open` at or below `w*0.035` | a stroked curve; bow depth = `curve × w × 0.45` |
| **lens** | above it | a filled shape; depth dominated by `mouth_open × w` |

This matters because **`curve` barely affects a lens**. An open mouth's shape is
driven by the opening, not the curve, so raising `curve` on an open mouth does
almost nothing — the trap that made `happy` under-deliver.

**A wide, strongly-curved LINE is the shape that reads as a smile.** A lens reads
as an open mouth (talking), not as happiness. `speaking` and `curious` keep open
mouths for that reason; `happy` is a closed line.

### Measured, not guessed

`smile%` is how far the mouth's centre sits below its corners, as a percentage of
its width. `drop%` is the same for the bottom edge (the salient one for an open
mouth).

| expression | curve | open | mult | smile% |
|---|---|---|---|---|
| neutral (old) | 0.18 | 0.02 | 1.00 | **5.80** |
| neutral (now) | 0.12 | 0.02 | 1.00 | 3.80 |
| happy (old) | 0.55 | 0.10 | 1.00 | **3.24** |
| happy (now) | 0.60 | 0.02 | 1.15 | **18.92** |

The old numbers are the interesting part: **neutral bowed 5.80% while happy bowed
3.24%**, so neutral was the *smileier* of the two and the idle face looked pleased
with itself. Happy only seemed happier because its mouth was open, which reads as
"about to talk", not "pleased to see you". Neutral was flattened; happy got a 5x
corner lift and a wider mouth.

### Brow mirroring

`_draw_brow` is called with `-brow_l` / `-brow_r` and applies **no per-side
mirroring**, so the same numeric value rotates both brows the same way *on
screen*. The left brow's inner end is its `+x` end; the right brow's inner end is
its `-x` end. Getting both inner ends **down** (the angry V) therefore needs
**opposite signs** — hence `angry: brow_l=-0.38, brow_r=+0.38`. Verified by
rendering and looking, not by reasoning about the rotation.

### Adding an expression

Add the name to `EXPRESSIONS` (also the `--snapshot --expression` choices list)
and a dict to `EXPRESSION_PARAMS`. Every key must exist in `"neutral"` — that dict
is the default set, and `FaceState.update` merges over it. A key missing from
`neutral` cannot ease back when switching away from an expression that set it,
and would stick forever.

## Rung 2: the illustrated face and its looks

The illustrated register is rung 2 of the realism ladder. It shares the same
`FaceState` as the cartoon, so gaze, expression and lip-sync are preserved by
construction, and it draws from ONE parameter table (`ILL_BASE` + `ILL_LOOKS` in
`face.py`): palette, head width and length, eye size and spacing, brow weight and
height, mouth width and lip thickness, nose scale, ink weight, cel-shadow
strength.

**That table exists so alternatives are comparable.** Four copied renderers drift
apart, and then what is being compared is four drawings rather than one design
decision. Every look is the same code.

| look | what it is | pick it if |
|---|---|---|
| `warm` (default) | the palette and proportions the rung was built with | nothing has been decided |
| `soft` | lighter and rounder, finer ink, bigger eyes, brown hair | the face has to work for someone anxious |
| `graphic` | near-black ink, deeper cel shadows, saturated iris | it has to read from across a room |
| `mature` | narrower and longer face, smaller eyes, greying hair | the participant should feel they are with a peer |

```bash
python3 face.py --style illustrated --look soft --snapshot /tmp/soft.png
```

`--look` / `$FACE_LOOK`; a `screen["look"]` in `session.json` overrides it per
stage, the same shape as `screen["style"]`. An unknown name falls back to the
default and says so in the journal, because a kiosk must never lose its face to a
typo in a file.

### What was wrong with the first rung-2 render

Two faults, both found by measuring rather than by reading the drawing code:

- **The nose was drawn in the top-left corner of the frame.** `_draw_nose()`
  draws in HEAD-LOCAL coordinates and was called outside the head's translate, so
  it painted at the frame's (0,0): **7,131 px** of stray ink in a **42x202** box
  — a tapered brown band (the bridge shadow), a pale tip and two nostril blobs —
  and a face with **no nose on it**. So the "flatter than the painted reference,
  especially around the nose" note was a bug, not a limitation. The whole face is
  now drawn inside ONE head-local transform, so a helper that draws head-local
  cannot be called in frame coordinates again.
- **The mouth read as OPEN while smiling.** A band traced between two curves
  rises only 3/4 of its control offset, so the lip padding drew a lip about 1.35x
  thicker than intended; the lower lip came out as a fat lens, which reads as
  mid-sentence rather than as a smile. Lips are now 0.11/0.17 of the mouth's
  width, which reads closed, and `opening` (the speech drive) is the only thing
  that opens the mouth.

Measured after the fix: **0** non-background pixels in the top-left 240x240 of
every look; `--selftest`'s 60 cases still pass; `gaze_bar_probe.py` still passes
against this face under the overlays; and the **cartoon frame is byte-identical**
to the pre-change render (`cmp` on the PNG), so the running kiosk's face is
untouched by any of it.

## How it fits together

```
   vision (OAK-D)  ─┐
                    ├─→  /run/face/state.json    ─┐   (what it looks like)
   dialogue/LLM    ─┘                             │
                                                  ├─→  face.py  ─→  DP-1
   session conductor ─→  /run/face/session.json  ─┐       (GTK4/Wayland)
   voice listener  ───→  /run/face/prompt.json   ─┤
   voice listener  ───→  /run/face/mic.json      ─┘
                          (which screen, any confirmation prompt, and
                           whether the robot can hear you at all)
```

The **only** interface between the face and the rest of the robot is a JSON
file. The vision and dialogue components write it; the face reads it. Nothing
calls into the renderer, and the renderer calls nothing.

### State contract

`/run/face/state.json`:

```json
{
  "expression": "neutral",
  "gaze": { "x": 0.0, "y": 0.0 },
  "blink": false
}
```

- `expression` — one of `neutral`, `happy`, `curious`, `confused`,
  `listening`, `thinking`, `speaking`
- `gaze` — `-1..1` on each axis; `x` is left/right, `y` is up/down
- `blink` — set `true` to force one blink (it resets itself)

**A missing or malformed file is not an error.** The face falls back to a calm
neutral idle. A robot that stops showing a face because a helper process
crashed is worse than one that keeps breathing.

Write it atomically if you care about not being read mid-write — the face
re-reads on mtime change and ignores anything it cannot parse.

### Session screen contract

`/run/face/session.json` — a second file, kept separate for the same reason the
mouth file is: one writer per file, so the face can re-read on change without
racing anybody. **The session conductor (`robot/voice/session.py`) is the only
writer**; it is the process that knows what a stage is.

```json
{ "phase": "standby" }

{ "phase": "running", "stage": 2, "stages": 4, "step": "session",
  "deadline": 12345.6, "seconds": 60.0,
  "screen": { "kind": "face", "style": "illustrated" },
  "gaze": { "progress": 0.42, "looking": true, "looked": 25.4,
            "target": 50.0, "verdict": null } }
```

| `screen.kind` | drawn | when the conductor uses it |
|---|---|---|
| *(no `running` phase)* | the word `Standby`, centred, nothing else | power-on, and after a run finishes |
| `face` | the face, `style` selects the renderer (`cartoon`, `illustrated`) | stages 1 and 2 |
| `label` | the `text` large, `placeholder face` under it | stages 3 and 4, until those rungs have renderers |
| `success` | `Success` and a countdown | the 30 s after stage 4 |
| `stats` | `How that session went`, then the session as a 0 s → 60 s timeline: green blocks where the camera saw looking, a thin slate band where it did not, a line at the target | a stage that is waiting for the word again after a session that did not pass |

- The `stats` screen is the only one whose content is the session's HISTORY rather
  than its state, and it is drawn from `screen.spans` - `[[start, end, looking], ...]`
  in seconds - plus `screen.target` for the marker. `spans` is validated like every
  other field, and a `stats` screen whose timeline is missing or malformed is drawn
  as the **face** instead: an empty bar would say *"you never looked at me"*, which is
  a claim about the participant that a file that did not parse cannot support. The
  green is the gaze bar's own end colour, and the two states differ in HEIGHT as well
  as colour, because colour alone is not readable for everyone.
- `--screen stats --stats-spans '[[0,10,true],[10,60,false]]' --stats-target 50`
  renders one for review without a conductor.

- The `gaze` block is the participant's score, and **only the bar reads it**. The face
  draws `progress`; `looking`/`looked`/`target` are in the file so the journal's
  version of events can be checked against what was on screen, and the verdict
  (`"pass"`/`"fail"`) appears when the session ends. A `progress` that is missing or
  not a number draws **no bar at all** rather than an empty one: a bar at zero is a
  claim about the participant, and a file that did not say cannot support it.
- **The bar is drawn only for `step: "session"`**, alongside the countdown. During
  `waiting` there is nothing to score yet, and on `Success` the score is the pause's
  business, not the participant's.

- `step` is `waiting` (the stage is up and its timed session has not begun),
  `session` (gaze is being recorded), `speaking` (the stage's face is saying its
  session-end line, and nothing is counting down while it does) or `success`. It is
  what the greeter reads: it plays the stage 1 introduction only for
  `stage: 1, step: "waiting"`.
- `deadline` is a **`CLOCK_MONOTONIC`** timestamp, set only while a timer runs. The
  face computes the countdown from it rather than being sent a tick per second: the
  conductor writes the file only when it CHANGES, so a steady screen costs no writes.
- `seconds` is how long the step RUNS FOR, and it exists because the timer draws a
  FRACTION: a reader can compute what is left from a deadline but cannot derive an
  interval's length from its end, and a constant `60` in the renderer would be a
  second copy of the conductor's timer waiting to disagree with it. A file with no
  `seconds` draws no timer rather than an empty track, which would read as "time is
  up" when it only means "this file did not say how long".
- The bottom line during a stage is for the **operator**, not the participant:
  `stage 3 of 4`. It replaces the HUD, which is a development aid. It used to carry
  the clock as well; the timer is that clock now, several times the size, and two
  numbers to reconcile is one too many.

**A missing or unreadable file means `standby`**, deliberately. `/run` is a tmpfs,
so at power-on the file does not exist and the robot comes up waiting, with nobody
having to write anything: a robot that starts performing before anyone has started
it is the failure this screen exists to prevent. An unknown phase name is standby
too, and is logged rather than obeyed or ignored in silence.

**A `running` session with an unknown `screen.kind` is the one case that does NOT
fall back to standby**: it draws the face and says so in the journal. The two
failures are not alike - standby is honest when no session exists and a lie when one
does and someone is mid-exercise.

Writes are atomic (write, fsync, `os.replace`) because this reader falls back to
standby on anything it cannot parse — a half-written file would blink the screen
back for a frame.

The kiosk logs every screen change (`face: screen stage 2/4 session (face)`). That
line is the only mechanical evidence of what is on the projector; take it over
reading the drawing code.

### Prompt contract

`/run/face/prompt.json` — written by the **voice listener**, read by the face. It is
the confirmation window: what the robot heard, and how long is left to answer.

```json
{ "command": "start", "phrase": "marigold wombat sierra",
  "deadline": 12345.6, "timeout": 5.0 }
```

- Drawn as a panel in the **lower third**, over whatever screen is up. The screen
  underneath still answers "what is this robot doing", and a panel across the middle
  would hide the only thing on screen a passer-by can read.
- `deadline` is `CLOCK_MONOTONIC`, and an expired one means NO prompt: the listener
  deletes the file when the window shuts, but that is another process and up to a
  tick later, and a countdown that has run out must not linger on screen.
- Absent is the ordinary state. Nothing pending, nothing drawn.

### Pill contract

`/run/face/mic.json` — written by the **voice listener**, read by the face. It is
the green `Listening` pill in the top-left corner, and it is the one thing on screen
that is not part of any screen's story: it is drawn over standby, over a stage's face
or label, over `Success`, and over a confirmation prompt.

```json
{ "state": "listening", "heartbeat": 7807.966, "pid": 33190 }
```

- `listening` — the robot can hear you: the microphone is open and the half-duplex
  gate is not dropping anything. The pill is drawn.
- `speaking` — the robot is talking, so the gate IS dropping everything. **Nothing is
  drawn**: a green light while the microphone is muted would invite someone to talk
  into it.
- **The file is re-stamped on a heartbeat** (every 0.5 s) while the state is
  unchanged, and **a stamp older than 2 s means no pill**. This is the only thing
  that keeps a green light off a robot whose listener has died: a stopped service, a
  SIGKILL and a dead `arecord` all leave their last state on disk, and a stamp that
  has stopped moving is what tells them apart from a quiet room. A stamp from the
  *future* is not evidence either.
- Absent is the ordinary state for a robot with no listener, and means no pill.
- The file is deleted on a clean stop, so the pill goes out at once rather than
  waiting out the staleness window.

**Why the face draws it instead of another process having its own window.** This
process is the only Wayland client on the kiosk's Weston, and Weston has no
layer-shell (that is a wlroots protocol), so there is no composited overlay surface
another process could own. Anything that wants to add to the screen therefore
publishes a file: one writer per file, and the face draws them all. The pill is
independent of the session in the way that matters — it is drawn whatever the session
is doing — and `--listen` renders it in a snapshot.

Measured from the rendered frame (1920x1080): box at x 43..287, y 43..107, i.e.
**245x65 px, 6.0% of the frame height**, 43 px from each edge, centre colour
`rgb(104, 208, 142)`, entirely above the standby word's ink (which starts at y 456).

### Timer contract

Every timed thing in the run draws the SAME widget: the confirmation window's 5 s,
a stage's 60 s session, and the 30 s on `Success`. It is drawn by `draw_timer()`, it
is a function of `(remaining, total)` only, and the participant therefore learns one
object and keeps reading it wherever the run has got to.

| style | shape | where | measured |
|---|---|---|---|
| `bar` | a rounded track that drains left-anchored, with the seconds beside it | top centre | 845x159 px, **14.8% of the frame height** |
| `disc` | a ring that drains clockwise from twelve, seconds inside it | top right | 259x259 px, **24.0% of the frame height** |

Both are measured from the rendered frame, and both clear the stated floor of being
15% larger than the pill (245x65): the bar is 24x its area, the disc 4.2x.

**The disc is the default, decided 2 November from rendered frames of both.** The
reason is the one number that matters for a small projector: the disc's seconds are
**78 px of ink against the bar's 46 px**, and they sit on the plain background at
13.44:1 on both faces, where the bar's sit at 11.01:1 over the illustrated face's
hair and cannot grow without colliding with it (measured clearance: 22 px). The disc
also takes a quarter of the bar's footprint and stays out of the frame's middle, and
this is a gaze exercise - the face should be the thing to look at. It is a
legibility judgement, not a correctness one, so the bar is still built and
`--timer bar|disc` (or `$FACE_TIMER`) switches in one flag.

- **The number is never inside the bar.** Inside, it straddles the fill and the
  empty track as the bar drains, so it is readable over only one of them: measured,
  near-white on the filled accent composites to `rgb(98,144,164)`, which is
  **2.4:1** — below the 3:1 large-text floor. Beside the bar it is near-white on the
  background: **13.44:1** measured. The disc keeps its number on the background
  inside the ring for the same reason.
- **The top band, not the bottom.** The face is drawn to fill the frame — its head
  runs past the bottom edge with a body under it — so the lower band is occupied on
  exactly the two screens that have a face on them. The top band is empty on every
  screen but one, and the pill is already in it, so both instruments end up in one
  row at the same margin: mic state top-left, time left beside it. The bar clears the
  pill by 250 px. The exception is the **illustrated** face, whose hair reaches y 149
  and so lies under the bar's number: measured, that number is drawn over
  `rgb(43,30,34)` and still reads at **11.01:1**. The cartoon face's band is empty
  (13.44:1 over the plain background).
- **The track is drawn at every value**, so the bar's extent never changes. A bar
  that drew only the remaining part would shrink as an object, which reads as
  something collapsing rather than as time passing. Measured: 60.0 s -> 99.9% of the
  track filled, 21.0 s -> 34.9%, 6.0 s -> 9.9%, 0.0 s -> 0.0%.
- **A `waiting` stage draws no timer.** Nothing is counting down until the
  participant speaks, and a stopped countdown reads as stalled rather than as
  waiting.
- **The confirmation window always uses the bar**, whatever `--timer` says: a disc
  big enough to read is 259 px across, which does not fit inside a panel that is also
  carrying the question, and hanging it below the panel would put it over the
  screen's own content. Its number goes ABOVE the bar there, because the panel ends
  below it.

### How to run a shortened bench session

**FIRST, THE TRAP THAT COSTS A WHOLE RUN: `deploy.sh` copies files and never restarts
a service.** `face-kiosk.service` keeps executing the code it loaded at start-up, so
after deploying a change to `face.py` the projector still draws the OLD one and a new
feature looks missing in the room while being provably present on disk. Restart it,
and take the evidence from the process or from a log line only the new code prints:

```bash
./robot/deploy.sh
sudo systemctl restart face-kiosk.service     # otherwise the screen shows old code
ps -eo pid,lstart,args | grep face.py         # which code is actually live
```

**A run-through takes TWO verbs, not one.** `start` raises the stage and the session
only opens on the participant's stage word (`begin`); a run that has printed *"waiting
for the stage word"* is not hung, it is waiting. Publishing both yourself is faster and
more repeatable than waiting for someone to say them — the listener writes
`{"command": ..., "seq": <monotonic_ns>}`, and so can you (see `bench-looking.py`).

The target is a FRACTION of the session, so a shortened timer asks for the same
share of its own length (20 s ⇒ 16.7 s of looking) — which is how the bar is tested
without sitting through four 60 s stages. `session.service` owns the screen, so it
must be stopped first: **one writer per file.**

```bash
sudo systemctl stop session.service
cd ~/robot/voice
setsid nohup python3 session.py --session-secs 20 --success-secs 10 \
    >/tmp/bench-conductor.log 2>&1 </dev/null &
# ... run it, then hand the screen back:
sudo systemctl start session.service
```

**Check for strays before believing a result.** Two conductors writing `session.json`
make the screen flicker between two stages and make a one-shot animation fire twice,
because each writer's dip below the threshold re-arms the rising edge:

```bash
ps -eo pid,etime,args | grep "session\.py --session" | grep -v grep   # exactly one
```

When the detection itself is not what is being tested, `bench-looking.py` publishes a
constant "looking" verdict so the fill and the burst can be watched on the projector
without anyone holding still. It must publish faster than the reader's staleness
cutoff (`GAZE_STALE`, 0.75 s) and outlive the human's timing, and `gaze.service` must
be stopped first, because it owns that file:

```bash
sudo systemctl stop gaze.service
setsid nohup python3 bench-looking.py /run/face/gaze-status.json 1800 >/tmp/bl.log 2>&1 </dev/null &
# ... and afterwards:
sudo systemctl start gaze.service
```

Two things to take from the run afterwards, because neither is visible on the screen:

```bash
cat /run/face/session.json                  # the live gaze block
tail /tmp/bench-conductor.log               # the verdict, with looked seconds
tail /tmp/session-log.txt                   # the durable record
```

`bench-trace.sh` (this directory) does that sampling: it records `looking` and
`progress` on one timestamp line, so the fill can be shown to track the flag rather
than asserted to. Run it ON the board — through an SSH pipe it block-buffers and lags
the session it is supposed to be observing:

```bash
setsid nohup bash bench-trace.sh /tmp/bench-trace.txt 2400 >/dev/null 2>&1 </dev/null &
echo $! > /tmp/bench-trace.pid          # NOT pkill -f: that matches your own ssh
tail -f /tmp/bench-trace.txt
```

## Running it

### Snapshot mode — no display, no compositor, no session

This is how the face is reviewed. It renders one frame to a PNG:

```bash
python3 face.py --snapshot /tmp/f.png
python3 face.py --snapshot /tmp/f-happy.png --expression happy
python3 face.py --snapshot /tmp/standby.png --screen standby
python3 face.py --snapshot /tmp/label.png --screen label --label-text "Stage 3"
python3 face.py --snapshot /tmp/success.png --screen success
python3 face.py --snapshot /tmp/prompt.png --screen standby --prompt start
python3 face.py --snapshot /tmp/pill.png --screen standby --listen
python3 face.py --snapshot /tmp/timed.png --screen face --step session --timer disc
```

Use it to check a change before it ever reaches a screen — including from a
machine with no monitor attached.

`--step` is what decides whether a countdown is drawn, so a preview of one needs it:
`--step session` draws the timer, `--step waiting` draws none, and without it a
screen renders in its plain state (for the face: no stage, and the development HUD).
`--timer bar|disc` picks the countdown's shape, over `DEFAULT_TIMER_STYLE`.

`--screen` is snapshot-only, and deliberately so: the running kiosk takes the screen
from `$FACE_SESSION`, so this switch cannot make a running robot disagree with its
own session file. `--prompt start` draws the confirmation panel the way the
listener's file would, at a fixed point in its countdown, so two snapshots of one
build are byte-comparable.

### Under systemd (normal operation)

```bash
sudo systemctl start face-kiosk      # start now
sudo systemctl status face-kiosk     # is it running?
journalctl -u face-kiosk -f          # follow the log
```

### By hand, for debugging

```bash
/home/jetson/robot/face/kiosk-start.sh
```

Runs in the foreground; Ctrl-C tears down both the compositor and the face.

## Design notes

### Why Weston, not cage

Both were tested on this board and **both work**. Weston is
`nvidia-l4t-weston` — NVIDIA's own build for this exact L4T release — so it is
the supported path and the one less likely to break on a BSP or kernel update.
`cage` is upstream wlroots with no NVIDIA validation for Tegra DRM, and its
0.1.4 release predates Orin.

Note that `cage` logs alarming-looking errors on Tegra and then renders fine
anyway. Do not judge it by the log.

### Why the drawing code has no GTK dependency

`draw_face()` is a pure function of `(cairo context, width, height, state)`.
That is what makes snapshot mode possible, and it means a 3D renderer can
replace it later without touching the state contract or the kiosk plumbing.

### Rendering cost, measured

| Renderer in use | `face.py` CPU | `weston` CPU | RSS |
|---|---|---|---|
| **GL** (target) | ~74% of one core @ 30fps | low | 127 MB |
| **pixman** (fallback) | **~162% of one core @ 30fps** | ~5% | 265 MB |

Memory is fine — a few percent of the board, and Orin's memory is unified so
there is no separate VRAM pool to compete with the LLM for.

**The renderer choice is not cosmetic — it more than doubles the face's CPU.**
The compositor's renderer decides how the *client* renders: with a GL
compositor GTK4 takes its GPU path, and with a pixman (CPU) compositor GTK4
silently falls back to software rendering and burns roughly 2.2× the CPU for
the same picture. So the `gl` → `pixman` fallback in `kiosk-start.sh` is a
correctness-of-availability trade, **not** a free one: it costs real CPU that a
local LLM would otherwise have. Treat a fallback to pixman as a signal that
something is wrong, not as the intended state. Check which renderer won with:

```bash
journalctl -u face-kiosk | grep -o 'using the [a-z]* renderer' | tail -1
```

If this ever needs to share the CPU with real-time vision or a local LLM, the
levers are:

1. **Fix GL** (see trap 0 — the module load order). This is the big one: it is
   worth more than every other lever combined.
2. **Raise the frame interval.** `TICK_MS` in `face.py`. 30fps is generous for
   a face; 15fps still reads as alive and halves the cost.
3. **Move the face itself to GPU rendering.** GTK4's GL renderer, or a
   GL/shader-based face, offloads the drawing entirely. This is the right answer
   if the face gets genuinely realistic — which is a stated goal — because the
   cost then scales with visual complexity rather than CPU.

### Boot and failure behaviour

- Starts on `multi-user.target`, **not** `graphical.target` — it replaces
  gdm3/GNOME rather than running alongside it.
- `Conflicts=gdm.service display-manager.service` so a stray display manager
  cannot hold DRM master.
- `Restart=always`, `RestartSec=5`, rate-limited via `StartLimit*` in
  `[Unit]` (note: systemd **ignores** those keys in `[Service]`).
- If `DP-1` is not connected, the start script exits 1 immediately and
  systemd retries every 5s. Deliberately no in-script retry loop, so a
  machine left running without a display cannot spin the CPU or flood the
  journal.
- `RuntimeDirectory=face` gives `/run/face` to the service user. Without it
  the unprivileged user cannot create its own state file.

### Traps already hit — do not reintroduce these

**Two cairo traps in the disc, both found by diffing a frame against the same frame
with no timer** (colour-hunting for the timer aliases with the face's own pale ink,
which is why the first version of the test missed both). *`arc()` first draws a LINE
from the current point to the arc's start*, and the current point is wherever the
last text ended — the footer, at the opposite corner of the screen, so the disc drew
a diagonal across the face: **26,850 px** of it, in track colour, visible on a
snapshot as a thin line pointing from under the face up to the timer. `rounded_rect()`
starts with `new_sub_path()`, which is why the bar and the pill never had it. And *a
stroke does not consume the path*, so stroking the remaining arc re-stroked the whole
track in the fill colour: the ring was complete at EVERY value and **the drain was
invisible**. `new_sub_path()` before each arc, `new_path()` between them. The test
that catches the second one is a proportionality test — the draining ink must fall
with the fraction (measured now: 18,853 px at full, 9,243 at half, 4,760 at a
quarter, 0 at zero, against 70,233 / 35,117 / 17,181 / 0 for the bar).

**0. TWO things are needed to get GL working on this image — load `nvidia_drm`
at boot AND reload it once.** Neither alone is enough; both are covered in detail
below.

The vendor file `/etc/modules-load.d/nv.conf` ships `nvmap`, `nvgpu`, `pwm-fan`,
`ina3221`, `nvidia-p2p` — but **not** `nvidia-drm`, and nothing else pulls it in.

> **A stock desktop install hides this completely: gdm loads the NVIDIA stack
> incidentally.** Removing the display manager (which is the whole point of a
> kiosk) removes the thing that was loading your display driver. That is how
> this bites you *at the moment you think you have finished*.

**Fix, part 1: load it at all** — `/etc/modules-load.d/zz-nvidia-drm.conf`
listing `nvidia_modeset` then `nvidia_drm`. `systemd-modules-load.service`
honours that directory at sysinit (verified — it inserts the modules from
`nv.conf` on every boot). Options `modeset=1 fbdev=1` already come from
`/etc/modprobe.d/nvidia-display.conf`.

**Fix, part 2: then reload it once, because the first load is unusable.** This is
the part that actually makes GL work. `nvidia-drm-reload.service` +
`/usr/local/sbin/nvidia-drm-reload.sh` unload and re-load `nvidia_drm` once at
boot, before the compositor starts. Costs about **1 second**.

### The real cause: nvidia_drm initialising before the Tegra DRM stack

This cost a long evening, so the reasoning is recorded in full — including two
theories that were **tested and disproven**, because both look convincing.

The correlation, from three independent runs on this board:

| when `nvidia_drm` initialises | result |
|---|---|
| **after** `tegra_drm` (gdm's incidental late load) | GL works |
| **after** `tegra_drm` (any manual/reload path) | GL works |
| **before** `tegra_drm` (the sysinit modules-load.d path) | `EGL_NOT_INITIALIZED`, permanently |

Kernel log for the broken case — the ordering is the tell:

```
Initialized nvidia-drm ... for 13800000.display on minor 0    <- line 504
Initialized tegra 1.0.99 ... for drm on minor 1               <- line 541
```

When `nvidia_drm` wins that race, EGL is dead for the whole boot:

```
failed to initialize display
EGL error state: EGL_NOT_INITIALIZED (0x3001)
```

**Waiting does not fix it.** 3.5 minutes of uptime (past `systemd-udev-settle`,
which the unit already waits for) still fails. Only re-initialising the module
does.

> ### Two theories that were tested and are WRONG — do not retry these
>
> **WRONG 1: `modules-load.d` filename sort order.** The theory was that
> `systemd-modules-load` loads files lexicographically, so a `60-nvidia-drm.conf`
> would load `nvidia_drm` before the vendor `nv.conf`'s `nvgpu`/`nvmap`. Renaming
> it to `zz-nvidia-drm.conf` **did** reorder the modules as intended (kernel log
> confirmed: `nvgpu` 27.811s → `nvidia-drm` 28.287s, previously reversed) **and
> changed nothing** — GL still failed. Sort order is not the cause. Keep the
> `zz-` name anyway (it is harmless and correctly expresses intent), but do not
> expect it to fix anything.
>
> **WRONG 2: DRM minor-number assignment / `renderD129`.** An NVIDIA forum
> thread on this exact hardware reports that weston's `gl-renderer.so` resolves
> `/dev/dri/renderD129` regardless of the card it is given, and that swapping the
> nodes fixes it. Plausible — but on **this** board the mapping is **identical
> before and after a reload**: `renderD128`→`nv_platform` and
> `renderD129`→`drm` in both states, while GL goes from broken to working. So the
> node numbers are not what changes. `zz-nvidia-drm-order.conf` (a
> `softdep nvidia_drm pre: tegra_drm` load-order fix built on that theory) was
> written, then withdrawn uninstalled. It is kept as `.DISPROVEN` in the repo as
> a record.
>
> A third candidate — "nvidia_drm must be *fully probed* later, so load order is
> irrelevant" — was never cleanly tested, because `tegra_drm` has a refcount of 9
> and cannot be unloaded to build the sequence. Noted as genuinely unknown.

**Consequence: do NOT hardcode a card number.** `nvidia-drm` and `tegra_drm` can
each end up as `card0`, so `card1-DP-1` is not a stable path — it has been both.
`kiosk-start.sh` discovers the card that actually has a `connected` output. Note
the tegra node is **not a KMS device at all** (`ERROR: DRM device 'card1' is not
a KMS device`), so there is no fallback card to point weston at.

**Trap within the fix: use underscores, not dashes, in the module name.**
`modprobe@nvidia-drm.service` silently does nothing: systemd unescapes `-` in a
unit instance to `/`, so it runs `modprobe nvidia/drm`, which does not exist —
and because `modprobe@.service` has `ExecStart=-/sbin/modprobe`, the error is
swallowed and systemd reports **`success`**. Verify with `lsmod | grep
nvidia_drm`, never with a unit's exit status.

**1. `ProtectHome=read-only` breaks the compositor.** systemd implements it by
remounting `/home`, `/root` **and `/run/user`** read-only. Weston must create
its Wayland socket at `/run/user/1000/<socket>`, and GLib/dconf must write
`/run/user/1000/dconf/user`. The failure is obscure: weston logs normally
through EGL init, then exits before the socket appears, and the only visible
symptom is `kiosk-start: weston exited before creating a socket` plus repeated
`unable to create file '/run/user/1000/dconf/user': Read-only file system`.
Leave it out. The face code is read-only anyway, because the service user does
not own anything system-level.

**2. `StartLimitIntervalSec` / `StartLimitBurst` belong in `[Unit]`.** In
`[Service]` systemd ignores them with a warning that is easy to miss.
`systemd-analyze verify` catches it — run that after every change.

**3. `TimeoutStopSec`** — the default let systemd SIGKILL weston on stop
(`State 'stop-sigterm' timed out. Killing.`). 10s is plenty.

**4. `gdm.service` is `static` and cannot be `disable`d.** `systemctl disable
gdm3` prints nothing useful and changes nothing. What actually prevents it from
starting is the **default target**. Setting `multi-user.target` is the fix, and
it is already set. Verify with `systemctl get-default` — and confirm
`multi-user.wants/` contains **no** gdm symlink while it does contain
`face-kiosk`.

**5. When debugging the service, do not use `sudo -n systemd-run`.** It is not
in the scoped NOPASSWD list, so the command fails silently and it looks like the
service is broken. Run `/home/jetson/robot/face/kiosk-start.sh` directly instead
— that works and is what the unit invokes.

**6. The Wayland socket existing does NOT mean the compositor is ready.**
Starting the face app between socket creation and the compositor actually
serving clients makes GTK fail with `Gtk couldn't be initialized` /
`Connection reset by peer`. Hit reliably on a cold boot. `face.py` now retries
with a short backoff before handing over to GTK. Note that
**`Gtk.init_check()` is not a valid readiness test here** — measured on this
board it returns `True` even with no display at all. `Gdk.Display.get_default()`
is the one that tells the truth (`None` until connected).

**7. `/run/user/1000` must exist before the service starts.** It is normally
created by logind on login, but this is a *system* service and nothing logs in
— at a cold boot the directory does not exist and weston cannot create its
socket there.

Declare **`Requires=user-runtime-dir@1000.service`** (not merely `Wants=`) and
also run, once, on the device:

```bash
sudo loginctl enable-linger jetson
```

Both, because they cover different failure modes. `user-runtime-dir@1000.service`
carries `StopWhenUnneeded=yes`, so during a restart storm it can be torn down at
the instant a new instance of the kiosk asks for the directory — which it was,
producing `FATAL /run/user/1000 does not exist` from a service whose dependency
*was* declared. `Requires=` makes the directory's presence a precondition of
the unit; linger makes logind create it at boot and hold it up. The start script
also waits up to 10s for it, so a beat of latency does not cost a restart cycle.

This **cannot** be fixed from inside the start script alone: `/run` is mode 755
owned by root, so an unprivileged user cannot `mkdir` under it.

**8. The renderer fallback is a safety net, not a plan.** `kiosk-start.sh` tries
the `gl` renderer, then `pixman`. It exists because a kiosk showing a slower face
beats a kiosk showing nothing — but pixman makes GTK4 render in software and
**more than doubles the face's CPU** (see "Rendering cost"). If the journal says
`using the pixman renderer`, treat it as a fault to investigate, not the
expected state.

**9. A clean stop must exit 0.** The start script traps SIGTERM, tears weston
down, and returns success. Letting the signal kill it produced `status=143` and
`Result=exit-code`, so every ordinary restart looked like a crash in
`systemctl status`.

**10. Ghostty over SSH** needs its terminfo on the target, otherwise
`systemctl status` fails with `WARNING: terminal is not fully functional` and
the pager misbehaves. Copy it (no root needed):

```bash
# from the machine running ghostty
scp /nix/store/*-ghostty-*/share/terminfo/x/xterm-ghostty \
    jetson@orin.home:.terminfo/x/xterm-ghostty
```

## Not yet done

- **Default target is set to `multi-user.target`** ✅ — gdm will not start at
  boot. Confirm after reboot with `systemctl get-default`.
- Desktop-only services still running (`colord`, `packagekit`, `ModemManager`)
  — ~47 MB and unnecessary attack surface on a kiosk appliance.
- ~~The face does not yet read real gaze data~~ ✅ — `gaze.py` writes gaze,
  expression and blink into `/run/face/state.json` and the face follows it; Phase 3
  is done (`docs/STATUS.md` §4). Still open for the face: rungs 3-4 of the realism
  ladder, and the dialogue/LLM half of Phase 5.
