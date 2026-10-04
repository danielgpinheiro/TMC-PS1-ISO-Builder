/*
 * TMC PS1 port: the m4a sound backend (docs/36 phase 5.2): the PC port's agbplay sequencer (libs/agbplay_core:
 * SequenceReader, MP2KTrack / MP2KPlayer, the channels' note and envelope state of MP2KChnPCM / MP2KChnPSG) in C.
 *
 * The game's m4a front end (port/port_m4a_stubs.c) calls the Port_M4A_Backend_* functions as on the PC. The song data
 * is TMC/SND.BIN (tools/ps1/snd_pack.py: the ROM bytes the sequencer reads, by GBA address). PS1SndFrame() runs once a
 * game frame after VBlankIntr and plays one GBA frame of sound per vsync since its last run (choice 117; tmc_pc's
 * TMC_SNDLOG ticks once a game frame: g_ps1SndClockExp = 1 for that A/B): agbplay's INTERFRAMES (4) sound frames a
 * GBA frame, the GBA's exact tempo (a tick per 150 of tempo a frame). The channel state follows agbplay's integer logic
 * (the CGB channels' mono-strict priority, envelopes, pseudo echo, the sustain bug) so that notes start and stop on the
 * same frames; the SPU side (voices, volumes, pitch) is phase 5.3.
 *
 * Differences from agbplay, both where the reference itself is not the GBA: the CGB priority tie between two tracks
 * compares their order in gMPlayTracks (the GBA's RAM order) instead of heap addresses; a one-shot PCM note ends when
 * its sample position (sample rate x pitch, per 1/240 s sound frame) reaches the end, not when the PC resampler's
 * look-ahead does.
 */
#include "global.h"
#include "room.h"
#include "gba/m4a.h"
#include "port_m4a_backend.h"
#include "tmc_ps1.h"
const RoomControls* PS1TmcRoomControls(void); /* ps1/tmc_vrom.c */
#include <stdio.h>
#include <string.h>

#include "tmc_snd_pow2.h"
#include "spu.hh"

void* psyqo_malloc(size_t size);
void psyqo_free(void* ptr);

#define SND_PLAYERS 32
#define SND_TRACK_POOL 96
#define SND_CHANNELS 48
#define SND_CALLS 3
#define INTERFRAMES 4
#define TEMPO_STEP (150 * INTERFRAMES) /* agbplay: UNIT_BPM * INTERFRAMES a tick; a sound frame adds the tempo */
#define PROG_UNDEFINED 0xFF

enum { ENV_INIT, ENV_ATK, ENV_DEC, ENV_SUS, ENV_REL, ENV_ECHO, ENV_DIE, ENV_DEAD };
enum { MODT_PITCH, MODT_VOL, MODT_PAN };
enum { CHN_PCM, CHN_SQ1, CHN_SQ2, CHN_WAVE, CHN_NOISE };
enum { PAN_LEFT, PAN_CENTER, PAN_RIGHT };

typedef struct SndTrack SndTrack;
typedef struct SndChn SndChn;

struct SndChn {
    SndChn *prev, *next; /* the track's channel list (not cleared on removal, as agbplay) */
    SndTrack *track;     /* NULL once removed from the track */
    SndTrack *trackOrg;
    u8 used, kind, envState, stop, fastRelease;
    u8 att, dec, sus, rel;
    /* note */
    u8 length, key, keyPitch, velocity, priority, echoVol, echoLen, trackIdx, playerIdx;
    s8 rhythmPan;
    /* envelope */
    u8 envInterStep, envLevelCur, envLevelPrev, envPeak, envSustain, envFrameCount, susBug, panCur;
    u8 leftVol, rightVol; /* PCM */
    u16 vol;              /* PSG */
    s16 pan;
    /* pitch / sample */
    u8 fixed, loop, hasFreq, spuTried;
    u16 smp; /* its SNDSMP entry while on a voice */
    s8 voice; /* SPU voice, -1 = none */
    u32 instr, sample, endPos, loopPos, midC;
    u32 pos16, step16; /* PCM sample position (16.16), per sound frame */
    s16 pitch;
};

struct SndTrack {
    const u8* pos;
    const u8* ret[SND_CALLS];
    SndChn* channels;
    u16 delay;
    u8 patternLevel, modt, lastCmd, lastKey, lastVel, lastLen, reptCount, prog, vol, mod, bendr, priority, lfos, lfodl,
        lfodlCount, lfoPhase, echoVol, echoLen, enabled, updateVolume, updatePitch, index, order;
    s8 lfoValue, pan, bend, tune, keyShift;
    s16 pitch;
};

typedef struct {
    SndTrack* tracks;
    u32 songPos, bankPos, tick;
    u16 bpm;
    u8 trackLimit, tracksUsed, priority, playing, finished, usePriority;
} SndPlayer;

static SndPlayer sPlayers[SND_PLAYERS];
static SndTrack sTracks[SND_TRACK_POOL];
static SndChn sChn[SND_CHANNELS];
static SndChn* sCgb[5]; /* mono strict: at most one channel per CGB type */
static u8 sMemacc[256];
static u16 sCurrentSong[SND_PLAYERS];
static u8 sInit, sVsync = 1, sFreq = 5;
static const u16 kFixedRate[16] = {0, 5734, 7884, 10512, 13379, 15768, 18157, 21024, 26758, 31536, 36314, 40137, 42048};
static const u8 kLen[49] = {0,  1,  2,  3,  4,  5,  6,  7,  8,  9,  10, 11, 12, 13, 14, 15, 16,
                            17, 18, 19, 20, 21, 22, 23, 24, 28, 30, 32, 36, 40, 42, 44, 48, 52,
                            54, 56, 60, 64, 66, 68, 72, 76, 78, 80, 84, 88, 90, 92, 96};

static inline u32 Rd32(const u8* p) {
    return p[0] | p[1] << 8 | p[2] << 16 | (u32)p[3] << 24;
}

/* ---- event log (TRACE): tmc_pc's TMC_SNDLOG lines, read by tools/ps1/ab_test_ds.sh ---- */
#ifdef PS1_TRACE
typedef struct {
    u32 frame, pos;
    u16 song;
    u8 kind, player, track, type, key, pitch, vel, len;
} SndEvent;
#ifdef PS1_LINK_2M
#define SND_LOG 256 /* a 15-frame sample window holds ~30 events on the route, area loads over 128 (tour 2) */
#else
#define SND_LOG 256
#endif
SndEvent g_ps1SndLog[SND_LOG];
volatile u32 g_ps1SndLogCount;
extern volatile uint32_t g_ps1TmcFrame; /* ps1/tmc_frame.cpp */
static void SndLogEvent(u8 kind, u8 player, u16 song, u8 track, u8 type, u8 key, u8 pitch, u8 vel, u8 len, u32 pos) {
    SndEvent* e = &g_ps1SndLog[g_ps1SndLogCount % SND_LOG];
    e->frame = g_ps1TmcFrame;
    e->pos = pos;
    e->song = song;
    e->kind = kind;
    e->player = player;
    e->track = track;
    e->type = type;
    e->key = key;
    e->pitch = pitch;
    e->vel = vel;
    e->len = len;
    g_ps1SndLogCount = g_ps1SndLogCount + 1;
}
#else
#define SndLogEvent(...) ((void)0)
#endif

/* ---- the song data (docs/36 5.4, tools/ps1/snd_pack.py): TMC/SND.BIN resident (the song index, the instruments),
 * every song's blob from TMC/SNDSONG.BIN into a RAM cache (at the area change: the area's songs; else when one
 * starts). SNDSONG.BIN lives in SPU RAM above the samples (6.5: copied there at boot; a song start is a short DMA,
 * not a CD read); read from the CD if it couldn't be placed ---- */
typedef struct {
    u32 gba, len, off;
} SndRange;
typedef struct {
    u32 head, blobOff;
    u16 blobBytes, pad;
} SongIndex;
typedef struct {
    u32 gba;
    u16 len, off;
} BlobRange;
#define SONG_CACHE 12288 /* the playing songs' blobs (the largest 6.2 KB, a BGM: sequenced since choice 122) */
#define SONG_BLOBS 32
typedef struct {
    u8* mem; /* in the cache: u16 ranges, u16 0, BlobRange[ranges], data */
    u16 song, bytes;
    u32 lastUse;
} Blob;
static const SongIndex* sSongs;
static const SndRange* sRanges;
static const u8* sData;
static u32 sSongCount, sRangeCount;
static u8* sSongCache;
static u16* sSongSpuAt; /* SPU address / 8 of each song's blob in the area's bank, 0 = not there (choice 122) */
volatile u32 g_ps1SndSongBankLoads = 0, g_ps1SndSongCdLoads = 0, g_ps1SndVoiceSteals = 0;
int PS1FsFindC(const char* path, u32* lba, u32* size); /* ps1/cdrom_fs.cpp */
#define SPU_DMA_UNIT 64u /* ps1/spu.cpp moves whole 16-word blocks */
static Blob sBlobs[SONG_BLOBS];
static u32 sBlobCount;
volatile u32 g_ps1SndMisses, g_ps1SndLastMiss, g_ps1SndChnFull, g_ps1SndNotes, g_ps1SndLoaded;
volatile u32 g_ps1SndSongLoads, g_ps1SndSongLoadsInPlay, g_ps1SndSongLoadFails, g_ps1SndSongLoadBytes;

/* ---- SPU samples (tools/ps1/snd_samples.py): TMC/SNDSMP.BIN's directory resident, SPU RAM a cache of the global
 * set + the current area's (TMC/SNDAREA.BIN), swapped at the area change ---- */
typedef struct {
    u32 gba, off, bytes, rate1024;
    u16 flags, pad;
} SmpEntry;
#define SPU_BASE 0x1000u
/* docs/36 choice 122 (Legend of Mana's model): the music is sequenced like the effects, from the area's sound bank;
 * only a disc with TMC/MUSIC.BIN (PS1_MUSIC_STREAM=1 tools/ps1/data_disc.sh, choice 67) streams the BGM player */
#define SPU_TOP_STREAM 0x71FE0u /* with the stream: its rings above (ps1/tmc_music.cpp), then the silent block */
#define SPU_TOP_SEQ 0x7FFE0u    /* without: up to ps1/spu.cpp's silent block */
static u32 SPU_TOP = SPU_TOP_STREAM;
static int VOICE_PCM = 18; /* voices 0..17 PCM (0..19 without the stream: 18..19 were its voices), 20..23 the CGB
                            * channels (SQ1, SQ2, WAVE, NOISE) */
