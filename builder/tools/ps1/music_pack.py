#!/usr/bin/env python3
"""The music stream for the PS1 (docs/36 5.5, choice 67): TMC/MUSIC.BIN.

Every song the BGM player (31) plays is rendered by agbplay (tools/ps1/music_render.cpp: the PC port's engine and
settings, 22,050 Hz stereo, the loop from track 0's GOTOs) and stored as SPU ADPCM in chunks of CHUNK blocks per
channel: a chunk = the left channel's CHUNK x 16 bytes, then the right's (8 KB = 4 CD sectors); each song starts on a
sector. ps1/tmc_snd.c streams them into an SPU ring from the CD a chunk at a time.

Layout (little-endian): "TMU1", u32 songs, u32 chunk blocks, u32 reserved; songs x (u32 first sector (from the file's
start; 0 = not streamed), u32 blocks, s32 loop block (-1 = plays once)); padding to a sector; the songs.
No ROM bytes: rendered audio of the user's ROM, in build/ (ignored) like every disc file.

Usage: tools/ps1/music_pack.py ROM OUT.BIN
"""
import os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, HERE)
import audio_fit  # noqa: E402

RATE = 22050
CHUNK = 256  # blocks per channel per chunk (7,168 samples, 0.33 s)
SECTOR = 2048
BGM_PLAYER = 31


def build_renderer(out_dir):
    exe = os.path.join(out_dir, 'music_render')
    srcs = [os.path.join(HERE, 'music_render.cpp')] + sorted(
        os.path.join(REPO, 'libs', 'agbplay_core', f) for f in os.listdir(os.path.join(REPO, 'libs', 'agbplay_core'))
        if f.endswith('.cpp'))
    if not os.path.exists(exe) or os.path.getmtime(exe) < max(os.path.getmtime(s) for s in srcs):
        subprocess.check_call(['c++', '-std=c++20', '-O2', '-I' + os.path.join(REPO, 'libs', 'agbplay_core'), '-o',
                               exe] + srcs)
    return exe


def bgm_songs(songs):
    """the BGM player's songs (tools/ps1/snd_sets.py) with a named header"""
    import snd_sets
    named = {sid for sid, name, hdr in songs if not name.startswith('song_')}
    return sorted(snd_sets.stream_songs() & named)


def main():
    rom_path, out = sys.argv[1], sys.argv[2]
    rom = open(rom_path, 'rb').read()
    work = os.path.join(REPO, 'build', 'ps1', 'music')
    os.makedirs(work, exist_ok=True)
    exe = build_renderer(work)
    songs = audio_fit.load_songs(REPO, rom)
    hdr = {sid: h for sid, name, h in songs}
    ids = bgm_songs(songs)
    count = len(songs)
    entries = [(0, 0, -1)] * count
    head_bytes = 16 + 12 * count
    data = bytearray()
    first = (head_bytes + SECTOR - 1) // SECTOR
    total_s = 0.0
    for sid in ids:
        base = os.path.join(work, 'song%03d' % sid)
        blocks, loop = (int(x) for x in subprocess.check_output([exe, rom_path, hex(hdr[sid]), str(RATE), base]).split())
        L, R = open(base + '.L', 'rb').read(), open(base + '.R', 'rb').read()
        sector = first + len(data) // SECTOR
        for c in range(0, blocks, CHUNK):
            n = min(CHUNK, blocks - c)
            data += L[c * 16:(c + n) * 16] + bytes((CHUNK - n) * 16)
            data += R[c * 16:(c + n) * 16] + bytes((CHUNK - n) * 16)
        data += bytes((-len(data)) % SECTOR)
        entries[sid] = (sector, blocks, loop)
        total_s += blocks * 28 / RATE
    head = b'TMU1' + struct.pack('<III', count, CHUNK, 0) + b''.join(struct.pack('<IIi', *e) for e in entries)
    head += bytes(first * SECTOR - len(head))
    open(out, 'wb').write(head + data)
    looped = sum(1 for e in entries if e[2] >= 0)
    print('music: %d songs (%d loop), %.0f s of audio, %.1f MB at %d Hz stereo SPU ADPCM' % (
        len(ids), looped, total_s, (len(head) + len(data)) / 1048576, RATE))


if __name__ == '__main__':
    main()
