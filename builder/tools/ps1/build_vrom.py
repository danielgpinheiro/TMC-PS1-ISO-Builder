#!/usr/bin/env python3
"""The virtual ROM on the disc (docs/36 phase 2.2): TMC/ROM.IDX + TMC/ROM.BIN, built from the user's ROM.

Segments (sorted, non-overlapping, covering every byte the game may read):
  - two contiguous images the PS1 loads at boot:
      LOW_A = ROM [0, 0x16986): the first tables and the ROM header. It starts at offset 0, so on the PS1 it *is*
             gRomData (gRomSize = its length); the rest of LOW (the code region up to Link's sprite sheet) is the
             Thumb code in 64 KB pieces and LOW_B, one segment of scripts and tables, both on demand;
      HIGH = ROM [0x9B1D90, 0xA12ED0): text, the song table and other tables after the graphics;
  - every range of the ROM map (tools/ps1/rommap.py --json: assets, songs, samples) outside them, one segment each;
  - the gaps between those, one segment each (tables / scripts / padding).
A segment is loaded whole, on first use, so a pointer into it can be walked to its end. Runs of consecutive small
segments (each < 16 KB; e.g. the 32-byte palettes, which one palette-group copy spans) are merged, up to 64 KB, so
code walking from one into the next stays inside one segment; big ones (sprite sheets, graphics, maps) stay alone.

ROM.IDX: "TMCV" u32 version=1, u32 count, then count x (u32 rom offset, u32 length, u32 offset in ROM.BIN, u32 flags:
bit 0 = preload at boot; bits 8-17 = the kinds of bytes the segment holds, bit 8 + KINDS index, bit 17 = bytes outside
the ROM map (code region, scripts, tables); bit 18 = text banks; bit 19 = inside the asset region: the PS1 frees a
segment of copy-only kinds, text or the asset region when the area changes, docs/36 2.4.2). ROM.BIN: the segments' bytes (by area, see the layout below), each padded to 2048 (a CD sector) so a segment
starts on a sector. LZ77 data stays compressed for now (the debug path decodes at run time; phase 2.4 stores it
decoded: golden rule).
Usage: tools/ps1/build_vrom.py ROM ROMMAP_JSON OUT_DIR
"""
import json, re, struct, sys, os

KINDS = ['gfx', 'tileset gfx', 'palettes', 'room maps / tile types', 'sprite gfx', 'animations', 'tables / other data',
         'songs', 'samples']  # flags bit 8 + index; bit 17 = unmapped bytes; bit 18 = text banks only
ASSETS = (0x13AE14, 0x9B1D90)  # sprite sheets, tile sets, maps, gGlobalGfxAndPalettes (graphics, palettes), frame
                               # lists: the game reaches them only by GBA address (DMA / LZ77 / gMapData / sheets) and
                               # copies out; the ROM tables in it were copied to BSS at boot (flags bit 19)
# VROM_FINE=lo:hi (hex ROM offsets): 16 KB merges only there, 64 KB elsewhere (bisects a layout-dependent A/B change)
FINE = tuple(int(x, 16) for x in os.environ['VROM_FINE'].split(':')) if os.environ.get('VROM_FINE') else ASSETS
TEXT_BASE = 0x9B1D90  # gTranslations (USA: every language): u32 bank offsets, then the banks (u32 message offsets +
                      # strings); src/text.c resolves them by ROM offset on the PS1, so each bank can be a segment
LOW = (0x000000, 0x13AE14)
HIGH = (0x9B1D90, 0xA12ED0)
# LOW split (docs/36 2.4.4): only its first part is a boot image (the PC layer's gRomData: tables, the ROM header);
# the Thumb code (never read on the PS1) is cut in 64 KB pieces a table copy may touch at boot; the data after it
# (LOW_B) is segmented like the rest: its ROM-map items (tables, animations, sprite frame tables) and the gaps between
# them (scripts and other unlisted data, never cut: pointer walks stay inside one).
LOW_A = (0x000000, 0x016986)
LOW_CODE = (0x016986, 0x0B2AA8)
LOW_B = (0x0B2AA8, 0x13AE14)
SECTOR = 2048


FRAME_OBJ_LISTS = (0x2F3D74, 0x30D6D)  # gFrameObjLists (USA): u32 index[512] of frame-table offsets, the frame tables
                                       # (u32 record offsets), the records (count, count x 5 bytes); offsets from its start


