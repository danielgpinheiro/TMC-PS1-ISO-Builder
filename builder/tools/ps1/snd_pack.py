#!/usr/bin/env python3
"""m4a song data for the PS1 sequencer (docs/36 5.2, 5.4): TMC/SND.BIN + TMC/SNDSONG.BIN.

The sequencer (ps1/tmc_snd.c, a C port of agbplay's) reads the songs as the GBA does: GBA addresses for the song
headers, track streams, jumps (GOTO / PATT / REPT / MEMACC) and the voicegroups. These files hold just the ROM bytes it
reads, as ranges keyed by GBA address:
  - SND.BIN, resident: the song index, every voicegroup entry a song's VOICE commands select (12 bytes), the drum kits'
    / key splits' sub-voicegroups (128 entries) and key maps (128 bytes), the sample headers (16 bytes; the PCM itself
    is SPU ADPCM, tools/ps1/snd_samples.py) and PSG wave tables (16 bytes) they point at;
  - (the BGM player's songs are streamed, tools/ps1/music_pack.py: only their header address is kept)
  - SNDSONG.BIN: every song as a blob of its own (header + every byte its tracks can reach), copied into SPU RAM at
    boot and from there into a RAM cache at the area change (the area's songs) or when it starts (docs/36 6.5: the
    global songs (tools/ps1/snd_sets.py) were resident, 13 KB of RAM).
Ranges closer than MERGE bytes are merged; each is padded by 4 bytes (the note parser looks ahead up to 3 bytes).

Layouts (little-endian):
  SND.BIN: "TSN2", u32 songs, u32 ranges, u32 data size; songs x (u32 header GBA address (0 = none: an entry
    sounds.json doesn't name, as the PC backend's song map), u32 blob offset in SNDSONG.BIN, u16 blob bytes (0 =
    resident), u16 reserved); ranges x (u32 GBA address, u32 length, u32 data offset), sorted by address; the data.
  SNDSONG.BIN blob: u16 ranges, u16 reserved; ranges x (u32 GBA address, u16 length, u16 offset from the blob's data);
    the data (each blob 8-byte aligned: SPU RAM addresses are in 8-byte units).
ROM bytes: build/ only (ignored), never committed.

Usage: tools/ps1/snd_pack.py ROM OUT_DIR
"""
import os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audio_fit  # noqa: E402
import snd_sets  # noqa: E402

R = 0x08000000
MERGE = 32
ARG1 = {0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC8}


def walk(rom, start, keep, progs):
    """Every byte the track at `start` can read (all branches), into `keep`; VOICE programs into `progs`."""
    u32 = lambda o: struct.unpack_from('<I', rom, o)[0]
    seen, todo = set(), [start]
    while todo:
        pc, last = todo.pop(), None
        while 0 <= pc < len(rom) - 8 and pc not in seen:
            seen.add(pc)
            s, b = pc, rom[pc]
            if b < 0x80:
                if last is None:
                    break
                cmd = last
            else:
                cmd = b
                pc += 1
                if cmd >= 0xBD:
                    last = cmd
            n, stop = 0, False
            if cmd <= 0xB0:
                pass
            elif cmd == 0xB1:
                stop = True
            elif cmd == 0xB2:
                todo.append(u32(pc) - R)
                n, stop = 4, True
            elif cmd == 0xB3:
                todo.append(u32(pc) - R)
                n = 4
            elif cmd == 0xB4:
                pass
            elif cmd == 0xB5:
                todo.append(u32(pc + 1) - R)
                n = 5
            elif cmd == 0xB9:
                n = 3
                if 6 <= rom[pc] <= 17:
                    todo.append(u32(pc + 3) - R)
                    n = 7
            elif cmd in ARG1:
                if cmd == 0xBD:
                    progs.add(rom[pc])
                n = 1
            elif cmd == 0xCD:
                x = rom[pc]
                n = 5 if x in (1, 13) else 3 if x == 12 else 2
            elif cmd == 0xCE:
                n = 1 if rom[pc] < 0x80 else 0
            elif cmd >= 0xCF:
                while n < 3 and rom[pc + n] < 0x80:
                    n += 1
            else:
                stop = True
            keep.update(range(s, pc + n + 4))
            pc += n
            if stop:
                break


