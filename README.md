# HRI 12060: the eye-contact practice robot

A prototype social robot. A cartoon face is projected onto a screen, an OAK-D Lite camera
tracks gaze and presence, and a local voice pipeline speaks. The board is a Jetson Orin Nano
Super devkit running the default JetPack image.

**Starting a session here? Read [`AGENTS.md`](AGENTS.md) first.** It is the map: what to read in
what order, where the durable knowledge lives, and the conventions that save you time. This
README is deliberately thin so that it cannot go stale.

- `robot/`: the running code, in `face/`, `perception/` and `voice/`
- `cad/`: the physical rig, FreeCAD sources, printable meshes and drawings
- `docs/`: `STATUS.md` (current state, read this first), `HANDOVER.md`, lab runbooks, the
  face-ladder study, and the presentation build
- `host-config/`: configuration that is live on the board, such as the sudo policy
- `flake.nix`, `pyproject.toml`: the devShells and the Python environment

## A note on history

This repository once carried an L4T flash toolchain, a rootfs rebuild, a recovery-ramdisk repro
and a BSP target profile. Those belonged to an earlier, separate project which needed them
because Ollama and GPIO required substantial OS and firmware modification. They were removed on
2026-10-31 so the two projects could not be confused again. `git log` has the full story, and
`git revert` brings any of it back.

A separate project at `~/Projects/Arduino/` holds the LED and Ollama work that the earlier project
produced. It is deliberately not part of this repository.