def frame_obj_cuts(rom):
    """cut points (offsets in gFrameObjLists) the drawing code never reads across (docs/36 2.4): after the index, each
    frame table's start, each record's start that no other record spans (46 records share bytes with the one before)"""
    base, size = FRAME_OBJ_LISTS
    d = rom[base:base + size]
    tabs = sorted(set(struct.unpack_from('<512I', d, 0)))
    recs = set()
    for i, t in enumerate(tabs):
        end = tabs[i + 1] if i + 1 < len(tabs) else None
        p = t
        while end is None or p < end:
            v = struct.unpack_from('<I', d, p)[0]
            if end is None and not tabs[-1] < v < size:  # the last table: until the records begin
                break
            recs.add(v)
            p += 4
    recs = sorted(recs)
    assert tabs[0] == 0x800 and recs[0] >= tabs[-1] and all(t % 4 == 0 for t in tabs)
    spans = [(r, r + 1 + 5 * d[r]) for r in recs]
    assert spans[-1][1] == size
    cuts, reach = [0] + tabs, 0
    for a, b in spans:
        if a >= reach:
            cuts.append(a)
        reach = max(reach, b)
    return sorted(set(cuts)) + [size]


def fallthrough_labels(root):
    """docs/36 phase 9: labels the block before runs into (map data whose last statement is an entity record, no
    `entity_list_end`: e.g. Entities_Beanstalks_EasternHills_1 ends in the next label's terminator, the empty
    Enemies_Beanstalks_EasternHills). A segment cut there split the list from its terminator: the walk read heap bytes"""
    import glob
    ent = set(re.findall(r'^\s*\.macro\s+(\w+)', open(os.path.join(root, 'asm', 'macros', 'entity.inc')).read(), re.M))
    ent -= {m for m in ent if m.endswith('_end') or m.startswith('exit')}
    out = set()
    for f in glob.glob(os.path.join(root, 'data', 'map', '*.s')):
        parts = re.split(r'^(\w+)::\s*@\s*08([0-9A-Fa-f]{6})\s*$', open(f, errors='replace').read(), flags=re.M)
        items = [(int(parts[i + 1], 16), parts[i + 2]) for i in range(1, len(parts) - 2, 3)]
        for (a, body), nxt in zip(items, items[1:]):
            lines = [l.split()[0] for l in body.split('\n') if l.strip() and not l.strip().startswith('@')]
            if lines and lines[-1] in ent:
                out.add(nxt[0])
    return out


def low_b_labels():
    """ROM offsets of the decomp's data labels ("Name:: @ 08XXXXXX" in data/ and asm/), inside LOW_B, less the ones a
    block before falls through into (fallthrough_labels)"""
    import glob
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import repo_meta  # (the public builder: TMC_META, docs/36 11)
    if repo_meta.meta_dir():
        return repo_meta.load('low_b_labels.json')
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
    out = set()
    for f in glob.glob(os.path.join(root, 'data', '**', '*.s'), recursive=True) + \
            glob.glob(os.path.join(root, 'asm', '**', '*.s'), recursive=True):
        for m in re.finditer(r'^\w+::\s*@\s*08([0-9A-Fa-f]{6})', open(f, errors='replace').read(), re.M):
            o = int(m.group(1), 16)
            if LOW_B[0] <= o < LOW_B[1] and o % 4 == 0:  # a segment starting off a 4-byte boundary misaligns the
                out.add(o)                                   # tables after it in RAM (a u32 load in LoadRoomEntity: AdEL)
    return sorted(out - fallthrough_labels(root))


