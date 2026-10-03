#!/usr/bin/env python3
"""docs/36 choice 128, Legend of Mana's FIELD.BIN (the field engine's common graphics, resident between scenes):
TMC/GLOBAL.BIN, what play reads from the CD inside rooms in many areas (Link's sprite pages, sprite frame lists, the
special-effects animations, common effect graphics), read once at boot into pinned VRAM cache slots nothing evicts.

  tools/ps1/global_hot.py add OUT [OUT...]   count, per item, the areas whose rooms read it from the CD (OUT.roombin:
                                             records with a CD read more than 30 frames after the room's first touch)
                                             into tools/ps1/play/global_hot.txt ("kind romOffset page areas reads")
  tools/ps1/global_hot.py build ROM OUT_DIR  OUT_DIR/GLOBAL.BIN: the items read in >= MIN_AREAS areas, most areas first,
                                             Link's sprite pages first (the player is in every area: his action
                                             pages, a roll, a lift, are read again in each room otherwise), up to
                                             GLOBAL_SLOTS 4 KB cache slots (choice 129's display window freed the
                                             VRAM: 48 of 69); the layout of one PACKS.BIN block (u32 count,
                                             count x (u16 segment, u16 page: 0xFFFF = whole), the bytes, each padded
                                             to 4); OUT_DIR holds ROM.IDX
"""
import bisect, collections, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIST = os.path.join(HERE, 'play', 'global_hot.txt')
REC = struct.Struct('<IIHHBBBB')
MIN_AREAS = 3
GLOBAL_SLOTS = 48  # 4 KB VRAM cache slots pinned (ps1/tmc_vcache.cpp)
LINK_SHEET = 0x13AE14  # gSprite_Link.4bpp's ROM offset (USA, the checked ROM)
SEG_MAX = 16384
PAGE = 8192


def segs_of(idx_path):
    idx = open(idx_path, 'rb').read()
    n = struct.unpack_from('<I', idx, 8)[0]
    return [struct.unpack_from('<IIII', idx, 12 + 16 * i) for i in range(n)]


def load():
    areas, reads = collections.defaultdict(set), collections.Counter()
    if os.path.exists(LIST):
        for line in open(LIST):
            p = line.split()
            if len(p) == 5:
                k = (p[0], int(p[1], 16), int(p[2]))
                areas[k] |= {int(a) for a in p[3].split(',') if a}
                reads[k] += int(p[4])
    return areas, reads


def add(runs):
    segs = segs_of(os.path.join(HERE, '..', '..', 'build', 'ps1', 'iso', 'TMC', 'ROM.IDX'))
    areas, reads = load()
    for out in runs:
        d = open(out + '.roombin', 'rb').read()
        cur, entry = None, 0
        for i in range(len(d) // REC.size):
            f, nb, seg, page, area, room, kind, cd = REC.unpack_from(d, i * REC.size)
            if (area, room) != cur:
                cur, entry = (area, room), f
            if cd != 1 or f - entry <= 30 or seg >= len(segs) or kind not in (0, 1, 3):
                continue
            k = ('p' if kind == 1 else 's', segs[seg][0], page if kind == 1 else 0)
            areas[k].add(area)
            reads[k] += 1
    with open(LIST, 'w') as fo:
        for k in sorted(areas):
            fo.write('%s %x %d %s %d\n' % (k[0], k[1], k[2], ','.join(str(a) for a in sorted(areas[k])), reads[k]))
    print('global_hot: %d items, %d in >= %d areas' % (len(areas), sum(1 for v in areas.values() if len(v) >= MIN_AREAS),
                                                         MIN_AREAS))


def build(rom_path, out):
    rom = open(rom_path, 'rb').read()
    segs = segs_of(os.path.join(out, 'ROM.IDX'))
    by_off = {s[0]: k for k, s in enumerate(segs)}
    areas, reads = load()
    picked, size, slots = [], 0, 0
    for k in sorted(areas, key=lambda k: (k[1] != LINK_SHEET, -len(areas[k]), -reads[k], k[1], k[2])):
        kind, off, pg = k
        if len(areas[k]) < MIN_AREAS or off not in by_off:
            continue
        i = by_off[off]
        o, ln, fo, fl = segs[i]
        if kind == 'p':
            o, ln = o + pg * PAGE, min(PAGE, ln - pg * PAGE)
        if (fl & 1) or ln <= 0 or ln > SEG_MAX or slots + (ln + 4095) // 4096 > GLOBAL_SLOTS:
            continue
        picked.append((i, pg if kind == 'p' else 0xFFFF, o, ln))
        size += ln
        slots += (ln + 4095) // 4096
    data = struct.pack('<I', len(picked)) + b''.join(struct.pack('<HH', i, pg) for i, pg, o, ln in picked)
    for i, pg, o, ln in picked:
        data += rom[o:o + ln] + bytes((-ln) % 4)
    open(os.path.join(out, 'GLOBAL.BIN'), 'wb').write(data)
    print('global set: %d items, %d KB, %d slots' % (len(picked), size // 1024, slots))


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'add':
        add(sys.argv[2:])
    elif len(sys.argv) == 4 and sys.argv[1] == 'build':
        build(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
