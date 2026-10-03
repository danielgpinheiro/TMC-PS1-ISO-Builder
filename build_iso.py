#!/usr/bin/env python3
"""Minish Cap PS1 ISO Builder: your The Legend of Zelda: The Minish Cap (USA) GBA ROM -> a PlayStation disc image.

    python3 build_iso.py --rom /path/to/baserom.gba [--license licensea.dat] [--out output]

1. checks the tools (mkpsxiso, a C compiler, a C++20 compiler) and the ROM (the USA release, SHA-1);
2. completes the prebuilt executable and code overlays (bin/files: the runs they share with the ROM were taken out,
   bin/files/holes.json says where) from your ROM, and checks each against the build's SHA-256;
3. converts the ROM for the PlayStation (the virtual ROM, the per-area packs, song data and SPU samples for the
   sequenced music, the per-room load manifests): builder/tools/ps1, the same converters as the port;
4. builds the disc with mkpsxiso and verifies it byte by byte (EDC/ECC, license sectors, every file);
5. writes output/TMC-PS1.bin + .cue + SHA256SUMS.

No game data is included in this repository: use your own cartridge's ROM (see README.md).
"""
import argparse, hashlib, os, shutil, subprocess, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILDER = os.path.join(ROOT, 'builder')
TOOLS = os.path.join(BUILDER, 'tools', 'ps1')
BIN = os.path.join(ROOT, 'bin')
ROM_SHA1 = 'b4bd50e4131b027c334547b4524e2dbbd4227130'  # The Legend of Zelda: The Minish Cap (USA)


def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def find_tools():
    env = {}
    mk = os.environ.get('MKPSXISO') or shutil.which('mkpsxiso')
    if not mk:
        sys.exit('mkpsxiso not found: install it (https://github.com/Lameguy64/mkpsxiso) or set MKPSXISO=/path/to/mkpsxiso')
    env['MKPSXISO'] = mk
    if not shutil.which('cc'):
        sys.exit('cc not found: a C compiler is needed (Xcode command line tools, gcc or clang)')
    return env


def check_bin():
    bad = []
    for line in open(os.path.join(BIN, 'SHA256SUMS')):
        h, name = line.split(None, 1)
        p = os.path.join(BIN, name.strip())
        if not os.path.isfile(p) or digest(p, 'sha256') != h:
            bad.append(name.strip())
    if bad:
        sys.exit('bin/ does not match bin/SHA256SUMS: %s' % ', '.join(bad))


def main():
    ap = argparse.ArgumentParser(description='Build a Minish Cap PlayStation disc image from your own GBA ROM.')
    ap.add_argument('--rom', required=True, help='The Legend of Zelda: The Minish Cap (USA) ROM, dumped from your cartridge')
    ap.add_argument('--license', help='optional Sony license file (e.g. licensea.dat, NTSC-U) for retail consoles')
    ap.add_argument('--out', default=os.path.join(ROOT, 'output'), help='output folder (default: output/)')
    a = ap.parse_args()

    if sys.version_info < (3, 8):
        sys.exit('Python 3.8 or newer is required.')
    if not os.path.isfile(a.rom):
        sys.exit('ROM not found: %s' % a.rom)
    if a.license and not os.path.isfile(a.license):
        sys.exit('License file not found: %s' % a.license)
    tools = find_tools()
    got = digest(a.rom, 'sha1')
    if got != ROM_SHA1:
        sys.exit('The ROM is not The Minish Cap (USA) (SHA-1 %s, expected %s).\n'
                 'Use an unmodified dump of the USA cartridge (headerless .gba, 16 MB).' % (got, ROM_SHA1))
    check_bin()

    rom = os.path.abspath(a.rom)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    work = os.path.join(BUILDER, 'build', 'ps1')  # the converters' own work folder (as in the port's repository)
    iso = os.path.join(work, 'iso')
    if os.path.exists(iso):
        shutil.rmtree(iso)
    tmc = os.path.join(iso, 'TMC')
    os.makedirs(tmc)
    env = dict(os.environ, TMC_META=os.path.join(BIN, 'meta'), PYTHONDONTWRITEBYTECODE='1', **tools)
    py = sys.executable
    t0 = time.time()

    def step(label, *args):
        print('[%s]' % label, flush=True)
        subprocess.run([py] + [str(x) for x in args], env=env, check=True, stdout=subprocess.DEVNULL if label.endswith('*') else None)

    files = os.path.join(work, 'files')
    step('executable', os.path.join(TOOLS, 'rom_holes.py'), 'fill', rom, os.path.join(BIN, 'files'), files)
    step('audio fit*', os.path.join(TOOLS, 'audio_fit.py'), rom, '--json', os.path.join(work, 'audio.json'))
    step('rom map*', os.path.join(TOOLS, 'rommap.py'), rom, '--audio', os.path.join(work, 'audio.json'), '--json',
         os.path.join(work, 'rommap.json'))
    step('virtual rom', os.path.join(TOOLS, 'build_vrom.py'), rom, os.path.join(work, 'rommap.json'), tmc)
    audio = os.path.join(work, 'audio')
    if not os.path.isfile(os.path.join(audio, 'samples.json')):
        step('samples', os.path.join(TOOLS, 'audio_pack.py'), rom, audio)
    step('song data', os.path.join(TOOLS, 'snd_pack.py'), rom, tmc)
    step('spu samples', os.path.join(TOOLS, 'snd_samples.py'), rom, audio, tmc)
    step('room manifests', os.path.join(TOOLS, 'room_sets.py'), 'build', rom, os.path.join(tmc, 'ROOMS.BIN'))
    step('area packs*', os.path.join(TOOLS, 'area_packs.py'), rom, tmc)
    step('global set*', os.path.join(TOOLS, 'global_hot.py'), 'build', rom, tmc)
    shutil.copy(os.path.join(BIN, 'LOADICON.BIN'), os.path.join(tmc, 'LOADICON.BIN'))
    exe = os.path.join(files, 'TMCPS1.EXE')
    os.makedirs(os.path.join(tmc, 'OVL'))
    for n in sorted(os.listdir(files)):
        if n.endswith('.bin'):
            shutil.copy(os.path.join(files, n), os.path.join(tmc, 'OVL', n[:-4].upper() + '.BIN'))

    prefix = os.path.join(out, 'TMC-PS1')
    lic = os.path.abspath(a.license) if a.license else 'none'
    step('disc', os.path.join(TOOLS, 'disc', 'build_disc.py'), iso, exe, lic, prefix)
    step('verify', os.path.join(TOOLS, 'disc', 'verify_disc.py'), prefix + '.bin', iso, exe, lic)
    with open(os.path.join(out, 'SHA256SUMS'), 'w') as f:
        for ext in ('.bin', '.cue'):
            f.write('%s  TMC-PS1%s\n' % (digest(prefix + ext, 'sha256'), ext))
    print(open(os.path.join(out, 'SHA256SUMS')).read(), end='')
    print('done in %.0f s: %s.cue (%s)' % (time.time() - t0, prefix, 'licensed' if a.license else 'no license sectors: '
                                           'emulators, or a console that boots unlicensed discs'))


if __name__ == '__main__':
    main()
