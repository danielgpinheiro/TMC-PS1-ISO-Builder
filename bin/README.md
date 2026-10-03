# bin/

`files/TMCPS1.EXE` + `files/*.bin` (code overlays): the PS1 executable, built from the TMC-ps1 port at commit
`5a2b29b7` (`tools/ps1/build_ovl.sh`: 2 MB retail link), with every run of 16 or more bytes it shares with
the ROM zeroed (`files/holes.json`: file, offset, ROM offset, length, the original's SHA-256). `build_iso.py` fills them
from your ROM. `LOADICON.BIN`: the loading indicator's texture. `meta/`: the decompilation's facts the converters
read (numbers and names only). SHA-256 of every file in `SHA256SUMS`.