int PS1MusicPresent(void); /* ps1/tmc_music.cpp: TMC/MUSIC.BIN found */
#define VOICE_CGB 20
#define PLAYER_BGM 31 /* its songs are streamed (ps1/tmc_music.cpp, docs/36 5.5) */
void PS1MusicInit(void);
int PS1MusicHas(uint16_t id);
void PS1MusicPlay(uint16_t id);
void PS1MusicStop(void);
void PS1MusicPause(void);
void PS1MusicResume(void);
void PS1MusicVolume(uint16_t volume);
int PS1MusicActive(void);
void PS1MusicFrame(void);
#define SPU_FREE_MAX 64
static SmpEntry* sSmp;
static u32 sSmpCount, sSmpDataOff;
static u32* sSmpAddr; /* SPU address while in SPU RAM, else 0 */
static u16 *sAreaSmpStart, *sAreaSmp, *sAreaSongStart, *sAreaSong;
static struct {
    u32 start, end;
} sSpuFree[SPU_FREE_MAX];
static u32 sSpuFreeN;
static u8 sSndArea = 0xFF, sAreaReady;
static SndChn* sVoiceChn[24];
static u32 sKeyOff;
static u8 sTrackGain[SND_PLAYERS][16];
static s8 sTrackPan[SND_PLAYERS][16];
volatile u32 g_ps1SndBankBad, g_ps1SndBankChecked;
#ifdef PS1_TRACE
#define PS1Hblanks() ((u32)*(volatile u16*)0xBF801110) /* root counter 1: hblanks (psx-spx "Timers") */
u16 g_ps1SpuSnap[24][8];
volatile u32 g_ps1SndSeqMax, g_ps1SndSpuMax, g_ps1SndMusicMax, g_ps1SndSeqSum, g_ps1SndSpuSum, g_ps1SndMusicSum,
    g_ps1SndFrames;
volatile u32 g_ps1SpuEndx, g_ps1SpuSoundingMax, g_ps1SpuSoundingSum, g_ps1SpuFrames;
static u8 sMissSeen[(512 + 7) / 8]; /* a missing sample logged once per area */
#endif
volatile u32 g_ps1SndBankBytes, g_ps1SndKeyOns, g_ps1SndNoSample, g_ps1SndLastNoSample, g_ps1SndNoVoice;
volatile u32 g_ps1SndAreaChanges, g_ps1SndSmpLoads, g_ps1SndSmpLoadBytes, g_ps1SndSmpEvicts, g_ps1SndSpuFull,
    g_ps1SndSpuUsedMax, g_ps1SndAreaMs;

static void* LoadFile(const char* name, u32* size) {
    FILE* f = fopen(name, "rb");
    if (!f)
        return NULL;
    fseek(f, 0, SEEK_END);
    u32 n = (u32)ftell(f);
    fseek(f, 0, SEEK_SET);
    u8* p = (u8*)psyqo_malloc(n);
    if (p && fread(p, 1, n, f) != n) {
        psyqo_free(p);
        p = NULL;
    }
    fclose(f);
    if (size)
        *size = n;
    return p;
}

/* ---- the SPU RAM allocator: free ranges (sorted, merged), best fit ---- */
static void SpuFreeRange(u32 start, u32 bytes) {
    u32 end = start + bytes, i = 0;
    while (i < sSpuFreeN && sSpuFree[i].start < start)
        ++i;
    if (i > 0 && sSpuFree[i - 1].end == start) { /* joins the one before */
        sSpuFree[i - 1].end = end;
        if (i < sSpuFreeN && sSpuFree[i].start == end) {
            sSpuFree[i - 1].end = sSpuFree[i].end;
            memmove(&sSpuFree[i], &sSpuFree[i + 1], (sSpuFreeN - i - 1) * sizeof(sSpuFree[0]));
            --sSpuFreeN;
        }
        return;
    }
    if (i < sSpuFreeN && sSpuFree[i].start == end) {
        sSpuFree[i].start = start;
        return;
    }
    if (sSpuFreeN == SPU_FREE_MAX)
        return; /* lost until the next reset (never seen: the sets are a few dozen samples) */
    memmove(&sSpuFree[i + 1], &sSpuFree[i], (sSpuFreeN - i) * sizeof(sSpuFree[0]));
    sSpuFree[i].start = start;
    sSpuFree[i].end = end;
    ++sSpuFreeN;
}

static u32 SpuAlloc(u32 bytes) {
    u32 best = SPU_FREE_MAX, bestSize = 0xFFFFFFFFu;
    for (u32 i = 0; i < sSpuFreeN; ++i) {
        u32 n = sSpuFree[i].end - sSpuFree[i].start;
        if (n >= bytes && n < bestSize) {
            best = i;
            bestSize = n;
        }
    }
    if (best == SPU_FREE_MAX)
        return 0;
    u32 at = sSpuFree[best].start;
    sSpuFree[best].start += bytes;
    if (sSpuFree[best].start == sSpuFree[best].end) {
        memmove(&sSpuFree[best], &sSpuFree[best + 1], (sSpuFreeN - best - 1) * sizeof(sSpuFree[0]));
        --sSpuFreeN;
    }
    return at;
}

static u32 SpuUsed(void) {
    u32 freeBytes = 0;
    for (u32 i = 0; i < sSpuFreeN; ++i)
        freeBytes += sSpuFree[i].end - sSpuFree[i].start;
    return SPU_TOP - SPU_BASE - freeBytes;
}

