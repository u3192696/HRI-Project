# Auditioned voices

Reference renders of one line, kept so a voice decision can be revisited
without re-downloading models or trusting memory of how something sounded.

All render the same line:

> Hello! My name is Pip, and I am a robot. I'm here to practise eye contact with
> you, but only in the way that suits you.

**The chosen voice is `pip`** — `en_GB-alan-medium` pitched up 2 semitones and
sped up 6%. It is *not* one of the files here; it is produced by
`render-intro.sh --preset pip` and shipped as `../intro.wav`.

## The files

| file | preset | what it is |
|---|---|---|
| `04-cori-high-uk-female.wav` | `cori` | UK female, high quality. Natural, unfiltered. |
| `05-vctk-s0-uk.wav` | `vctk-s0` | VCTK speaker 0 — brighter and noticeably quicker. |
| `06-vctk-s51-uk.wav` | `vctk-s51` | VCTK speaker 51, UK. |
| `07-hfc-female-pitch3.wav` | `hfc-up3` | US female, pitched up 3 semitones. Cartoon register. |
| `08-ljspeech-pitch3.wav` | `ljspeech-up3` | US female, pitched up 3 semitones. |
| `09-CHOSEN-alan-pitch2-speed106.wav` | `pip` | **The chosen voice**, kept here as the delivered artefact. |
| `voice-audition-reel.mp3` | — | The original 9-candidate reel, each announced by a robotic label. |

## Not preserved, deliberately

- **`en_US-hfc_female-medium`** and **`en_US-ljspeech-high`** *unpitched*. Only
  their pitched-up variants were judged worth keeping; the unpitched originals
  were rejected.
- **`en_GB-alan-medium`** unmodified is reproducible with
  `--preset alan-stock` and is not duplicated here.

## What was measured, not guessed

Median F0 and 10–90 pitch spread per voice, which is what separates "friendly"
from "boring" objectively:

| voice | F0 | spread |
|---|---|---|
| alan (stock) | 100 Hz | 25% |
| **pip (+2 st, 1.06x)** | **110 Hz** | — |
| hfc_female | 253 Hz | 217 Hz |
| ljspeech | 212 Hz | 187 Hz |
| cori | 220 Hz | 137 Hz |
| vctk speaker 0 | 200 Hz | 279 Hz |

Stock Alan is both low *and* flat. Perceived warmth tracks mean pitch and pitch
*variation*; he has neither. The 2-semitone lift keeps his delivery while
raising the floor.

## Adding a candidate

    ./render-intro.sh --voice en_GB-jenny_dioco-medium --out /tmp/cand.wav
    ./render-intro.sh --voice <voice> --speaker <n> --pitch 3 --speed 1.05 --out /tmp/cand.wav

`en_GB-vctk-medium` has **109 speakers** and `en_US-libritts_r-medium` has **904**,
so both are real search spaces rather than single options —
`--speaker N` selects one. The full catalogue is `voices.json` in the Piper
voices repository (177 voices, 177 with English variants).

If a candidate wins, add it to `PRESETS` in `render-intro.sh` so it has a name,
and record it here.
