#!/usr/bin/env python3
"""m4a (Sappy / MP2K) songs -> the samples they play, sized as SPU ADPCM (docs/36 phase 1.4).

For each entry of the ROM's song table (song id = index; names from assets/sounds.json), the header
(track count, voicegroup, track pointers) is read from the ROM and every track's event stream is walked: VOICE
selects the program, KEYSH shifts keys, notes (TIE / N01..N96, with running status) give the keys played;
GOTO / PATT / PEND / REPT are followed once. Each (program, key) resolves through the voicegroup: DirectSound
(type & 7 == 0) -> its sample; keysplit (0x40) -> the sub-voicegroup entry for that key; drum kit (0x80) -> the
sub-voicegroup entry at the key; square / wave / noise are PSG voices (no sample; wave = 16 bytes).

Sample header: u32 flags (bit 30 = loop), u32 pitch (Hz * 1024), u32 loop start, u32 length (samples), then
signed 8-bit PCM. SPU ADPCM: 16 bytes per 28 samples (+ one block for the end / loop flags).

Usage: tools/ps1/audio_fit.py ROM [--songs] [--json OUT]
"""
import json, struct, sys, os

ROMBASE = 0x08000000


def u32(rom, o):
    return struct.unpack_from('<I', rom, o)[0]


def load_songs(repo, rom):
    """The ROM's song table (song id = index; found by the pointer to the first song's header) named from
    assets/sounds.json (USA entries); unnamed entries point at a shared dummy header."""
    import repo_meta  # (the public builder: TMC_META, docs/36 11)
    s = repo_meta.load('sounds.json') if repo_meta.meta_dir() else json.load(open(os.path.join(repo, 'assets', 'sounds.json')))
    usa = [e for e in s if 'path' in e and 'start' in e and ('variants' not in e or 'USA' in e['variants'])]
    by = {e['start'] + e['options']['headerOffset']: e['path'].split('/')[-1].rsplit('.', 1)[0] for e in usa}
    first = usa[0]['start'] + usa[0]['options']['headerOffset']
    table = rom.find(struct.pack('<I', first + ROMBASE))
    songs = []
    for k in range(2048):
        p = u32(rom, table + 8 * k) - ROMBASE
        if not 0 <= p < len(rom) or rom[p] > 16:
            break
        songs.append((k, by.get(p, 'song_%d' % k), p))
    return songs


def walk_track(rom, start):
    """-> set of (program, key) played, following jumps / calls once."""
    played = set()
    visited = set()
    stack = []
    pc = start
    prog = 0
    keysh = 0
    last_cmd = None
    last_key = 60
    steps = 0
    while steps < 200000:
        steps += 1
        if pc in visited and not stack:
            break
        visited.add(pc)
        b = rom[pc]
        if b < 0x80:  # running status: argument of the last command
            if last_cmd is None:
                break
            cmd = last_cmd
        else:
            cmd = b
            pc += 1
            if cmd >= 0xBD:
                last_cmd = cmd
        if cmd <= 0xB0:  # wait
            continue
        if cmd == 0xB1:  # FINE
            if stack:
                pc = stack.pop()
                continue
            break
        if cmd == 0xB2:  # GOTO
            t = u32(rom, pc) - ROMBASE
            if t in visited or not (0 <= t < len(rom)):
                if stack:
                    pc = stack.pop()
                    continue
                break
            pc = t
            continue
        if cmd == 0xB3:  # PATT
            t = u32(rom, pc) - ROMBASE
            stack.append(pc + 4)
            if not (0 <= t < len(rom)) or len(stack) > 8:
                pc = stack.pop()
                continue
            pc = t
            continue
        if cmd == 0xB4:  # PEND
            if stack:
                pc = stack.pop()
            continue
        if cmd == 0xB5:  # REPT count, ptr: walk the pattern once
            t = u32(rom, pc + 1) - ROMBASE
            stack.append(pc + 5)
            pc = t if 0 <= t < len(rom) else stack.pop()
            continue
        if cmd == 0xB9:  # MEMACC op addr data
            pc += 3
            continue
        if cmd in (0xBA, 0xBB, 0xBE, 0xBF, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC8):
            pc += 1
            continue
        if cmd == 0xBC:
            v = rom[pc]
            keysh = v - 256 if v > 127 else v
            pc += 1
            continue
        if cmd == 0xBD:
            prog = rom[pc]
            pc += 1
            continue
        if cmd == 0xCD:  # XCMD
            pc += 2
            continue
        if cmd == 0xCE:  # EOT [key]
            if rom[pc] < 0x80:
                pc += 1
            continue
        if cmd >= 0xCF:  # TIE / notes: [key] [velocity] [gate]
            if rom[pc] < 0x80:
                last_key = rom[pc]
                pc += 1
                if rom[pc] < 0x80:
                    pc += 1
                    if rom[pc] < 0x80:
                        pc += 1
            played.add((prog, max(0, min(127, last_key + keysh))))
            continue
        # unknown command: stop this track
        break
    return played


