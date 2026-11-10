# The spoken introduction — script and voice

This is what the robot says when it introduces itself. It is **pre-rendered to
WAV** (not spoken live) so the voice is high quality and costs the Orin no CPU
at all. The lab has no LAN, so any cloud voice has to be baked in at build time
— which is exactly what pre-rendering gives us.

The robot ships a **file**, not a TTS engine. No model, no Piper, no network at
runtime.

---

## Script

**This document deliberately does not quote the script.** The `.txt` files are
the single source of truth, and quoting the text here made it drift once already
— a version quoted in this file had commas where `intro.txt` had em-dashes.
Read the file.

| file | status |
|---|---|
| `intro.txt` | v1 — rendered and auditioned; superseded |
| `intro_v2.txt` | **rendered and shipped** — Danny's rewrite; since 4 November its last line names the stage word, so Pip and Owen both end by asking for "Let's Begin" |
| `stage2_intro.txt` | **rendered** — Owen's introduction (Danny's wording, 4 November), played when stage 1 completes |
| `stage2_skip.txt` | **rendered** — Owen's introduction, played when stage 2 is skipped to |

| draft | lines | words | rendered |
|---|---|---|---|
| v1 | 6 | 151 | 50.8 s |
| v2 | 10 | 171 | 58.0 s |
| `stage2_intro.txt` | 6 | 95 | 38.1 s |
| `stage2_skip.txt` | 11 | 171 | 59.6 s |

v1's figures are the Piper render. v2 and the two stage 2 clips are Breeze renders of
the voice ladder (see below), which is the engine Pip actually speaks through — that
is why their lengths differ from the Piper draft. Loudness is matched by integrated
loudness, not peak: see `clips/README.md`.

---

## Stage 2: Owen, and its two introductions

**The rung 2 face is a different person, and it has a name.** Pip is the cartoon;
the illustrated face is **Owen** — named 3 November 2026. The ladder's decision is
four distinct personas rather than one character getting more realistic, so the
change of face is accompanied by a change of name.

Owen's introduction used to say the voice was unchanged "because it's the same
robot"; Danny's own rewrite of 4 November drops that line, so nothing in the spoken
script now explains why a new name is wearing a familiar voice. One clause after
"my name is Owen" would restore it — see `owen-voice.txt` for the voice itself.

The name was chosen against the same criteria `keywords.conf` records for trigger
words: two sonorant syllables, no common homophone ("Pip" was decoded as *pit*,
"robot" as *robert*), nothing close to any of the six verbs, the pass phrase or the
stage word, and maximally distinct from Pip's short high plosive.

### Owen's voice: neutral register, at the cost of the 25% placement

The voice climbs the same ladder as the face (`breeze-tts/render-ladder.sh`), and
the rung descriptions are the ladder — same text, same model, same seed, only the
description changes. Pip **is** rung 1. Danny's brief for Owen was a voice **25%
closer to real on the scale from Pip (0) to the stage 4 ceiling (1)**, which is not
the same thing as "the rung 2 description" and had to be measured.

Measured on the four ladder renders with Praat (median F0, and melodic range as F0
p10-p90). Only two features move monotonically along the ladder; HNR (11.3, 15.2,
13.4, 13.7 dB), pause count (23, 30, 15, 25) and articulation rate do **not**, so
they are excluded rather than averaged in. Both surviving features are normalised
across rung 1 to rung 4 and combined with equal weight:

| rung | median F0 | melodic range | register pos | range pos | **composite** |
|---|---|---|---|---|---|
| 1 Pip (cartoon) | 291.5 Hz | 15.5 st | 0.00 | 0.00 | **0.00** |
| 2 animated assistant | 266.6 Hz | 14.4 st | 0.10 | 0.12 | **0.11** |
| 3 warm conversational | 165.8 Hz | 13.3 st | 0.60 | 0.24 | **0.42** |
| 4 natural human | 114.0 Hz | 6.4 st | 1.00 | 1.00 | **1.00** |
| **Owen, skip clip** | **180.1 Hz** | 13.2 st | 0.51 | 0.26 | **0.39** |
| **Owen, intro clip** | **176.3 Hz** | 13.6 st | 0.54 | 0.21 | **0.37** |

