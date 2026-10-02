/*
 * spuadpcm: a GBA DirectSound sample (signed 8-bit PCM) -> SPU ADPCM, loops on block boundaries (docs/36 5.1).
 *
 * The SPU plays 28-sample blocks and can only loop from a block's start to a block's end. A GBA sample loops from
 * sample L to its end E at any position. So: the loop body [L, E) is resampled to M = the next multiple of 28 samples
 * (ratio M / (E - L), at most 1 + 27 / (E - L)), the part before it [0, L) by the same ratio, and the front is padded
 * with silence up to a block (at most 27 samples); the player multiplies the sample's rate by the same ratio, so the
 * pitch is exact. A one-shot sample is only padded at its end. Encoding: the five SPU filters, per block the filter and
 * shift with the least squared error (decoded history carried), flags: loop start on the loop's first block, loop end +
 * repeat on the last block of a looped sample, loop end (mute) on a one-shot's last block.
 *
 * Usage: spuadpcm IN.s8 OUT.adpcm LOOP(0|1) LOOPSTART LENGTH
 * Prints: "blocks front_pad ratio_num ratio_den" (rate' = rate * ratio_num / ratio_den; loop block = front_pad+L' / 28)
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const int kFilter[5][2] = {{0, 0}, {60, 0}, {115, -52}, {98, -55}, {122, -60}};

/* windowed-sinc resampling of n samples to m (ratio close to 1: a short kernel is enough) */
static void resample(const double *in, int n, double *out, int m) {
    if (m == n) {
        memcpy(out, in, sizeof(double) * n);
        return;
    }
    const int taps = 8;
    for (int j = 0; j < m; ++j) {
        double x = (double)j * n / m, acc = 0, wsum = 0;
        int c = (int)floor(x);
        for (int k = c - taps + 1; k <= c + taps; ++k) {
            double d = x - k, w;
            if (k < 0 || k >= n)
                continue;
            double sinc = fabs(d) < 1e-9 ? 1.0 : sin(M_PI * d) / (M_PI * d);
            double win = 0.5 + 0.5 * cos(M_PI * d / taps);
            w = sinc * win;
            acc += in[k] * w;
            wsum += w;
        }
        out[j] = wsum != 0 ? acc / wsum : 0;
    }
}

static int clamp16(double v) {
    long r = lround(v);
    return r > 32767 ? 32767 : r < -32768 ? -32768 : (int)r;
}

/* one block: pick filter / shift, write 16 bytes, update the decoder history */
static void encode_block(const int *s, uint8_t *out, int *h1, int *h2, uint8_t flags) {
    double best = 1e300;
    uint8_t bestBlk[16];
    int bestH1 = 0, bestH2 = 0;
    for (int f = 0; f < 5; ++f)
        for (int shift = 0; shift <= 12; ++shift) {
            int p1 = *h1, p2 = *h2;
            double err = 0;
            uint8_t blk[16] = {0};
            blk[0] = (uint8_t)(shift | f << 4);
            blk[1] = flags;
            for (int i = 0; i < 28; ++i) {
                int pred = (p1 * kFilter[f][0] + p2 * kFilter[f][1] + 32) >> 6;
                int want = s[i] - pred;
                /* nibble: decoded = (nib << 12) >> shift, i.e. nib = round(want << shift >> 12) */
                long q = lround((double)want * (1 << shift) / 4096.0);
                if (q > 7)
                    q = 7;
                if (q < -8)
                    q = -8;
                int dec = clamp16((double)(((int)q << 12) >> shift) + pred);
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

int main(int argc, char **argv) {
    if (argc != 6) {
        fprintf(stderr, "usage: spuadpcm IN.s8 OUT.adpcm LOOP LOOPSTART LENGTH\n");
        return 1;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f)
        return 1;
    int loop = atoi(argv[3]), L = atoi(argv[4]), E = atoi(argv[5]);
    int8_t *pcm = malloc(E + 1);
    int got = (int)fread(pcm, 1, E, f);
    fclose(f);
    if (got < E)
        memset(pcm + got, 0, E - got);
    if (!loop || L >= E)
        loop = 0, L = E;
    double *src = malloc(sizeof(double) * (E + 1));
    for (int i = 0; i < E; ++i)
        src[i] = pcm[i] * 256.0;
    int num = 1, den = 1, Lr = L, M = 0;
    double *pre = NULL, *body = NULL;
    if (loop) {
        int N = E - L;
        M = (N + 27) / 28 * 28;
        num = M, den = N;
        Lr = (int)lround((double)L * M / N);
        pre = malloc(sizeof(double) * (Lr + 1));
        body = malloc(sizeof(double) * M);
        if (L > 0)
            resample(src, L, pre, Lr);
        resample(src + L, N, body, M);
    }
    int pad = loop ? (28 - Lr % 28) % 28 : 0;
    int total = loop ? pad + Lr + M : (E + 27) / 28 * 28;
    int *s = calloc(total + 28, sizeof(int));
    if (loop) {
        for (int i = 0; i < Lr; ++i)
            s[pad + i] = clamp16(pre[i]);
        for (int i = 0; i < M; ++i)
            s[pad + Lr + i] = clamp16(body[i]);
    } else {
        for (int i = 0; i < E; ++i)
            s[i] = clamp16(src[i]);
    }
    int blocks = total / 28, loopBlock = loop ? (pad + Lr) / 28 : -1;
    uint8_t *out = calloc(blocks, 16);
    int h1 = 0, h2 = 0;
    for (int b = 0; b < blocks; ++b) {
        uint8_t flags = 0;
        if (b == loopBlock)
            flags |= 4;
        if (b == blocks - 1)
            flags |= loop ? 3 : 1;
        encode_block(s + b * 28, out + b * 16, &h1, &h2, flags);
    }
    f = fopen(argv[2], "wb");
    fwrite(out, 16, blocks, f);
    fclose(f);
    printf("%d %d %d %d %d\n", blocks, pad, num, den, loopBlock);
    return 0;
}
