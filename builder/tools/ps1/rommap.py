#!/usr/bin/env python3
"""ROM map (docs/36 phase 1.1): every known range of the 16 MB ROM with its kind, from
  - the PC port's asset index (port/port_asset_index.c: offset, size, extracted path) — graphics, maps, tile sets,
    palettes, sprites, animations, tables;
  - assets/sounds.json (song blocks) and the sample list tools/ps1/audio_fit.py writes (--audio JSON);
  - the code region (the ROM's start up to the first data range).
LZ77 ranges (first byte 0x10 and a .lz path) get their decompressed size.
Prints the coverage by kind and writes JSON [offset, size, kind, path, decompressed] with --json.
Usage: tools/ps1/rommap.py ROM [--audio audio.json] [--json OUT]
"""
import collections, json, os, re, struct, sys


def kind_of(path):
    p = path
    if p.startswith('maps/') and '/tileSets/' in p:
        return 'tileset gfx'
    if p.startswith('maps/'):
        return 'room maps / tile types'
    if p.startswith('tilemaps/'):
        return 'tilemaps'
    if p.startswith('gfx/'):
        return 'gfx'
    if p.startswith('sprites/'):
        return 'sprite gfx'
    if p.startswith('palettes/'):
        return 'palettes'
    if p.startswith('animation'):
        return 'animations'
    if p.startswith('strings/') or p.startswith('texts/'):
        return 'text'
    if p.startswith('sounds/'):
        return 'songs'
    if p.startswith('samples'):
        return 'samples'
    return 'tables / other data'


def main():
    rom = open(sys.argv[1], 'rb').read()
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import repo_meta  # (the public builder: TMC_META, docs/36 11)
    if repo_meta.meta_dir():
        ents = [tuple(e) for e in repo_meta.load('asset_index.json')]
        snd = repo_meta.load('sounds.json')
    else:
        s = open(os.path.join(repo, 'port', 'port_asset_index.c')).read()
        ents = [(int(a, 16), int(b, 16), c) for a, b, c in re.findall(r'\{ 0x([0-9A-F]+), 0x([0-9A-F]+), "([^"]+)" \}', s)]
        snd = json.load(open(os.path.join(repo, 'assets', 'sounds.json')))
    for e in snd:
        if 'path' in e and 'start' in e and ('variants' not in e or 'USA' in e['variants']):
            ents.append((e['start'], e['size'], e['path']))
    if '--audio' in sys.argv:
        a = json.load(open(sys.argv[sys.argv.index('--audio') + 1]))
        for w, v in a['samples'].items():
            ents.append((int(w), 16 + v['pcm'], 'samples/%x' % int(w)))
    ents.sort()
    out = []
    cov = collections.Counter()
    dec = collections.Counter()
    last = 0
    for o, n, p in ents:
        k = kind_of(p)
        d = None
        if p.endswith('.lz') and rom[o] == 0x10:
            d = rom[o + 1] | rom[o + 2] << 8 | rom[o + 3] << 16
        if o >= last:
            cov[k] += n
            dec[k] += d if d is not None else n
        elif o + n > last:
            cov[k] += o + n - last
            dec[k] += o + n - last
        last = max(last, o + n)
        out.append([o, n, k, p, d])
    first_data = ents[0][0]
    known = sum(cov.values())
    print('ROM %d bytes; ranges %d; covered %.2f MB (%.0f %%); code + untyped before the first data range: %.0f KB' % (
        len(rom), len(ents), known / 1048576, known * 100 / len(rom), first_data / 1024))
    print('%-24s %9s %12s' % ('kind', 'ROM KB', 'decomp. KB'))
    for k, v in cov.most_common():
        print('%-24s %9.0f %12.0f' % (k, v / 1024, dec[k] / 1024))
    tail = len(rom) - last
    pad = rom[last:].count(0xFF) + rom[last:].count(0x00)
    print('after the last range: %.0f KB (%.0f %% 0x00/0xFF padding)' % (tail / 1024, pad * 100 / max(1, tail)))
    if '--json' in sys.argv:
        json.dump(out, open(sys.argv[sys.argv.index('--json') + 1], 'w'))


if __name__ == '__main__':
    main()