**The register here is a choice Danny made after hearing the first version.** As
rendered, the voice measured 0.18 and 0.26 — on target — but both of us read it as
*female* on first listen, and that impression is carried by exactly the measurement
the axis is built on: 215-245 Hz is adult-female register. So the clips are now
shifted to 176/180 Hz, the middle of the 155-185 Hz band where adult male and
female distributions overlap, which moves them to **0.37 and 0.39**. Neutrality
costs the 25% placement; it does not cost quality — HNR-cc is 13.4 dB against 13.9
before the shift, and the spectral centroid (1998/2115 Hz) sits in the range of the
natural rungs (2246/2127). The recipe and the rejected deeper shift are in
`owen-voice.txt`.

Three consequences worth knowing:

- **The stage 2 clips are Breeze renders, like Pip.** They were briefly Piper
  (`en_GB-alan-medium`, preset `pip`) while the ladder was being built; that made
  Owen a different engine from Pip, which the ladder never intended.
- **The model is near-bimodal on register** (265-336 Hz for "animated/bright",
  167 Hz for "adult/unhurried"), it ignores instructions to sound androgynous
  (223/248/279 Hz at three seeds), and seed plus description does not hold identity
  across texts — so `owen-voice.txt` records the description, the seed, the shift
  and the cloning order, not just the description.
- **The second clip is cloned from the shifted first.** Order matters: shifting two
  independently rendered clips left one at 214 Hz (still female), because their
  deliveries differ by 3.3 semitones. Cloning the skip from the shifted intro makes
  the pair 0.4 semitones apart instead.

Stage 2 is reached two ways, and the two situations are not the same, so there are
two scripts:

| file | plays when | what it does |
|---|---|---|
| `stage2_intro.txt` | stage 1's session completes | Danny's own wording (4 November, 95 words): introduces itself as Owen, congratulates the participant on reaching stage 2, promises to work with them until stage 3, revises the process (monitoring, looking away is fine, not a staring contest, pause or stop), then invites the stage word |
| `stage2_skip.txt` | `system command skip stage two` | Pip's introduction (`intro_v2.txt`) word for word, renamed to Owen, with the last sentence replaced by "When you're ready, just say \"Let's Begin\"" |

Which one plays is **not** the operator's choice: `session.py` records how the stage
was entered (`Session.entry` — `advance` or `skip`) and `announce_name()` picks the
clip. Nobody who arrives by skipping has met Pip, so the congratulating version
would congratulate them on something they did not do.

### Why the stage word changed

All three scripts end by telling the participant to say **"Let's Begin"** — the two
stage 2 scripts since 3 November, Pip's from 4 November, when its last line stopped
being "Whenever you're ready, shall we begin?". The listener therefore accepts that
phrase as a stage word alongside the measured `moonlight`. A script that teaches a
word the robot cannot hear is a lie told to a participant mid-session, which is why
the alias exists rather than a rewritten script.

`keywords.conf` records the measurement, and it has now changed twice with Owen's
voice. In the old (Piper) voice the intro clip's final words decoded as `'[unk]
[unk] lets begin'`, so the whole-endpoint rule refused it — **0 events**. In the
first Breeze voice, clearer diction decoded the same clip as exactly `'lets begin'`
at confidence 1.00 — **1 event** in the dry run, with only the half-duplex gate
between the robot and its own stage word. The neutral register of 4 November
degrades it again: `'[unk] [unk] [unk] lets begin'` (intro, 0.98) and `'[unk] [unk]
lets begin'` (skip, 1.00), both **0 events**. Pip's greeting now ends the same way
(4 November) and measures `'[unk] lets begin'` at 1.00 — 13 endpoints, 0 events, but
the margin is a single unknown word. The wording was never changed to earn any of
these results; the voice moved and the endpoint moved with it, which is exactly why
every clip is re-measured after every render rather than trusted.

### One line in the skip version to decide

`stage2_skip.txt` is Pip's script with the name and the last sentence changed, as
asked, so it keeps one line that does not quite hold when a participant has jumped
straight to rung 2:

*"When I can see you're comfortable, we'll move to a more realistic face."* — for
someone who skipped, this is the face they are already looking at.

It was kept, not silently repaired. Edit `stage2_skip.txt` and re-render if it
should change; nothing else depends on the wording. (The "it can only good happen"
line is deliberate and stays exactly as written.)

---

## Design rules behind the wording

1. **It says what it is**, not what it isn't pretending to be. No false
   personhood, but no cold disclaimer either.
2. **It discloses the monitoring before it monitors.** Gaze watching is stated
   up front, in plain language, as part of the greeting — this is the
   informed-consent mechanism expressed *inside* the interaction itself.
