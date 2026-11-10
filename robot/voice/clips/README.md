# Voice clip library

Speech the robot actually performs. **If a file is in here, the robot may say
it to a participant.**

## What belongs here, and what does not

| | where | why |
|---|---|---|
| the voice library | `clips/` — here | Speech, chosen and promoted. Committed. |
| the workshop | `~/Projects/breeze-tts` | Where clips are generated and auditioned. Not this repo. |
| test signals | `../tones/` | Tone bursts and cues for measuring lip-sync. Not speech, never spoken to anyone. |

That split is the point: `clips/` answers *"what does the robot say"*, `tones/`
answers *"how do we measure it"*. They should never be confusable.

## Promotion: workshop → library

A clip enters this directory only once it has been **decided on**. The route:

1. **Generate and audition** in `~/Projects/breeze-tts`.
   `./render-ladder.sh` renders all four rungs from
   `../nvidia-orin/robot/voice/intro_v2.txt` — it reads that file directly, so
   the text and the audio cannot drift apart.
2. **Listen.** `output/voice-ladder-reel.mp3` is the loudness-normalised reel.
3. **Decide.** Until a clip is chosen it stays in the workshop.
4. **Promote** the chosen clip here, under a name describing the clip:
   ```
   cp ~/Projects/breeze-tts/output/ladder/rung1-pip-cartoon.wav \
      robot/voice/clips/rung1-cartoon-intro.wav
   ```
5. **Record its provenance** in the table below, with the source hash.
6. **Commit**, then `./robot/deploy.sh` to land it on the robot.

The workshop copy is deliberately **not** deleted: `voice-ladder-reel.mp3` is
built from all four rungs, so removing one breaks rebuilding the reel. Two
copies means two things that can drift — which is exactly why the hash goes in
the table. `sha256sum` both sides and compare before trusting either.

## Naming

`<voice-rung>-<content>.wav`

- **rung** — `rung1-cartoon`, `rung2-animated-assistant`,
  `rung3-warm-conversational`, `rung4-natural-human`
- **content** — `intro`, `greeting`, … what the clip *says*, not which file it
  came from.

Generator filenames describe the render (`rung1-pip-cartoon`); library filenames
describe the role. That is why they differ, and why the provenance table matters.

**Stage 2's four clips break the rule and are left alone.** They are `stage2-intro`,
`stage2-skip`, `stage2-pass` and `stage2-retry`, named by stage rather than by rung
(`rung2-animated-assistant`), because they were made before the rule settled. Renaming
them would churn the stage table, the installer's pre-flight list and every doc that
points at them, in exchange for consistency nobody reads. A reader who wonders why
rung 2 is the odd one out now knows: it is older, not different.

**The session-end lines are named by the VOICE, not by the stage, and that is the
one place the rule above needs care.** Each stage's face says one line when its
session ends - `pass` when it reached the target, `retry` when it did not - so the
obvious names would be `stage1-pass.wav`, `stage2-pass.wav`, and so on. But stage 2's
face is rung 2's renderer, and **as of 5 November every stage speaks in its own voice**:
Milo's lines are `rung3-warm-conversational-*` and Elliot's are `rung4-natural-human-*`.
The rule survived two stages of borrowing (stages 3 and 4 both used Owen's
`stage2-retry.wav` while they had faces but no voices), and its whole point was that the
borrowing was VISIBLE in the name - `stage2-retry.wav` played under Elliot's face said
exactly where the voice came from. That is over: a stage may now only name clips
belonging to its own rung, and `session.py --selftest` asserts it.