def main():
    rom = bytearray(open(sys.argv[1], 'rb').read())
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import text_patch  # the PS1 wording of the save messages (docs/36 7b.1), in the virtual ROM's copy only
    print('text_patch: %d save messages in PS1 wording' % text_patch.apply(rom))
    rom = bytes(rom)
    rmap = sorted(json.load(open(sys.argv[2])))
    # an item continued by "<name>_<n>.bin" right after it is one table the extractor split (a font's first glyph and
    # the rest, 0x692F60 + 0x692FA0): join them, so no segment boundary falls inside one (code that walks a table
    # past its segment's end would read another segment's bytes)
    joined = []
    for o, n, k, p, d in rmap:
        if joined:
            jo, jn, jk, jp, jd = joined[-1]
            base = re.sub(r'(_\d+)?\.bin(\.lz)?$', '', jp)
            if jo + jn == o and re.fullmatch(re.escape(base) + r'_\d+\.bin(\.lz)?', p):
                joined[-1] = [jo, jn + n, jk, jp, jd]
                continue
        joined.append([o, n, k, p, d])
    rmap = joined
    out = sys.argv[3]
    size = len(rom)
    # HIGH (docs/36 2.4.4): the text's bank table, the banks in message chunks, then the rest (song table, ...) in one
    nb = struct.unpack_from('<I', rom, TEXT_BASE)[0] // 4
    starts = [TEXT_BASE + x for x in struct.unpack_from('<%dI' % nb, rom, TEXT_BASE)]
    assert starts == sorted(starts) and starts[0] > TEXT_BASE
    last = starts[-1]
    nm = struct.unpack_from('<I', rom, last)[0] // 4
    text_end = max(rom.index(b'\0', last + m) + 1 for m in struct.unpack_from('<%dI' % nm, rom, last))
    text_end = (text_end + 3) & ~3
    text = (starts[0], text_end)
    segs = [(LOW_A[0], LOW_A[1] - LOW_A[0], 1), (TEXT_BASE, starts[0] - TEXT_BASE, 0)]
    # each bank: its message-offset table, then one piece per message (offsets rise strictly in every USA bank, so a
    # message is [its offset, the next one's); the merge below groups them up to 4 KB: a message box's token pointer
    # (src/text.c) never crosses a segment, and an area keeps only the chunks it showed, not whole banks
    for a, b in zip(starts, starts[1:] + [text_end]):
        nm = struct.unpack_from('<I', rom, a)[0] // 4
        offs = struct.unpack_from('<%dI' % nm, rom, a)
        assert all(x < y for x, y in zip(offs, offs[1:])) and a + offs[-1] < b, 'bank %x: messages out of order' % a
        cuts = [a] + [a + x for x in offs] + [b]
        for x, y in zip(cuts, cuts[1:]):
            if y > x:
                segs.append((x, y - x, 0))
    segs.append((text_end, HIGH[1] - text_end, 0))
    for o in range(LOW_CODE[0], LOW_CODE[1], 0x10000):
        segs.append((o, min(0x10000, LOW_CODE[1] - o), 0))

    def outside(o, n):
        """parts of [o, o + n) outside LOW_A, the Thumb code and HIGH (LOW_B's items are segments like the rest)"""
        parts = [(o, o + n)]
        for a, b in ((LOW_A[0], LOW_CODE[1]), HIGH):
            nxt = []
            for x, y in parts:
                if y <= a or x >= b:
                    nxt.append((x, y))
                else:
                    if x < a:
                        nxt.append((x, a))
                    if y > b:
                        nxt.append((b, y))
            parts = nxt
        return parts

    # ROM-map ranges (first one wins where they overlap)
    covered = []
    last = 0
    for o, n, k, p, d in rmap:
        o0 = max(o, last)
        if o0 >= o + n:
            continue
        if o == FRAME_OBJ_LISTS[0]:
            assert o0 == o and n == FRAME_OBJ_LISTS[1]
            for x, y in zip(frame_obj_cuts(rom), frame_obj_cuts(rom)[1:]):
                segs.append((o + x, y - x, 0))
            last = o + n
            continue
        for x, y in outside(o0, o + n - o0):
            segs.append((x, y - x, 0))
        last = max(last, o + n)
    segs.sort()
    # gaps -> their own segments; in LOW_B cut at the decomp's labels (room properties, entity / enemy / tile-entity
    # lists, area tile sets: per-room data a room reads from its start, docs/36 2.6), merged back to 4 KB below
    labels = low_b_labels()
    full = []
    pos = 0

    def gap(a, b):
        cuts = [x for x in labels if a < x < b] if LOW_B[0] <= a < LOW_B[1] else []
        for x, y in zip([a] + cuts, cuts + [b]):
            full.append((x, y - x, 0))
    for o, n, f in segs:
        if o > pos:
            gap(pos, o)
        full.append((o, n, f))
        pos = max(pos, o + n)
    if pos < size:
        full.append((pos, size - pos, 0))
    # merge runs of small consecutive segments (never the boot images)
    pal_starts = {o for o, n, k, p, d in rmap if k == 'palettes'}  # palettes: re-read on every room load (never
                                                                    # transient), so 4 KB groups (docs/36 2.6)
    merged, merged_pal = [], []
    for o, n, f in full:
        in_text = text[0] <= o < text[1]
        in_low_b = LOW_B[0] <= o < LOW_B[1]
        # a touched byte costs its whole merged segment: LOW_B (init reads a little of it everywhere, area tables) 4 KB
        # (1 KB split a structure the game walks: the A/B diverged at frame 7200);
        # the asset region 16 KB (a graphics group's 768-byte copies no longer read 64 KB each); the rest 64 KB
        in_assets = FINE[0] <= o < FINE[1]
        in_fol = FRAME_OBJ_LISTS[0] <= o < FRAME_OBJ_LISTS[0] + FRAME_OBJ_LISTS[1]  # read every frame, a record at a time
        is_pal = o in pal_starts
        limit = 4096 if in_low_b or in_text or in_fol or is_pal else 16384 if in_assets else 65536  # songs / samples: 64 KB (the m4a
                                                                                   # state reads song data across items)
        if merged and not f and not merged[-1][2] and n < 16384 and merged[-1][1] < 16384 * 4 \
                and merged[-1][1] + n <= limit and merged[-1][0] + merged[-1][1] == o \
                and in_text == (text[0] <= merged[-1][0] < text[1]) \
                and in_low_b == (LOW_B[0] <= merged[-1][0] < LOW_B[1]) \
                and in_assets == (FINE[0] <= merged[-1][0] < FINE[1]) \
                and in_fol == (FRAME_OBJ_LISTS[0] <= merged[-1][0] < FRAME_OBJ_LISTS[0] + FRAME_OBJ_LISTS[1]) \
                and is_pal == merged_pal[-1]:
            mo, mn, mf = merged[-1]
            merged[-1] = (mo, mn + n, 0)
        else:
            merged.append((o, n, f))
            merged_pal.append(is_pal)
    full = merged
    # sanity: sorted, contiguous, no overlap
    p = 0
    for o, n, f in full:
        assert o == p and n > 0, (hex(o), hex(p))
        p = o + n
    assert p == size
    # kinds per segment (bytes of each ROM-map kind inside it; unmapped bytes set bit 17)
    def kind_bits(o, n):
        bits, cov = 0, 0
        for ro, rn, k, path, d in rmap:
            if ro >= o + n:
                break
            x0, x1 = max(ro, o), min(ro + rn, o + n)
            if x1 > x0:
                bits |= 1 << (8 + KINDS.index(k))
                cov += x1 - x0
        if cov < n:
            bits |= 1 << 17
        if text[0] <= o and o + n <= text[1]:
            bits = 1 << 18  # whole text banks (never mixed with other data: merges stop at the text's edges)
        if ASSETS[0] <= o and o + n <= ASSETS[1] and bits != 1 << (8 + KINDS.index('sprite gfx')):
            bits |= 1 << 19  # in the asset region: only copied out through GBA addresses, freed at area changes
        return bits
    full = [(o, n, f | kind_bits(o, n)) for o, n, f in full]
    os.makedirs(out, exist_ok=True)
    idx = bytearray(b'TMCV' + struct.pack('<II', 1, len(full)))
    # ROM.BIN's layout (docs/36 6.6): the boot segments, then each area's (a segment with the lowest area whose rooms
    # touch it, tools/ps1/play/rooms.txt; in room order), then the rest in ROM order: an area change reads nearby
    # sectors instead of seeking across the 17 MB. ROM.IDX stays in ROM order (the file offsets say where).
    order = [k for k, (o, n, f) in enumerate(full) if f & 1]
    placed = set(order)
    manifest = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'play', 'rooms.txt')
    if os.path.exists(manifest):
        import bisect
        starts = [o for o, n, f in full]
        uses = sorted((int(a_), int(r_), bisect.bisect_right(starts, int(o_, 16)) - 1)  # (lines: area room 0xOFFSET)
                      for a_, r_, o_ in (line.split() for line in open(manifest)
                                         if line.strip() and not line.startswith('#')))
        for a_, r_, k in uses:
            if 0 <= k < len(full) and k not in placed:
                order.append(k)
                placed.add(k)
    order += [k for k in range(len(full)) if k not in placed]
    file_off = {}
    with open(os.path.join(out, 'ROM.BIN'), 'wb') as b:
        off = 0
        for k in order:
            o, n, f = full[k]
            file_off[k] = off
            data = rom[o:o + n]
            pad = (-len(data)) % SECTOR
            b.write(data + b'\0' * pad)
            off += len(data) + pad
    for k, (o, n, f) in enumerate(full):
        idx += struct.pack('<IIII', o, n, file_off[k], f)
    open(os.path.join(out, 'ROM.IDX'), 'wb').write(idx)
    pre = sum(n for o, n, f in full if f & 1)
    print('segments %d (preload %d KB), ROM.BIN %.1f MB, ROM.IDX %d B' % (len(full), pre // 1024, off / 1048576, len(idx)))


if __name__ == '__main__':
    main()
