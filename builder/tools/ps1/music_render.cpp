/*
 * music_render: one m4a song rendered by agbplay (libs/agbplay_core, the PC port's engine and settings) and encoded
 * as SPU ADPCM for the PS1's music stream (docs/36 5.5, choice 67).
 *
 * The song plays on player 31 (the BGM player, 12 tracks) with the PC backend's sound modes, mixed at RATE Hz stereo.
 * A looping song is rendered for MAX_SECONDS, then: every track that loops (GOTO) has a period (not always track 0,
 * and periods differ: Cave of Flames loops 33.9 s and 7.53 s tracks; tracks that never loop are left out). The
 * reference is the longest-period track; E1 = its first GOTO once every looping track has made its first, E2 = its
 * first later GOTO where every looping track has a GOTO at the same distance before it as before E1 (within a frame);
 * none: the reference's own next GOTO. The stream is [0, E2) and loops back from E2 to E1: the body is a pass whose
 * note / reverb tails at its start match those at its end. For the PS1's ring (SPU DMA in 64-byte units) E1 and the body's length are made multiples of 4 ADPCM blocks
 * (112 samples): silence before the intro (< 112 samples) and the body stretched by < 56 samples (windowed sinc). A
 * song without a GOTO is rendered until its players finish and its channels die out (at most MAX_SECONDS).
 *
 * Output: OUT.L / OUT.R raw SPU ADPCM blocks (no flags: the PS1 sets the ring's loop flags), and on stdout
 * "blocks loopBlock" (loopBlock = -1: no loop), both in 28-sample blocks.
 *
 * Build (tools/ps1/music_pack.py does it): c++ -std=c++20 -O2 -Ilibs/agbplay_core tools/ps1/music_render.cpp
 *   libs/agbplay_core/(every .cpp) -o build/ps1/music/music_render
 * Usage: music_render ROM SONG_HEADER_OFFSET RATE OUT
 */
#include "MP2KContext.hpp"
#include "Rom.hpp"
#include "Types.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>

extern "C" void (*agbplay_goto_hook)(uint8_t player, uint8_t track);

static constexpr int kBgmPlayer = 31;
static constexpr int kMaxSeconds = 400;
static constexpr int kAlign = 112; /* 4 ADPCM blocks */
static size_t sRendered;

static std::vector<std::vector<size_t>> sTrackGotos(16);
static void OnGoto(uint8_t player, uint8_t track) {
    if (player != kBgmPlayer)
        return;
    if (track < 16) {
        sTrackGotos[track].push_back(sRendered);
    }
}

/* the loop (E1, E2) from the whole render, see above */
static bool FindLoop(size_t tol, size_t *e1, size_t *e2) {
    int ref = -1;
    size_t refPeriod = 0, start = 0;
    for (int t = 0; t < 16; ++t) {
        const auto &g = sTrackGotos[t];
        if (g.empty())
            continue; /* never loops (ends, or holds a note): not part of the loop */
        start = std::max(start, g[0]);
        if (g.size() >= 2 && g[1] - g[0] > refPeriod) {
            refPeriod = g[1] - g[0];
            ref = t;
        }
    }
    if (ref < 0)
        return false;
    const auto &r = sTrackGotos[ref];
    size_t a = 0;
    while (a < r.size() && r[a] + tol < start)
        ++a;
    /* a looping track is in step at E2 if it has a GOTO at E2 minus its phase at E1 (its last GOTO at or before E1) */
    auto near = [tol](const std::vector<size_t> &g, size_t x) {
        for (size_t v : g)
            if (v + tol >= x && v <= x + tol)
                return true;
        return false;
    };
    for (size_t b = a + 1; b < r.size(); ++b) {
        bool ok = true;
        for (int t = 0; t < 16 && ok; ++t) {
            const auto &g = sTrackGotos[t];
            if (g.empty() || t == ref)
                continue;
            size_t last = 0;
            bool found = false;
            for (size_t v : g)
                if (v <= r[a] + tol) {
                    last = v;
                    found = true;
                }
            if (!found)
                continue;
            size_t phase = r[a] + tol - last; /* how long before E1 (+ tol) it looped */
            ok = near(g, r[b] + tol - phase);
        }
        if (ok) {
            *e1 = r[a];
            *e2 = r[b];
            return true;
        }
    }
    /* no common repeat inside the render: the longest-period track's own loop */
    if (a + 1 < r.size()) {
        *e1 = r[a];
        *e2 = r[a + 1];
        return true;
    }
    return false;
}

