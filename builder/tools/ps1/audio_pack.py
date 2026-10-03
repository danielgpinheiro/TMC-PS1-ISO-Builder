#!/usr/bin/env python3
"""m4a samples -> SPU ADPCM (docs/36 5.1).

Every DirectSound sample a song plays (tools/ps1/audio_fit.py walks the songs) is converted by tools/ps1/spuadpcm (a
host C tool built here): loops on 28-sample blocks (the loop body resampled to a block multiple, the front padded),
the rate scaled by the same ratio so the pitch is exact. Each result is decoded back here and compared with the source
(SNR over the part before the loop / a one-shot's whole length, where the samples line up).

Writes OUT/samples.bin (the ADPCM data, each sample padded to 64 bytes for the SPU DMA) and OUT/samples.json (per
sample: ROM offset, offset / size in samples.bin, loop block, rate x 1024 after the ratio, source length / loop).
No ROM bytes leave build/ (the ROM-derived output stays in the ignored build directory).

Usage: tools/ps1/audio_pack.py ROM OUT_DIR
"""
import json, math, os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audio_fit  # noqa: E402

ROMBASE = 0x08000000
FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))


def build_tool(out):
    exe = os.path.join(out, 'spuadpcm')
    src = os.path.join(HERE, 'spuadpcm.c')
    if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(src):
        subprocess.check_call(['cc', '-O2', '-o', exe, src, '-lm'])
    return exe


def decode(data):
    out, h1, h2 = [], 0, 0
    for b in range(len(data) // 16):
        blk = data[b * 16:b * 16 + 16]
        shift, f = blk[0] & 15, blk[0] >> 4
        if shift > 12:
            shift = 9
        f0, f1 = FILTERS[min(f, 4)]
        for i in range(28):
            nib = (blk[2 + i // 2] >> ((i & 1) * 4)) & 15
            if nib >= 8:
                nib -= 16
            v = ((nib << 12) >> shift) + ((h1 * f0 + h2 * f1 + 32) >> 6)
            v = max(-32768, min(32767, v))
            out.append(v)
            h2, h1 = h1, v
    return out


def song_samples(rom, repo):
    used = set()
    for sid, name, hdr in audio_fit.load_songs(repo, rom):
        ntracks = rom[hdr]
        vg = audio_fit.u32(rom, hdr + 4) - ROMBASE
        out = {'samples': set(), 'waves': set(), 'noise': False, 'square': False}
        if ntracks and 0 <= vg < len(rom):
            for t in range(ntracks):
                tp = audio_fit.u32(rom, hdr + 8 + 4 * t) - ROMBASE
                if 0 <= tp < len(rom):
                    for prog, key in audio_fit.walk_track(rom, tp):
                        try:
                            audio_fit.resolve(rom, vg, prog, key, out)
                        except (IndexError, struct.error):
                            pass
        used |= out['samples']
    return sorted(used)


def encode(rom, w, tool, tmp_in, tmp_out, scale=1.0):
    """DirectSound sample at ROM offset w -> (SPU ADPCM, num, den, pad, loopBlock); scale < 1 resamples the 8-bit PCM
    first (linear, scale x the samples; the caller scales the pitch the same: docs/36 choice 122, an area's sound bank
    that doesn't fit SPU RAM)"""
    flags, pitch, loop, n = struct.unpack_from('<IIII', rom, w)
    looped = bool(flags & 0x40000000) and loop < n
    src = rom[w + 16:w + 16 + n]
    if scale < 1.0:
        sv = [b - 256 if b >= 128 else b for b in src]
        n2 = max(28, int(n * scale))
        out = []
        for i in range(n2):
            x = i / scale
            k = int(x)
            f = x - k
            a = sv[min(k, n - 1)]
            b = sv[min(k + 1, n - 1)]
            out.append(int(round(a + (b - a) * f)))
        src = bytes((v + 256) % 256 for v in out)
        loop, n = min(int(loop * scale), n2 - 1), n2
    open(tmp_in, 'wb').write(src)
    r = subprocess.check_output([tool, tmp_in, tmp_out, '1' if looped else '0', str(loop), str(n)]).split()
    blocks, pad, num, den, loopBlock = (int(x) for x in r)
    return open(tmp_out, 'rb').read(), num, den, pad, loopBlock


def main():
    rom = open(sys.argv[1], 'rb').read()
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    repo = os.path.abspath(os.path.join(HERE, '..', '..'))
    tool = build_tool(out)
    samples = song_samples(rom, repo)
    blob, index, snrs = bytearray(), {}, []
    tmp_in, tmp_out = os.path.join(out, 'tmp.s8'), os.path.join(out, 'tmp.adpcm')
    for w in samples:
        mode = rom[w]
        flags, pitch, loop, n = struct.unpack_from('<IIII', rom, w)
        looped = bool(flags & 0x40000000) and loop < n
        data, num, den, pad, loopBlock = encode(rom, w, tool, tmp_in, tmp_out)
        # check: the decoded samples against the source where they line up (before any resampling / padding)
        dec = decode(data)
        ref_n = n if not looped else (loop if num == den else 0)
        if ref_n > 64:
            src = [(b - 256 if b >= 128 else b) * 256 for b in rom[w + 16:w + 16 + ref_n]]
            got = dec[pad:pad + ref_n]
            sig = sum(v * v for v in src) or 1
            err = sum((a - b) ** 2 for a, b in zip(src, got)) or 1
            snrs.append(10 * math.log10(sig / err))
        off = len(blob)
        blob += data
        blob += bytes((-len(blob)) % 64)
        index[str(w)] = {'mode': mode, 'offset': off, 'size': len(data), 'loop': looped, 'loopBlock': loopBlock,
                         'rate1024': pitch * num // den, 'srcLength': n, 'srcLoop': loop, 'pad': pad,
                         'ratio': [num, den]}
    open(os.path.join(out, 'samples.bin'), 'wb').write(blob)
    json.dump(index, open(os.path.join(out, 'samples.json'), 'w'), indent=1)
    for p in (tmp_in, tmp_out):
        os.remove(p)
    looped = sum(1 for v in index.values() if v['loop'])
    snrs.sort()
    print('samples %d (looped %d, other modes %d): SPU ADPCM %.0f KB' % (
        len(index), looped, sum(1 for v in index.values() if v['mode']), len(blob) / 1024))
    if snrs:
        print('decode check SNR dB: min %.1f, median %.1f (%d samples compared)' % (snrs[0], snrs[len(snrs) // 2],
                                                                                   len(snrs)))


if __name__ == '__main__':
    main()
