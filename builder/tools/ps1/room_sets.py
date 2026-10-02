#!/usr/bin/env python3
"""Per-room manifests for the PS1's prefetcher (docs/36 6.1 / 6.3): TMC/ROOMS.BIN.

Input: tools/ps1/play/rooms.txt, "area room 0xOFFSET" lines (a segment by its first ROM byte) (the virtual-ROM segments a room touched: resolves, LZ77
streams and direct copies, from the PS1's room-touch log over the route and the tours; `add` merges a run's log in),
and the ROM's room headers (tools/ps1/room_geom.py: the neighbours of a room = the rooms of its area whose rectangles
touch it). A room is prefetched from: its own segments, then its neighbours'.

Layout (little-endian): "TRM2", u32 256, u32 offsets[257] (area a's block = [offsets[a], offsets[a + 1]), empty when
equal); a block (one area: a room's neighbours are its area's rooms, so the PS1 loads only the current area's block,
docs/36 choice 103): u32 rooms, u32 segment entries, u32 neighbour entries; rooms x (u16 key = area << 8 | room, u16
segments, u16 first segment entry, u16 neighbours, u16 first neighbour entry, u16 0), sorted by key; u16 segment
indices; u16 neighbour keys; padded to 4. Numbers only (segment indices, room keys): no ROM bytes.

Usage: tools/ps1/room_sets.py add OUT.roombin      (merge a run's room-touch log into tools/ps1/play/rooms.txt; the
                                                    ROM.IDX in build/ps1/iso must be that run's)
       tools/ps1/room_sets.py convert OLD_ROM.IDX  (index lines -> offset lines)
       tools/ps1/room_sets.py build ROM OUT.BIN
"""
import os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import room_geom  # noqa: E402

MANIFEST = os.path.join(HERE, 'play', 'rooms.txt')
REC = struct.Struct('<IIHHBBBB')  # ps1/tmc_vrom.c RoomEvent: frame, bytes, seg, page, area, room, kind, cd


IDX = os.path.join(HERE, '..', '..', 'build', 'ps1', 'iso', 'TMC', 'ROM.IDX')


def idx_starts(path=IDX):
    """the segments' ROM offsets (ROM.IDX order = ROM order)"""
    d = open(path, 'rb').read()
    n = struct.unpack_from('<I', d, 8)[0]
    return [struct.unpack_from('<I', d, 12 + 16 * k)[0] for k in range(n)]


def seg_of(starts, off):
    import bisect
    return bisect.bisect_right(starts, off) - 1


def load():
    """(area, room, ROM offset of a segment's start): docs/36 phase 9, offsets so a new segmentation keeps them"""
    out = set()
    if os.path.exists(MANIFEST):
        for line in open(MANIFEST):
            if line.strip() and not line.startswith('#'):
                a, r, s = line.split()
                out.add((int(a), int(r), int(s, 16)))
    return out


def add(path):
    have = load()
    data = open(path, 'rb').read()
    new = set()
    starts = idx_starts()  # (the run's disc: add before the next build)
    for i in range(len(data) // REC.size):
        fr, nb, seg, page, area, room, kind, cd = REC.unpack_from(data, i * REC.size)
        if kind in (0, 2, 3) and seg < len(starts):  # segment resolve, direct copy, stream: resident = no CD read
            new.add((area, room, starts[seg]))
    added = new - have
    save(have | new)
    print('rooms.txt: %d entries (%d new) over %d rooms' % (len(have | new), len(added),
                                                            len({(a, r) for a, r, s in have | new})))


def save(entries):
    with open(MANIFEST, 'w') as f:
        f.write('# area room segment: virtual-ROM segments each room touched, by their first ROM offset '
                '(tools/ps1/room_sets.py add)\n')
        for a, r, o in sorted(entries):
            f.write('%d %d 0x%06x\n' % (a, r, o))


def build(rom_path, out):
    rom = open(rom_path, 'rb').read()
    nbs = room_geom.neighbours(room_geom.rooms(rom))
    starts = idx_starts(os.path.join(os.path.dirname(out), 'ROM.IDX'))
    segs = {}
    for a, r, o in load():
        k = seg_of(starts, o)
        if k >= 0 and k not in segs.get((a, r), []):
            segs.setdefault((a, r), []).append(k)
    keys = sorted(set(segs) | {k for k in nbs if k in segs or any(n in segs for n in [(k[0], x) for x in nbs[k]])})
    blocks, nrooms, nsegs, nnbs = [], 0, 0, 0
    for area in range(256):
        table, seg_list, nb_list = [], [], []
        for a, r in (k for k in keys if k[0] == area):
            ss = sorted(segs.get((a, r), []))
            nn = [(a << 8) | x for x in nbs.get((a, r), []) if (a, x) in segs]
            table.append(struct.pack('<6H', (a << 8) | r, len(ss), len(seg_list), len(nn), len(nb_list), 0))
            seg_list += ss
            nb_list += nn
        blk = b''
        if table:
            blk = struct.pack('<III', len(table), len(seg_list), len(nb_list)) + b''.join(table)
            blk += struct.pack('<%dH' % len(seg_list), *seg_list) + struct.pack('<%dH' % len(nb_list), *nb_list)
            blk += b'\0' * (-len(blk) % 4)
        blocks.append(blk)
        nrooms, nsegs, nnbs = nrooms + len(table), nsegs + len(seg_list), nnbs + len(nb_list)
    offs, at = [], 8 + 257 * 4
    for blk in blocks:
        offs.append(at)
        at += len(blk)
    offs.append(at)
    data = b'TRM2' + struct.pack('<I', 256) + struct.pack('<257I', *offs) + b''.join(blocks)
    open(out, 'wb').write(data)
    print('rooms: %d with a manifest, %d segment entries, %d neighbour links, %.1f KB (largest area %.1f KB)' % (
        nrooms, nsegs, nnbs, len(data) / 1024, max(len(b) for b in blocks) / 1024))


if __name__ == '__main__':
    if sys.argv[1] == 'add':
        add(sys.argv[2])
    elif sys.argv[1] == 'convert':  # legacy "area room index" lines -> offsets, with the ROM.IDX they were made with
        starts = idx_starts(sys.argv[2])
        ents = set()
        for line in open(MANIFEST):
            if line.strip() and not line.startswith('#'):
                a, r, k = (int(v) for v in line.split())
                ents.add((a, r, starts[k]))
        save(ents)
        print('converted', len(ents))
    else:
        build(sys.argv[2], sys.argv[3])
