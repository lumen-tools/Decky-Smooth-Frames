# Decky Smooth Frames

Driver-style frame generation for the Steam Deck, in the spirit of AMD Fluid Motion Frames and
NVIDIA Smooth Motion: one interpolated frame between every two real frames, paced in the middle.
The interpolation itself is done by the open-source
[afmf-linux](https://github.com/serialexperimentslainnnn/afmf-linux) Vulkan layer. The plugin ships
its own build of it with one addition, a **base frame rate limiter** (the counterpart of Radeon
Chill next to AFMF on Windows), switches it on per game from the Quick Access Menu and shows
whether it is really generating frames.

> **Not affiliated with or endorsed by AMD, NVIDIA or Valve.** It is not AMD's AFMF: afmf-linux is an
> independent implementation of the same idea. "AMD", "Fluid Motion Frames", "Steam" and
> "Steam Deck" are trademarks of their owners. afmf-linux is a community project, too, and the
> Steam Deck (RDNA2) is not officially tested by it yet.

**Tested on:** Steam Deck LCD.

## Features

- **Built around the running game**: start a game, open the Quick Access Menu, flip *Smooth Frames*.
  The game's name and its live status sit right under the toggle; the frame rate limit and the
  frame generation options appear once it is on. Every game keeps its own settings; a newly enabled game starts
  from the device defaults. The plugin writes the Steam launch option for you
  (`<wrapper> <appid> %command%`) and keeps any options you already had.
- **Live status**: reads the layer's own statistics and shows whether frames are really being
  generated (real FPS → output FPS) and plain-language reasons when not (frame rate too low,
  pass-through, layer not loaded, frames only repeated, …), plus a hint when a change needs a
  game restart.
- **afmf-linux's frame generation options, named after AFMF 2.1 in AMD Software** (collapsed
  under *Frame Generation Options*): Performance Mode (motion estimated at half or full
  resolution), Search Mode (how far motion is searched) and Fast Motion Response (repeat or blend
  where motion is uncertain). On the Steam Deck, Performance Mode *Auto* resolves to
  *Performance*, as AMD does for integrated graphics. Gamescope is detected automatically at
  launch.
- **Frame rate limit** for the game's real (base) frames, like Radeon Chill next to AFMF on
  Windows: on the Steam Deck's screen (no VRR) half the refresh rate lets frame generation fill every refresh
  (default 30 on the 60 Hz LCD, 45 on the 90 Hz OLED). Docked to a VRR monitor you can raise it or
  switch it off. The limiter lives inside the layer and delays the game's next frame after each
  present, like a driver limiter does.
- **Nothing to install separately**: the layer ships with the plugin and is registered under its
  own name (`VK_LAYER_DECKY_SMOOTH_FRAMES`, enabled by `SMOOTH_FRAMES_ENABLE=1`), so an upstream afmf-linux
  installed by other tools never loads alongside it.

## Install

1. Install [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader).
2. Download `Decky-Smooth-Frames.zip` from the
   [latest release](https://github.com/lumen-tools/Decky-Smooth-Frames/releases/latest).
3. Decky settings → *Developer mode* on → *Developer* → *Install plugin from ZIP* → pick
   `Decky-Smooth-Frames.zip`.
4. Start a game, open the Quick Access Menu, turn on *Smooth Frames*, then **restart the game** once (the
   layer is loaded when the game starts). From then on it is on for that game.

## How it works

```
Steam launch option:  /home/deck/.local/share/decky-smooth-frames/smooth-frames-run <appid> %command%
                         │
                         ├─ reads ~/homebrew/settings/Decky Smooth Frames/env/<appid>.env
                         │    (SMOOTH_FRAMES_ENABLE=1, AFMF_SEARCH_MODE=…, AFMF_PERFORMANCE_MODE=…)
                         ├─ AFMF_MAX_BASE_FPS=<limit> when the limit is on
                         ├─ sets AFMF_GAMESCOPE=1 when the game runs under Gamescope
                         ├─ writes a session marker + the layer's log for the live status
                         └─ exec %command%  →  afmf-linux (Vulkan implicit layer) interpolates
```

The layer manifest only activates with `SMOOTH_FRAMES_ENABLE=1`, so nothing else on the system is
affected. Turning a game off writes `SMOOTH_FRAMES_DISABLE=1` and removes the launch option.

With a base limit set, the wrapper also lowers afmf-linux's minimum frame rate (30 by default)
to 85 % of the limit; otherwise a game held at 30 FPS would sit on that floor and generation would
keep switching off.

### Recommended settings

| Display | Base FPS limit (plugin) | Steam's frame limit |
|---|---|---|
| Steam Deck LCD (60 Hz) | 30 | 60 |
| Steam Deck OLED (90 Hz) | 45 | 90 |
| External monitor with VRR | off, or higher | the monitor's refresh rate |

"Allow Tearing" is not needed: afmf-linux presents in mailbox mode. In-game V-Sync is not needed
either.

### Steam's frame limit and the performance overlay

Steam's frame limit applies after the generated frames are added, so set it to 60 and use the plugin's limit for the base frames. The performance overlay also counts after the generated frames (about 60 with a base limit of 30).

## Uninstalling

Press *Remove Smooth Frames from all games* first (that also cleans the launch options), then uninstall
the plugin; it removes its layer from `~/.local`. If you uninstall the plugin first, the wrapper
stays in place and simply starts games without frame generation.

## Limits

- Vulkan only (Proton DX9–DX12 via DXVK/vkd3d-proton is fine); 64-bit games only; OpenGL games
  are not covered.
- Colour buffer only (no motion vectors): UI and fast motion can show artifacts. Below 30 real
  FPS afmf-linux pauses frame generation (its default floor; the live status says so).
- Changes apply at the next game start.

## Building

Needs git, CMake ≥ 3.28, Ninja, GCC, glslang, Vulkan headers ≥ 1.3.250, Node.js and pnpm.

```sh
./scripts/release.sh       # builds the layer and the frontend → release/Decky-Smooth-Frames.zip
```

`scripts/build-layer.sh` alone builds `bin/libafmf-linux.so`: afmf-linux 1.3.1 with
`patches/afmf-max-base-fps.patch` applied.

## License

BSD-3-Clause (see `LICENSE`), based on the Decky plugin template. Third-party notices in
`THIRD_PARTY.md`.
