# Project-local devShell for the Jetson Orin Nano Super work in this folder.
#
# Why this file instead of an entry in /home/nixos-config/nix/devshells.nix:
# this environment is specific to this project and unlikely to be reused
# elsewhere, so it lives with the project (same pattern as
# ~/Projects/hsk1-anki and ~/Projects/echo360-downloader). Reshaping or
# deleting it never touches the system flake -- no edit there is warranted.
#
# Entry: direnv (`.envrc` here runs `use flake`; direnv + nix-direnv are in
# home-manager core, so it just needs a one-time `direnv allow` in this dir)
# or ad-hoc via `nix develop`.
#
# Editing: add/remove packages in the list below, then re-cd (or save the
# file) -- nix-direnv watches flake.nix and rebuilds automatically on the
# next prompt.
{
  description = "NVIDIA Jetson Orin Nano Super dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      # Work happens on serenity (x86_64 host), not on the aarch64 devkit.
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      # face.py's only non-stdlib import is cairo. Kept as a named binding
      # rather than folded into the package list so the reason is at the
      # definition and `with pkgs` cannot quietly hand back a bare interpreter.
      pythonFace = pkgs.python3.withPackages (p: [ p.pycairo ]);

      # Shared libraries that the PyPI CAD wheels dlopen at RUNTIME. They are
      # not nixpkgs packages, so nothing puts their dependencies on the loader
      # path -- the first `import cadquery` dies with
      #   ImportError: libGL.so.1: cannot open shared object file
      # (OCP = OpenCASCADE, which links libGL even for headless geometry work).
      # libstdc++ is included for the same reason: the wheel was built against
      # a distro libstdc++, not ours. Verified by the import smoke test below.
      cadRuntimeLibs = with pkgs; [
        libGL
        libGLU
        # NOTE: bare `libx11`-style attrs, NOT `xorg.libX11` -- the xorg package
        # set is deprecated and every `xorg.*` use emits an eval warning.
        libx11
        libxext
        libxrender
        libxi
        libxt
        libsm
        libice
        libxkbcommon
        glib
        zlib
        freetype
        fontconfig
        stdenv.cc.cc.lib
        # Resolved one at a time by the import smoke test (the wheels dlopen a
        # long tail): expat comes up before libGL on this build of OCP, so
        # these are the ones its XML/geometry/image paths actually reach for.
        expat
        libxml2
        libpng
        libjpeg
        libtiff
        libwebp
        onetbb
        # X libs the OCC/VTK display-less paths still link against.
        libxfixes
        libxcursor
        libxrandr
        libxinerama
        libxdamage
        libxxf86vm
      ];
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          # Python is the established Orin workflow: scripts are edited here,
          # in this shell, and run on the board. Board-only packages (the
          # Jetson-specific ones) cannot be installed on NixOS.
          #
          # pycairo is not optional: face.py imports it and needs nothing else
          # outside the stdlib. A bare python3 here cannot run the renderer at
          # all, which would leave the face checkable only on the board -- and
          # the board has no screenshot tool. With it, `--snapshot` and
          # `--selftest` both run offline, which is the whole review loop.
          # Fonts are deliberately NOT added: face.py asks fontconfig for the
          # generic `sans`/`monospace`, and the system stack it resolves today
          # is the one the measured ink boxes in the self-tests were taken
          # against. Adding a font here would silently change those numbers.
          pythonFace
          # The voice work is measured, not listened to: ffmpeg's
          # asetrate/aresample/atempo is the pitch transform the recipe files
          # specify, and every clip check reads pitch back out of the WAV.
          ffmpeg
          # Per-project venvs / packages without polluting the closure.
          uv
          # Serial console to the devkit over its USB-C debug port.
          picocom
          # Pushing scripts/files to the board over ssh.
          rsync

          # CAD: render/check a .scad (or CadQuery/build123d-exported) part
          # headlessly -- `openscad -o out.stl part.scad`. Take the -unstable
          # attr deliberately: 2021.01 in nixpkgs stable trails upstream and
          # lacks the newer geometry features (see the CAD ranking notes in
          # this project's docs). FreeCAD/PrusaSlicer are deliberately NOT
          # here -- those are desktop apps and live in the Home Manager
          # `engineering/` modules on serenity + athena, not in a devShell.
          openscad-unstable
        ];

        # Libraries only (no /bin to add to PATH) -- see cadRuntimeLibs above.
        buildInputs = cadRuntimeLibs;

        shellHook = ''
          # Foreign wheels need an explicit loader path; nixpkgs inputs do not
          # populate it for us.
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath cadRuntimeLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

          # CadQuery + build123d come from PyPI through uv (see pyproject.toml
          # in this directory), NOT from nixpkgs: the wheels track upstream
          # OCP/build123d releases far faster, and nothing CAD-shaped enters
          # the system closure. First use in a fresh checkout:
          #   uv sync                  # creates .venv + uv.lock (gitignored)
          #   uv run python part.py    # or: uv run python -c 'import cadquery'
          # openscad (above) is on PATH for rendering any .scad source.
        '';

        # Likely future additions, uncomment as the project grows:
        #   pkgsCross.aarch64-multiplatform.stdenv  # cross-build for the aarch64 board
        #   cudaPackages.cudatoolkit                # CUDA-side tooling (heavy; pulls big closure)
      };
    };
}