def resolve(rom, vg, prog, key, out):
    e = vg + prog * 12
    t = rom[e]
    if t & 0x40:  # keysplit
        sub = u32(rom, e + 4) - ROMBASE
        table = u32(rom, e + 8) - ROMBASE
        return resolve(rom, sub, rom[table + key], key, out)
    if t & 0x80:  # drum kit
        sub = u32(rom, e + 4) - ROMBASE
        return resolve(rom, sub, key, key, out)
    kind = t & 7
    if kind == 0:  # DirectSound
        w = u32(rom, e + 4) - ROMBASE
        if 0 <= w < len(rom):
            out['samples'].add(w)
    elif kind == 3:
        out['waves'].add(u32(rom, e + 4) - ROMBASE)
    elif kind == 4:
        out['noise'] = True
    else:
        out['square'] = True


def sample_info(rom, w):
    flags, pitch, loop, n = struct.unpack_from('<IIII', rom, w)
    pcm = n + 1
    adpcm = ((n + 27) // 28 + 1) * 16
    return {'loop': bool(flags & 0x40000000), 'rate': pitch / 1024, 'samples': n, 'pcm': pcm, 'adpcm': adpcm}


def main():
    rom = open(sys.argv[1], 'rb').read()
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    songs = load_songs(repo, rom)
    res = {}
    allsamples = {}
    for sid, name, hdr in songs:
        ntracks = rom[hdr]
        vg = u32(rom, hdr + 4) - ROMBASE
        out = {'samples': set(), 'waves': set(), 'noise': False, 'square': False}
        if ntracks and 0 <= vg < len(rom):
            for t in range(ntracks):
                tp = u32(rom, hdr + 8 + 4 * t) - ROMBASE
                if 0 <= tp < len(rom):
                    for prog, key in walk_track(rom, tp):
                        try:
                            resolve(rom, vg, prog, key, out)
                        except (IndexError, struct.error):
                            pass
        for w in out['samples']:
            allsamples.setdefault(w, sample_info(rom, w))
        res[sid] = {'name': name, 'tracks': ntracks, 'samples': sorted(out['samples']), 'waves': len(out['waves']),
                    'noise': out['noise'], 'square': out['square'],
                    'adpcm': sum(allsamples[w]['adpcm'] for w in out['samples']),
                    'pcm': sum(allsamples[w]['pcm'] for w in out['samples'])}
    bgm = {k: v for k, v in res.items() if v['name'].startswith('bgm')}
    sfx = {k: v for k, v in res.items() if not v['name'].startswith('bgm')}
    sfx_union = set().union(*[set(v['samples']) for v in sfx.values()]) if sfx else set()
    bgm_union = set().union(*[set(v['samples']) for v in bgm.values()]) if bgm else set()
    K = lambda n: n / 1024
    print('songs %d (bgm %d, other %d); distinct samples used %d of the ROM\'s' % (len(res), len(bgm), len(sfx),
          len(allsamples)))
    print('all used samples: PCM %.0f KB -> SPU ADPCM %.0f KB' % (K(sum(s['pcm'] for s in allsamples.values())),
          K(sum(s['adpcm'] for s in allsamples.values()))))
    print('sfx / jingles union: ADPCM %.0f KB (%d samples); bgm union: %.0f KB (%d samples)' % (
        K(sum(allsamples[w]['adpcm'] for w in sfx_union)), len(sfx_union),
        K(sum(allsamples[w]['adpcm'] for w in bgm_union)), len(bgm_union)))
    top = sorted(bgm.items(), key=lambda kv: -kv[1]['adpcm'])[:12]
    print('largest bgm (ADPCM KB, of which not in the sfx set):')
    for k, v in top:
        own = sum(allsamples[w]['adpcm'] for w in v['samples'] if w not in sfx_union)
        print('  %3d %-32s %6.0f  %6.0f  samples %d' % (k, v['name'], K(v['adpcm']), K(own), len(v['samples'])))
    v = sorted(x['adpcm'] for x in bgm.values())
    print('bgm ADPCM KB: median %.0f, max %.0f' % (K(v[len(v) // 2]), K(v[-1])))
    if '--songs' in sys.argv:
        for k, v in sorted(res.items()):
            print('%3d %-34s tracks %2d samples %3d adpcm %6.0f KB' % (k, v['name'], v['tracks'], len(v['samples']),
                                                                       K(v['adpcm'])))
    if '--json' in sys.argv:
        json.dump({'songs': res, 'samples': {str(k): v for k, v in allsamples.items()}},
                  open(sys.argv[sys.argv.index('--json') + 1], 'w'))


if __name__ == '__main__':
    main()
