# Voice, hearing and the run-through

What lives here, what each file owns, and where the contracts are. Read this before
changing anything in this directory: most of the files exist because of a specific
failure, and the comment at the top of each one says which.

## The three processes, and what each owns

| file | runs as | owns |
|---|---|---|
| `listener.py` | `listener.service` | the microphone. Hears `"system command <verb>"`, confirms with the pass phrase, publishes the command. Writes `/run/face/command.json`, `/run/face/prompt.json`, `/run/face/mic.json` (the listening pill), `/tmp/voice-detection.log` |
| `session.py` | `session.service` | the run-through: four stages, the 60 s session in each, the 30 s `Success`, the announcement clips. **The gaze bar inside a session is the gate**: reaching its target passes the stage, missing it repeats the same stage's session, so nothing reaches the next stage (or `Success`) without a pass. A pass or a miss is spoken FIRST by the face that just finished (`step: speaking`), and then - either way - the stage waits for the word with the **stats screen** up, so the participant can look at how the session went and take a break before the next attempt or the next face. The word is what starts a session after a miss and what moves the run on after a pass. Writes `/run/face/session.json` (the screen, the deadline, `seconds` - the length of the step, which the face needs to draw a countdown fraction - and `attempt`/`sessions_done` for the greeter's gate) and `/tmp/session-log.txt` |
| `greeter.py` | `greeter.service` | the 58 s stage 1 introduction, played when a subject holds for 10 s while stage 1 is waiting |

One writer per file is the rule that makes the rest work. Nothing calls into
anything else, so any of the three can be down and the others carry on: with the
listener down the conductor sits in standby, with the conductor down the robot
simply never changes screen.

**Where the pieces meet** (all of it is files, on purpose):

```
  mic ─→ listener.py ─┬─→ /run/face/prompt.json   ─→ face.py     (what was heard, 5 s)
                      ├─→ /run/face/mic.json      ─→ face.py     (can it hear you? pill)
                      └─→ /run/face/command.json  ─→ session.py  (what was confirmed)
                                                      │
                                                      └─→ /run/face/session.json
                                                            ├─→ face.py    (what to draw)
                                                            └─→ greeter.py (is stage 1 waiting?)
```

## The commands, and which of them act

The vocabulary is closed (`COMMANDS` in `keywords.conf`) so the decoder cannot
invent a command, and every one of them needs the `system command` prefix plus the
pass phrase before anything happens. Whether a confirmed verb then ACTS is a
separate question, and `EXECUTORS` in `listener.py` is the single place that
answers it:

| verb | what a confirmation does |
|---|---|
| `start` | publishes `start`: the conductor puts stage 1 up and the greeter may greet |
| `standby` | publishes `standby`: the conductor abandons whatever is running, from **any** state, and returns to the power-on standby. The operator's reset, so one stage can be tested without walking all four |
| `skip stage two` | publishes `skip stage two`: stage 2 goes up **waiting**, without walking stage 1, and is announced with its skip introduction. Also from any state, and the run it abandoned is reported the same way `standby` reports it |
| everything else | recognised, shown on the confirmation prompt, and logged as `WOULD EXECUTE (executor not wired)`. `shutdown` and `reboot` stay here deliberately |

The stage number in `skip stage two` is spelled out because this decoder's
vocabulary has no digit tokens — written as a digit the word is dropped from the
grammar with a warning, and the phrase it then listens for is not the phrase anyone
said. Spoken aloud, nothing changes.

`standby` is the only command that can interrupt: it applies mid-session as well as
between stages, and the gaze it discards is summarised into the log and labelled
**partial**, so an abandoned stage can never be mistaken for a completed one. It
cannot be heard while the robot is speaking either, because the half-duplex gate
drops the microphone through every clip.

## Supporting files

| file | what it is |
|---|---|
| `mic.conf` | the **pinned** capture source. There is no fallback: the board's default source records exact digital zeros |
| `mic.py` | the only thing that opens the microphone. `--list` names the pinned source and the trap |
| `keywords.conf` | the closed vocabulary: verbs, the pass phrase, the participant's stage word, and the reasons for each |
| `display.conf` | sink and lip-sync `--lead`. Both are properties of the *display*, not the robot |
| `lipsync.py` | drives the mouth from a clip's audio envelope |
| `clips/` | speech the robot performs. See its own README, including which clips are placeholders |
| `tones/` | tuning tones. Never spoken to anyone (plus one synthesised positive control for the voice listener: `positive-control-skip-stage-two.wav`, which must fire `PROMPT` then `EXECUTE` under `listener.py --test-file`) |
| `SCRIPT.md`, `intro*.txt` | what the robot says, and the words the introductions render from |
| `requirements.txt` | `vosk`, for the listener only. Device-local venv, device-local model |
| `fetch-model.sh` | re-fetches the recognition model against a pinned checksum (needs network) |

## Running one of them by hand

Every one of these takes `--dry-run` (do the whole thing, play no audio) and
`--selftest` (drive the logic with no audio, no camera and no service). Run the
self-test before believing a change:

```bash
cd /home/jetson/robot/voice
/usr/bin/python3 session.py --selftest          # the whole run-through, synthetic clock
/usr/bin/python3 greeter.py --selftest          # presence, the gate, the hold
./.venv/bin/python listener.py --selftest       # the ritual, the vocabulary, the gate
```

Only ONE of each may run at a time. The units are single-instance, but a script run
by hand alongside its service doubles every log line and, for `session.py`, fights it
for the screen.

## Installing the units

Each unit has an installer that **does not copy code**: deploy first
(`cd ~/Projects/hri-project && ./robot/deploy.sh`), then on the board

```bash
sudo bash install-listener-service.sh    # needs the device-local venv and model
sudo bash install-greeter-service.sh
sudo bash install-session-service.sh
```

All three pre-flight their prerequisites and refuse to run if something is missing,
enable the unit, and leave it **not started**: arming a robot to listen, or taking
over a screen someone may be looking at, should be a decision rather than a side
effect of installing.

## Where the detail lives

- `docs/STATUS.md` §4 and §6 — the current state, the run-through, and the units.
- `robot/face/README.md` — the screen, prompt and state contracts, from the face's side.
- `clips/README.md` — the clip library, provenance, and which files are placeholders.
- `AGENTS.md` — the project map.
