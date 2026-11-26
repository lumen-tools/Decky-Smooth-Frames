# Third-party software

## afmf-linux (bundled, modified)

`bin/libafmf-linux.so` is built from afmf-linux 1.3.1
(https://github.com/serialexperimentslainnnn/afmf-linux) with three changes in
`patches/afmf-linux-1.3.1-smooth-frames.patch`: a base frame rate limiter, a control file that
turns generation on and off and changes the limit while a game runs, and handling of every
structure that may extend a present, so frames stay paced in every game (`scripts/build-layer.sh`
reproduces the build). The licence texts ship in `bin/` and are copied to the plugin's data
folder (`licenses/`).

- afmf-linux: MIT License, Copyright (c) 2026 Lain <lain@digitalexperiments.dev>
- AMD FidelityFX Optical Flow shaders vendored by afmf-linux: MIT License,
  Copyright (c) Advanced Micro Devices, Inc. (see `NOTICE.fidelityfx.md` and
  `LICENSE.fidelityfx.txt` shipped with afmf-linux)

## Decky plugin template

BSD 3-Clause License, Copyright (c) 2022-2024 Steam Deck Homebrew.

## Libraries in the frontend bundle

- @decky/api, @decky/ui: LGPL-2.1 (linked as libraries)
- react-icons: MIT
- tslib: 0BSD
