#!/usr/bin/env python3
"""SPU samples and per-area sample sets for the PS1 sequencer (docs/36 5.4): TMC/SNDSMP.BIN + TMC/SNDAREA.BIN.

SNDSMP.BIN holds every sample the songs can play, as SPU ADPCM: the DirectSound samples (tools/ps1/audio_pack.py's
samples.bin / samples.json: loops on 28-sample blocks, the rate scaled by the same ratio), the four square duties
(one period of agbplay's patterns: 12.5 % over 56 samples, the others over 28) and every 4-bit wave table a song's
voicegroup uses (32 steps over 56 samples), at the PCM scale (pattern value 1.0 = sample 127, like PCM's 127 / 128).
Noise uses the SPU's noise generator (no sample). The PS1 keeps SPU RAM as a cache of these (ps1/tmc_snd.c).

SNDAREA.BIN says which samples to have in SPU RAM (choice 65): those of the global songs (+ the squares) always, those
of an area's songs from its change on (tools/ps1/snd_sets.py). A sample outside both is a miss on the PS1 (silent,
counted and logged in TRACE builds).

Layouts (little-endian):
  SNDSMP.BIN: "TSS1", u32 entries, u32 data bytes, u32 data offset in the file (2048-aligned); entries x (u32 GBA
    address of the sample header or wave table, or 0xFFFFFFF0 + duty for a square; u32 offset of its data from the
    data start; u32 bytes (a multiple of 64); u32 rate x 1024 (a DirectSound sample's pitch with its loop ratio; a
    PSG wave: samples a period x 1024); u16 flags (1 = loops); u16 reserved), sorted by address; then the data.
    After the data, a pack per sample list (docs/36 6.6): the list's samples back to back in list order, from a
    sector boundary, so an area change reads its set sequentially (one seek, not one per sample).
  SNDAREA.BIN: "TSA3", u32 lists (257: areas 0..255, then global), u32 sample list entries, u32 song list entries;
    u16 start[258] (list k = index[start[k] .. start[k + 1]]); u16 index[] into SNDSMP's entries; the same for the
    songs: u16 songStart[258], u16 song ids (an area's songs to load at the change; global = the resident ones);
    (padding to 4) u32 packOff[257] (list k's pack, from SNDSMP.BIN's start; 0 = none), u32 packBytes[257].

Usage: tools/ps1/snd_samples.py ROM SAMPLES_DIR OUT_DIR
"""
import json, os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, HERE)
import audio_fit  # noqa: E402
import snd_sets  # noqa: E402

R = 0x08000000
SQUARES = ((56, 7), (28, 7), (28, 14), (28, 21))  # (period, high samples): 12.5 %, 25 %, 50 %, 75 %
SQ_LEVEL = ((0.875, -0.125), (0.75, -0.25), (0.5, -0.5), (0.25, -0.75))
SPU_BYTES = 0x7FFE0 - 0x1000  # SPU RAM for samples (ps1/spu.hh: capture buffers below, the silent block above)


def encode_loop(tool, tmp, pcm):
    """one looped period (a multiple of 28 samples) -> ADPCM bytes"""
    open(tmp + '.s8', 'wb').write(bytes((v + 256) % 256 for v in pcm))
    subprocess.check_output([tool, tmp + '.s8', tmp + '.adpcm', '1', '0', str(len(pcm))])
    return open(tmp + '.adpcm', 'rb').read()


def song_voices(rom, songs):
    """song id -> {'samples', 'waves' (ROM offsets), 'square', 'noise'}"""
    out = {}
    u32 = lambda o: struct.unpack_from('<I', rom, o)[0]
    for sid, name, hdr in songs:
        nt = rom[hdr]
        vg = u32(hdr + 4) - R
        o = {'samples': set(), 'waves': set(), 'noise': False, 'square': False}
        if nt and 0 <= vg < len(rom):
            for t in range(nt):
                tp = u32(hdr + 8 + 4 * t) - R
                if 0 <= tp < len(rom):
                    for prog, key in audio_fit.walk_track(rom, tp):
                        try:
                            audio_fit.resolve(rom, vg, prog, key, o)
                        except (IndexError, struct.error):
                            pass
        out[sid] = o
    return out