static const int kFilter[5][2] = {{0, 0}, {60, 0}, {115, -52}, {98, -55}, {122, -60}};

static int Clamp16(double v) {
    long r = lround(v);
    return r > 32767 ? 32767 : r < -32768 ? -32768 : (int)r;
}

/* one 28-sample block: the filter / shift with the least squared error (spuadpcm.c's encoder), decoder history kept */
static void EncodeBlock(const int *s, uint8_t *out, int *h1, int *h2) {
    double best = 1e300;
    uint8_t bestBlk[16];
    int bestH1 = 0, bestH2 = 0;
    for (int f = 0; f < 5; ++f)
        for (int shift = 0; shift <= 12; ++shift) {
            int p1 = *h1, p2 = *h2;
            double err = 0;
            uint8_t blk[16] = {0};
            blk[0] = (uint8_t)(shift | f << 4);
            for (int i = 0; i < 28; ++i) {
                int pred = (p1 * kFilter[f][0] + p2 * kFilter[f][1] + 32) >> 6;
                long q = lround((double)(s[i] - pred) * (1 << shift) / 4096.0);
                q = q > 7 ? 7 : q < -8 ? -8 : q;
                int dec = Clamp16((double)(((int)q << 12) >> shift) + pred);
                err += (double)(dec - s[i]) * (dec - s[i]);
                if (err >= best)
                    break;
                blk[2 + i / 2] |= (uint8_t)((q & 15) << ((i & 1) * 4));
                p2 = p1;
                p1 = dec;
            }
            if (err < best) {
                best = err;
                memcpy(bestBlk, blk, 16);
                bestH1 = p1;
                bestH2 = p2;
            }
        }
    memcpy(out, bestBlk, 16);
    *h1 = bestH1;
    *h2 = bestH2;
}

/* windowed-sinc resampling of n samples to m (m close to n) */
static std::vector<float> Resample(const float *in, size_t n, size_t m) {
    std::vector<float> out(m);
    if (m == n) {
        std::copy(in, in + n, out.begin());
        return out;
    }
    const int taps = 16;
    for (size_t j = 0; j < m; ++j) {
        double x = (double)j * n / m, acc = 0, wsum = 0;
        long c = (long)floor(x);
        for (long k = c - taps + 1; k <= c + taps; ++k) {
            if (k < 0 || k >= (long)n)
                continue;
            double d = x - k;
            double sinc = fabs(d) < 1e-9 ? 1.0 : sin(M_PI * d) / (M_PI * d);
            double w = sinc * (0.5 + 0.5 * cos(M_PI * d / taps));
            acc += in[k] * w;
            wsum += w;
        }
        out[j] = (float)(wsum != 0 ? acc / wsum : 0);
    }
    return out;
}