| clip | voice | says | used by |
|---|---|---|---|
| `rung1-cartoon-pass.wav` | Pip (rung 1) | *"Great job! Time for you to move on to my friend Owen."* | stage 1, on a pass |
| `rung1-cartoon-retry.wav` | Pip | the repeat line | stage 1, on a miss |
| `stage2-retry.wav` | Owen (rung 2) | the repeat line | stage 2, on a miss |
| `rung3-warm-conversational-intro.wav` | Milo (rung 3) | his stage 3 introduction | stage 3, on arrival |
| `rung3-warm-conversational-pass.wav` | Milo | *"Great job! Time for you to move on to my friend Elliot."* | stage 3, on a pass |
| `rung3-warm-conversational-retry.wav` | Milo | the repeat line | stage 3, on a miss |
| `rung4-natural-human-intro.wav` | Elliot (rung 4) | his stage 4 introduction: the last stage, nothing after him | stage 4, on arrival |
| `rung4-natural-human-pass.wav` | Elliot | *"That's it - you've completed the training. There are no more stages…"* | stage 4, on a pass — **and it ends the run** |
| `rung4-natural-human-retry.wav` | Elliot | the repeat line | stage 4, on a miss |
| `stage2-pass.wav` | Owen (rung 2) | *"Great job! Time for you to move on to my friend Milo."* | stage 2, on a pass |

**The repeat line** (same words, both voices, one text file each):

> Good try! Let's practice that again. When you're ready, just say "Let's begin".

Text lives beside the audio, as it does for every clip here: `../rung1_pass.txt`,
`../rung1_retry.txt`, `../stage2_retry.txt`, `../stage3_intro.txt`, `../stage3_pass.txt`,
`../stage2_pass.txt`, `../stage3_intro.txt`, `../stage3_pass.txt`, `../stage3_retry.txt`,
`../stage4_intro.txt`, `../stage4_pass.txt`, `../stage4_retry.txt`, read by
`say.sh --text-file` so the words and the WAV cannot drift apart.

The repeat line has no stage number and no mention of the gaze bar in it, on purpose:
Danny intends further pass conditions later (heart rate among them), and a line that
says *"the bar didn't fill"* would be wrong the day one of those lands. The words live
beside the audio in `robot/voice/*.txt`, as every clip's do, so the two cannot drift.

A stage with no line is SILENT rather than wrong: the conductor logs
`no line for this stage` and moves on, which is why the two empty cells above are a
state of the project rather than a bug.

## Provenance