static const SmpEntry* SmpFind(u32 gba) {
    u32 lo = 0, hi = sSmpCount;
    while (lo < hi) {
        u32 mid = (lo + hi) >> 1;
        if (sSmp[mid].gba < gba)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo < sSmpCount && sSmp[lo].gba == gba ? &sSmp[lo] : NULL;
}

/* SPU RAM <- the samples of `want` (one bit per entry) that aren't there; those not wanted and not sounding out */
static const u32 *sPackOff, *sPackLen; /* SNDAREA.BIN "TSA3": list k's pack in SNDSMP.BIN (0 = none) */
volatile u32 g_ps1SndPackReads = 0, g_ps1SndPackBytes = 0;

/* SPU RAM <- bytes of the sample loaded at `at`; TRACE reads them back */
volatile u32 g_ps1SndVerify = 1; /* TRACE, GDB: 0 = no read-back of SPU uploads */
static void SpuPut(u32 to, const u8* src, u32 n) {
    PS1SpuWrite(to, src, n);
#ifdef PS1_TRACE
    u8* chk = g_ps1SndVerify ? (u8*)psyqo_malloc(n) : NULL;
    if (chk) {
        PS1SpuRead(to, chk, n);
        if (memcmp(chk, src, n) != 0)
            g_ps1SndBankBad = g_ps1SndBankBad + 1;
        g_ps1SndBankChecked = g_ps1SndBankChecked + n;
        psyqo_free(chk);
    }
#endif
}

/* list k's samples that `want` asks for and SPU RAM lacks, from its pack in one sequential read (docs/36 6.6: a sample
 * at a time was a seek each, seconds of an area change). The pack is the list's samples back to back in list order,
 * from a sector boundary; sizes are multiples of 64, so every piece written is whole SPU blocks. */
static void SpuLoadPack(u32 list, const u8* want) {
    if (!sPackOff || !sPackOff[list])
        return;
    u32 first = sAreaSmpStart[list], end = sAreaSmpStart[list + 1], at[64], lastEnd = 0, pos = 0, k;
    if (end - first > 64)
        return; /* (lists are ~30 samples at most) */
    for (k = first; k < end; ++k) { /* the SPU addresses first, and where the reading can stop */
        u32 i = sAreaSmp[k];
        at[k - first] = 0;
        if (!sSmpAddr[i] && (want[i >> 3] >> (i & 7) & 1)) {
            if (!(at[k - first] = SpuAlloc(sSmp[i].bytes)))
                g_ps1SndSpuFull = g_ps1SndSpuFull + 1;
            else
                lastEnd = pos + sSmp[i].bytes;
        }
        pos += sSmp[i].bytes;
    }
    if (!lastEnd)
        return;
    u32 bufSize = 32768;
    u8* buf = (u8*)psyqo_malloc(bufSize);
    if (!buf)
        buf = (u8*)psyqo_malloc(bufSize = 8192);
    FILE* f = fopen("TMC/SNDSMP.BIN", "rb");
    if (!buf || !f || fseek(f, (long)sPackOff[list], SEEK_SET) != 0) {
        for (k = first; k < end; ++k) /* not read: give the SPU RAM back (the per-sample path retries) */
            if (at[k - first])
                SpuFreeRange(at[k - first], sSmp[sAreaSmp[k]].bytes);
        if (buf)
            psyqo_free(buf);
        if (f)
            fclose(f);
        return;
    }
    for (u32 c0 = 0; c0 < lastEnd; c0 += bufSize) { /* the pack in bufSize pieces; each sample gets its overlap */
        u32 n = lastEnd - c0 < bufSize ? lastEnd - c0 : bufSize;
        if (fread(buf, 1, n, f) != n)
            break;
        g_ps1SndPackReads = g_ps1SndPackReads + 1;
        g_ps1SndPackBytes = g_ps1SndPackBytes + n;
        for (k = first, pos = 0; k < end; pos += sSmp[sAreaSmp[k]].bytes, ++k) {
            u32 i = sAreaSmp[k], s0 = pos, s1 = pos + sSmp[i].bytes;
            if (!at[k - first] || s1 <= c0 || s0 >= c0 + n)
                continue;
            u32 o0 = s0 > c0 ? s0 : c0, o1 = s1 < c0 + n ? s1 : c0 + n;
            SpuPut(at[k - first] + (o0 - s0), buf + (o0 - c0), o1 - o0);
            if (o1 == s1) { /* the sample is complete */
                sSmpAddr[i] = at[k - first];
                g_ps1SndSmpLoads = g_ps1SndSmpLoads + 1;
                g_ps1SndSmpLoadBytes = g_ps1SndSmpLoadBytes + sSmp[i].bytes;
            }
        }
    }
    for (k = first; k < end; ++k) /* a short read: what didn't complete goes back (the per-sample path retries) */
        if (at[k - first] && !sSmpAddr[sAreaSmp[k]])
            SpuFreeRange(at[k - first], sSmp[sAreaSmp[k]].bytes);
    psyqo_free(buf);
    fclose(f);
}

static void SpuLoadSet(const u8* want, u32 area) {
    u8 keep[(512 + 7) / 8];
    memcpy(keep, want, sizeof(keep));
    for (int v = 0; v < 24; ++v) /* samples still sounding stay (the BGM playing across the change) */
        if (sVoiceChn[v] && sVoiceChn[v]->smp < sSmpCount)
            keep[sVoiceChn[v]->smp >> 3] |= (u8)(1u << (sVoiceChn[v]->smp & 7));
    for (u32 i = 0; i < sSmpCount; ++i)
        if (sSmpAddr[i] && !(keep[i >> 3] >> (i & 7) & 1)) {
            SpuFreeRange(sSmpAddr[i], sSmp[i].bytes);
            sSmpAddr[i] = 0;
            g_ps1SndSmpEvicts = g_ps1SndSmpEvicts + 1;
        }
    SpuLoadPack(256, want); /* the global set, then the area's: sequential reads */
    SpuLoadPack(area, want);
    FILE* f = NULL;
    u8* buf = NULL;
    for (u32 i = 0; i < sSmpCount; ++i) { /* in file order (the entries are) */
        if (sSmpAddr[i] || !(want[i >> 3] >> (i & 7) & 1))
            continue;
        u32 at = SpuAlloc(sSmp[i].bytes);
        if (!at) {
            g_ps1SndSpuFull = g_ps1SndSpuFull + 1;
            continue;
        }
        if (!f && !(f = fopen("TMC/SNDSMP.BIN", "rb")))
            break;
        if (!buf && !(buf = (u8*)psyqo_malloc(8192)))
            break;
        fseek(f, (long)(sSmpDataOff + sSmp[i].off), SEEK_SET);
        u32 left = sSmp[i].bytes, to = at;
        while (left) { /* SPU DMA in 8 KB pieces (whole 64-byte blocks: every sample is padded to 64) */
            u32 n = left < 8192 ? left : 8192;
            if (fread(buf, 1, n, f) != n)
                break;
            PS1SpuWrite(to, buf, n);
#ifdef PS1_TRACE
            { /* read it back: SPU RAM holds the sample (g_ps1SndBankBad = pieces that differ) */
                u8* chk = (u8*)psyqo_malloc(n);
                if (chk) {
                    PS1SpuRead(to, chk, n);
                    if (memcmp(chk, buf, n) != 0)
                        g_ps1SndBankBad = g_ps1SndBankBad + 1;
                    g_ps1SndBankChecked = g_ps1SndBankChecked + n;
                    psyqo_free(chk);
                }
            }
#endif
            to += n;
            left -= n;
        }
        sSmpAddr[i] = at;
        g_ps1SndSmpLoads = g_ps1SndSmpLoads + 1;
        g_ps1SndSmpLoadBytes = g_ps1SndSmpLoadBytes + sSmp[i].bytes;
    }
    if (buf)
        psyqo_free(buf);
    if (f)
        fclose(f);
    u32 used = SpuUsed();
    if (used > g_ps1SndSpuUsedMax)
        g_ps1SndSpuUsedMax = used;
    g_ps1SndBankBytes = used;
}

/* ---- the song cache ---- */
static const u8* BlobAt(const Blob* b, u32 gba) {
    u32 n = b->mem[0] | b->mem[1] << 8;
    const BlobRange* r = (const BlobRange*)(b->mem + 4);
    const u8* data = b->mem + 4 + n * sizeof(BlobRange);
    for (u32 k = 0; k < n; ++k)
        if (gba - r[k].gba < r[k].len)
            return data + r[k].off + (gba - r[k].gba);
    return NULL;
}

static const u8* SndAt(u32 gba) {
    u32 lo = 0, hi = sRangeCount;
    while (lo < hi) { /* the last range starting at or below gba */
        u32 mid = (lo + hi) >> 1;
        if (sRanges[mid].gba <= gba)
            lo = mid + 1;
        else
            hi = mid;
    }
    if (lo) {
        const SndRange* r = &sRanges[lo - 1];
        if (gba - r->gba < r->len)
            return sData + r->off + (gba - r->gba);
    }
    for (u32 i = 0; i < sBlobCount; ++i) {
        const u8* p = BlobAt(&sBlobs[i], gba);
        if (p)
            return p;
    }
    g_ps1SndMisses = g_ps1SndMisses + 1;
    g_ps1SndLastMiss = gba;
    return NULL;
}

static int BlobPinned(const Blob* b);
static u8 sInAreaChange;

static int SongCached(u16 song) {
    if (song >= sSongCount || !sSongs[song].blobBytes)
        return 1; /* resident (or no song) */
    for (u32 i = 0; i < sBlobCount; ++i)
        if (sBlobs[i].song == song) {
            sBlobs[i].lastUse = g_ps1TmcFrame;
            return 1;
        }
    return 0;
}

/* the song's blob into the cache (blocking CD read): the first gap that fits, else the least recently used blob no
 * player is on goes */
static u8 sSndBusy; /* inside the sequencer, an area change or a song load: the timer leaves it alone (choice 127) */
static int SongLoadInner(u16 song);
static int SongLoad(u16 song) {
    if (SongCached(song))
        return 1;
    u8 wasBusy = sSndBusy; /* (a CD fallback read pumps psyqo's timers: not inside a load) */
    sSndBusy = 1;
    int r = SongLoadInner(song);
    sSndBusy = wasBusy;
    return r;
}
static int SongLoadInner(u16 song) {
    u32 blob = sSongs[song].blobBytes, bytes = (blob + SPU_DMA_UNIT - 1) & ~(SPU_DMA_UNIT - 1);
    for (;;) {
        u32 at = 0, i = 0; /* blobs sorted by address: first fit */
        for (; i < sBlobCount; ++i) {
            u32 start = (u32)(sBlobs[i].mem - sSongCache);
            if (start - at >= bytes)
                break;
            at = start + sBlobs[i].bytes;
        }
        if (SONG_CACHE - at >= bytes || i < sBlobCount) {
            if (sBlobCount == SONG_BLOBS)
                goto evict;
            if (sSongSpuAt && sSongSpuAt[song]) {
                PS1SpuRead((u32)sSongSpuAt[song] * 8, sSongCache + at, bytes);
            } else {
                g_ps1SndSongCdLoads = g_ps1SndSongCdLoads + 1; /* outside the area's bank: the CD (counted) */
                FILE* f = fopen("TMC/SNDSONG.BIN", "rb");
                if (!f || fseek(f, (long)sSongs[song].blobOff, SEEK_SET) != 0 ||
                    fread(sSongCache + at, 1, blob, f) != blob) {
                    if (f)
                        fclose(f);
                    g_ps1SndSongLoadFails = g_ps1SndSongLoadFails + 1;
                    return 0;
                }
                fclose(f);
            }
            memmove(&sBlobs[i + 1], &sBlobs[i], (sBlobCount - i) * sizeof(Blob));
            sBlobs[i].mem = sSongCache + at;
            sBlobs[i].song = song;
            sBlobs[i].bytes = (u16)bytes;
            sBlobs[i].lastUse = g_ps1TmcFrame;
            ++sBlobCount;
            g_ps1SndSongLoads = g_ps1SndSongLoads + 1;
            if (!sInAreaChange) /* a blocking read mid-play: the area's list missed it */
                g_ps1SndSongLoadsInPlay = g_ps1SndSongLoadsInPlay + 1;
            g_ps1SndSongLoadBytes = g_ps1SndSongLoadBytes + bytes;
            SndLogEvent('L', 0, song, 0, 0, 0, 0, 0, 0, bytes);
            return 1;
        }
    evict:;
        int victim = -1;
        for (u32 k = 0; k < sBlobCount; ++k)
            if (!BlobPinned(&sBlobs[k]) && (victim < 0 || sBlobs[k].lastUse < sBlobs[victim].lastUse))
                victim = (int)k;
        if (victim < 0) {
            g_ps1SndSongLoadFails = g_ps1SndSongLoadFails + 1;
            return 0;
        }
        memmove(&sBlobs[victim], &sBlobs[victim + 1], (sBlobCount - victim - 1) * sizeof(Blob));
        --sBlobCount;
    }
}

static void SndLoad(void) {
    static u8 tried;
    if (tried)
        return;
    tried = 1;
#ifdef PS1_NO_SOUND
    return; /* a silent build (RAM comparisons, docs/36 choice 66) */
#endif
    u32 n;
    const u8* p = (const u8*)LoadFile("TMC/SND.BIN", &n);
    if (p && Rd32(p) == 0x324E5354 /* "TSN2" */) {
        sSongCount = Rd32(p + 4);
        sRangeCount = Rd32(p + 8);
        sSongs = (const SongIndex*)(p + 16);
        sRanges = (const SndRange*)(p + 16 + sSongCount * sizeof(SongIndex));
        sData = (const u8*)(sRanges + sRangeCount);
        g_ps1SndLoaded = n;
    }
    sSongCache = (u8*)psyqo_malloc(SONG_CACHE);
    u8* d = (u8*)LoadFile("TMC/SNDAREA.BIN", NULL);
    if (d && Rd32(d) == 0x33415354 /* "TSA3" */) {
        u32 lists = Rd32(d + 4), smps = Rd32(d + 8), songs = Rd32(d + 12);
        sAreaSmpStart = (u16*)(d + 16);
        sAreaSmp = sAreaSmpStart + lists + 1;
        sAreaSongStart = sAreaSmp + smps;
        sAreaSong = sAreaSongStart + lists + 1;
        uintptr_t packs = ((uintptr_t)(sAreaSong + songs) + 3) & ~(uintptr_t)3;
        sPackOff = (const u32*)packs;
        sPackLen = sPackOff + lists;
    }
    FILE* f = fopen("TMC/SNDSMP.BIN", "rb");
    u32 head[4];
    if (f && fread(head, 4, 4, f) == 4 && head[0] == 0x31535354 /* "TSS1" */ && head[1] <= 512 &&
        (sSmp = (SmpEntry*)psyqo_malloc(head[1] * sizeof(SmpEntry))) != NULL &&
        (sSmpAddr = (u32*)psyqo_malloc(head[1] * 4)) != NULL &&
        fread(sSmp, sizeof(SmpEntry), head[1], f) == head[1]) {
        sSmpCount = head[1];
        sSmpDataOff = head[3];
        memset(sSmpAddr, 0, head[1] * 4);
    }
    if (f)
        fclose(f);
    PS1SpuInit(); /* ps1/spu.cpp: voices parked on a silent block, master volume up */
    PS1MusicInit();
    if (!PS1MusicPresent()) {
        SPU_TOP = SPU_TOP_SEQ;
        VOICE_PCM = 20;
    }
    sSpuFreeN = 1;
    sSpuFree[0].start = SPU_BASE;
    sSpuFree[0].end = SPU_TOP;
    /* the songs' blobs go to SPU RAM per area (SongBankChange, choice 122): a table of where each one is */
    if (sSongCount && (sSongSpuAt = (u16*)psyqo_malloc(sSongCount * 2)) != NULL)
        memset(sSongSpuAt, 0, sSongCount * 2);
}

/* docs/36 choice 122 (Legend of Mana's per-scene sound bank): at an area change the global songs' and the area's
 * blobs are put in SPU RAM (the others' freed), read from TMC/SNDSONG.BIN in file order through a small stack buffer;
 * a song that starts reads its blob from there into the RAM cache (SongLoad), the CD only for one outside the bank */
static void SongBankChange(u32 area, int load) {
    if (!sSongSpuAt || !sAreaSongStart)
        return;
    u8 want[(1024 + 7) / 8];
    memset(want, 0, sizeof(want));
    for (u32 k = sAreaSongStart[256]; k < sAreaSongStart[257]; ++k)
        if (sAreaSong[k] < 1024)
            want[sAreaSong[k] >> 3] |= (u8)(1u << (sAreaSong[k] & 7));
    for (u32 k = sAreaSongStart[area]; k < sAreaSongStart[area + 1]; ++k)
        if (sAreaSong[k] < 1024)
            want[sAreaSong[k] >> 3] |= (u8)(1u << (sAreaSong[k] & 7));
    for (u32 i = 0; i < sSongCount && i < 1024; ++i)
        if (sSongSpuAt[i] && !(want[i >> 3] >> (i & 7) & 1)) {
            SpuFreeRange((u32)sSongSpuAt[i] * 8, (sSongs[i].blobBytes + SPU_DMA_UNIT - 1) & ~(SPU_DMA_UNIT - 1));
            sSongSpuAt[i] = 0;
        }
    if (!load)
        return; /* (the frees only: before the samples' swap, so they get the room) */
    FILE* f = NULL;
    u8 piece[1024] __attribute__((aligned(4)));
    for (u32 i = 0; i < sSongCount && i < 1024; ++i) { /* song order = file order (tools/ps1/snd_pack.py) */
        u32 blob = sSongs[i].blobBytes;
        if (!blob || sSongSpuAt[i] || !(want[i >> 3] >> (i & 7) & 1))
            continue;
        u32 bytes = (blob + SPU_DMA_UNIT - 1) & ~(SPU_DMA_UNIT - 1), at = SpuAlloc(bytes);
        if (!at) {
            g_ps1SndSpuFull = g_ps1SndSpuFull + 1;
            continue;
        }
        if (!f && !(f = fopen("TMC/SNDSONG.BIN", "rb"))) {
            SpuFreeRange(at, bytes);
            break;
        }
        fseek(f, (long)sSongs[i].blobOff, SEEK_SET);
        for (u32 done = 0; done < blob; done += sizeof(piece)) {
            u32 n = blob - done < sizeof(piece) ? blob - done : sizeof(piece);
            memset(piece, 0, sizeof(piece));
            fread(piece, 1, n, f);
            PS1SpuWrite(at + done, piece, (n + SPU_DMA_UNIT - 1) & ~(SPU_DMA_UNIT - 1));
        }
        sSongSpuAt[i] = (u16)(at / 8);
        g_ps1SndSongBankLoads = g_ps1SndSongBankLoads + 1;
    }
    if (f)
        fclose(f);
}

/* the area's songs and samples (PS1SndFrame at an area change, before the frame's sound frames; the game is in its
 * area transition) */
#define SPU_CACHE_RESERVE 16384u
void PS1VCacheSpuAttach(u32 addr, u32 bytes); /* ps1/tmc_vcache.cpp */
void PS1VCacheSpuDetach(void);
static u32 sCacheSpu, sCacheSpuBytes;
volatile u32 g_ps1VcSpuOff = 0; /* GDB: 1 = no SPU slots */
static void SndAreaChange(u8 area) {
    u32 t0 = g_ps1TmcFrame;
    (void)t0;
    sSndArea = area;
    sInAreaChange = 1;
    g_ps1SndAreaChanges = g_ps1SndAreaChanges + 1;
    if (sSmp && sAreaSmpStart) {
        u8 want[(512 + 7) / 8];
        memset(want, 0, sizeof(want));
        for (u32 k = sAreaSmpStart[256]; k < sAreaSmpStart[257]; ++k) /* global */
            want[sAreaSmp[k] >> 3] |= (u8)(1u << (sAreaSmp[k] & 7));
        for (u32 k = sAreaSmpStart[area]; k < sAreaSmpStart[area + 1]; ++k)
            want[sAreaSmp[k] >> 3] |= (u8)(1u << (sAreaSmp[k] & 7));
        if (sCacheSpuBytes) { /* the virtual ROM's SPU slots (choice 119) give their room back to the samples */
            PS1VCacheSpuDetach();
            SpuFreeRange(sCacheSpu, sCacheSpuBytes);
            sCacheSpuBytes = 0;
        }
        SongBankChange(area, 0);
        SpuLoadSet(want, area);
        SongBankChange(area, 1); /* the area's songs in SPU RAM too (choice 122) */
        u32 big = 0; /* then the largest free SPU range less a reserve becomes cache slots until the next change */
        for (u32 i = 0; i < sSpuFreeN; ++i)
            if (sSpuFree[i].end - sSpuFree[i].start > big)
                big = sSpuFree[i].end - sSpuFree[i].start;
        if (big > SPU_CACHE_RESERVE + 16384 && !g_ps1VcSpuOff) {
            u32 bytes = (big - SPU_CACHE_RESERVE) & ~4095u;
            if ((sCacheSpu = SpuAlloc(bytes)) != 0) {
                sCacheSpuBytes = bytes;
                PS1VCacheSpuAttach(sCacheSpu, bytes);
            }
        }
    }
    if (sAreaSongStart) /* the area's songs into the RAM cache (from the bank just loaded) */
        for (u32 k = sAreaSongStart[area]; k < sAreaSongStart[area + 1]; ++k)
            SongLoad(sAreaSong[k]);
#ifdef PS1_TRACE
    memset(sMissSeen, 0, sizeof(sMissSeen));
#endif
    sInAreaChange = 0;
    sAreaReady = 1;
}



/* ---- channels (MP2KChn, MP2KChnPCM, MP2KChnPSG) ---- */
static void ChnRemoveFromTrack(SndChn* c) {
    SndTrack* t = c->track;
    if (!t)
        return;
    if (c->prev)
        c->prev->next = c->next;
    else
        t->channels = c->next;
    if (c->next)
        c->next->prev = c->prev;
    c->track = NULL; /* prev / next kept: loops over the list may still be on this channel */
}

static void ChnKill(SndChn* c) {
    c->envState = ENV_DEAD;
    ChnRemoveFromTrack(c);
}

static void ChnFree(SndChn* c) {
    ChnRemoveFromTrack(c);
    if (c->voice >= 0) { /* its SPU voice keyed off at the frame's end (unless a new note takes it) */
        sKeyOff |= 1u << c->voice;
        sVoiceChn[(int)c->voice] = NULL;
    }
    if (c->kind != CHN_PCM && sCgb[c->kind] == c)
        sCgb[c->kind] = NULL;
    c->used = 0;
}

static void ChnRelease(SndChn* c, u8 fast) {
    c->stop = 1;
    if (c->kind != CHN_PCM)
        c->fastRelease = fast;
}

static SndChn* ChnNew(SndTrack* t) {
    for (int i = 0; i < SND_CHANNELS; ++i)
        if (!sChn[i].used) {
            SndChn* c = &sChn[i];
            memset(c, 0, sizeof(*c));
            c->used = 1;
            c->voice = -1;
            c->smp = 0xFFFF;
            c->track = c->trackOrg = t;
            c->next = t->channels;
            if (t->channels)
                t->channels->prev = c;
            t->channels = c;
            return c;
        }
    g_ps1SndChnFull = g_ps1SndChnFull + 1;
    return NULL;
}

static int ChnTickNote(SndChn* c) {
    if (c->kind == CHN_PCM ? !c->stop : c->envState < ENV_REL) {
        if (c->length > 0 && --c->length == 0) {
            ChnRelease(c, 0);
            return 0;
        }
        return 1;
    }
    return 0;
}

static void ChnSetVol(SndChn* c, u16 vol, s16 pan) {
    if (c->stop)
        return;
    if (c->kind == CHN_PCM) {
        int p = pan + c->rhythmPan;
        p = p < -128 ? -128 : p > 128 ? 128 : p;
        if (p >= 126)
            p = 128;
        int l = c->velocity * vol * (-p + 128) >> 15, r = c->velocity * vol * (p + 128) >> 15;
        c->leftVol = l > 255 ? 255 : l;
        c->rightVol = r > 255 ? 255 : r;
    } else {
        c->vol = vol;
        c->pan = pan < -128 ? -128 : pan > 127 ? 127 : pan;
        c->susBug = 1;
    }
}

/* 2^(x / 768) x v (x in 1/768 octave) */
static u32 Pow2Mul(u32 v, int x) {
    int oct = x >= 0 ? x / 768 : -((-x + 767) / 768);
    u32 frac = (u32)(x - oct * 768);
    unsigned long long r = (unsigned long long)v * (65536u + kSndPow2[frac]);
    return oct >= 16 ? (u32)(r << (oct - 16)) : (u32)(r >> (16 - oct));
}

static void ChnSetPitch(SndChn* c, s16 pitch) {
    c->pitch = pitch;
    if (c->kind != CHN_PCM)
        return;
    if (c->stop && c->hasFreq)
        return;
    c->hasFreq = 1;
    /* samples per sound frame (1/240 s at 48 kHz: agbplay's 200-sample buffers) in 16.16 */
    u32 hz1024 = Pow2Mul(c->midC, (c->keyPitch - 60) * 64 + pitch);
    c->step16 = c->fixed ? (u32)(kFixedRate[sFreq] * 65536u / 240) : (u32)((unsigned long long)hz1024 * 64 / 240);
}

static void PcmStepEnvelope(SndChn* c) {
    if (c->envState == ENV_INIT) {
        if (c->stop) {
            c->envState = ENV_DEAD;
            return;
        }
        c->envLevelPrev = c->att == 0xFF ? 0xFF : 0;
        c->envLevelCur = 0;
        c->envInterStep = 0;
        c->envState = ENV_ATK;
    } else {
        if (++c->envInterStep < INTERFRAMES)
            return;
        c->envLevelPrev = c->envLevelCur;
        c->envInterStep = 0;
    }
    if (c->envState == ENV_ECHO) {
        if (--c->echoLen == 0) {
            c->envState = ENV_DIE;
            c->envLevelCur = 0;
        }
    } else if (c->stop) {
        if (c->envState == ENV_DIE) {
            c->envState = ENV_DEAD;
        } else {
            c->envLevelCur = (u8)((c->envLevelCur * c->rel) >> 8);
            if (c->envLevelCur <= c->echoVol) {
            release:
                if (c->echoVol == 0 || c->echoLen == 0) {
                    c->envState = ENV_DIE;
                    c->envLevelCur = 0;
                } else {
                    c->envState = ENV_ECHO;
                    c->envLevelCur = c->echoVol;
                }
            }
        }
    } else if (c->envState == ENV_DEC) {
        c->envLevelCur = (u8)((c->envLevelCur * c->dec) >> 8);
        if (c->envLevelCur <= c->sus) {
            c->envLevelCur = c->sus;
            if (c->envLevelCur == 0)
                goto release;
            c->envState = ENV_SUS;
        }
    } else if (c->envState == ENV_ATK) {
        u32 lvl = c->envLevelCur + c->att;
        if (lvl >= 0xFF) {
            c->envLevelCur = 0xFF;
            c->envState = ENV_DEC;
        } else {
            c->envLevelCur = (u8)lvl;
        }
    }
}

static u8 PsgEchoLevel(const SndChn* c) {
    return (u8)(((c->envPeak * c->echoVol) + 0xFF) >> 8);
}

static void PsgApplyVol(SndChn* c) {
    int ml = ((127 - c->pan) * c->vol) >> 8, mr = ((c->pan + 128) * c->vol) >> 8;
    int l = (((127 - c->rhythmPan) * c->velocity) * ml) >> 14, r = (((c->rhythmPan + 128) * c->velocity) * mr) >> 14;
    c->panCur = r / 2 >= l ? PAN_RIGHT : l / 2 >= r ? PAN_LEFT : PAN_CENTER;
    if (c->kind != CHN_WAVE && c->susBug && c->envState == ENV_SUS) { /* agbplay's emulateCgbSustainBug */
        c->envLevelCur = c->envSustain;
        c->susBug = 0;
    }
    int peak = (l + r) >> 4;
    c->envPeak = (u8)(peak > 15 ? 15 : peak);
    int sus = (c->envPeak * c->sus + 15) >> 4;
    c->envSustain = (u8)(sus > 15 ? 15 : sus);
}

static void PsgStepEnvelope(SndChn* c) {
    if (c->envState == ENV_INIT) {
        if (c->stop) {
            c->envState = ENV_DEAD;
            return;
        }
        PsgApplyVol(c);
        c->envInterStep = 0;
        c->envLevelCur = 0;
        c->envFrameCount = c->att;
        c->envState = ENV_ATK;
        if (c->envFrameCount > 0)
            return;
        goto decay_start;
    } else {
        if (c->fastRelease && c->envState != ENV_DIE) {
            c->envInterStep = (c->rel == 0 || c->envState == ENV_ECHO) ? INTERFRAMES - 1 : 0;
            c->envState = ENV_DIE;
            c->envFrameCount = 1;
            return;
        }
        if (++c->envInterStep < INTERFRAMES)
            return;
        c->envInterStep = 0;
        c->envFrameCount--;
    }
    if (c->envState == ENV_ECHO) {
        c->envFrameCount = 1;
        if (--c->echoLen == 0) {
            c->envState = ENV_DIE;
            c->envInterStep = INTERFRAMES - 1;
        }
    } else if (c->stop && c->envState < ENV_REL) {
        c->envState = ENV_REL;
        c->envFrameCount = c->rel;
        if (c->envLevelCur == 0 || c->envFrameCount == 0)
            goto echo_start;
        return;
    } else if (c->envFrameCount == 0) {
        PsgApplyVol(c);
        if (c->envState == ENV_REL) {
            c->envLevelCur--;
            if (c->envLevelCur == 0) {
            echo_start:
                c->envFrameCount = 1;
                c->envLevelCur = PsgEchoLevel(c);
                if (c->envLevelCur != 0 && c->echoLen != 0) {
                    c->envState = ENV_ECHO;
                } else {
                    c->envState = ENV_DIE;
                    c->envInterStep = INTERFRAMES - 1;
                    return;
                }
            } else {
                c->envFrameCount = c->rel;
            }
        } else if (c->envState == ENV_SUS) {
        sustain_state:
            c->envFrameCount = 7;
            if (c->kind == CHN_WAVE)
                c->envLevelCur = c->envSustain;
        } else if (c->envState == ENV_DEC) {
            c->envLevelCur--;
            if (c->envLevelCur <= c->envSustain) {
            sustain_start:
                if (c->sus == 0) {
                    c->envState = ENV_REL;
                    goto echo_start;
                }
                c->envState = ENV_SUS;
                c->envLevelCur = c->envSustain;
                goto sustain_state;
            }
            c->envFrameCount = c->dec;
        } else if (c->envState == ENV_ATK) {
            c->envLevelCur++;
            if (c->envLevelCur >= c->envPeak) {
            decay_start:
                c->envState = ENV_DEC;
                c->envFrameCount = c->dec;
                if (c->envPeak == 0 || c->envFrameCount == 0 || c->envPeak == c->envSustain)
                    goto sustain_start;
                c->envLevelCur = c->envPeak;
            } else {
                c->envFrameCount = c->att;
            }
        } else if (c->envState == ENV_DIE) {
            c->envState = ENV_DEAD;
            return;
        }
    }
}

/* one sound frame of the mixer's channel state (SoundMixer::Process without the mixing) */
static void MixerProcess(void) {
    for (int i = 0; i < SND_CHANNELS; ++i) {
        SndChn* c = &sChn[i];
        if (!c->used || c->envState == ENV_DEAD)
            continue;
        if (c->kind == CHN_PCM) {
            PcmStepEnvelope(c);
            if (c->envState == ENV_DEAD)
                continue;
            u32 p = c->pos16 + c->step16;
            c->pos16 = p;
            if ((p >> 16) >= c->endPos) {
                if (c->loop && c->endPos > c->loopPos)
                    c->pos16 = p - ((c->endPos - c->loopPos) << 16);
                else
                    ChnKill(c);
            }
        } else {
            PsgStepEnvelope(c);
        }
    }
    for (int i = 0; i < SND_CHANNELS; ++i)
        if (sChn[i].used && sChn[i].envState == ENV_DEAD)
            ChnFree(&sChn[i]);
}

/* ---- the channels on SPU voices (phase 5.3): once a game frame, after the sound frames ---- */
/* 1/768-octave pitch x v, as Hz x 1024 -> the SPU's pitch (0x1000 = 44.1 kHz): Hz x 4096 / 44100 = hz1024 / 11025 */
static u16 SpuPitch(u32 hz1024) {
    u32 p = hz1024 / 11025;
    return (u16)(p > 0x3FFF ? 0x3FFF : p);
}

/* the GBA noise channel's LFSR clock (agbplay MP2KChnPSGNoise::SetPitch) as the SPU noise clock:
 * 44100 x (4 + step) / (0x20000 >> shift) (psx-spx "SPU Noise Generator") */
static void SpuNoiseClock(const SndChn* c) {
    int k = c->keyPitch * 64 + c->pitch; /* 1/64 semitone */
    u32 hz;
    if (k < 76 * 64)
        hz = Pow2Mul(4096, (k - 60 * 64) * 3); /* 4096 x 8^((key - 60) / 12) */
    else if (k < 78 * 64)
        hz = Pow2Mul(65536, (k - 76 * 64) * 6);
    else if (k < 80 * 64)
        hz = Pow2Mul(131072, (k - 78 * 64) * 12);
    else
        hz = 524288;
    int best = 0, bestStep = 0;
    u32 bestErr = 0xFFFFFFFFu;
    for (int shift = 0; shift < 16; ++shift)
        for (int step = 0; step < 4; ++step) {
            u32 f = 44100u * (4 + step) / (0x20000u >> shift);
            u32 err = f > hz ? f - hz : hz - f;
            if (err < bestErr) {
                bestErr = err;
                best = shift;
                bestStep = step;
            }
        }
    PS1SpuNoise(1u << 23, best, bestStep);
}

static void SpuUpdate(void) {
    u32 keyOn = 0;
    for (int i = 0; i < SND_CHANNELS; ++i) {
        SndChn* c = &sChn[i];
        if (!c->used || c->envState == ENV_DEAD)
            continue;
        if (c->voice < 0) {
            if (c->spuTried)
                continue;
            c->spuTried = 1;
            int v = -1;
            u32 addr = PS1SpuDummyAddr(); /* noise: the SPU's generator, on a silent looping block */
            if (c->kind != CHN_NOISE) {
                const SmpEntry* se =
                    SmpFind(c->kind == CHN_PCM || c->kind == CHN_WAVE ? c->sample : 0xFFFFFFF0u + (c->sample & 3));
                u32 idx = se ? (u32)(se - sSmp) : 0;
                if (!se || !sSmpAddr[idx]) { /* not in SPU RAM: silent (the sets missed it) */
                    g_ps1SndNoSample = g_ps1SndNoSample + 1;
                    g_ps1SndLastNoSample = c->sample;
#ifdef PS1_TRACE
                    if (se && !(sMissSeen[idx >> 3] >> (idx & 7) & 1)) {
                        sMissSeen[idx >> 3] |= (u8)(1u << (idx & 7));
                        SndLogEvent('M', c->playerIdx, sSndArea, c->trackIdx, c->kind, c->key, 0, 0, 0, c->sample);
                    }
#endif
                    continue;
                }
                c->smp = (u16)idx;
                addr = sSmpAddr[idx];
            }
            if (c->kind == CHN_PCM) {
                for (int k = 0; k < VOICE_PCM; ++k)
                    if (!sVoiceChn[k]) {
                        v = k;
                        break;
                    }
                if (v < 0) { /* all PCM voices busy (the BGM shares them since choice 122): take the quietest one already
                              * fading out (its rest is inaudible against a new note), else none */
                    int best = -1;
                    for (int k = 0; k < VOICE_PCM; ++k) {
                        SndChn* o = sVoiceChn[k];
                        if (o && o->stop && (best < 0 || o->envLevelCur < sVoiceChn[best]->envLevelCur))
                            best = k;
                    }
                    if (best < 0) {
                        g_ps1SndNoVoice = g_ps1SndNoVoice + 1;
                        continue;
                    }
                    sVoiceChn[best]->voice = -1;
                    sVoiceChn[best] = NULL;
                    sKeyOff &= ~(1u << best); /* (the new note's key-on restarts the voice) */
                    g_ps1SndVoiceSteals = g_ps1SndVoiceSteals + 1;
                    v = best;
                }
            } else {
                v = VOICE_CGB + c->kind - CHN_SQ1;
                if (sVoiceChn[v]) /* mono strict frees the old channel first; defensive */
                    sVoiceChn[v]->voice = -1;
            }
            PS1SpuVoiceSetup(v, addr);
            sVoiceChn[v] = c;
            c->voice = (s8)v;
            keyOn |= 1u << v;
            g_ps1SndKeyOns = g_ps1SndKeyOns + 1;
        }
        int v = c->voice;
        /* pitch */
        u32 hz1024;
        int x = (c->keyPitch - 60) * 64 + c->pitch;
        const SmpEntry* e = c->kind != CHN_NOISE ? &sSmp[c->smp] : NULL;
        if (c->kind == CHN_PCM)
            hz1024 = c->fixed ? (u32)((unsigned long long)kFixedRate[sFreq] * e->rate1024 / (c->midC ? c->midC : 1) * 1024)
                              : Pow2Mul(e->rate1024, x);
        else if (c->kind == CHN_WAVE) /* agbplay: 7040 Hz x 2^(key - 69) steps a second over 32 steps */
            hz1024 = Pow2Mul(220 * 1024, x - 9 * 64) * (e->rate1024 >> 10);
        else if (c->kind != CHN_NOISE) /* squares: 3520 Hz x 2^(key - 69) over 8 pattern steps */
            hz1024 = Pow2Mul(440 * 1024, x - 9 * 64) * (e->rate1024 >> 10);
        else
            hz1024 = 44100u * 1024;
        if (c->kind == CHN_NOISE)
            SpuNoiseClock(c);
        /* volume: agbplay's channel level (PCM: side volume x envelope / 65536; CGB: envelope / 32 on its side), the
         * front end's track gain / pan (the PC backend's mix), 1.0 = 0x3FFF */
        u32 l, r;
        if (c->kind == CHN_PCM) {
            l = (c->leftVol * c->envLevelCur) >> 2;
            r = (c->rightVol * c->envLevelCur) >> 2;
        } else {
            u32 lvl = c->envLevelCur;
            if (c->kind == CHN_WAVE) /* accurateCh3Volume: 0, 1/4, 1/2, 3/4, 1 of 16/32 */
                lvl = lvl < 2 ? 0 : lvl < 6 ? 4 : lvl < 10 ? 8 : lvl < 14 ? 12 : 16;
            l = c->panCur == PAN_RIGHT ? 0 : lvl << 9;
            r = c->panCur == PAN_LEFT ? 0 : lvl << 9;
        }
        u32 gain = sTrackGain[c->playerIdx][c->trackIdx & 15];
        int pan = sTrackPan[c->playerIdx][c->trackIdx & 15];
        l = l * gain / 255;
        r = r * gain / 255;
        if (pan > 0)
            l = pan >= 64 ? 0 : l * (64 - pan) / 64;
        else if (pan < 0)
            r = pan <= -64 ? 0 : r * (64 + pan) / 64;
        PS1SpuVoiceSet(v, SpuPitch(hz1024), (u16)(l > 0x3FFF ? 0x3FFF : l), (u16)(r > 0x3FFF ? 0x3FFF : r));
        if (keyOn & (1u << v))
            SndLogEvent('K', (u8)v, SpuPitch(hz1024), c->trackIdx, c->kind, c->key, c->playerIdx, (u8)(l >> 6),
                        (u8)(r >> 6), e ? sSmpAddr[c->smp] : 0);
    }
    u32 off = sKeyOff & ~keyOn;
    if (off)
        PS1SpuVoiceStop(off);
    if (keyOn)
        PS1SpuKeyOn(keyOn);
    sKeyOff = 0;
}

/* ---- tracks (MP2KTrack) ---- */
static void TrackInit(SndTrack* t, const u8* pos) {
    u8 index = t->index, order = t->order;
    memset(t, 0, sizeof(*t));
    t->index = index;
    t->order = order;
    t->pos = pos;
    t->prog = PROG_UNDEFINED;
    t->bendr = 2;
    t->lfos = 22;
    t->enabled = pos != NULL;
}

static void TrackStop(SndTrack* t) {
    if (!t->enabled)
        return;
    for (SndChn* c = t->channels; c; c = c->next)
        ChnKill(c);
}

static void TrackResetLfo(SndTrack* t) {
    t->lfoValue = 0;
    t->lfoPhase = 0;
    if (t->modt == MODT_PITCH)
        t->updatePitch = 1;
    else
        t->updateVolume = 1;
}

static void TrackFine(SndTrack* t) {
    for (SndChn* c = t->channels; c; c = c->next) {
        ChnRelease(c, 0);
        ChnRemoveFromTrack(c);
    }
    t->enabled = 0;
}

static const u8* Jump(SndTrack* t, const u8* at) {
    const u8* p = SndAt(Rd32(at));
    if (!p)
        TrackFine(t);
    return p;
}

static int CgbAllowed(SndChn** slot, u8 priority, const SndTrack* t) {
    SndChn* c = *slot;
    if (c) {
        if (!c->stop) {
            if (c->priority > priority)
                return 0;
            /* agbplay: `channels.front().track < &trk` (a removed channel's track is NULL: lowest) */
            if (c->priority == priority && (c->track ? c->track->order : -1) < t->order)
                return 0;
        }
        ChnFree(c);
    }
    return 1;
}

static void PlayNote(SndPlayer* pl, SndTrack* t, u8 cmd) {
    t->lastLen = kLen[cmd - 0xCF];
    if (t->pos[0] < 0x80) {
        t->lastKey = *t->pos++;
        if (t->pos[0] < 0x80) {
            t->lastVel = *t->pos++;
            if (t->pos[0] < 0x80)
                t->lastLen += *t->pos++;
        }
    }
    if (t->prog > 127)
        return;
    u32 instrPos = pl->bankPos + t->prog * 12;
    const u8* ins = SndAt(instrPos);
    if (!ins)
        return;
    u8 keyPitch;
    s8 rhythmPan = 0;
    if (ins[0] & 0x40) { /* key split */
        const u8* keymap = SndAt(Rd32(ins + 8));
        if (!keymap)
            return;
        instrPos = Rd32(ins + 4) + keymap[t->lastKey] * 12;
        if (!(ins = SndAt(instrPos)) || (ins[0] & 0xC0))
            return;
        keyPitch = t->lastKey;
    } else if (ins[0] == 0x80) { /* drum kit */
        instrPos = Rd32(ins + 4) + t->lastKey * 12;
        if (!(ins = SndAt(instrPos)) || (ins[0] & 0xC0))
            return;
        if (ins[3] & 0x80)
            rhythmPan = (s8)((ins[3] - 0xC0) * 2);
        keyPitch = ins[1];
    } else {
        keyPitch = t->lastKey;
    }
    t->lfodlCount = t->lfodl;
    if (t->lfodl != 0)
        TrackResetLfo(t);

    u8 type = ins[0], kind;
    SndChn* c;
    u32 samplePos = 0;
    const u8* smp = NULL;
    if (type & 7) {
        kind = type & 7;
        if (kind > CHN_NOISE)
            return;
        if (!CgbAllowed(&sCgb[kind], t->priority, t))
            return;
    } else {
        kind = CHN_PCM;
        samplePos = Rd32(ins + 4);
        if (!(smp = SndAt(samplePos)) || smp[0] > 1)
            return;
    }
    SndLogEvent('N', pl - sPlayers, 0, t->index, type, t->lastKey, keyPitch, t->lastVel, t->lastLen, instrPos & 0xFFFFFF);
    g_ps1SndNotes = g_ps1SndNotes + 1;
    if (!(c = ChnNew(t)))
        return;
    c->kind = kind;
    c->length = t->lastLen;
    c->key = t->lastKey;
    c->keyPitch = keyPitch;
    c->velocity = t->lastVel;
    c->priority = t->priority;
    c->rhythmPan = rhythmPan;
    c->echoVol = t->echoVol;
    c->echoLen = t->echoLen;
    c->trackIdx = t->index;
    c->playerIdx = (u8)(pl - sPlayers);
    c->instr = instrPos;
    c->att = ins[8];
    c->dec = ins[9];
    c->sus = ins[10];
    c->rel = ins[11];
    if (kind == CHN_PCM) {
        c->sample = samplePos;
        c->fixed = (type & 0x08) != 0;
        c->loop = (smp[3] & 0xC0) != 0;
        c->midC = Rd32(smp + 4);
        c->loopPos = Rd32(smp + 8);
        c->endPos = Rd32(smp + 12);
        if (c->loopPos > c->endPos)
            c->loopPos = 0;
        if (c->loopPos == c->endPos)
            c->loop = 0;
    } else {
        c->att &= 7;
        c->dec &= 7;
        c->sus &= 15;
        c->rel &= 7;
        c->sample = Rd32(ins + 4); /* duty / wave table / noise period */
        sCgb[kind] = c;
    }
    t->updateVolume = 1;
    t->updatePitch = 1;
}

static void Memacc(SndTrack* t) {
    u8 op = *t->pos++;
    u8* mem = &sMemacc[*t->pos++];
    u8 data = *t->pos++;
    int jump;
    switch (op) {
    case 0: *mem = data; return;
    case 1: *mem += data; return;
    case 2: *mem -= data; return;
    case 3: *mem = sMemacc[data]; return;
    case 4: *mem += sMemacc[data]; return;
    case 5: *mem -= sMemacc[data]; return;
    case 6: jump = *mem == data; break;
    case 7: jump = *mem != data; break;
    case 8: jump = *mem > data; break;
    case 9: jump = *mem >= data; break;
    case 10: jump = *mem <= data; break;
    case 11: jump = *mem < data; break;
    case 12: jump = *mem == sMemacc[data]; break;
    case 13: jump = *mem != sMemacc[data]; break;
    case 14: jump = *mem > sMemacc[data]; break;
    case 15: jump = *mem >= sMemacc[data]; break;
    case 16: jump = *mem <= sMemacc[data]; break;
    case 17: jump = *mem < sMemacc[data]; break;
    default: return;
    }
    if (jump)
        t->pos = Jump(t, t->pos);
    else
        t->pos += 4;
}

static void XCmd(SndTrack* t) {
    u8 x = *t->pos++;
    switch (x) {
    case 1: case 13: t->pos += 4; break;
    case 2: case 4: case 5: case 6: case 7: case 10: case 11: t->pos++; break;
    case 8: t->echoVol = *t->pos++; break;
    case 9: t->echoLen = *t->pos++; break;
    case 12: t->delay = t->pos[0] | t->pos[1] << 8; t->pos += 2; break;
    default: TrackFine(t); break;
    }
}

static void PlayCommand(SndPlayer* pl, SndTrack* t, u8 cmd) {
    switch (cmd) {
    case 0xB1: TrackFine(t); break;
    case 0xB2: t->pos = Jump(t, t->pos); break;
    case 0xB3:
        if (t->patternLevel >= SND_CALLS) {
            TrackFine(t);
            break;
        }
        t->ret[t->patternLevel++] = t->pos + 4;
        t->pos = Jump(t, t->pos);
        break;
    case 0xB4:
        if (t->patternLevel)
            t->pos = t->ret[--t->patternLevel];
        break;
    case 0xB5: {
        u8 count = *t->pos++;
        if (count == 0) {
            TrackFine(t);
            break;
        }
        if (++t->reptCount < count) {
            t->pos = Jump(t, t->pos);
        } else {
            t->reptCount = 0;
            t->pos += 4;
        }
        break;
    }
    case 0xB9: Memacc(t); break;
    case 0xBA: t->priority = *t->pos++; break;
    case 0xBB: pl->bpm = (u16)(*t->pos++ * 2); break;
    case 0xBC: t->keyShift = (s8)*t->pos++; break;
    case 0xBD: t->prog = *t->pos++; break;
    case 0xBE: t->vol = *t->pos++; t->updateVolume = 1; break;
    case 0xBF: t->pan = (s8)((s8)*t->pos++ - 0x40); t->updateVolume = 1; break;
    case 0xC0: t->bend = (s8)((s8)*t->pos++ - 0x40); t->updatePitch = 1; break;
    case 0xC1: t->bendr = *t->pos++; t->updatePitch = 1; break;
    case 0xC2:
        t->lfos = *t->pos++;
        if (t->lfos == 0)
            TrackResetLfo(t);
        break;
    case 0xC3: t->lfodlCount = t->lfodl = *t->pos++; break;
    case 0xC4:
        t->mod = *t->pos++;
        if (t->mod == 0)
            TrackResetLfo(t);
        break;
    case 0xC5: {
        u8 m = *t->pos++;
        if (m == t->modt)
            return;
        t->modt = m;
        t->updateVolume = 1;
        t->updatePitch = 1;
        break;
    }
    case 0xC8: t->tune = (s8)((s8)*t->pos++ - 0x40); t->updatePitch = 1; break;
    case 0xCD: XCmd(t); break;
    case 0xCE: {
        u8 key = t->pos[0];
        if (key >= 0x80) {
            key = t->lastKey;
        } else {
            t->pos++;
            t->lastKey = key;
        }
        for (SndChn* c = t->channels; c; c = c->next) {
            if (c->envState == ENV_DEAD || c->stop)
                continue;
            if (c->key == key) {
                ChnRelease(c, 0);
                break;
            }
        }
        break;
    }
    default: TrackFine(t); break;
    }
}

static s16 TrackGetPitch(const SndTrack* t) {
    int p = t->tune + t->bend * t->bendr + t->keyShift * 64;
    if (t->modt == MODT_PITCH)
        p += t->lfoValue * 4;
    return (s16)p;
}

static int TrackMain(SndPlayer* pl, SndTrack* t) {
    if (!t->enabled)
        return 0;
    for (SndChn* c = t->channels; c; c = c->next)
        ChnTickNote(c);
    while (t->delay == 0) {
        u8 cmd = t->pos[0];
        if (cmd < 0x80) {
            cmd = t->lastCmd;
            if (cmd < 0x80) {
                TrackFine(t);
                return 0;
            }
        } else {
            t->pos++;
            if (cmd >= 0xBD)
                t->lastCmd = cmd;
        }
        if (cmd >= 0xCF) {
            PlayNote(pl, t, cmd);
        } else if (cmd >= 0xB1) {
            PlayCommand(pl, t, cmd);
            if (!t->enabled)
                return 0;
        } else {
            t->delay = kLen[cmd - 0x80];
        }
    }
    t->delay--;
    if (t->lfos != 0 && t->mod != 0) {
        if (t->lfodlCount == 0) {
            t->lfoPhase += t->lfos;
            int pt = (s8)(t->lfoPhase - 64) >= 0 ? 128 - t->lfoPhase : (s8)t->lfoPhase;
            pt = (pt * t->mod) >> 6;
            if (t->lfoValue != pt) {
                t->lfoValue = (s8)pt;
                if (t->modt == MODT_PITCH)
                    t->updatePitch = 1;
                else
                    t->updateVolume = 1;
            }
        } else {
            t->lfodlCount--;
        }
    }
    return 1;
}

static void TrackVolPitchMain(SndTrack* t) {
    if (!t->enabled)
        return;
    t->pitch = TrackGetPitch(t);
    if (!t->updateVolume && !t->updatePitch)
        return;
    int v = t->vol << 1;
    if (t->modt == MODT_VOL)
        v = (v * (t->lfoValue + 128)) >> 7;
    int p = t->pan << 1;
    if (t->modt == MODT_PAN)
        p += t->lfoValue;
    for (SndChn* c = t->channels; c; c = c->next) {
        if (t->updateVolume)
            ChnSetVol(c, (u16)v, (s16)p);
        if (t->updatePitch)
            ChnSetPitch(c, t->pitch);
    }
    t->updateVolume = 0;
    t->updatePitch = 0;
}

/* ---- players (MP2KPlayer, SequenceReader::PlayerMain, MP2KContext) ---- */
static void PlayerInit(SndPlayer* pl, u32 songPos) {
    const u8* h = songPos ? SndAt(songPos) : NULL;
    pl->songPos = songPos;
    pl->tracksUsed = 0;
    if (h) {
        pl->tracksUsed = h[0] < pl->trackLimit ? h[0] : pl->trackLimit;
        pl->priority = h[2];
        u32 bank = Rd32(h + 4);
        pl->bankPos = (bank >> 24) == 0x08 ? bank : 0;
        for (int i = 0; i < pl->tracksUsed; ++i) {
            const u8* p = SndAt(Rd32(h + 8 + 4 * i));
            TrackInit(&pl->tracks[i], p);
        }
        pl->playing = 1;
        pl->finished = pl->tracksUsed == 0;
    } else {
        pl->playing = 0;
        pl->finished = 1;
    }
    for (int i = pl->tracksUsed; i < pl->trackLimit; ++i)
        TrackInit(&pl->tracks[i], NULL);
    pl->tick = 0;
    pl->bpm = 150;
}

static void MPlayStart(u8 idx, u32 songPos) {
    SndPlayer* pl = &sPlayers[idx];
    if (pl->usePriority && pl->songPos && songPos && pl->playing) {
        const u8* h = SndAt(songPos);
        if (h && pl->priority > h[2])
            return;
    }
    for (int i = 0; i < pl->trackLimit; ++i)
        TrackStop(&pl->tracks[i]);
    PlayerInit(pl, songPos);
}

static void MPlayStop(u8 idx) {
    SndPlayer* pl = &sPlayers[idx];
    pl->playing = 0;
    for (int i = 0; i < pl->trackLimit; ++i)
        TrackStop(&pl->tracks[i]);
}

static int PlayerMain(SndPlayer* pl) {
    if (!pl->playing || pl->finished)
        return 0;
    pl->tick += pl->bpm;
    while (pl->tick >= TEMPO_STEP) {
        int any = 0;
        for (int i = 0; i < pl->trackLimit; ++i)
            any |= TrackMain(pl, &pl->tracks[i]);
        pl->tick -= TEMPO_STEP;
        if (!any) {
            pl->finished = 1;
            pl->playing = 0;
        }
    }
    for (int i = 0; i < pl->trackLimit; ++i)
        TrackVolPitchMain(&pl->tracks[i]);
    return pl->playing;
}

static int ActivePlayback(void) {
    for (int i = 0; i < SND_PLAYERS; ++i)
        if (sPlayers[i].playing || !sPlayers[i].finished)
            return 1;
    for (int i = 0; i < SND_CHANNELS; ++i)
        if (sChn[i].used)
            return 1;
    return 0;
}

/* a song blob stays while a player that isn't finished is on it (its tracks point into it) */
static int BlobPinned(const Blob* b) {
    for (int i = 0; i < SND_PLAYERS; ++i)
        if (sPlayers[i].songPos && !sPlayers[i].finished && BlobAt(b, sPlayers[i].songPos))
            return 1;
    return 0;
}

static void Rebuild(void) {
    SndLoad();
    PS1MusicStop();
    if (sSmp)
        PS1SpuVoiceStop(0xFFFFFF);
    memset(sVoiceChn, 0, sizeof(sVoiceChn));
    sKeyOff = 0;
    memset(sTrackGain, 0xFF, sizeof(sTrackGain)); /* the PC backend's ResetTrackMixControlsLocked */
    memset(sTrackPan, 0, sizeof(sTrackPan));
    memset(sChn, 0, sizeof(sChn));
    memset(sCgb, 0, sizeof(sCgb));
    memset(sMemacc, 0, sizeof(sMemacc));
    u32 used = 0;
    for (int i = 0; i < SND_PLAYERS; ++i) {
        SndPlayer* pl = &sPlayers[i];
        memset(pl, 0, sizeof(*pl));
        pl->trackLimit = gMusicPlayers[i].nTracks;
        pl->usePriority = gMusicPlayers[i].unk_A != 0;
        pl->tracks = &sTracks[used];
        for (int k = 0; k < pl->trackLimit; ++k) {
            sTracks[used + k].index = (u8)k;
            sTracks[used + k].order = (u8)(used + k); /* gMPlayTracks order: by player, then track */
            TrackInit(&sTracks[used + k], NULL);
        }
        used += pl->trackLimit;
        pl->playing = 0;
        pl->finished = 1;
        pl->bpm = 150;
    }
    sInit = sData != NULL;
}

#ifdef PS1_TRACE
/* at the trace's samples (ps1/tmc_trace.c; every frame would slow DuckStation: an SPU register read syncs its SPU):
 * voices sounding (ADSR level and volume nonzero), max / sum, and the last snapshot (GDB) */
void PS1SndTraceSample(void) {
    if (!sSmp)
        return;
    g_ps1SpuEndx = PS1SpuSnapshot(&g_ps1SpuSnap[0][0]);
    u32 n = 0;
    for (int v = 0; v < 24; ++v)
        n += g_ps1SpuSnap[v][4] != 0 && (g_ps1SpuSnap[v][0] | g_ps1SpuSnap[v][1]) != 0;
    if (n > g_ps1SpuSoundingMax)
        g_ps1SpuSoundingMax = n;
    g_ps1SpuSoundingSum = g_ps1SpuSoundingSum + n;
    g_ps1SpuFrames = g_ps1SpuFrames + 1;
}
#endif

/* once a game frame, after VBlankIntr (ps1/tmc_frame.cpp) */
#define SND_CATCHUP 6
uint32_t PS1VsyncCount(void); /* ps1/tmc_main.cpp */
static u32 sLastVsync;
volatile u32 g_ps1SndClockExtra = 0, g_ps1SndClockDropped = 0; /* sequencer runs beyond one a frame; vsyncs dropped */
volatile u32 g_ps1SndClockExp = 0; /* GDB: 1 = once a game frame (tmc_pc's frame-synced ticks, for the sound A/B) */
static void SndTicks(u32 ticks) {
    for (u32 t = 0; t < ticks; ++t) {
        for (int i = 0; i < INTERFRAMES; ++i) {
            if (!ActivePlayback())
                break;
            for (int p = 0; p < SND_PLAYERS; ++p)
                PlayerMain(&sPlayers[p]);
            MixerProcess();
        }
        if (sSmp)
            SpuUpdate();
    }
}
/* docs/36 choice 127: a psyqo timer (every vsync; psyqo runs timers inside its CD waits and the flip wait) plays the
 * sound frames a stalled game frame would otherwise only catch up afterwards: during a blocking CD read the music and
 * the effects keep their time. Only for a stall (2+ vsyncs since the last sound frame: the frame start plays its own),
 * never inside the sequencer, an area change or a song load */
volatile u32 g_ps1SndTimerTicks = 0;
void PS1SndTimerTick(void) {
    if (!sInit || !sVsync || sSndBusy || g_ps1SndClockExp)
        return;
    u32 now = PS1VsyncCount(), lag = (u32)(now - sLastVsync);
    if (lag < 2 || lag > 60)
        return;
    sSndBusy = 1;
    sLastVsync = now - 1; /* (the next frame start plays one more: the frame's own) */
    SndTicks(lag - 1);
    g_ps1SndTimerTicks = g_ps1SndTimerTicks + (lag - 1);
    sSndBusy = 0;
}
void PS1SndFrame(void) {
    if (!sInit)
        return;
    sSndBusy = 1;
    /* the new area's songs and samples, before its first sound frame; a sub-task keeps its room's (choice 102: it
     * cleared gRoomControls, area 0, and reloaded area 0's samples, then the area's, at every shop / fusion / map) */
    u8 area = PS1TmcRoomControls()->area;
    if (area != sSndArea)
        SndAreaChange(area);
    /* the sequencer runs once per vsync since its last run (docs/36 choice 117): the GBA runs m4aSoundMain from the
     * line-80 interrupt of every displayed frame, whatever the game does, so a slow or stalled game frame doesn't slow
     * or stop the sound; up to SND_CATCHUP vsyncs are caught up (a longer stall resumes where it was) */
    u32 now = PS1VsyncCount(), ticks = (u32)(now - sLastVsync);
    sLastVsync = now;
    if (!ticks || g_ps1SndClockExp || now == ticks) /* (the first run: no vsync before it) */
        ticks = 1;
    if (ticks > SND_CATCHUP) {
        g_ps1SndClockDropped = g_ps1SndClockDropped + (ticks - SND_CATCHUP);
        ticks = SND_CATCHUP;
    }
    g_ps1SndClockExtra = g_ps1SndClockExtra + (ticks - 1);
    if (!sVsync) {
        sSndBusy = 0;
        return;
    }
#ifdef PS1_TRACE
    u32 h0 = PS1Hblanks();
#endif
#ifdef PS1_TRACE
    u32 spuH = 0;
#endif
    for (u32 t = 0; t < ticks; ++t) {
        for (int i = 0; i < INTERFRAMES; ++i) {
            if (!ActivePlayback())
                break;
            for (int p = 0; p < SND_PLAYERS; ++p)
                PlayerMain(&sPlayers[p]);
            MixerProcess();
        }
        /* the voices after each GBA frame of sound: a note that starts and ends within a caught-up run still keys on */
#ifdef PS1_TRACE
        u32 s0 = PS1Hblanks();
#endif
        if (sSmp)
            SpuUpdate();
#ifdef PS1_TRACE
        spuH += (PS1Hblanks() - s0) & 0xFFFF;
#endif
    }
#ifdef PS1_TRACE
    u32 h2 = PS1Hblanks(), h1 = h2 - spuH;
#endif
    PS1MusicFrame();
#ifdef PS1_TRACE
    { /* hblanks: sequencer, SPU voices, music (max over the run; docs/36 5.6) */
        u32 h3 = PS1Hblanks(), a = (h1 - h0) & 0xFFFF, b = (h2 - h1) & 0xFFFF, c = (h3 - h2) & 0xFFFF;
        if (a > g_ps1SndSeqMax)
            g_ps1SndSeqMax = a;
        if (b > g_ps1SndSpuMax)
            g_ps1SndSpuMax = b;
        if (c > g_ps1SndMusicMax)
            g_ps1SndMusicMax = c;
        g_ps1SndSeqSum = g_ps1SndSeqSum + a;
        g_ps1SndSpuSum = g_ps1SndSpuSum + b;
        g_ps1SndMusicSum = g_ps1SndMusicSum + c;
        g_ps1SndFrames = g_ps1SndFrames + 1;
    }
#endif
    sSndBusy = 0;
}

/* ---- the backend API (port/port_m4a_backend.h; the PC's port_m4a_backend.cpp) ---- */
void Port_Audio_Reset(void) {}
bool Port_M4A_Backend_Init(uint32_t sampleRate) {
    (void)sampleRate;
    return true;
}
void Port_M4A_Backend_Shutdown(void) {}
void Port_M4A_Backend_Reset(void) {
    memset(sCurrentSong, 0, sizeof(sCurrentSong));
    Rebuild();
}
void Port_M4A_Backend_SoundInit(uint32_t soundMode) {
    sVsync = 1;
    memset(sCurrentSong, 0, sizeof(sCurrentSong));
    Rebuild();
    Port_M4A_Backend_SetSoundMode(soundMode);
}
void Port_M4A_Backend_SetSoundMode(uint32_t soundMode) {
    u8 freq = (soundMode >> 16) & 0xF;
    if (freq)
        sFreq = freq;
}
void Port_M4A_Backend_SetVSyncEnabled(bool enabled) { sVsync = enabled; }

bool Port_M4A_Backend_StartSongById(uint8_t playerIndex, uint16_t songId) {
    if (!sData)
        return true; /* no song data: silent, every song accepted */
    u32 songPos = songId < sSongCount ? sSongs[songId].head : 0;
    SndLogEvent('S', playerIndex, songId, 0, 0, 0, 0, 0, 0, songPos & 0xFFFFFF);
    if (!sInit || playerIndex >= SND_PLAYERS)
        return false;
    if (songPos == 0) {
        MPlayStop(playerIndex);
        sCurrentSong[playerIndex] = 0;
        return false;
    }
    /* as the PC backend: a BGM (ids 1..99) already running on this player is left alone */
    if (songId >= 1 && songId <= 99 && sCurrentSong[playerIndex] == songId)
        return true;
    if (playerIndex == PLAYER_BGM && PS1MusicHas(songId)) { /* streamed, not sequenced */
        MPlayStop(playerIndex);
        PS1MusicPlay(songId);
        sCurrentSong[playerIndex] = songId;
        return true;
    }
    if (!SongLoad(songId)) /* its data couldn't be read: accepted (as on the PC), silent */
        return true;
    MPlayStart(playerIndex, songPos);
    sCurrentSong[playerIndex] = songId;
    return true;
}
void Port_M4A_Backend_StartSong(uint8_t playerIndex, const SongHeader* songHeader) {
    (void)playerIndex;
    (void)songHeader; /* the PC's is only reached through ROM pointers; the front end starts songs by id */
}
void Port_M4A_Backend_StopPlayer(uint8_t playerIndex) {
    if (!sInit || playerIndex >= SND_PLAYERS)
        return;
    MPlayStop(playerIndex);
    if (playerIndex == PLAYER_BGM)
        PS1MusicPause();
    sCurrentSong[playerIndex] = 0;
}
void Port_M4A_Backend_ContinuePlayer(uint8_t playerIndex) {
    if (!sInit || playerIndex >= SND_PLAYERS)
        return;
    if (playerIndex == PLAYER_BGM)
        PS1MusicResume();
    sPlayers[playerIndex].playing = 1;
}
void Port_M4A_Backend_SetTrackVolume(uint8_t playerIndex, uint16_t trackBits, uint16_t volume) {
    if (playerIndex >= SND_PLAYERS)
        return;
    for (int t = 0; t < 16; ++t)
        if (trackBits >> t & 1)
            sTrackGain[playerIndex][t] = volume > 0xFF ? 0xFF : (u8)volume;
    if (playerIndex == PLAYER_BGM && (trackBits & 1)) /* the stream: the front end sets every track alike */
        PS1MusicVolume(volume);
}
void Port_M4A_Backend_SetTrackPan(uint8_t playerIndex, uint16_t trackBits, int8_t pan) {
    if (playerIndex >= SND_PLAYERS)
        return;
    for (int t = 0; t < 16; ++t)
        if (trackBits >> t & 1)
            sTrackPan[playerIndex][t] = pan;
}
bool Port_M4A_Backend_IsPlayerActive(uint8_t playerIndex) {
    if (!sInit || playerIndex >= SND_PLAYERS)
        return false;
    if (playerIndex == PLAYER_BGM && PS1MusicActive())
        return true;
    return sPlayers[playerIndex].playing && !sPlayers[playerIndex].finished;
}
