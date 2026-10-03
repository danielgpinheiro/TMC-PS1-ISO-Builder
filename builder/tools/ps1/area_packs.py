#!/usr/bin/env python3
"""docs/36 choice 124, Legend of Mana's scene file: TMC/PACKS.BIN, each area's pack, read in one pass at its change.

An area's pack = the virtual-ROM segments its rooms' manifests name (tools/ps1/play/rooms.txt), the ones most of its
rooms use first (ties in ROM order), the kinds the PS1 can keep in its VRAM / SPU cache (asset-region segments up to
16 KB), at most PACK_MAX bytes; their bytes copied back to back (a segment shared by many areas is in each of their
packs: the disc has room, the reads don't seek). ps1/tmc_vrom.c reads the current area's pack under the area change's
loading episode and keeps what RAM and the cache can hold.

First in each pack: what stalled play in that area (tools/ps1/area_hot.py -> play/area_hot.txt: segments and 8 KB
sprite-sheet pages read from the CD inside a room), most frequent first.

Layout (little-endian): "TPK2", u32 256, u32 0, u32 offset[257] (area a's block = [offset[a], offset[a + 1]), empty
when equal); a block: u32 count, count x (u16 segment index, u16 page: 0xFFFF = the whole segment, else its 8 KB
page), then the items' bytes, each padded to 4.
Usage: tools/ps1/area_packs.py ROM OUT_DIR   (OUT_DIR holds ROM.IDX; writes OUT_DIR/PACKS.BIN)"""
import collections, os, struct, sys, bisect

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_MAX = 256 * 1024
SEG_MAX = 16384
PAGE = 8192  # a sprite sheet's page (ps1/tmc_vrom.c VROM_PAGE)
ASSETS = 1 << 19


def main():
    rom = open(sys.argv[1], 'rb').read()
    out = sys.argv[2]
    idx = open(os.path.join(out, 'ROM.IDX'), 'rb').read()
    n = struct.unpack_from('<I', idx, 8)[0]
    segs = [struct.unpack_from('<IIII', idx, 12 + 16 * i) for i in range(n)]
    starts = [s[0] for s in segs]
    use = collections.defaultdict(collections.Counter)
    for line in open(os.path.join(HERE, 'play', 'rooms.txt')):
        p = line.split()
        if len(p) != 3 or line.startswith('#'):
            continue
        k = bisect.bisect_right(starts, int(p[2], 16)) - 1
        if 0 <= k < n and segs[k][0] == int(p[2], 16):
            use[int(p[0])][k] += 1
    by_off = {segs[k][0]: k for k in range(n)}
    hot = collections.defaultdict(list)  # area -> [(count, k, page)]: what stalled play (tools/ps1/area_hot.py)
    hp = os.path.join(HERE, 'play', 'area_hot.txt')
    if os.path.exists(hp):
        for line in open(hp):
            p = line.split()
            if len(p) == 5 and int(p[2], 16) in by_off:
                hot[int(p[0])].append((int(p[4]), by_off[int(p[2], 16)], int(p[3]) if p[1] == 'p' else 0xFFFF))
    blocks, total, sizes = [], 0, []
    for a in range(256):
        picked, size, seen = [], 0, set()
        for c, k, pg in sorted(hot[a], key=lambda t: (-t[0], segs[t[1]][0], t[2])):
            o, ln, fo, fl = segs[k]
            nb = ln if pg == 0xFFFF else min(PAGE, ln - pg * PAGE)
            if (fl & 1) or nb <= 0 or nb > SEG_MAX or size + nb > PACK_MAX or (k, pg) in seen:
                continue
            picked.append((k, pg))
            seen.add((k, pg))
            size += nb
        for k, c in sorted(use[a].items(), key=lambda kc: (-kc[1], segs[kc[0]][0])):
            o, ln, fo, fl = segs[k]
            if (fl & 1) or not (fl & ASSETS) or ln > SEG_MAX or size + ln > PACK_MAX or (k, 0xFFFF) in seen:
                continue
            picked.append((k, 0xFFFF))
            seen.add((k, 0xFFFF))
            size += ln
        if not picked:
            blocks.append(b'')
            continue
        head = struct.pack('<I', len(picked)) + b''.join(struct.pack('<HH', k, pg) for k, pg in picked)
        data = bytearray()
        for k, pg in picked:
            o, ln, fo, fl = segs[k]
            if pg != 0xFFFF:
                o, ln = o + pg * PAGE, min(PAGE, ln - pg * PAGE)
            data += rom[o:o + ln] + bytes((-ln) % 4)
        blocks.append(head + bytes(data))
        sizes.append(len(data))
        total += len(head) + len(data)
    off, offs = 12 + 4 * 257, []
    for b in blocks:
        offs.append(off)
        off += len(b)
    offs.append(off)
    with open(os.path.join(out, 'PACKS.BIN'), 'wb') as f:
        f.write(b'TPK2' + struct.pack('<I', 256) + struct.pack('<I', 0) + struct.pack('<257I', *offs))
        for b in blocks:
            f.write(b)
    print('area packs: %d areas, %.1f MB, largest %d KB' % (len(sizes), total / 1048576, max(sizes) // 1024))


if __name__ == '__main__':
    main()