| library file | generator source | sha256 |
|---|---|---|
| `rung1-cartoon-intro.wav` | Breeze TTS 2, rung 1 description + seed 42, **cloned from the previous Pip clip** so the voice is the one already calibrated against the projector, then -1.5 st and 0.9% slower (F0 back to ~291 Hz and pace back to 177 wpm; the fresh render came out 1.5 st high and 9% quicker) — see "Re-rendering Pip's intro" below | `038f8b9579f84e0474144895e688bfcf21a212f34092869ea2a806c3ad9b94d0` |
| `stage2-intro.wav` | Breeze TTS 2, description + seed 21, then **-3.5 st** pitch/formant shift; the anchor everything else follows — recipe in `../owen-voice.txt` | `5393f77358a7c7ef754e67f690d276ad767c44426a05c5c2f981f2405f4ef02c` |
| `stage2-pass.wav` | Breeze TTS 2, Owen's description + **seed 12**, cloned from `owen-final-ref.wav`; seed 21 on the same recipe came out at **251.6 Hz** and seed 12 at 187.5, so the render is the seed's, not the clone's — trimmed **−1.07 st** onto the anchor, landing 178.8 Hz against the intro's 176.3 — text `../stage2_pass.txt`, recipe in `../owen-voice.txt` §4 | `d7bf7c57d47a05653e1fa453e19fdc999dd2425bfae548311e63036d0b5f8290` |
| `stage2-skip.wav` | Breeze TTS 2, description + seed 21, **cloned from the shifted intro** so the pair is one person at one register — recipe in `../owen-voice.txt` | `58694888c09f1f7b7edad65f46d0ef7e1287d6109ce188a37fdac831bf44eb84` |
| `rung3-warm-conversational-intro.wav` | Breeze TTS 2, rung-3 description + seed 42, **cloned from a 14.2 s cut of the ladder's own rung-3 render** (the voice Danny chose on 5 November, not a re-roll of the description), no shift needed: the clone landed at 166.7 Hz against the anchor's 165.8 — text `../stage3_intro.txt` | `1f530c1d7780a4b45f4d32325dc5937b6098e7a3045bc6d9b7bcc15bf34584a7` |
| `rung3-warm-conversational-pass.wav` | cloned from a 5.9 s cut of the intro **that contains the name Milo**, so the clone is told who it is; came out 153.4 Hz and was shifted **+1.34 st** onto the anchor, landing 166.5 — text `../stage3_pass.txt` | `3c3855ccbb5031d0c1e68c6890fcb570afda15ce102bb1c95a2d2a2774484518` |
| `rung3-warm-conversational-retry.wav` | same reference, same delivery; came out 176.3 Hz and was shifted **−1.06 st** onto the anchor, landing 165.9 — text `../stage3_retry.txt` | `fc92cfc0b162a29d93ec9810b63216adda004901fc9d1a95e9c4f3abf90da650` |
| `rung4-natural-human-intro.wav` | Breeze TTS 2, **E2's description (the sex stated) + seed 42**, cloned from a 9.18 s cut of the E2 render; came out 1.4 st low and was trimmed **+1.37 st** onto the anchor, landing 114.4 Hz — text `../stage4_intro.txt` | `2759caac84dd59b8b8c5be8601e205dd01f27b036fa445912acd5be9eb131467` |
| `rung4-natural-human-pass.wav` | cloned from a 5.2 s cut of the **trimmed** intro that contains the name Elliot; 114.2 Hz, no second shift — text `../stage4_pass.txt` | `ba06dd8974b0e177d78d574795495a30a67dfa354b4c1b4b54f1c3e8e99816e6` |
| `rung4-natural-human-retry.wav` | same reference, same delivery; 113.3 Hz, no second shift — text `../stage4_retry.txt` | `b22c2916f5ec90c16ce5c3d523b508f9961d948c7c451fdf998982e888896b07` |
| `rung1-cartoon-pass.wav` | Breeze TTS 2, rung 1 description + seed 42, **cloned from a cut of the shipped Pip intro** (the same `pip-ref.wav` its own re-render used) so it is the voice already calibrated against the projector, with a warm-encouragement delivery note, then **-3.3 st** pitch/formant shift: the clone came out at 335.7 Hz against the intro's 277.9 Hz, and lands at 277.2 Hz — text `../rung1_pass.txt` | `080e7dde63857a4d94363331e2bf433e2d0b9fcc53cc316ec993d47a40b6daba` |
| `rung1-cartoon-retry.wav` | the same recipe and text file `../rung1_retry.txt`; 336.2 Hz before the shift, 281.7 Hz after | `68eea23275fa5001e0c7639ae4ce38cd9a6d9bbbd8ad86c53a2cf5b09685e3eb` |
| `stage2-retry.wav` | Breeze TTS 2, Owen's description + seed 21, **cloned from `owen-final-ref.wav`** (the shifted-intro reference its own skip clip was built from), same delivery note, then **-1.65 st**: 194.1 Hz before the shift, 176.6 Hz after, against the intro's 176.1 Hz — text `../stage2_retry.txt`, and it is the clip stages 2, 3 AND 4 repeat with | `73dd7f6e204a91c869ae3e2f633df676f3c242be6ed35835dfe178cd5893f5cc` |

**Every session-end line needed a register correction, and it is the delivery that costs
it** - though not always in the same direction, which is why each line is measured and
none is predicted. Milo's pass came out *lower* than its anchor by 1.34 st (153.4 Hz)
while his retry came out higher by 1.06 st (176.3 Hz), from the same reference, the
same delivery note and the same clone. Asked for a line that sounds glad to keep going rather than disappointed, both
voices came back out of the clone measurably HIGHER than the clip they follow — Pip by
3.3 st on both lines, Owen by 1.65 st — because cheerful *is* a higher register. The fix
is the one the ladder already uses for its own intro (`../owen-voice.txt` §2): `asetrate`
+ `aresample` + `atempo`, which moves pitch and formants together and keeps the duration,
so the character is unchanged and only the register moves. Measure with

```
praat --run <script> <file.wav>     # median F0 over voiced frames
```

