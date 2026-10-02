#!/usr/bin/env python3
"""Room rectangles and neighbours from the ROM (docs/36 6.1): every area's room headers (port/port_rom_tables.c's
kAreaRoomHeaderOffsets: u16 map_x, map_y, pixel_width, pixel_height, tileSet_id each, a list ended by map_x = 0xFFFF),
and per room the rooms of its area whose rectangles touch or overlap it (the screens a player can scroll into).

Usage (a check): tools/ps1/room_geom.py ROM [PC_DUMP]   (PC_DUMP: tmc_pc's TMC_ROOMDUMP lines, compared)
"""
import os, re, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))


def header_offsets():
    import repo_meta  # (the public builder: TMC_META, docs/36 11)
    if repo_meta.meta_dir():
        return repo_meta.load('room_header_offsets.json')
    txt = open(os.path.join(REPO, 'port', 'port_rom_tables.c')).read()
    m = re.search(r'kAreaRoomHeaderOffsets\[\d+\]\s*=\s*\{([^}]*)\}', txt)
    return [int(x, 16) for x in re.findall(r'0x[0-9A-Fa-f]+', m.group(1))]


def rooms(rom):
    """{(area, room): (x, y, w, h)}"""
    out = {}
    for a, off in enumerate(header_offsets()):
        if not off or off >= len(rom):
            continue
        for r in range(256):
            x, y, w, h, _ = struct.unpack_from('<5H', rom, off + 10 * r)
            if x == 0xFFFF:
                break
            out[(a, r)] = (x, y, w, h)
    return out


def neighbours(rects, slack=16):
    """{(area, room): [rooms of the same area touching it]}"""
    by_area = {}
    for (a, r), rc in rects.items():
        by_area.setdefault(a, []).append((r, rc))
    out = {}
    for a, lst in by_area.items():
        for r, (x, y, w, h) in lst:
            if not w or not h:
                out[(a, r)] = []
                continue
            n = []
            for r2, (x2, y2, w2, h2) in lst:
                if r2 == r or not w2 or not h2:
                    continue
                if x2 <= x + w + slack and x <= x2 + w2 + slack and y2 <= y + h + slack and y <= y2 + h2 + slack:
                    n.append(r2)
            out[(a, r)] = n
    return out


def main():
    rom = open(sys.argv[1], 'rb').read()
    rc = rooms(rom)
    nb = neighbours(rc)
    print('rooms %d in %d areas; neighbours per room: avg %.1f, max %d' % (
        len(rc), len({a for a, r in rc}), sum(len(v) for v in nb.values()) / len(nb), max(len(v) for v in nb.values())))
    if len(sys.argv) > 2:
        pc = {}
        for line in open(sys.argv[2]):
            a, r, x, y, w, h = (int(v) for v in line.split())
            pc[(a, r)] = (x, y, w, h)
        same = sum(1 for k in pc if rc.get(k) == pc[k])
        print('vs tmc_pc: %d / %d rooms identical, %d only here' % (same, len(pc), len(set(rc) - set(pc))))


if __name__ == '__main__':
    main()
