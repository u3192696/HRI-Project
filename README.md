# HRI 12060: the eye-contact practice robot

A prototype social robot. A cartoon face is projected onto a screen, an OAK-D Lite camera
tracks gaze and presence, and a local voice pipeline speaks. The board is a Jetson Orin Nano
Super devkit running the default JetPack image.

- `robot/`: the running code, in `face/`, `perception/` and `voice/`
- `cad/`: the physical rig, FreeCAD sources, printable meshes and drawings
  face-ladder study, and the presentation build
- `flake.nix`, `pyproject.toml`: the devShells and the Python environment