against the clip the line FOLLOWS, not against the number in an older note: this tool
reads the shipped Pip intro at 277.9 Hz where the notes record 291.5/303 Hz, and the
shipped Owen clips at 176.1 and 180.0 Hz where the notes record 176 and 180.1. So the
tool agrees with the Owen numbers and not with the Pip ones, and comparing a new render
to the SHIPPED FILE with the same tool is the comparison that means something.

**The repeat line was tested against the listener before promoting it**, because it says
"let's begin" inside a sentence and the phrase must not fire on the robot's own voice:
`./.venv/bin/python listener.py --test-file <clip + 1.2 s of silence>` on the board
reports `3 endpoint result(s), 0 event(s) — no trigger, no prompt, nothing executed` for
both voices. The dry run feeds the clip to Vosk UNMUTED, so this is the worst case; live
there are two more defences on top of it (the half-duplex gate drops the audio feed
entirely while the mouth file is fresh, and `rec.Reset()` on every suppressed block).

Rendered from `../intro_v2.txt` — 171 words, 58.0 s, 24 kHz mono PCM, 177 wpm
including pauses: the same pace as the clip it replaced.

### Re-rendering Pip's intro

`intro_v2.txt` is Pip's script and `rung1-cartoon-intro.wav` is the only clip built
from it. On 4 November 2026 its last line changed from "Whenever you're ready, shall
we begin?" to "When you're ready, just say \"Let's Begin\"" so that stage 1 teaches
the stage word out loud, like stage 2 does. The re-render had to be *the same
voice*, and description plus seed does not hold identity across texts, so it is a
clone of the clip being replaced:

```bash
cd ~/Projects/breeze-tts
# 1. Reference: the first three sentences of the OLD clip, cut on its 0.49 s pause
#    at 10.92 s, with the transcript the cut actually contains.
ffmpeg -y -i ~/Projects/hri-project/robot/voice/clips/rung1-cartoon-intro.wav \
       -t 10.92 -c:a pcm_s16le output/audition/pip-ref.wav

# 2. The new text, rung 1's description (from render-ladder.sh), cloned.
./say.sh --text-file ~/Projects/hri-project/robot/voice/intro_v2.txt \
  --instruction "A cheerful, clearly artificial cartoon companion voice. Pitched high, bright and slightly buzzy, with a simple upbeat delivery. Sounds like a friendly animated sidekick and is obviously synthetic." \
  --cfg-scale 4 --seed 42 \
  --ref-audio /out/audition/pip-ref.wav \
  --ref-text "Hello. My name is Pip, and I am a robot. I'm here to practice eye contact with you, but in a way that suits you. There are two things I'd like you to know first." \
  --output /out/audition/pip-intro.wav

# 3. The clone came out 1.5 st high (317.5 Hz against the old clip's 291.5) and 9%
#    quicker (52.8 s against 57.9), and four seeds all landed 317-347 Hz, so both
#    are put back explicitly. asetrate drops the register with the formants;
#    atempo 0.9924 (not 1/0.9170) is what also restores the pace.
ffmpeg -y -i output/audition/pip-intro.wav \
  -af "asetrate=24000*0.9170,aresample=24000,atempo=0.9924" \
  -c:a pcm_s16le output/audition/pip-intro-final.wav
```

Measured against the clip it replaces: median F0 291.5 Hz (the shifted file's own
Praat reading is 303 Hz — the tracker is noisier on resampled audio, and the
centroid confirms the shift applied, 2688 → 2443 Hz being the 0.917 factor asked
for), spectral centroid 2438 Hz, HNR 12.2 dB against 11.3. Duration 58.0 s against
57.9 and 177 wpm against 177, so the pace is unchanged too.

### Stage 2 has TWO introductions, and both are real now