int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "usage: music_render ROM SONG_HEADER_OFFSET RATE OUT\n");
        return 1;
    }
    std::ifstream f(argv[1], std::ios::binary);
    std::vector<uint8_t> romData((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    size_t songPos = strtoul(argv[2], nullptr, 0);
    uint32_t rate = (uint32_t)atoi(argv[3]);
    Rom rom = Rom::LoadFromBufferRef(std::span<uint8_t>(romData.data(), romData.size()));

    MP2KSoundMode mode; /* port/port_m4a_backend.cpp's MakeSoundMode + the front end's m4aSoundInit mode */
    mode.vol = 0x0f;
    mode.rev = 0x80;
    mode.freq = 0x05;
    mode.maxChannels = 0x08;
    mode.dacConfig = 0x09;
    AgbplaySoundMode amode; /* MakeAgbplayMode */
    amode.resamplerTypeNormal = ResamplerType::SINC;
    amode.resamplerTypeFixed = ResamplerType::SINC;
    amode.reverbType = ReverbType::NORMAL;
    amode.reverbForce = 32;
    amode.cgbPolyphony = CGBPolyphony::MONO_STRICT;
    amode.dmaBufferLen = 0x630;
    amode.accurateCh3Quantization = true;
    amode.accurateCh3Volume = true;
    amode.emulateCgbSustainBug = true;
    SongTableInfo sti;
    PlayerTableInfo players(32);
    for (auto &p : players)
        p.maxTracks = 2;
    players[30].maxTracks = 12;
    players[31].maxTracks = 12;
    MP2KContext ctx(rate, -1, rom, mode, amode, sti, players);
    ctx.m4aSoundMode(0x0090F800u | 0x00050000u); /* DA 8 bit, 15768 Hz, volume 15, 8 channels (m4aSoundInit) */
    agbplay_goto_hook = OnGoto;
    ctx.m4aMPlayStart(kBgmPlayer, songPos);

    std::vector<float> L, R;
    size_t maxSamples = (size_t)rate * kMaxSeconds, quietSince = 0, e1 = 0, e2 = 0;
    bool looped = false;
    while (L.size() < maxSamples) {
        sRendered = L.size();
        ctx.m4aSoundMain();
        for (const auto &s : ctx.masterAudioBuffer) {
            L.push_back(std::clamp(s.left, -1.0f, 1.0f));
            R.push_back(std::clamp(s.right, -1.0f, 1.0f));
        }
        const auto &pl = ctx.players[kBgmPlayer];
        bool silent = ctx.sndChannels.empty() && ctx.sq1Channels.empty() && ctx.sq2Channels.empty() &&
                      ctx.waveChannels.empty() && ctx.noiseChannels.empty();
        if (!pl.playing && silent) {
            if (!quietSince)
                quietSince = L.size();
            if (L.size() - quietSince > rate / 2) /* the reverb's tail */
                break;
        }
    }
    if (getenv("MUSIC_GOTOS")) /* debug: every track's GOTO times (seconds) */
        for (int t = 0; t < 16; ++t)
            if (!sTrackGotos[t].empty()) {
                fprintf(stderr, "track %d:", t);
                for (size_t g : sTrackGotos[t])
                    fprintf(stderr, " %.2f", (double)g / rate);
                fprintf(stderr, "\n");
            }
    looped = FindLoop(rate / 60, &e1, &e2);
    long loopBlock = -1;
    size_t pad = 0;
    std::vector<float> outL, outR;
    if (looped) {
        pad = (kAlign - e1 % kAlign) % kAlign;
        size_t body = e2 - e1, body2 = (body + kAlign / 2) / kAlign * kAlign;
        auto bl = Resample(&L[e1], body, body2), br = Resample(&R[e1], body, body2);
        outL.assign(pad, 0.0f);
        outR.assign(pad, 0.0f);
        outL.insert(outL.end(), L.begin(), L.begin() + e1);
        outR.insert(outR.end(), R.begin(), R.begin() + e1);
        outL.insert(outL.end(), bl.begin(), bl.end());
        outR.insert(outR.end(), br.begin(), br.end());
        loopBlock = (long)((pad + e1) / 28);
    } else {
        outL = L;
        outR = R;
        size_t n = (outL.size() + kAlign - 1) / kAlign * kAlign;
        outL.resize(n, 0.0f);
        outR.resize(n, 0.0f);
    }
    size_t blocks = outL.size() / 28;
    for (int ch = 0; ch < 2; ++ch) {
        const std::vector<float> &src = ch ? outR : outL;
        std::vector<int> pcm(src.size());
        for (size_t i = 0; i < src.size(); ++i)
            pcm[i] = Clamp16(src[i] * 32767.0);
        std::vector<uint8_t> adpcm(blocks * 16);
        int h1 = 0, h2 = 0;
        for (size_t b = 0; b < blocks; ++b)
            EncodeBlock(&pcm[b * 28], &adpcm[b * 16], &h1, &h2);
        std::string path = std::string(argv[4]) + (ch ? ".R" : ".L");
        FILE *o = fopen(path.c_str(), "wb");
        fwrite(adpcm.data(), 1, adpcm.size(), o);
        fclose(o);
    }
    printf("%zu %ld\n", blocks, loopBlock);
    return 0;
}