3. **Progression is earned by comfort, not by staring.** The masking literature
   is the reason: a participant must never read comfort behaviour as failure.
4. **It hands over control:** pause, stop, or take a break. The participant is
   the one in charge.
5. **It ends by inviting, not commanding.** "When you're ready" — the participant
   chooses the moment, and naming the phrase out loud is what makes it sayable.

---

## The script is ahead of the prototype, deliberately

**The script is written to the Phase-5 target, not to the prototype as it runs
today** (Danny, 2026-10-08). Writing the introduction first is the right order:
the script states the interaction contract, and the prototype is built to meet
it.

Every row below is therefore a **Phase-5 work item, not a defect** — but it is
recorded, so the gap is tracked rather than assumed away. The demo will be run
against the prototype, and the audience will hear the script.

| What the script promises | How Phase 5 delivers it | State today |
|---|---|---|
| "I can see where you're looking" | `gaze.py` publishes gaze into the face state | **working** |
| "casual eye contact" | the gaze-level threshold that drives the expression policy | **working but unexposed** — the threshold exists; nothing rewards it |
| "monitoring how comfortable you are" | **heart rate, via a Polar H10 chest strap** over BLE | sensor not yet integrated; no HR code in the tree |
| "remaining calm" | same HR signal | not built |
| "you will receive a reward" | administered by the robot, or by the tester after the session | not built |
| "move to a more realistic face" | **three further personas**, each progressively more realistic | **built** — rungs 2-4 (Owen, Milo, Elliot), each with a face and its own voice |
| "pause or stop at any time" | participant-facing control | no mechanism; only SIGTERM cleanup exists |

### Phrasing: the one open wording question

Line 6 makes progression contingent on the participant *demonstrating* a state —
"when you are able to maintain casual eye contact while remaining calm".

Gating advancement on measured calm is legitimate; it is how graded exposure and
biofeedback work, and the reward *is* the relaxation. The concern is narrower
than the mechanic: the phrasing frames it as a test the participant passes.
"When I can see you're comfortable" describes the same gate as the robot's
observation and leaves the participant less to prove. Phrasing only — no
capability change needed. Worth a look; not worth blocking on.

### Heart rate: settled, and one interaction consequence

**Decided: a Polar H10 chest strap over BLE.** This removes the rPPG problem
entirely — no 30 fps requirement, no motion confounder, and no dependence on
lighting. Two consequences worth stating plainly:

- **The BLE half already works.** The Orin's controller reports
  `Roles: central`, BlueZ is 5.64 with `bluetooth.service` active, and
  python-dbus plus `gatttool`/`hcitool` are present on L4T. `bleak` is *not*
  installed, and the JetPack venv does not exist yet, so there is a venv step —
  but the platform is capable today.
- **It changes what the participant is told.** The chest strap moves the HR
  sensor off the camera, so the script's "I can see where you're looking" no
  longer implies "and that is how I know your heart rate." Those become two
  separate disclosures: eyes *and* a worn sensor. The current script discloses
  only the gaze.

The second point is the one that matters for consent. The robot now watches the
participant **and** asks them to wear something that measures them — and the
introduction is the only place that can say so before it starts.

Two smaller notes for whenever the sensor work begins: a strap battery below
~10% and a chest strap that is on but not wetted both look like signal loss, so
"no data" needs to be distinguishable from "not wearing it". And the H10 exposes
RR intervals rather than a plain BPM, which is a *better* signal — HRV is exactly
the graded-exposure metric — but it is not HR, so the mapping needs deciding
rather than assuming.

---

## Why this wording

- **"Looking away is not a mistake. It's allowed."** is doing the heavy ethical
  lifting. The masking literature is the reason: a participant must not read
  comfort behaviour as failure. Three short sentences, escalating, so the last
  one lands plainly. This is the design commitment made audible.
- **"this is not a staring contest"** names the failure mode out loud. A
  participant told to practise eye contact will otherwise reasonably assume more
  is better.
- **"Until you are ready, you simply stay with me"** reads as *not yet* rather
  than *you declined* — the right register for someone who is anxious.
- **"I'll show you a more realistic face"** states the actual mechanic, so no
  participant is surprised by the face changing.
- **No promises about outcomes.** It never says "this will help you." That is
  deliberate — the proposal makes no clinical claim and the robot must not
  either.