def instrument(rom, e, keep, depth=0):
    u32 = lambda o: struct.unpack_from('<I', rom, o)[0]
    if e < 0 or e + 12 > len(rom):
        return
    keep.update(range(e, e + 12))
    t = rom[e]
    if t & 0xC0 and depth == 0:
        sub = u32(e + 4) - R
        for k in range(128):
            instrument(rom, sub + 12 * k, keep, 1)
        if t & 0x40:
            km = u32(e + 8) - R
            keep.update(range(km, km + 128))
        return
    if t & 0xC0:
        return
    p = u32(e + 4) - R
    if t & 7 == 0 and 0 <= p < len(rom):
        keep.update(range(p, p + 16))  # sample header
    elif t & 7 == 3 and 0 <= p < len(rom):
        keep.update(range(p, p + 16))  # wave table


def ranges_of(keep):
    ranges = []
    for p in sorted(keep):
        if ranges and p <= ranges[-1][1] + MERGE:
            ranges[-1][1] = p + 1
        else:
            ranges.append([p, p + 1])
    return ranges


def main():
    rom = open(sys.argv[1], 'rb').read()
    odir = sys.argv[2]
    repo = os.path.abspath(os.path.join(HERE, '..', '..'))
    songs = audio_fit.load_songs(repo, rom)
    glob_songs, _ = snd_sets.song_sets()
    streamed = snd_sets.stream_songs()  # the BGM player's songs are streamed audio (docs/36 5.5): no data here
    u32 = lambda o: struct.unpack_from('<I', rom, o)[0]
    resident, heads, blobs = set(), [], {}
    for sid, name, hdr in songs:
        assert sid == len(heads)
        if name.startswith('song_'):
            heads.append(0)
            continue
        heads.append(hdr + R)
        if sid in streamed:
            continue
        nt = rom[hdr]
        keep = set(range(hdr, hdr + 8 + 4 * nt))
        progs = set()
        for t in range(nt):
            walk(rom, u32(hdr + 8 + 4 * t) - R, keep, progs)
        vg = u32(hdr + 4) - R
        if nt and 0 <= vg < len(rom):
            for p in progs:
                if p < 128:
                    instrument(rom, vg + 12 * p, resident)
        blobs[sid] = keep
    # SNDSONG.BIN
    song_bin, index = bytearray(), []
    for sid in range(len(heads)):
        if sid not in blobs:
            index.append((heads[sid], 0, 0))
            continue
        rs = ranges_of(blobs[sid])
        data, table = bytearray(), []
        for a, b in rs:
            table.append(struct.pack('<IHH', a + R, b - a, len(data)))
            data += rom[a:b]
        blob = struct.pack('<HH', len(rs), 0) + b''.join(table) + data
        blob += bytes((-len(blob)) % 8)
        assert len(blob) < 65536
        index.append((heads[sid], len(song_bin), len(blob)))
        song_bin += blob
    open(os.path.join(odir, 'SNDSONG.BIN'), 'wb').write(song_bin)
    # SND.BIN
    data, table = bytearray(), []
    for a, b in ranges_of(resident):
        table.append((a + R, b - a, len(data)))
        data += rom[a:b]
        data += bytes((-len(data)) % 4)
    out = b'TSN2' + struct.pack('<III', len(heads), len(table), len(data))
    out += b''.join(struct.pack('<IIHH', h, off, n, 0) for h, off, n in index)
    out += b''.join(struct.pack('<III', *t) for t in table)
    out += data
    open(os.path.join(odir, 'SND.BIN'), 'wb').write(out)
    print('song data: %d songs (%d named); resident %.1f KB (index, instruments; %d global songs now blobs: %d ranges); '
          'SNDSONG.BIN %d blobs %.1f KB, largest %d bytes' % (
              len(heads), sum(1 for h in heads if h), len(out) / 1024, len(glob_songs), len(table), len(blobs),
              len(song_bin) / 1024, max(n for h, o, n in index)))


if __name__ == '__main__':
    main()
