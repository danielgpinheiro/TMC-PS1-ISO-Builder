#!/usr/bin/env python3
"""Which songs the PS1 keeps in RAM and which per area (docs/36 5.4, choice 65); used by tools/ps1/snd_pack.py (song
data) and tools/ps1/snd_samples.py (SPU samples).

  - global songs (always in RAM): the sound effects (ids >= 100) the resident code names (src/ outside the area code
    overlays; not the song table src/sound.c);
  - an area's songs (loaded at the area change): the songs its code overlay names (build/ps1/ovl/A<area>/), its
    region's script folder names (data/scripts/<region>, SCRIPT_AREAS), and those tmc_pc played there (the room tours'
    areas.json; the route's sound log, each song start in the area of its frame from the route's trace).
Anything else loads when it starts (song data) or is a miss (samples): counted on the PS1.
The BGM player's songs are sequenced like the rest (choice 122); with PS1_MUSIC_STREAM=1 they are streamed instead
(tools/ps1/music_pack.py, docs/36 5.5): no song data or samples for them then.
"""
import collections, glob, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
# data/scripts/<folder> -> the areas whose names contain these words (include/area.h)
SCRIPT_AREAS = {'dhc': ('DARK_HYRULE_CASTLE', 'VAATI'), 'sanctuary': ('SANCTUARY',),
                'graveyard': ('ROYAL_VALLEY_GRAVES', 'ROYAL_CRYPT'), 'lakeHylia': ('LAKE_HYLIA',),
                'castorWilds': ('CASTOR',), 'cloudTops': ('CLOUD_TOPS',), 'fow': ('FORTRESS_OF_WINDS',),
                'hyruleCastle': ('HYRULE_CASTLE',), 'hyruleCastleGarden': ('CASTLE_GARDEN',),
                'hyruleTown': ('HYRULE_TOWN',), 'lonLonRanch': ('LON_LON',), 'minishWoods': ('MINISH_WOODS',),
                'mtCrenel': ('CRENEL',), 'northHyruleField': ('HYRULE_FIELD',), 'southHyruleField': ('HYRULE_FIELD',),
                'veilFalls': ('VEIL_FALLS',), 'westernWood': ('HYRULE_FIELD',), 'windRuins': ('RUINS',)}


def enum_values(path, prefix_re):
    txt = open(path).read()
    out, i = {}, 0
    for m in re.finditer(r'^\s*(%s)\s*(?:=\s*(0x[0-9A-Fa-f]+|\d+))?\s*,' % prefix_re, txt, re.M):
        if m.group(2):
            i = int(m.group(2), 0)
        out[m.group(1)] = i
        i += 1
    return out


def stream_songs():
    """the song ids src/sound.c's gSongTable puts on the BGM player (31) when they are streamed (PS1_MUSIC_STREAM=1,
    docs/36 choice 67); none by default: the music is sequenced from the areas' sound banks (choice 122)"""
    if os.environ.get('PS1_MUSIC_STREAM', '0') != '1':
        return set()
    return bgm_songs()


def bgm_songs():
    """the song ids src/sound.c's gSongTable puts on the BGM player (31)"""
    import repo_meta  # (the public builder: TMC_META, docs/36 11)
    if repo_meta.meta_dir():
        return set(repo_meta.load('snd_sets.json')['stream'])
    txt = open(os.path.join(REPO, 'src', 'sound.c')).read()
    table = txt[txt.index('const Song gSongTable[]'):]
    sound = enum_values(os.path.join(REPO, 'include', 'sound.h'), r'(?:SFX|BGM|SONG)_[A-Z0-9_]+')
    names = [m.group(1) for m in re.finditer(r'\[(\w+)\]\s*=\s*\{[^}]*MUSIC_PLAYER_BGM\s*,\s*MUSIC_PLAYER_BGM', table)]
    return {sound[n] for n in names if n in sound and sound[n] != 0}


