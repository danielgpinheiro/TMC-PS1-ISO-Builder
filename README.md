# The Minish Cap PS1 ISO Builder

Build a PlayStation 1 disc image of **The Legend of Zelda: The Minish Cap** from your own GBA cartridge's ROM. The PS1
executable is prebuilt; this repository contains the tools that convert the ROM for the PlayStation and put the disc
together.

**No game data is included.** You need a ROM dumped from your own cartridge (USA), see [Usage](#usage).

## What does this do / why is it needed?

The port runs the game's own code (the [zeldaret/tmc](https://github.com/zeldaret/tmc) decompilation, through the
[PC port](https://github.com/MatheoVignaud/tmc)'s layer) on the PlayStation's 2 MB of RAM. The GBA reads its 16 MB
cartridge directly; the PS1 can't, so everything is converted **once, on your computer**:

- **The ROM** → a virtual ROM on the disc: the game's data split into segments the PS1 loads per area and per room
  (with per-room load manifests, so the next room's data is usually already read when Link walks in).
- **Music and sound effects** → the game's own sequencer runs on the PS1's sound chip, like the GBA's: its 271 samples
  converted to SPU-ADPCM and loaded with the songs as one sound bank per area (as Legend of Mana does per scene), so
  the CD drive is free for the game's data.
- **Per-area packs** → each area's most-needed data copied back to back on the disc and read at the area change, so
  play inside the area rarely waits for the CD.
- **A global set** → Link's common sprite pages and the effects every area uses, read once at boot and kept in video
  memory for the whole game, so the first roll, pickup or lifted pot of a room doesn't wait for the CD.
- **Text** → the save messages reworded for the memory card.
- **The executable and its code overlays** carry tables that are also in the ROM; they are shipped with those bytes
  taken out (`bin/files/holes.json` says where) and completed from your ROM, then checked against the original build's
  SHA-256.

Graphics are drawn by the PS1's GPU from the GBA's own tile, sprite and palette formats as the game writes them.

## Usage

1. Dump the ROM from your own **The Legend of Zelda: The Minish Cap** cartridge, **USA** release (unmodified,
   16 MB, SHA-1 `b4bd50e4131b027c334547b4524e2dbbd4227130`; the builder checks it).
2. Install the [requirements](#requirements).
3. Run:
   ```bash
   python3 build_iso.py --rom /path/to/baserom.gba
   ```
   Optional: `--license licensea.dat` (see [the license file](#the-license-file)), `--out folder` (default `output/`).
4. The disc image is `output/TMC-PS1.bin` + `TMC-PS1.cue` (about 61 MB), with its SHA-256 in `output/SHA256SUMS`.
   The build takes about a minute.

The builder verifies the image byte by byte (EDC/ECC, license sectors, every file) before it finishes.

## Requirements

Tested on macOS (Apple Silicon) with Python 3.9, clang 22 and mkpsxiso 2.30. Linux works the same way; on Windows,
use WSL.

| Tool | What for |
|---|---|
| Python 3.8+ (no extra packages) | the converters |
| a C compiler (clang or gcc) | the SPU-ADPCM encoder, built on the fly |
| [mkpsxiso](https://github.com/Lameguy64/mkpsxiso) | writes the disc image |

The builder finds `mkpsxiso` on your `PATH` or through the `MKPSXISO` environment variable.

**macOS:** `xcode-select --install`, then download mkpsxiso from its
[releases](https://github.com/Lameguy64/mkpsxiso/releases) and put it on your `PATH`.
**Linux (Debian/Ubuntu):** `sudo apt install python3 build-essential`, then mkpsxiso as above (or build it with CMake).

## The license file

PlayStation discs carry Sony's license data, which retail consoles check at boot. It can't be distributed, so by default
the image is built **without** it: it plays in emulators (DuckStation, PCSX-Redux, …), on optical drive emulators and
on modded consoles. If you have `licensea.dat` (NTSC-U) from the official SDK, pass `--license licensea.dat` to make a
disc that also boots on retail NTSC-U consoles.

## Playing it

- **Emulator:** open `TMC-PS1.cue` in DuckStation or PCSX-Redux (2 MB of RAM, the default). Put a memory card in
  slot 1 to save.
- **Real hardware:** burn the BIN/CUE at the slowest speed on a CD-R, or copy it to an optical drive emulator.
- **Controls:** D-pad to move, Cross = A, Circle = B, L1 = L, R1 = R, Start, Select.
- **Saves:** the game's three files are kept on the memory card in slot 1 (2 blocks, "ZELDA MINISH CAP"), written in
  the background while the game saves.

## Limitations and caveats

This is a port of a decompilation to a console with 2 MB of RAM that reads its data from a 2x CD drive, where the GBA
read a 16 MB cartridge instantly. It plays the whole game, with these differences:

**Testing**
- **Not yet tested on a real PlayStation.** It was developed and tested in emulators: DuckStation (also with a retail
  BIOS) and PCSX-Redux. Reports from real hardware are welcome.
- **No complete human playthrough yet.** The opening (up to leaving Link's house and meeting Zelda) is played by a
  script and compared with the PC port frame by frame. Every room was reached by an automated tour that warps from room
  to room, and its picture checked. The bosses were checked on screen, not fought to the end. Expect bugs in places the
  tests didn't reach; please report them.
- Only the **USA** ROM is supported (the builder checks its SHA-1).

**Loading**
- **Changing areas takes a few seconds** (about 3 to 11 s in DuckStation), with a loading icon. The area's data is read
  then, so play inside an area rarely waits.
- **A short hitch can still happen inside a room** when it needs data that isn't in memory yet, mostly in the biggest
  rooms (Hyrule Town during the festival). The music keeps playing through it.
- The boot takes a few seconds longer than the GBA's, also behind the loading icon.

**Picture**
- The GBA's 240x160 picture is shown **unscaled, centred in a black border**.
- The PlayStation can't do every GBA transparency ratio. Most cases are exact; a few are approximated: **Hyrule Field's
  cloud shadows** are a little lighter, and a few rotated sprites (such as a bobbing lily pad) can be one pixel off.
- Most rooms keep 60 frames per second. The busiest scenes (many enemies, big effects) can slow down briefly; frames
  are never skipped.

**Sound**
- The music and effects are the game's own sequences played on the PlayStation's sound chip. Its 271 instrument samples
  are converted to the PlayStation's ADPCM format, which is lossy. In two late areas (Dark Hyrule Castle, Vaati) a
  couple of samples use a lower sample rate so the area's sounds fit in sound memory.

**Saves and controls**
- Saves go to the **memory card in slot 1** (2 blocks), not to a GBA save file.
- The pause menu has **no Sleep option**, and the save messages are reworded for the memory card.
- The D-pad or the left analog stick moves Link. The buttons are fixed (see [Playing it](#playing-it)).

**Legal**
- The decompilation this port is built from has **no license file upstream**. This repository ships no game data, only
  the executable with every byte it shares with the ROM removed; you need your own cartridge's ROM.

## How it's made

The executable in `bin/files` is built from a PS1 port of the decompilation using
[psyqo](https://github.com/pcsx-redux/nugget/tree/main/psyqo), developed with DuckStation and
[PCSX-Redux](https://github.com/grumpycoders/pcsx-redux); `bin/README.md` gives its version. Every room of the game was
visited on the 2 MB build by an automated tour (625 rooms in one boot) and its picture checked against the PC port and
the GBA's rules; the scripted test routes play identically to the PC port, frame by frame. `builder/` holds the
conversion tools, each documented in its header; `bin/meta` holds the few facts they take from the decompilation's
sources (song names and ROM offsets, data label offsets, the per-area song sets), as numbers only.

## License & credits

- **The Legend of Zelda: The Minish Cap**: Nintendo and Capcom (Flagship). This project is not affiliated with them.
- **The Minish Cap decompilation**: [zeldaret](https://github.com/zeldaret/tmc) and contributors. **PC port**:
  [Mathéo Vignaud](https://github.com/MatheoVignaud/tmc) and contributors.
- **agbplay**: ipatix and contributors (LGPL-3.0); the executable's sound sequencer is a C port of agbplay's; its source is in
  [source/sound](source/sound).
- **psyqo / nugget / PCSX-Redux**: the PCSX-Redux authors. **EASTL / EABase**: Electronic Arts.
- **[ps1-bare-metal](https://github.com/spicyjpeg/ps1-bare-metal)** (sound and CD-ROM driver model): spicyjpeg.
- **[mkpsxiso](https://github.com/Lameguy64/mkpsxiso)**: Lameguy64 and contributors.
- **[psx-spx](https://psx-spx.consoledev.net/)** hardware documentation: Martin "nocash" Korth and contributors.
- **Loading icon and memory card icon**: fan-made pixel art, author unknown (contact us for credit).

**Not for commercial use. No game assets are distributed** — you build the disc from your own cartridge. Third-party
licenses are in [licenses/](licenses/README.md).
