#!/usr/bin/env python3
"""docs/36 phase 11: the repository facts the disc pipeline reads besides the ROM, as numbers-only JSON, so the public
builder runs the same tools without the decomp's sources (port_rom_tables.c carries ROM-derived tables: never shipped).
With TMC_META=<dir> set, tools/ps1/{audio_fit,build_vrom,snd_sets,room_geom,rommap}.py read <dir>/*.json instead of the repo:
  sounds.json (assets/sounds.json: song paths, ROM offsets), asset_index.json (port/port_asset_index.c: offset, size,
  path), room_header_offsets.json (the area room header tables' ROM offsets), snd_sets.json (the BGM player's songs,
  the global and per-area song sets), low_b_labels.json (the decomp's data labels inside LOW_B: segment starts).
Usage: tools/ps1/repo_meta.py export OUT_DIR   (in the TMC-ps1 repo)"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))


def meta_dir():
    return os.environ.get('TMC_META') or None


def load(name):
    return json.load(open(os.path.join(meta_dir(), name)))


def export(out):
    sys.path.insert(0, HERE)
    os.makedirs(out, exist_ok=True)
    os.environ.pop('TMC_META', None)
    import build_vrom, room_geom, snd_sets
    json.dump(json.load(open(os.path.join(REPO, 'assets', 'sounds.json'))), open(os.path.join(out, 'sounds.json'), 'w'))
    s = open(os.path.join(REPO, 'port', 'port_asset_index.c')).read()
    ents = [[int(a, 16), int(b, 16), c] for a, b, c in re.findall(r'\{ 0x([0-9A-F]+), 0x([0-9A-F]+), "([^"]+)" \}', s)]
    json.dump(ents, open(os.path.join(out, 'asset_index.json'), 'w'))
    json.dump(room_geom.header_offsets(), open(os.path.join(out, 'room_header_offsets.json'), 'w'))
    json.dump(build_vrom.low_b_labels(), open(os.path.join(out, 'low_b_labels.json'), 'w'))
    g, per = snd_sets.song_sets()
    json.dump({'stream': sorted(snd_sets.stream_songs()), 'global': sorted(g),
               'areas': {str(a): sorted(v) for a, v in per.items()}}, open(os.path.join(out, 'snd_sets.json'), 'w'))
    print('repo_meta: %s written' % out)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'export':
        export(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
