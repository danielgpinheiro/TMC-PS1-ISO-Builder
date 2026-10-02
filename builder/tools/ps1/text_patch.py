#!/usr/bin/env python3
"""PS1 wording for the save messages (docs/36 phase 7b.1, the user's request 2026-09-29: "Saving on Memory Card Slot 1,
don't turn off the PS1"): the GBA's texts of bank TEXT_SAVE that warn about the Game Pak and the power rewritten
in the virtual ROM at build time (data, not engine code; tools/ps1/build_vrom.py applies it to its copy of the ROM).

Each new text replaces the old one in place (it is not longer: the rest padded with 0), found through the ROM's own
tables (the language table -> bank TEXT_SAVE's table -> the message's offset) and checked against the old text's SHA-1
first (the public builder carries no ROM text): another ROM (or an already patched one) is left alone, with a warning. The texts are plain ASCII (0x0A = new line).
PS1_TEXT_PATCH=0 builds without it.
"""
import hashlib, os, struct, sys

LANG_TABLE = 0x9B1D90  # USA: the language table (texts.json's table_rom_offset); bank k's table at + u32[k]
BANK_SAVE = 0
PATCHES = [  # (message index, SHA-1 of the old text, new text)
    (5, 'd82439a8357bd6c330f7fc3a2d769490c162aeca', b"\nCopying on Memory Card Slot 1...\nDon't turn off the PS1.\n"),
    (7, '639609fd5dec9131e0b859b2890c05851212f040', b"\nErasing file on Memory Card Slot 1...\nDon't turn off the PS1."),
    (8, 'e95cf9399975e296e6f4da45423de26d1d3c2ccc', b"\nSaving file on Memory Card Slot 1...\nDon't turn off the PS1."),
    (0x0B, 'd782fe102a90dd24c6c21445d0310ced3f0ab20e', b"\nSaving on Memory Card Slot 1...\n\nDon't turn off the PS1.\n"),
]


def apply(rom):
    """rom: a bytearray of the ROM; returns the number of messages rewritten"""
    if os.environ.get('PS1_TEXT_PATCH', '1') == '0':
        return 0
    u32 = lambda o: struct.unpack_from('<I', rom, o)[0]
    try:
        bank = LANG_TABLE + u32(LANG_TABLE + 4 * BANK_SAVE)
    except struct.error:
        return 0
    done = 0
    for idx, sha, new in PATCHES:
        at = bank + u32(bank + 4 * idx)
        end = rom.index(b'\0', at)
        old = bytes(rom[at:end])
        if hashlib.sha1(old).hexdigest() != sha or len(new) > len(old):
            print('text_patch: TEXT_SAVE message %d not as expected, left alone' % idx, file=sys.stderr)
            continue
        rom[at:end + 1] = new + bytes(end + 1 - at - len(new))
        done += 1
    return done
