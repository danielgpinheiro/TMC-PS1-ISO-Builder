#!/usr/bin/env python3
"""docs/36 phase 11: the public builder's EXE and overlays without the ROM's bytes.

punch ROM OUT_DIR FILE...: every run of 16+ bytes of FILE that is also in the ROM (tools/ps1/rom_scan.py's search) is
zeroed in OUT_DIR/<name>; OUT_DIR/holes.json lists (file, offset, ROM offset, length) and the SHA-256 of each original
file (numbers only: no ROM bytes). fill ROM HOLES_DIR OUT_DIR: copies those runs back from the user's ROM and checks
every file's SHA-256 against the original's (the builder's step); exits non-zero on a mismatch.
"""
import hashlib, json, os, sys

MIN = 16


def index(rom):  # the ROM's 8-byte words at 4-byte steps (a run starting off that step is found 1-3 bytes in, then
    idx = {}      # extended back)
    for o in range(0, len(rom) - 8, 4):
        w = rom[o:o + 8]
        if w.count(w[0]) == 8:
            continue
        idx.setdefault(w, []).append(o) if len(idx.get(w, ())) < 4 else None
    return idx


def runs(data, rom, idx):
    out, o = [], 0
    while o < len(data) - 8:
        best = (0, 0)
        for r in idx.get(data[o:o + 8], ()):
            n = 8
            while o + n < len(data) and r + n < len(rom) and data[o + n] == rom[r + n]:
                n += 1
            if n > best[0]:
                best = (n, r)
        n, r = best
        start = o
        if n >= 8:
            lo = out[-1][0] + out[-1][2] if out else 0
            while start > lo and r > 0 and data[start - 1] == rom[r - 1]:
                start, r, n = start - 1, r - 1, n + 1
        if n >= MIN and len(set(data[start:start + n])) > 2:
            out.append((start, r, n))
            o = start + n
        else:
            o += 1
    return out


def punch(rom_path, out_dir, files):
    rom = open(rom_path, 'rb').read()
    idx = index(rom)
    os.makedirs(out_dir, exist_ok=True)
    table = {'rom_sha1': hashlib.sha1(rom).hexdigest(), 'files': {}}
    total = 0
    for f in files:
        data = bytearray(open(f, 'rb').read())
        sha = hashlib.sha256(data).hexdigest()
        rs = []
        for _ in range(8):  # again on the punched data: runs behind a commoner word or next to a hole
            more = runs(bytes(data), rom, idx)
            if not more:
                break
            for o, r, n in more:
                data[o:o + n] = bytes(n)
            rs += more
        name = os.path.basename(f)
        open(os.path.join(out_dir, name), 'wb').write(data)
        table['files'][name] = {'sha256': sha, 'holes': rs}
        total += sum(n for o, r, n in rs)
    json.dump(table, open(os.path.join(out_dir, 'holes.json'), 'w'), indent=0)
    print('rom_holes: %d files, %d bytes of the ROM taken out' % (len(files), total))


def fill(rom_path, holes_dir, out_dir):
    rom = open(rom_path, 'rb').read()
    table = json.load(open(os.path.join(holes_dir, 'holes.json')))
    os.makedirs(out_dir, exist_ok=True)
    bad = 0
    for name, t in table['files'].items():
        data = bytearray(open(os.path.join(holes_dir, name), 'rb').read())
        for o, r, n in reversed(t['holes']):  # later passes first: an earlier hole restores what one overlapped
            data[o:o + n] = rom[r:r + n]
        if hashlib.sha256(data).hexdigest() != t['sha256']:
            print('rom_holes: %s does not match the build (another ROM?)' % name)
            bad += 1
        open(os.path.join(out_dir, name), 'wb').write(data)
    if bad:
        sys.exit(1)
    print('rom_holes: %d files filled from the ROM, all match' % len(table['files']))


if __name__ == '__main__':
    if sys.argv[1] == 'punch':
        punch(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif sys.argv[1] == 'fill':
        fill(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)
        sys.exit(1)