- **"I'll follow your lead"** inverts the usual robot-dictates framing.
- **"Pip"** gives the participant something to address. It is one token in the
  script, so changing it is free.

---

## Timing

Measured from the rendered WAV, not estimated.

| draft | words | rendered | effective pace |
|---|---|---|---|
| v1 | 151 | 50.8 s | 178 wpm |
| v2 | 171 | 58.0 s | 177 wpm |

Both include the engine's pauses at sentence boundaries, so articulation is a normal
~150 wpm and the two drafts are the same pace to within a word per minute. v1 was
rendered by Piper and v2 by Breeze, so neither the pauses nor the length are the same
kind of measurement — the pace is the comparable figure.

**Check the session length against the script.** v2 says "we will talk for a
minute", but the introduction alone runs 61 s — the robot spends as long
introducing the minute as the minute lasts. Either the intro gets shorter or the
session gets longer.

---

## Voice

Default preset is **`pip`**: `en_GB-alan-medium`, pitched up **2 semitones** and
sped up **6%**.

Alan unmodified is a flat voice — measured **100 Hz median F0 with only 25%
pitch variation**, which is why it reads as boring. Perceived warmth tracks
*mean pitch* and *pitch variation*, and stock Alan has neither. The preset is a
pitch/formant shift that preserves duration (`asetrate` moves pitch *and* speed;
`atempo` puts the duration back), giving ~110 Hz while keeping Alan's delivery.
It brightens the voice rather than replacing it.

Auditioned alternatives are kept in `auditions/` and selectable by preset, so a
change of mind costs one flag:

    ./render-intro.sh --list-presets

| preset | voice | transform |
|---|---|---|
| `pip` | en_GB-alan-medium | +2 st, 1.06x — **chosen** |
| `alan-stock` | en_GB-alan-medium | none (the original) |
| `cori` | en_GB-cori-high | none |
| `vctk-s0` | en_GB-vctk-medium | speaker 0 |
| `vctk-s51` | en_GB-vctk-medium | speaker 51 |
| `hfc-up3` | en_US-hfc_female-medium | +3 st |
| `ljspeech-up3` | en_US-ljspeech-high | +3 st |

The auditioned WAVs are in `auditions/` (see `auditions/README.md`). The two
rejected voices, `en_US-hfc_female-medium` and `en_US-ljspeech-high` *unpitched*,
are deliberately not preserved — only their pitched-up variants, which were the
ones judged.

---

## Rendering

Runs on a machine **with** network (serenity or athena), never on the Orin.

    ./render-intro.sh                                # preset: pip
    ./render-intro.sh --preset cori                  # a different auditioned voice
    ./render-intro.sh --pitch 3 --speed 1.05         # ad hoc overrides
    ./render-intro.sh --list-presets
    ./render-intro.sh --text intro_v2.txt --out intro.wav

Writes `intro.wav` next to the script. It does **not** deploy — `deploy.sh` does
that.

**Piper is stochastic.** Rendering the same text twice produces different bytes,
so comparing two renders by hash proves nothing about pronunciation — compare
what the audio actually sounds like.

### Two traps in this pipeline, both of which produce a file that *looks* fine

**1. Do not pass `--sentence-silence`.** In this piper-tts build that option
emits **broadband noise** where the silence should be, not silence: with it,
~40% of the output measures as noise; without it, ~12% (and that residual is
just sibilants). A single sentence rendered in isolation sounds fine either way,
so it only shows up on multi-sentence input — exactly the case you would not
spot-check. Piper already pauses naturally at sentence boundaries.

**2. Piper writes an odd `data`-chunk size** for 16-bit PCM, which is malformed:
a sample cannot split across a byte boundary, and ffmpeg and aplay report
*"Invalid PCM packet, data has size 1 but at least a size of 2 was expected"*.
The script trims the trailing byte and rewrites both size fields. Without this
the file plays on some players and not others — the worst kind of bug to find
during a demo.

### The build verifies itself

`render-intro.sh` measures its own output and **fails the build** if more than
25% of it measures as noise, rather than trusting that a render succeeded. It
also reports median pitch, so a regression to a flat voice is visible at build
time.

| render | speech | noise | pitch |
|---|---|---|---|
| v1 (shipped) | 83.9% | 11.8% | 110 Hz |
| v2 (draft) | 87.2% | 9.0% | 111 Hz |

An audible defect that a listener had to catch by ear is now a build failure.
