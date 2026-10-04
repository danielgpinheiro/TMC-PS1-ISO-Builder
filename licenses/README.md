# Third-party licenses

This repository holds the builder scripts and a prebuilt PlayStation executable with its code overlays (`bin/files`,
with the bytes they share with the ROM taken out). No game data.

## Covering this repository

| Component | Where | License |
|---|---|---|
| agbplay (ipatix and contributors): its sequencer, ported to C | the sound engine in `bin/files/TMCPS1.EXE` | LGPL-3.0 — [LGPL-3.0.txt](LGPL-3.0.txt), which adds to the GPL-3.0, [GPL-3.0.txt](GPL-3.0.txt) |
| The Minish Cap decompilation (zeldaret and contributors) and its PC port (Mathéo Vignaud and contributors) | the game code in `bin/files` | **no license file upstream** (see the main README) |
| PS1 port code and builder scripts | `build_iso.py`, `builder/tools/`, the PS1 layer in `bin/files/TMCPS1.EXE` | same terms as the decompilation |

## Compiled into `bin/files/TMCPS1.EXE`

| Component | Authors | License |
|---|---|---|
| psyqo / nugget (PS1 SDK) | PCSX-Redux authors | MIT — [psyqo-nugget-MIT.txt](psyqo-nugget-MIT.txt) |
| EASTL, EABase (C++ containers, via psyqo) | Electronic Arts | BSD 3-Clause — [EASTL-BSD-3-Clause.txt](EASTL-BSD-3-Clause.txt), [EABase-BSD-3-Clause.txt](EABase-BSD-3-Clause.txt) |
| SPU / CD-ROM driver model (ps1-bare-metal) | spicyjpeg | MIT — [ps1-bare-metal-MIT.txt](ps1-bare-metal-MIT.txt) |
| Loading icon and memory card icon | fan-made pixel art, author unknown (contact us for credit) | credited to its author |

## External tools (installed by you, not included)

| Tool | License |
|---|---|
| [mkpsxiso](https://github.com/Lameguy64/mkpsxiso) | GPL 2.0 or later |
| a C compiler (clang or gcc) | — |