Stage 2 is reached two ways, so it has two clips, chosen by the conductor from how
the stage was entered (`session.py`'s `announce_name()`), not by the operator:

| clip | when | script | length |
|---|---|---|---|
| `stage2-intro.wav` | stage 1 finished | `../stage2_intro.txt` — Owen introduces itself, congratulates, revises the process, promises stage 3 | 38.1 s |
| `stage2-skip.wav` | `system command skip stage two` | `../stage2_skip.txt` — Pip's introduction, renamed, ending on "just say 'Let's Begin'" | 59.6 s |

Both are **Breeze TTS 2**, the same engine as Pip's introduction, because the voice
ladder is one model climbing four described rungs and Pip *is* rung 1. Owen sits
between rungs 2 and 3 (measured 0.18 and 0.26 on the ladder's axis against a 0.25
target); `../owen-voice.txt` holds the description, the seed and the cloning recipe,
and `../SCRIPT.md` holds the measurement. **Do not re-render these with Piper** —
that was a stopgap while the ladder was being built, and it made Owen a different
engine from Pip.

### There are no placeholders left, and the library is four voices

Both `stage3-intro.wav` and `stage4-intro.wav` were Piper stopgaps: one sentence each,
the stage's number and the word "introduction", rendered on the bench because no rung
owned those lines yet. Their names deliberately broke the `<rung>-<content>` rule, and
that was the point - a name that claimed a rung would have been a lie about which voice
the line belonged to.

Both are deleted, replaced on 5 November by `rung3-warm-conversational-*` and
`rung4-natural-human-*` clips. Every clip in this directory is now the voice of the face
above it, which is the invariant the ladder existed to reach.

Worth noticing what the replacement cost, because it is the difference between a
placeholder and a line: each placeholder was 3 words and 2 seconds. The real
introductions are 85 and 91 words, 31.2 and 31.7 seconds, because an introduction has to
do the work - congratulate, revise the rules, say what happens next, teach the stage
word, and hand over. **A placeholder states a fact; a line is part of the training.**

Everything here is levelled to **about -21.3 LUFS integrated**, so no clip is
louder than another in the room. This used to be a PEAK match (-5.8 dBFS), which
was wrong for the Breeze renders: peak-matching Owen against Pip left it 4.4 LU
quieter, because its crest factor is different. Measured, integrated loudness and
sample peak:

| clip | integrated | sample peak |
|---|---|---|
| `rung1-cartoon-intro.wav` | -21.3 LUFS | -2.3 dBFS |
| `stage2-intro.wav` | -21.1 LUFS | -3.9 dBFS |
| `stage2-skip.wav` | -21.3 LUFS | -3.7 dBFS |
| `rung3-warm-conversational-intro.wav` | -21.3 LUFS | -1.6 dBFS |
| `rung3-warm-conversational-pass.wav` | -21.3 LUFS | -3.6 dBFS |
| `rung3-warm-conversational-retry.wav` | -21.3 LUFS | -3.5 dBFS |
| `rung4-natural-human-intro.wav` | -21.3 LUFS | -1.2 dBFS |
| `rung4-natural-human-pass.wav` | -21.3 LUFS | -2.9 dBFS |
| `rung4-natural-human-retry.wav` | -21.3 LUFS | -2.5 dBFS |
| `rung1-cartoon-pass.wav` | -21.3 LUFS | -6.4 dBFS |
| `rung1-cartoon-retry.wav` | -21.3 LUFS | -6.1 dBFS |
| `stage2-retry.wav` | -21.3 LUFS | -6.7 dBFS |
| `stage2-pass.wav` | -21.3 LUFS | -6.1 dBFS |

The peak now varies by clip and that is fine: what a listener hears as "louder" is
loudness, and every file stays more than 1 dB below full scale.

To re-render any of these, follow the recipe file for the voice, not this README:
`../owen-voice.txt`, `../milo-voice.txt` (§4) and `../elliot-voice.txt` (§5) each carry
the description, the seed, the clone reference and the order to apply them in. The
clone reference matters as much as the description, because the anchor is a render and
not a recipe - and for Elliot the reference is a cut of the ALREADY-TRIMMED clip, which
is why the pass and retry needed no second correction.

Level with the gain `ebur128` reports against -21.3 LUFS, never by peak.

## Size

Each 170-word render is ~2.8 MB of 24 kHz mono PCM. A large library will make
this repo heavy. If that starts to hurt, the options are FLAC (roughly half the
size, and the driver would need to decode it) or keeping the library out of git
behind a documented fetch. Neither is needed yet; noting it so the decision is
made deliberately rather than discovered.