def main():
    rom = open(sys.argv[1], 'rb').read()
    sdir, odir = sys.argv[2], sys.argv[3]
    index = json.load(open(os.path.join(sdir, 'samples.json')))
    blob = open(os.path.join(sdir, 'samples.bin'), 'rb').read()
    tool = os.path.join(sdir, 'spuadpcm')
    songs = audio_fit.load_songs(REPO, rom)
    voices = song_voices(rom, songs)

    # ---- SNDSMP.BIN ----
    items = []  # (gba, data, rate1024, flags)
    for w in sorted(int(k) for k in index):
        e = index[str(w)]
        items.append((w + R, blob[e['offset']:e['offset'] + e['size']], e['rate1024'], 1 if e['loop'] else 0))
    tmp = os.path.join(sdir, 'bank_tmp')
    for d, ((period, high), (hi, lo)) in enumerate(zip(SQUARES, SQ_LEVEL)):
        pcm = [round(127 * (hi if i < high else lo)) for i in range(period)]
        items.append((0xFFFFFFF0 + d, encode_loop(tool, tmp, pcm), period * 1024, 1))
    waves = sorted(set().union(*[v['waves'] for v in voices.values()]))
    for w in waves:
        nib = [(rom[w + i // 2] >> (4 if i % 2 == 0 else 0)) & 15 for i in range(32)]
        mean = sum(nib) / 32.0
        pcm = [round((nib[(i * 32) // 56] - mean) / 16 * 127) for i in range(56)]  # agbplay: nibble / 16, DC removed
        items.append((w + R, encode_loop(tool, tmp, pcm), 56 * 1024, 1))
    for p in (tmp + '.s8', tmp + '.adpcm'):
        if os.path.exists(p):
            os.remove(p)
    items.sort(key=lambda t: t[0])
    pos = {it[0]: k for k, it in enumerate(items)}
    table, data, size = [], bytearray(), {}
    for gba, d, rate, flags in items:
        d = d + bytes((-len(d)) % 64)
        table.append(struct.pack('<IIIIHH', gba, len(data), len(d), rate, flags, 0))
        size[gba] = len(d)
        data += d
    head_len = 16 + 20 * len(items)
    data_off = (head_len + 2047) // 2048 * 2048  # sector-aligned: whole-sector reads
    head = b'TSS1' + struct.pack('<III', len(items), len(data), data_off) + b''.join(table)
    smp_file = bytearray(head + bytes(data_off - len(head)) + data)
    starts = [struct.unpack_from('<I', t, 4)[0] for t in table]

    # ---- the sets ----
    def samples_of(song_ids):
        out = set()
        for s in song_ids:
            v = voices.get(s)
            if v:
                out |= {w + R for w in v['samples']} | {w + R for w in v['waves']}
                if v['square']:
                    out |= {0xFFFFFFF0 + d for d in range(4)}
        return out

    glob_songs, area_songs = snd_sets.song_sets()
    glob_set = samples_of(glob_songs) | {0xFFFFFFF0 + d for d in range(4)}
    lists, worst = [], (0, -1)
    for a in range(256):
        s = samples_of(area_songs.get(a, set())) - glob_set
        lists.append(sorted(pos[g] for g in s if g in pos))
        worst = max(worst, (sum(size[g] for g in s if g in pos), a))
    glist = sorted(pos[g] for g in glob_set if g in pos)
    lists.append(glist)
    gsize = sum(size[items[k][0]] for k in glist)

    def packed(lists):
        start, flat = [], []
        for L in lists:
            start.append(len(flat))
            flat += L
        start.append(len(flat))
        return start, flat
    pack_off, pack_len = [], []
    for L in lists:  # the packs: each list's samples back to back, from a sector boundary
        if not L:
            pack_off.append(0)
            pack_len.append(0)
            continue
        smp_file += bytes((-len(smp_file)) % 2048)
        pack_off.append(len(smp_file))
        for k in L:
            smp_file += data[starts[k]:starts[k] + size[items[k][0]]]
        pack_len.append(len(smp_file) - pack_off[-1])
    smp_file += bytes((-len(smp_file)) % 2048)
    open(os.path.join(odir, 'SNDSMP.BIN'), 'wb').write(smp_file)
    start, flat = packed(lists)
    sstart, sflat = packed([sorted(area_songs.get(a, set())) for a in range(256)] + [sorted(glob_songs)])
    out = b'TSA3' + struct.pack('<III', len(lists), len(flat), len(sflat))
    out += struct.pack('<%dH' % len(start), *start) + struct.pack('<%dH' % len(flat), *flat)
    out += struct.pack('<%dH' % len(sstart), *sstart) + struct.pack('<%dH' % len(sflat), *sflat)
    out += bytes((-len(out)) % 4)
    out += struct.pack('<%dI' % len(pack_off), *pack_off) + struct.pack('<%dI' % len(pack_len), *pack_len)
    open(os.path.join(odir, 'SNDAREA.BIN'), 'wb').write(out)
    print('sound samples: %d (%d DirectSound, 4 squares, %d waves), %.0f KB; global set %d samples %.0f KB, largest '
          'area set %.0f KB (area %d): %.0f of %.0f KB SPU RAM' % (
              len(items), len(index), len(waves), len(data) / 1024, len(glist), gsize / 1024, worst[0] / 1024,
              worst[1], (gsize + worst[0]) / 1024, SPU_BYTES / 1024))
    if gsize + worst[0] > SPU_BYTES:
        print('WARNING: an area set does not fit SPU RAM with the global one')


if __name__ == '__main__':
    main()