def song_sets():
    """-> (global song ids, {area: song ids})"""
    import repo_meta
    if repo_meta.meta_dir():
        m = repo_meta.load('snd_sets.json')
        return set(m['global']), {int(a): set(v) for a, v in m['areas'].items()}
    sound = enum_values(os.path.join(REPO, 'include', 'sound.h'), r'(?:SFX|BGM|SONG)_[A-Z0-9_]+')
    areas = enum_values(os.path.join(REPO, 'include', 'area.h'), r'AREA_[A-Z0-9_]+')

    def ids_in(path):
        return {sound[t] for t in re.findall(r'\b((?:SFX|BGM)_[A-Z0-9_]+)\b', open(path, errors='ignore').read())
                if t in sound}

    mk = os.path.join(REPO, 'build', 'ps1', 'ovl', 'objs.mk')  # tools/ps1/build_ovl.sh (none: no area code sets)
    objs = open(mk).read() if os.path.exists(mk) else 'OVL_OBJS :=\n'
    ovl = {o[:-2] + '.c' for o in objs.split('OVL_OBJS :=')[1].split('\n')[0].split()}
    resident = set()
    for f in glob.glob(os.path.join(REPO, 'src', '**', '*.c'), recursive=True):
        rel = os.path.relpath(f, REPO)
        if rel != 'src/sound.c' and rel not in ovl:
            resident |= ids_in(f)
    glob_songs = {s for s in resident if s >= 100}
    per = collections.defaultdict(set)
    for d in glob.glob(os.path.join(REPO, 'build', 'ps1', 'ovl', 'A[0-9][0-9][0-9]')):
        a = int(d[-3:])
        for o in glob.glob(os.path.join(d, 'src__*.o')):
            src = os.path.join(REPO, os.path.basename(o)[:-2].replace('__', '/') + '.c')
            if os.path.exists(src):
                per[a] |= ids_in(src)
    for folder, words in SCRIPT_AREAS.items():
        ids = set()
        for f in glob.glob(os.path.join(REPO, 'data', 'scripts', folder, '**', '*.inc'), recursive=True):
            ids |= ids_in(f)
        for name, a in areas.items():
            if any(w in name for w in words):
                per[a] |= ids
    for t in ('tmctour2', 'tmctour3'):
        p = os.path.join(REPO, 'build', 'ps1', 'tour', t, 'areas.json')
        if os.path.exists(p):
            for a, v in json.load(open(p)).items():
                per[int(a)] |= set(v.get('songs', []))
    # the area's own music (gAreaMetadata[area].queueBgm: what an area change queues, src/gameUtils.c), now that the BGM
    # is sequenced from the area's sound bank (choice 122)
    meta = open(os.path.join(REPO, 'src', 'data', 'areaMetadata.c')).read()
    body = meta[meta.index('gAreaMetadata[] = {'):]
    for a, entry in enumerate(re.findall(r'\{([^{}]*)\}', body[:body.index('};')])):
        name = entry.split(',')[-1].strip()
        if name in sound and sound[name]:
            per[a].add(sound[name])
    # what the PS1's own runs heard per area (tools/ps1/snd_areas.py: the route, the tours, the whole game; choice 122)
    sa = os.path.join(REPO, 'tools', 'ps1', 'play', 'snd_areas.txt')
    if os.path.exists(sa):
        for line in open(sa):
            p = line.split()
            if len(p) == 2:
                per[int(p[0])].add(int(p[1]))
    play = os.path.join(REPO, 'tools', 'ps1', 'play')
    area_at = {}
    for line in open(os.path.join(play, 'newgame.ref.txt')):
        f = line.split()
        area_at[int(f[1])] = int(f[3].split('=')[1])
    frames = sorted(area_at)
    for line in open(os.path.join(play, 'newgame.snd.txt')):
        f = line.split()
        if f[0] == 'S':
            fr = int(f[1])
            k = max([x for x in frames if x <= fr] or frames[:1])
            per[area_at[k]].add(int(f[3]))
    streamed = stream_songs()
    return glob_songs - streamed, {a: v - glob_songs - streamed for a, v in per.items()}
