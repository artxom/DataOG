/* Data.O.G Offline License Cryptography, Hardware Fingerprinting & Codec Core
 * Pure ANSI C / C11 with zero third-party dependencies (macOS / Linux / Windows).
 * Implements:
 *   - SHA-256 & SHA-512
 *   - RFC 8032 Ed25519 (deterministic signing & strict verification)
 *   - Base32 (RFC 4648, no padding, case-insensitive decode) + CRC16-CCITT
 *   - Gregorian Date <-> Epoch Day (Epoch 2025-01-01 = day 0)
 *   - Offline Hardware Machine Code (MCH-XXXX-XXXX-XXXX-XXXX) & Local IPv4 Probe
 *   - Request Code (REQ1-...) & License Payload (16B) + Signature (64B) -> DOG1-...
 */
#ifndef DATAOG_LICENSE_CORE_H
#define DATAOG_LICENSE_CORE_H

#ifdef __APPLE__
#  ifndef _DARWIN_C_SOURCE
#    define _DARWIN_C_SOURCE 1
#  endif
#endif
#ifndef _DEFAULT_SOURCE
#  define _DEFAULT_SOURCE 1
#endif
#if !defined(__APPLE__) && !defined(_POSIX_C_SOURCE)
#  define _POSIX_C_SOURCE 200809L
#endif
#ifdef _WIN32
#  ifndef _CRT_SECURE_NO_WARNINGS
#    define _CRT_SECURE_NO_WARNINGS 1
#  endif
#  ifndef _WINSOCK_DEPRECATED_NO_WARNINGS
#    define _WINSOCK_DEPRECATED_NO_WARNINGS 1
#  endif
#  ifndef _WIN32_WINNT
#    define _WIN32_WINNT 0x0601
#  endif
#endif

#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <windows.h>
#  include <iphlpapi.h>
#  include <winreg.h>
#  ifndef KEY_WOW64_64KEY
#    define KEY_WOW64_64KEY 0x0100
#  endif
#  ifdef _MSC_VER
#    pragma comment(lib, "ws2_32.lib")
#    pragma comment(lib, "iphlpapi.lib")
#    pragma comment(lib, "advapi32.lib")
#  endif
#else
#  include <arpa/inet.h>
#  include <ifaddrs.h>
#  include <net/if.h>
#  include <netinet/in.h>
#  include <sys/socket.h>
#  include <unistd.h>
#endif

#define DATAOG_LICENSE_VERSION 1u
#define DATAOG_SCOPE_MACHINE   1u
#define DATAOG_SCOPE_GLOBAL    2u

#define DATAOG_STATUS_OK              0
#define DATAOG_STATUS_INVALID_FORMAT -1
#define DATAOG_STATUS_BAD_SIGNATURE  -2
#define DATAOG_STATUS_EXPIRED        -3
#define DATAOG_STATUS_NOT_YET_VALID  -4
#define DATAOG_STATUS_DEVICE_MISMATCH -5
#define DATAOG_STATUS_BAD_SCOPE      -6

static const uint8_t DATAOG_SIG_DOMAIN[] = "DataOG-License-v1:";
static const size_t DATAOG_SIG_DOMAIN_LEN = 18u;

static inline void dataog_secure_zero(void *ptr, size_t len) {
    volatile uint8_t *p = (volatile uint8_t *)ptr;
    while (len--) {
        *p++ = 0;
    }
}

static inline uint32_t dataog_load_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static inline uint16_t dataog_load_be16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static inline void dataog_store_be16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xffu);
}

static inline void dataog_store_be32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)((v >> 16) & 0xffu);
    p[2] = (uint8_t)((v >> 8) & 0xffu);
    p[3] = (uint8_t)(v & 0xffu);
}

static inline void dataog_store_be64(uint8_t *p, uint64_t v) {
    for (int i = 7; i >= 0; --i) {
        p[i] = (uint8_t)(v & 0xffu);
        v >>= 8;
    }
}

static inline uint64_t dataog_load_be64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v = (v << 8) | (uint64_t)p[i];
    }
    return v;
}

/* =========================================================================
 * 1. SHA-256
 * ========================================================================= */

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t data[64];
    size_t datalen;
} dataog_sha256_ctx;

#define DOG_ROTR32(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void dataog_sha256_transform(dataog_sha256_ctx *ctx, const uint8_t data[64]) {
    static const uint32_t k[64] = {
        0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
        0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
        0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
        0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
        0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
        0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
        0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
        0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
    };
    uint32_t m[64];
    for (int i = 0, j = 0; i < 16; ++i, j += 4) {
        m[i] = dataog_load_be32(data + j);
    }
    for (int i = 16; i < 64; ++i) {
        uint32_t s0 = DOG_ROTR32(m[i - 15], 7) ^ DOG_ROTR32(m[i - 15], 18) ^ (m[i - 15] >> 3);
        uint32_t s1 = DOG_ROTR32(m[i - 2], 17) ^ DOG_ROTR32(m[i - 2], 19) ^ (m[i - 2] >> 10);
        m[i] = m[i - 16] + s0 + m[i - 7] + s1;
    }
    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2], d = ctx->state[3];
    uint32_t e = ctx->state[4], f = ctx->state[5], g = ctx->state[6], h = ctx->state[7];
    for (int i = 0; i < 64; ++i) {
        uint32_t S1 = DOG_ROTR32(e, 6) ^ DOG_ROTR32(e, 11) ^ DOG_ROTR32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t temp1 = h + S1 + ch + k[i] + m[i];
        uint32_t S0 = DOG_ROTR32(a, 2) ^ DOG_ROTR32(a, 13) ^ DOG_ROTR32(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = S0 + maj;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c; ctx->state[3] += d;
    ctx->state[4] += e; ctx->state[5] += f; ctx->state[6] += g; ctx->state[7] += h;
}

static void dataog_sha256_init(dataog_sha256_ctx *ctx) {
    ctx->datalen = 0;
    ctx->bitlen = 0;
    ctx->state[0] = 0x6a09e667u; ctx->state[1] = 0xbb67ae85u;
    ctx->state[2] = 0x3c6ef372u; ctx->state[3] = 0xa54ff53au;
    ctx->state[4] = 0x510e527fu; ctx->state[5] = 0x9b05688cu;
    ctx->state[6] = 0x1f83d9abu; ctx->state[7] = 0x5be0cd19u;
}

static void dataog_sha256_update(dataog_sha256_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; ++i) {
        ctx->data[ctx->datalen++] = data[i];
        if (ctx->datalen == 64u) {
            dataog_sha256_transform(ctx, ctx->data);
            ctx->bitlen += 512u;
            ctx->datalen = 0;
        }
    }
}

static void dataog_sha256_final(dataog_sha256_ctx *ctx, uint8_t hash[32]) {
    size_t i = ctx->datalen;
    if (ctx->datalen < 56u) {
        ctx->data[i++] = 0x80u;
        while (i < 56u) ctx->data[i++] = 0x00u;
    } else {
        ctx->data[i++] = 0x80u;
        while (i < 64u) ctx->data[i++] = 0x00u;
        dataog_sha256_transform(ctx, ctx->data);
        memset(ctx->data, 0, 56u);
    }
    ctx->bitlen += (uint64_t)ctx->datalen * 8u;
    dataog_store_be64(ctx->data + 56, ctx->bitlen);
    dataog_sha256_transform(ctx, ctx->data);
    for (int j = 0; j < 8; ++j) {
        dataog_store_be32(hash + j * 4, ctx->state[j]);
    }
}

static void dataog_sha256(const uint8_t *data, size_t len, uint8_t out[32]) {
    dataog_sha256_ctx ctx;
    dataog_sha256_init(&ctx);
    dataog_sha256_update(&ctx, data, len);
    dataog_sha256_final(&ctx, out);
    dataog_secure_zero(&ctx, sizeof(ctx));
}

/* =========================================================================
 * 2. SHA-512
 * ========================================================================= */

typedef struct {
    uint64_t state[8];
    uint64_t bitlen_lo;
    uint64_t bitlen_hi;
    uint8_t data[128];
    size_t datalen;
} dataog_sha512_ctx;

#define DOG_ROTR64(x, n) (((x) >> (n)) | ((x) << (64 - (n))))

static void dataog_sha512_transform(dataog_sha512_ctx *ctx, const uint8_t data[128]) {
    static const uint64_t k[80] = {
        0x428a2f98d728ae22ULL, 0x7137449123ef65cdULL, 0xb5c0fbcfec4d3b2fULL, 0xe9b5dba58189dbbcULL,
        0x3956c25bf348b538ULL, 0x59f111f1b605d019ULL, 0x923f82a4af194f9bULL, 0xab1c5ed5da6d8118ULL,
        0xd807aa98a3030242ULL, 0x12835b0145706fbeULL, 0x243185be4ee4b28cULL, 0x550c7dc3d5ffb4e2ULL,
        0x72be5d74f27b896fULL, 0x80deb1fe3b1696b1ULL, 0x9bdc06a725c71235ULL, 0xc19bf174cf692694ULL,
        0xe49b69c19ef14ad2ULL, 0xefbe4786384f25e3ULL, 0x0fc19dc68b8cd5b5ULL, 0x240ca1cc77ac9c65ULL,
        0x2de92c6f592b0275ULL, 0x4a7484aa6ea6e483ULL, 0x5cb0a9dcbd41fbd4ULL, 0x76f988da831153b5ULL,
        0x983e5152ee66dfabULL, 0xa831c66d2db43210ULL, 0xb00327c898fb213fULL, 0xbf597fc7beef0ee4ULL,
        0xc6e00bf33da88fc2ULL, 0xd5a79147930aa725ULL, 0x06ca6351e003826fULL, 0x142929670a0e6e70ULL,
        0x27b70a8546d22ffcULL, 0x2e1b21385c26c926ULL, 0x4d2c6dfc5ac42aedULL, 0x53380d139d95b3dfULL,
        0x650a73548baf63deULL, 0x766a0abb3c77b2a8ULL, 0x81c2c92e47edaee6ULL, 0x92722c851482353bULL,
        0xa2bfe8a14cf10364ULL, 0xa81a664bbc423001ULL, 0xc24b8b70d0f89791ULL, 0xc76c51a30654be30ULL,
        0xd192e819d6ef5218ULL, 0xd69906245565a910ULL, 0xf40e35855771202aULL, 0x106aa07032bbd1b8ULL,
        0x19a4c116b8d2d0c8ULL, 0x1e376c085141ab53ULL, 0x2748774cdf8eeb99ULL, 0x34b0bcb5e19b48a8ULL,
        0x391c0cb3c5c95a63ULL, 0x4ed8aa4ae3418acbULL, 0x5b9cca4f7763e373ULL, 0x682e6ff3d6b2b8a3ULL,
        0x748f82ee5defb2fcULL, 0x78a5636f43172f60ULL, 0x84c87814a1f0ab72ULL, 0x8cc702081a6439ecULL,
        0x90befffa23631e28ULL, 0xa4506cebde82bde9ULL, 0xbef9a3f7b2c67915ULL, 0xc67178f2e372532bULL,
        0xca273eceea26619cULL, 0xd186b8c721c0c207ULL, 0xeada7dd6cde0eb1eULL, 0xf57d4f7fee6ed178ULL,
        0x06f067aa72176fbaULL, 0x0a637dc5a2c898a6ULL, 0x113f9804bef90daeULL, 0x1b710b35131c471bULL,
        0x28db77f523047d84ULL, 0x32caab7b40c72493ULL, 0x3c9ebe0a15c9bebcULL, 0x431d67c49c100d4cULL,
        0x4cc5d4becb3e42b6ULL, 0x597f299cfc657e2aULL, 0x5fcb6fab3ad6faecULL, 0x6c44198c4a475817ULL
    };
    uint64_t m[80];
    for (int i = 0, j = 0; i < 16; ++i, j += 8) {
        m[i] = dataog_load_be64(data + j);
    }
    for (int i = 16; i < 80; ++i) {
        uint64_t s0 = DOG_ROTR64(m[i - 15], 1) ^ DOG_ROTR64(m[i - 15], 8) ^ (m[i - 15] >> 7);
        uint64_t s1 = DOG_ROTR64(m[i - 2], 19) ^ DOG_ROTR64(m[i - 2], 61) ^ (m[i - 2] >> 6);
        m[i] = m[i - 16] + s0 + m[i - 7] + s1;
    }
    uint64_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2], d = ctx->state[3];
    uint64_t e = ctx->state[4], f = ctx->state[5], g = ctx->state[6], h = ctx->state[7];
    for (int i = 0; i < 80; ++i) {
        uint64_t S1 = DOG_ROTR64(e, 14) ^ DOG_ROTR64(e, 18) ^ DOG_ROTR64(e, 41);
        uint64_t ch = (e & f) ^ ((~e) & g);
        uint64_t temp1 = h + S1 + ch + k[i] + m[i];
        uint64_t S0 = DOG_ROTR64(a, 28) ^ DOG_ROTR64(a, 34) ^ DOG_ROTR64(a, 39);
        uint64_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint64_t temp2 = S0 + maj;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c; ctx->state[3] += d;
    ctx->state[4] += e; ctx->state[5] += f; ctx->state[6] += g; ctx->state[7] += h;
}

static void dataog_sha512_init(dataog_sha512_ctx *ctx) {
    ctx->datalen = 0;
    ctx->bitlen_lo = 0;
    ctx->bitlen_hi = 0;
    ctx->state[0] = 0x6a09e667f3bcc908ULL; ctx->state[1] = 0xbb67ae8584caa73bULL;
    ctx->state[2] = 0x3c6ef372fe94f82bULL; ctx->state[3] = 0xa54ff53a5f1d36f1ULL;
    ctx->state[4] = 0x510e527fade682d1ULL; ctx->state[5] = 0x9b05688c2b3e6c1fULL;
    ctx->state[6] = 0x1f83d9abfb41bd6bULL; ctx->state[7] = 0x5be0cd19137e2179ULL;
}

static void dataog_sha512_add_bits(dataog_sha512_ctx *ctx, uint64_t bits) {
    uint64_t old = ctx->bitlen_lo;
    ctx->bitlen_lo += bits;
    if (ctx->bitlen_lo < old) {
        ctx->bitlen_hi += 1ULL;
    }
}

static void dataog_sha512_update(dataog_sha512_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; ++i) {
        ctx->data[ctx->datalen++] = data[i];
        if (ctx->datalen == 128u) {
            dataog_sha512_transform(ctx, ctx->data);
            dataog_sha512_add_bits(ctx, 1024ULL);
            ctx->datalen = 0;
        }
    }
}

static void dataog_sha512_final(dataog_sha512_ctx *ctx, uint8_t hash[64]) {
    size_t i = ctx->datalen;
    dataog_sha512_add_bits(ctx, (uint64_t)ctx->datalen * 8ULL);
    if (ctx->datalen < 112u) {
        ctx->data[i++] = 0x80u;
        while (i < 112u) ctx->data[i++] = 0x00u;
    } else {
        ctx->data[i++] = 0x80u;
        while (i < 128u) ctx->data[i++] = 0x00u;
        dataog_sha512_transform(ctx, ctx->data);
        memset(ctx->data, 0, 112u);
    }
    dataog_store_be64(ctx->data + 112, ctx->bitlen_hi);
    dataog_store_be64(ctx->data + 120, ctx->bitlen_lo);
    dataog_sha512_transform(ctx, ctx->data);
    for (int j = 0; j < 8; ++j) {
        dataog_store_be64(hash + j * 8, ctx->state[j]);
    }
}

static void dataog_sha512(const uint8_t *data, size_t len, uint8_t out[64]) {
    dataog_sha512_ctx ctx;
    dataog_sha512_init(&ctx);
    dataog_sha512_update(&ctx, data, len);
    dataog_sha512_final(&ctx, out);
    dataog_secure_zero(&ctx, sizeof(ctx));
}

/* =========================================================================
 * 3. RFC 8032 Ed25519 (TweetNaCl 16-limb GF(2^255-19) field arithmetic)
 * ========================================================================= */

typedef int64_t dog_gf[16];

static const dog_gf DOG_GF0 = {0};
static const dog_gf DOG_GF1 = {1};
static const dog_gf DOG_D = {
    0x78a3, 0x1359, 0x4dca, 0x75eb, 0xd8ab, 0x4141, 0x0a4d, 0x0070,
    0xe898, 0x7779, 0x4079, 0x8cc7, 0xfe73, 0x2b6f, 0x6cee, 0x5203
};
static const dog_gf DOG_D2 = {
    0xf159, 0x26b2, 0x9b94, 0xebd6, 0xb156, 0x8283, 0x149a, 0x00e0,
    0xd130, 0xeef3, 0x80f2, 0x198e, 0xfce7, 0x56df, 0xd9dc, 0x2406
};
static const dog_gf DOG_X = {
    0xd51a, 0x8f25, 0x2d60, 0xc956, 0xa7b2, 0x9525, 0xc760, 0x692c,
    0xdc5c, 0xfdd6, 0xe231, 0xc0a4, 0x53fe, 0xcd6e, 0x36d3, 0x2169
};
static const dog_gf DOG_Y = {
    0x6658, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666,
    0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666, 0x6666
};
static const dog_gf DOG_I = {
    0xa0b0, 0x4a0e, 0x1b27, 0xc4ee, 0xe478, 0xad2f, 0x1806, 0x2f43,
    0xd7a7, 0x3dfb, 0x0099, 0x2b4d, 0xdf0b, 0x4fc1, 0x2480, 0x2b83
};

static void dog_set25519(dog_gf r, const dog_gf a) {
    for (int i = 0; i < 16; ++i) r[i] = a[i];
}

static void dog_car25519(dog_gf o) {
    for (int i = 0; i < 16; ++i) {
        o[i] += (1LL << 16);
        int64_t c = o[i] >> 16;
        o[(i + 1) * (i < 15)] += c - 1 + 37 * (c - 1) * (i == 15);
        o[i] -= c << 16;
    }
}

static void dog_sel25519(dog_gf p, dog_gf q, int b) {
    int64_t c = ~(int64_t)(b - 1);
    for (int i = 0; i < 16; ++i) {
        int64_t t = c & (p[i] ^ q[i]);
        p[i] ^= t;
        q[i] ^= t;
    }
}

static void dog_A(dog_gf o, const dog_gf a, const dog_gf b) {
    for (int i = 0; i < 16; ++i) o[i] = a[i] + b[i];
}

static void dog_Z(dog_gf o, const dog_gf a, const dog_gf b) {
    for (int i = 0; i < 16; ++i) o[i] = a[i] - b[i];
}

static void dog_M(dog_gf o, const dog_gf a, const dog_gf b) {
    int64_t t[31];
    for (int i = 0; i < 31; ++i) t[i] = 0;
    for (int i = 0; i < 16; ++i) {
        for (int j = 0; j < 16; ++j) {
            t[i + j] += a[i] * b[j];
        }
    }
    for (int i = 0; i < 15; ++i) t[i] += 38 * t[i + 16];
    for (int i = 0; i < 16; ++i) o[i] = t[i];
    dog_car25519(o);
    dog_car25519(o);
}

static void dog_S(dog_gf o, const dog_gf a) {
    dog_M(o, a, a);
}

static void dog_inv25519(dog_gf o, const dog_gf i) {
    dog_gf c;
    dog_set25519(c, i);
    for (int a = 253; a >= 0; --a) {
        dog_S(c, c);
        if (a != 2 && a != 4) dog_M(c, c, i);
    }
    dog_set25519(o, c);
}

static void dog_pow2523(dog_gf o, const dog_gf i) {
    dog_gf c;
    dog_set25519(c, i);
    for (int a = 250; a >= 0; --a) {
        dog_S(c, c);
        if (a != 1) dog_M(c, c, i);
    }
    dog_set25519(o, c);
}

static void dog_pack25519(uint8_t o[32], const dog_gf n) {
    dog_gf t, m;
    dog_set25519(t, n);
    dog_car25519(t);
    dog_car25519(t);
    dog_car25519(t);
    for (int j = 0; j < 2; ++j) {
        m[0] = t[0] - 0xffed;
        for (int i = 1; i < 15; ++i) {
            m[i] = t[i] - 0xffff - ((m[i - 1] >> 16) & 1);
            m[i - 1] &= 0xffff;
        }
        m[15] = t[15] - 0x7fff - ((m[14] >> 16) & 1);
        int b = (int)((m[15] >> 16) & 1);
        m[14] &= 0xffff;
        dog_sel25519(t, m, 1 - b);
    }
    for (int i = 0; i < 16; ++i) {
        o[2 * i] = (uint8_t)(t[i] & 0xff);
        o[2 * i + 1] = (uint8_t)(t[i] >> 8);
    }
}

static void dog_unpack25519(dog_gf o, const uint8_t n[32]) {
    for (int i = 0; i < 16; ++i) {
        o[i] = (int64_t)n[2 * i] + ((int64_t)n[2 * i + 1] << 8);
    }
    o[15] &= 0x7fff;
}

static int dog_neq25519(const dog_gf a, const dog_gf b) {
    uint8_t c[32], d[32];
    dog_pack25519(c, a);
    dog_pack25519(d, b);
    uint8_t diff = 0;
    for (int i = 0; i < 32; ++i) diff |= (c[i] ^ d[i]);
    return diff != 0;
}

static int dog_par25519(const dog_gf a) {
    uint8_t d[32];
    dog_pack25519(d, a);
    return (int)(d[0] & 1u);
}

static void dog_cswap(dog_gf p[4], dog_gf q[4], uint8_t b) {
    for (int i = 0; i < 4; ++i) dog_sel25519(p[i], q[i], (int)b);
}

static void dog_add_pt(dog_gf p[4], dog_gf q[4]) {
    dog_gf a, b, c, d, t, e, f, g, h;
    dog_Z(a, p[1], p[0]);
    dog_Z(t, q[1], q[0]);
    dog_M(a, a, t);
    dog_A(b, p[0], p[1]);
    dog_A(t, q[0], q[1]);
    dog_M(b, b, t);
    dog_M(c, p[3], q[3]);
    dog_M(c, c, DOG_D2);
    dog_M(d, p[2], q[2]);
    dog_A(d, d, d);
    dog_Z(e, b, a);
    dog_Z(f, d, c);
    dog_A(g, d, c);
    dog_A(h, b, a);
    dog_M(p[0], e, f);
    dog_M(p[1], h, g);
    dog_M(p[2], g, f);
    dog_M(p[3], e, h);
}

static void dog_scalarmult(dog_gf p[4], dog_gf q[4], const uint8_t s[32]) {
    dog_set25519(p[0], DOG_GF0);
    dog_set25519(p[1], DOG_GF1);
    dog_set25519(p[2], DOG_GF1);
    dog_set25519(p[3], DOG_GF0);
    for (int i = 255; i >= 0; --i) {
        uint8_t b = (uint8_t)((s[i / 8] >> (i & 7)) & 1u);
        dog_cswap(p, q, b);
        dog_add_pt(q, p);
        dog_add_pt(p, p);
        dog_cswap(p, q, b);
    }
}

static void dog_scalarbase(dog_gf p[4], const uint8_t s[32]) {
    dog_gf q[4];
    dog_set25519(q[0], DOG_X);
    dog_set25519(q[1], DOG_Y);
    dog_set25519(q[2], DOG_GF1);
    dog_M(q[3], DOG_X, DOG_Y);
    dog_scalarmult(p, q, s);
}

static void dog_pack_pt(uint8_t r[32], dog_gf p[4]) {
    dog_gf tx, ty, zi;
    dog_inv25519(zi, p[2]);
    dog_M(tx, p[0], zi);
    dog_M(ty, p[1], zi);
    dog_pack25519(r, ty);
    r[31] ^= (uint8_t)(dog_par25519(tx) << 7);
}

static int dog_unpackneg(dog_gf r[4], const uint8_t p[32]) {
    dog_gf t, chk, num, den, den2, den4, den6;
    dog_set25519(r[2], DOG_GF1);
    dog_unpack25519(r[1], p);
    dog_S(num, r[1]);
    dog_M(den, num, DOG_D);
    dog_Z(num, num, r[2]);
    dog_A(den, r[2], den);
    dog_S(den2, den);
    dog_S(den4, den2);
    dog_M(den6, den4, den2);
    dog_M(t, den6, num);
    dog_M(t, t, den);
    dog_pow2523(t, t);
    dog_M(t, t, num);
    dog_M(t, t, den);
    dog_M(t, t, den);
    dog_M(r[0], t, den);
    dog_S(chk, r[0]);
    dog_M(chk, chk, den);
    if (dog_neq25519(chk, num)) dog_M(r[0], r[0], DOG_I);
    dog_S(chk, r[0]);
    dog_M(chk, chk, den);
    if (dog_neq25519(chk, num)) return -1;
    if (dog_par25519(r[0]) == (p[31] >> 7)) dog_Z(r[0], DOG_GF0, r[0]);
    dog_M(r[3], r[0], r[1]);
    return 0;
}

static const uint8_t DOG_ED25519_L[32] = {
    0xed, 0xd3, 0xf5, 0x5c, 0x1a, 0x63, 0x12, 0x58,
    0xd6, 0x9c, 0xf7, 0xa2, 0xde, 0xf9, 0xde, 0x14,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10
};

static void dog_modL(uint8_t r[32], int64_t x[64]) {
    int64_t carry;
    for (int i = 63; i >= 32; --i) {
        carry = 0;
        int j;
        for (j = i - 32; j < i - 12; ++j) {
            x[j] += carry - 16 * x[i] * (int64_t)DOG_ED25519_L[j - (i - 32)];
            carry = (x[j] + 128) >> 8;
            x[j] -= carry << 8;
        }
        x[j] += carry;
        x[i] = 0;
    }
    carry = 0;
    for (int j = 0; j < 32; ++j) {
        x[j] += carry - (x[31] >> 4) * (int64_t)DOG_ED25519_L[j];
        carry = x[j] >> 8;
        x[j] &= 255;
    }
    for (int j = 0; j < 32; ++j) {
        x[j] -= carry * (int64_t)DOG_ED25519_L[j];
    }
    for (int i = 0; i < 32; ++i) {
        x[i + 1] += x[i] >> 8;
        r[i] = (uint8_t)(x[i] & 255);
    }
}

static void dog_reduce(uint8_t r[64]) {
    int64_t x[64];
    for (int i = 0; i < 64; ++i) x[i] = (int64_t)r[i];
    for (int i = 0; i < 64; ++i) r[i] = 0;
    dog_modL(r, x);
}

static int dog_is_canonical_scalar(const uint8_t s[32]) {
    for (int i = 31; i >= 0; --i) {
        if (s[i] < DOG_ED25519_L[i]) return 1;
        if (s[i] > DOG_ED25519_L[i]) return 0;
    }
    return 0; /* s == L is non-canonical */
}

static void dataog_ed25519_pubkey(const uint8_t seed[32], uint8_t pk[32]) {
    uint8_t d[64];
    dog_gf p[4];
    dataog_sha512(seed, 32u, d);
    d[0] &= 248u;
    d[31] &= 127u;
    d[31] |= 64u;
    dog_scalarbase(p, d);
    dog_pack_pt(pk, p);
    dataog_secure_zero(d, sizeof(d));
}

static void dataog_ed25519_sign(
    const uint8_t *msg,
    size_t msg_len,
    const uint8_t seed[32],
    const uint8_t pk[32],
    uint8_t sig[64]
) {
    uint8_t d[64], r_hash[64], hram[64];
    dog_gf p[4];
    dataog_sha512_ctx hctx;

    dataog_sha512(seed, 32u, d);
    d[0] &= 248u;
    d[31] &= 127u;
    d[31] |= 64u;

    /* r = SHA512(prefix || msg) */
    dataog_sha512_init(&hctx);
    dataog_sha512_update(&hctx, d + 32, 32u);
    dataog_sha512_update(&hctx, msg, msg_len);
    dataog_sha512_final(&hctx, r_hash);
    dog_reduce(r_hash);

    dog_scalarbase(p, r_hash);
    dog_pack_pt(sig, p);

    /* k = SHA512(R || pk || msg) */
    dataog_sha512_init(&hctx);
    dataog_sha512_update(&hctx, sig, 32u);
    dataog_sha512_update(&hctx, pk, 32u);
    dataog_sha512_update(&hctx, msg, msg_len);
    dataog_sha512_final(&hctx, hram);
    dog_reduce(hram);

    int64_t x[64];
    for (int i = 0; i < 64; ++i) x[i] = 0;
    for (int i = 0; i < 32; ++i) x[i] = (int64_t)r_hash[i];
    for (int i = 0; i < 32; ++i) {
        for (int j = 0; j < 32; ++j) {
            x[i + j] += (int64_t)hram[i] * (int64_t)d[j];
        }
    }
    dog_modL(sig + 32, x);

    dataog_secure_zero(d, sizeof(d));
    dataog_secure_zero(r_hash, sizeof(r_hash));
    dataog_secure_zero(hram, sizeof(hram));
    dataog_secure_zero(x, sizeof(x));
    dataog_secure_zero(&hctx, sizeof(hctx));
}

static int dataog_ed25519_verify(
    const uint8_t *msg,
    size_t msg_len,
    const uint8_t sig[64],
    const uint8_t pk[32]
) {
    if (!dog_is_canonical_scalar(sig + 32)) {
        return 0;
    }
    dog_gf p[4], q[4];
    if (dog_unpackneg(q, pk) != 0) {
        return 0;
    }
    uint8_t h[64], t[32];
    dataog_sha512_ctx hctx;
    dataog_sha512_init(&hctx);
    dataog_sha512_update(&hctx, sig, 32u);
    dataog_sha512_update(&hctx, pk, 32u);
    dataog_sha512_update(&hctx, msg, msg_len);
    dataog_sha512_final(&hctx, h);
    dog_reduce(h);

    dog_scalarmult(p, q, h);
    dog_scalarbase(q, sig + 32);
    dog_add_pt(p, q);
    dog_pack_pt(t, p);

    uint8_t diff = 0;
    for (int i = 0; i < 32; ++i) {
        diff |= (uint8_t)(t[i] ^ sig[i]);
    }
    return diff == 0;
}

/* =========================================================================
 * 4. Base32 (No Padding) & CRC16-CCITT
 * ========================================================================= */

static const char DATAOG_B32_ALPHABET[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

static int dataog_b32_encode(const uint8_t *in, size_t in_len, char *out, size_t out_cap) {
    size_t out_pos = 0;
    uint32_t buf = 0;
    int bits = 0;
    for (size_t i = 0; i < in_len; ++i) {
        buf = (buf << 8) | (uint32_t)in[i];
        bits += 8;
        while (bits >= 5) {
            bits -= 5;
            if (out_pos + 1 >= out_cap) return -1;
            out[out_pos++] = DATAOG_B32_ALPHABET[(buf >> bits) & 0x1fu];
        }
    }
    if (bits > 0) {
        if (out_pos + 1 >= out_cap) return -1;
        out[out_pos++] = DATAOG_B32_ALPHABET[(buf << (5 - bits)) & 0x1fu];
    }
    if (out_pos >= out_cap) return -1;
    out[out_pos] = '\0';
    return (int)out_pos;
}

static int dataog_b32_char_val(char ch) {
    unsigned char uc = (unsigned char)toupper((unsigned char)ch);
    if (uc >= 'A' && uc <= 'Z') return (int)(uc - 'A');
    if (uc >= '2' && uc <= '7') return (int)(uc - '2' + 26);
    return -1;
}

static int dataog_b32_decode(const char *in, uint8_t *out, size_t max_out) {
    uint32_t buf = 0;
    int bits = 0;
    size_t out_pos = 0;
    for (size_t i = 0; in[i] != '\0'; ++i) {
        char ch = in[i];
        if (ch == '-' || ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n' || ch == '=') {
            continue;
        }
        int val = dataog_b32_char_val(ch);
        if (val < 0) return -1;
        buf = (buf << 5) | (uint32_t)val;
        bits += 5;
        if (bits >= 8) {
            bits -= 8;
            if (out_pos >= max_out) return -1;
            out[out_pos++] = (uint8_t)((buf >> bits) & 0xffu);
        }
    }
    return (int)out_pos;
}

static uint16_t dataog_crc16_ccitt(const uint8_t *data, size_t len) {
    uint16_t crc = 0xffffu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; ++b) {
            if (crc & 0x8000u) crc = (uint16_t)((crc << 1) ^ 0x1021u);
            else crc <<= 1;
        }
    }
    return crc;
}

/* =========================================================================
 * 5. Gregorian Date <-> Epoch Day (Epoch 2025-01-01 = day 0)
 * ========================================================================= */

static int64_t dataog_days_from_civil(int y, int m, int d) {
    y -= m <= 2;
    int64_t era = (y >= 0 ? y : y - 399) / 400;
    int64_t yoe = (int64_t)(y - era * 400);
    int64_t doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

static void dataog_civil_from_days(int64_t z, int *y, int *m, int *d) {
    z += 719468;
    int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    int64_t doe = z - era * 146097;
    int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    int64_t y_val = yoe + era * 400;
    int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    int64_t mp = (5 * doy + 2) / 153;
    int64_t d_val = doy - (153 * mp + 2) / 5 + 1;
    int64_t m_val = mp + (mp < 10 ? 3 : -9);
    y_val += (m_val <= 2);
    *y = (int)y_val;
    *m = (int)m_val;
    *d = (int)d_val;
}

#define DATAOG_EPOCH_UNIX_DAYS 20089LL /* 2025-01-01 */

static uint16_t dataog_date_to_epoch_day(int y, int m, int d) {
    int64_t diff = dataog_days_from_civil(y, m, d) - DATAOG_EPOCH_UNIX_DAYS;
    if (diff < 0) return 0u;
    if (diff > 65535LL) return 65535u;
    return (uint16_t)diff;
}

static void dataog_epoch_day_to_str(uint16_t epoch_day, char out[16]) {
    int y = 2025, m = 1, d = 1;
    dataog_civil_from_days(DATAOG_EPOCH_UNIX_DAYS + (int64_t)epoch_day, &y, &m, &d);
    snprintf(out, 16, "%04d-%02d-%02d", y, m, d);
}

static uint16_t dataog_today_epoch_day(void) {
    time_t now = time(NULL);
    struct tm tmv;
#ifdef _WIN32
    localtime_s(&tmv, &now);
#else
    localtime_r(&now, &tmv);
#endif
    return dataog_date_to_epoch_day(tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday);
}

static int dataog_parse_date_str(const char *s, int *out_y, int *out_m, int *out_d) {
    int y = 0, m = 0, d = 0;
    if (!s || sscanf(s, "%d-%d-%d", &y, &m, &d) != 3) return 0;
    if (y < 2025 || y > 2199 || m < 1 || m > 12 || d < 1 || d > 31) return 0;
    static const int mdays[12] = {31,28,31,30,31,30,31,31,30,31,30,31};
    int maxd = mdays[m - 1];
    if (m == 2 && ((y % 4 == 0 && y % 100 != 0) || (y % 400 == 0))) maxd = 29;
    if (d > maxd) return 0;
    *out_y = y; *out_m = m; *out_d = d;
    return 1;
}

/* =========================================================================
 * 6. Offline Machine Code & Local IPv4 Discovery
 * ========================================================================= */

static void dataog_trim_upper_inplace(char *s) {
    size_t len = strlen(s);
    size_t start = 0;
    while (start < len && isspace((unsigned char)s[start])) start++;
    while (len > start && isspace((unsigned char)s[len - 1])) len--;
    size_t n = len - start;
    if (start > 0 && n > 0) memmove(s, s + start, n);
    s[n] = '\0';
    for (size_t i = 0; i < n; ++i) {
        s[i] = (char)toupper((unsigned char)s[i]);
    }
}

static void dataog_read_raw_machine_id(char *out, size_t cap) {
    if (!out || cap == 0u) return;
    out[0] = '\0';
#ifdef _WIN32
    {
        const REGSAM access_modes[2] = {
            (REGSAM)(KEY_READ | KEY_WOW64_64KEY),
            (REGSAM)KEY_READ
        };
        for (int idx = 0; idx < 2; ++idx) {
            HKEY hKey = NULL;
            if (RegOpenKeyExA(
                    HKEY_LOCAL_MACHINE,
                    "SOFTWARE\\Microsoft\\Cryptography",
                    0,
                    access_modes[idx],
                    &hKey) == ERROR_SUCCESS && hKey != NULL) {
                DWORD dwType = 0;
                DWORD dwSize = (DWORD)(cap - 1u);
                LONG qrc = RegQueryValueExA(
                    hKey,
                    "MachineGuid",
                    NULL,
                    &dwType,
                    (LPBYTE)out,
                    &dwSize
                );
                RegCloseKey(hKey);
                if (qrc == ERROR_SUCCESS && (dwType == REG_SZ || dwType == REG_EXPAND_SZ)) {
                    out[cap - 1u] = '\0';
                    if ((size_t)dwSize < cap) {
                        out[dwSize] = '\0';
                    }
                    dataog_trim_upper_inplace(out);
                    if (out[0] != '\0') {
                        return;
                    }
                }
                out[0] = '\0';
            }
        }
    }
#elif defined(__APPLE__)
    FILE *fp = popen("ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null", "r");
    if (fp) {
        char line[512];
        while (fgets(line, sizeof(line), fp) != NULL) {
            if (strstr(line, "IOPlatformUUID") != NULL) {
                char *eq = strchr(line, '=');
                if (eq) {
                    char *q1 = strchr(eq, '"');
                    if (q1) {
                        char *q2 = strchr(q1 + 1, '"');
                        if (q2 && (size_t)(q2 - q1 - 1) < cap) {
                            size_t n = (size_t)(q2 - q1 - 1);
                            memcpy(out, q1 + 1, n);
                            out[n] = '\0';
                            dataog_trim_upper_inplace(out);
                            break;
                        }
                    }
                }
            }
        }
        pclose(fp);
        if (out[0] != '\0') return;
    }
    fp = popen("sysctl -n kern.uuid 2>/dev/null", "r");
    if (fp) {
        if (fgets(out, (int)cap, fp) != NULL) {
            dataog_trim_upper_inplace(out);
        }
        pclose(fp);
        if (out[0] != '\0') return;
    }
#else
    const char *paths[2] = {"/etc/machine-id", "/var/lib/dbus/machine-id"};
    for (int i = 0; i < 2; ++i) {
        FILE *fp = fopen(paths[i], "r");
        if (fp) {
            if (fgets(out, (int)cap, fp) != NULL) {
                dataog_trim_upper_inplace(out);
            }
            fclose(fp);
            if (out[0] != '\0') return;
        }
    }
#endif
    {
#ifdef _WIN32
        WSADATA wsa_mc;
        int wsa_mc_ok = (WSAStartup(MAKEWORD(2, 2), &wsa_mc) == 0);
#endif
        char host[128] = {0};
        if (gethostname(host, sizeof(host) - 1) == 0 && host[0] != '\0') {
            snprintf(out, cap, "HOST-%s", host);
            dataog_trim_upper_inplace(out);
        } else {
            snprintf(out, cap, "DATAOG-OFFLINE-NODE");
        }
#ifdef _WIN32
        if (wsa_mc_ok) {
            WSACleanup();
        }
#endif
    }
}

static void dataog_get_machine_code(char out_mc[32]) {
    char raw_id[256] = {0};
    dataog_read_raw_machine_id(raw_id, sizeof(raw_id));

    char msg[320] = {0};
    snprintf(msg, sizeof(msg), "DataOG-MachineCode-v1:%s", raw_id);
    uint8_t digest[32];
    dataog_sha256((const uint8_t *)msg, strlen(msg), digest);

    char b32[32] = {0};
    dataog_b32_encode(digest, 10u, b32, sizeof(b32)); /* 16 base32 chars */
    snprintf(out_mc, 32, "MCH-%.4s-%.4s-%.4s-%.4s", b32, b32 + 4, b32 + 8, b32 + 12);
}

static int dataog_is_virtual_ifname(const char *name) {
    if (!name) return 0;
    const char *prefixes[] = {
        "lo", "utun", "awdl", "llw", "anpi", "bridge", "ap", "docker",
        "veth", "br-", "virbr", "vmnet", "vboxnet", "tun", "tap", "tailscale",
        "wg", "zt", "ham", "gif", "stf", NULL
    };
    char lower[128];
    size_t i = 0;
    for (; name[i] && i < sizeof(lower) - 1; ++i) {
        lower[i] = (char)tolower((unsigned char)name[i]);
    }
    lower[i] = '\0';
    for (int k = 0; prefixes[k] != NULL; ++k) {
        size_t plen = strlen(prefixes[k]);
        if (strncmp(lower, prefixes[k], plen) == 0) return 1;
    }
    /* Distinguish host-only virtual switches (vEthernet / VMnet / VirtualBox Host-Only / WSL)
     * from guest primary NICs inside enterprise Hyper-V / Azure / VMware VMs. */
    if (strstr(lower, "loopback") || strstr(lower, "vmnet") ||
        strstr(lower, "vboxnet") || strstr(lower, "host-only") ||
        strstr(lower, "virtual ethernet") || strstr(lower, "vethernet") ||
        strstr(lower, "default switch") || strstr(lower, "wsl") ||
        strstr(lower, "pseudo")) {
        return 1;
    }
    return 0;
}

static int dataog_is_usable_ipv4(const char *ip, uint32_t addr_host_order) {
    (void)ip;
    uint8_t b0 = (uint8_t)((addr_host_order >> 24) & 0xffu);
    uint8_t b1 = (uint8_t)((addr_host_order >> 16) & 0xffu);
    if (b0 == 0u || b0 == 127u) return 0;
    if (b0 == 169u && b1 == 254u) return 0;
    if (b0 == 198u && (b1 == 18u || b1 == 19u)) return 0; /* 过滤代理 TUN 虚拟地址 */
    if (b0 >= 224u) return 0;
    return 1;
}

static int dataog_ip_priority(uint32_t addr_host_order) {
    uint8_t b0 = (uint8_t)((addr_host_order >> 24) & 0xffu);
    uint8_t b1 = (uint8_t)((addr_host_order >> 16) & 0xffu);
    if (b0 == 192u && b1 == 168u) return 0;
    if (b0 == 10u) return 1;
    if (b0 == 172u && b1 >= 16u && b1 <= 31u) return 2;
    return 3;
}

typedef struct {
    char ip[24];
    uint32_t addr;
    int is_virtual;
    int prio;
} dataog_ip_entry_t;

static void dataog_add_ip_entry(
    dataog_ip_entry_t items[32],
    int *count,
    const char *ip_str,
    uint32_t host_addr,
    int is_virtual
) {
    if (!dataog_is_usable_ipv4(ip_str, host_addr)) return;
    for (int i = 0; i < *count; ++i) {
        if (strcmp(items[i].ip, ip_str) == 0) {
            if (is_virtual < items[i].is_virtual) {
                items[i].is_virtual = is_virtual;
            }
            return;
        }
    }
    if (*count < 32) {
        snprintf(items[*count].ip, sizeof(items[*count].ip), "%s", ip_str);
        items[*count].addr = host_addr;
        items[*count].is_virtual = is_virtual;
        items[*count].prio = dataog_ip_priority(host_addr);
        (*count)++;
    }
}

static void dataog_udp_route_probe_ips(dataog_ip_entry_t items[32], int *count) {
    static const uint32_t probe_targets[4] = {
        0xc0a80101u, /* 192.168.1.1 */
        0x0a000001u, /* 10.0.0.1    */
        0xac100001u, /* 172.16.0.1  */
        0x08080808u  /* 8.8.8.8     */
    };
    for (int k = 0; k < 4; ++k) {
#ifdef _WIN32
        SOCKET sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (sock == INVALID_SOCKET) continue;
#else
        int sock = socket(AF_INET, SOCK_DGRAM, 0);
        if (sock < 0) continue;
#endif
        struct sockaddr_in dst;
        memset(&dst, 0, sizeof(dst));
        dst.sin_family = AF_INET;
        dst.sin_port = htons(9u);
        dst.sin_addr.s_addr = htonl(probe_targets[k]);
        if (connect(sock, (const struct sockaddr *)&dst, (int)sizeof(dst)) == 0) {
            struct sockaddr_in local_sin;
            memset(&local_sin, 0, sizeof(local_sin));
#ifdef _WIN32
            int slen = (int)sizeof(local_sin);
#else
            socklen_t slen = (socklen_t)sizeof(local_sin);
#endif
            if (getsockname(sock, (struct sockaddr *)&local_sin, &slen) == 0 &&
                local_sin.sin_family == AF_INET) {
                uint32_t h_addr = ntohl((uint32_t)local_sin.sin_addr.s_addr);
                char ip_buf[24] = {0};
                snprintf(
                    ip_buf,
                    sizeof(ip_buf),
                    "%u.%u.%u.%u",
                    (unsigned)((h_addr >> 24) & 0xffu),
                    (unsigned)((h_addr >> 16) & 0xffu),
                    (unsigned)((h_addr >> 8) & 0xffu),
                    (unsigned)(h_addr & 0xffu)
                );
                dataog_add_ip_entry(items, count, ip_buf, h_addr, 0);
            }
        }
#ifdef _WIN32
        closesocket(sock);
#else
        close(sock);
#endif
    }
}

static void dataog_collect_local_ips(char *out_primary, size_t p_cap, char *out_csv, size_t csv_cap) {
    dataog_ip_entry_t items[32];
    int count = 0;

#ifdef _WIN32
    WSADATA wsa_ip;
    int wsa_ip_ok = (WSAStartup(MAKEWORD(2, 2), &wsa_ip) == 0);
    ULONG buf_len = 16384;
    PIP_ADAPTER_INFO adapter_info = (PIP_ADAPTER_INFO)malloc(buf_len);
    if (adapter_info) {
        if (GetAdaptersInfo(adapter_info, &buf_len) == ERROR_BUFFER_OVERFLOW) {
            free(adapter_info);
            adapter_info = (PIP_ADAPTER_INFO)malloc(buf_len);
        }
        if (adapter_info && GetAdaptersInfo(adapter_info, &buf_len) == NO_ERROR) {
            for (PIP_ADAPTER_INFO cur = adapter_info; cur != NULL; cur = cur->Next) {
                if (cur->Type == MIB_IF_TYPE_LOOPBACK) continue;
                int is_virt = (
                    dataog_is_virtual_ifname(cur->Description) ||
                    dataog_is_virtual_ifname(cur->AdapterName)
                ) ? 1 : 0;
                for (PIP_ADDR_STRING addr = &cur->IpAddressList; addr != NULL; addr = addr->Next) {
                    const char *ip_s = addr->IpAddress.String;
                    if (ip_s && ip_s[0] && strcmp(ip_s, "0.0.0.0") != 0) {
                        unsigned long raw_in = inet_addr(ip_s);
                        if (raw_in != INADDR_NONE) {
                            dataog_add_ip_entry(items, &count, ip_s, ntohl((uint32_t)raw_in), is_virt);
                        }
                    }
                }
            }
        }
        free(adapter_info);
    }
    if (count == 0) {
        dataog_udp_route_probe_ips(items, &count);
    }
    if (wsa_ip_ok) {
        WSACleanup();
    }
#else
    struct ifaddrs *ifaddr = NULL;
    if (getifaddrs(&ifaddr) == 0) {
        for (struct ifaddrs *ifa = ifaddr; ifa != NULL; ifa = ifa->ifa_next) {
            if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET) continue;
            if ((ifa->ifa_flags & IFF_LOOPBACK) != 0) continue;
            if ((ifa->ifa_flags & IFF_UP) == 0) continue;
            int is_virt = dataog_is_virtual_ifname(ifa->ifa_name) ? 1 : 0;
            const struct sockaddr_in *sin = (const struct sockaddr_in *)ifa->ifa_addr;
            char ip_buf[INET_ADDRSTRLEN] = {0};
            if (inet_ntop(AF_INET, &sin->sin_addr, ip_buf, sizeof(ip_buf)) != NULL) {
                dataog_add_ip_entry(items, &count, ip_buf, ntohl(sin->sin_addr.s_addr), is_virt);
            }
        }
        freeifaddrs(ifaddr);
    }
    if (count == 0) {
        dataog_udp_route_probe_ips(items, &count);
    }
#endif

    /* Sort physical NICs first (is_virtual == 0), then by prio, then lexical ip */
    for (int i = 0; i < count; ++i) {
        for (int j = i + 1; j < count; ++j) {
            int swap_needed = 0;
            if (items[j].is_virtual < items[i].is_virtual) {
                swap_needed = 1;
            } else if (items[j].is_virtual == items[i].is_virtual) {
                if (items[j].prio < items[i].prio ||
                    (items[j].prio == items[i].prio && strcmp(items[j].ip, items[i].ip) < 0)) {
                    swap_needed = 1;
                }
            }
            if (swap_needed) {
                dataog_ip_entry_t tmp = items[i];
                items[i] = items[j];
                items[j] = tmp;
            }
        }
    }

    if (count == 0) {
        snprintf(out_primary, p_cap, "127.0.0.1");
        snprintf(out_csv, csv_cap, "127.0.0.1");
        return;
    }
    snprintf(out_primary, p_cap, "%s", items[0].ip);
    out_csv[0] = '\0';
    size_t pos = 0;
    for (int i = 0; i < count; ++i) {
        size_t need = strlen(items[i].ip) + (i > 0 ? 1u : 0u);
        if (pos + need + 1 >= csv_cap) break;
        if (i > 0) out_csv[pos++] = ',';
        memcpy(out_csv + pos, items[i].ip, strlen(items[i].ip));
        pos += strlen(items[i].ip);
        out_csv[pos] = '\0';
    }
}

/* =========================================================================
 * 7. License Tags, Request Code (REQ1-...) & Token (DOG1-...) Codec
 * ========================================================================= */

static void dataog_compute_machine_tag(const char *machine_code, uint8_t out_tag[6]) {
    char norm[64] = {0};
    size_t j = 0;
    for (size_t i = 0; machine_code && machine_code[i] && j + 1 < sizeof(norm); ++i) {
        char ch = machine_code[i];
        if (!isspace((unsigned char)ch)) {
            norm[j++] = (char)toupper((unsigned char)ch);
        }
    }
    norm[j] = '\0';

    char buf[96] = {0};
    snprintf(buf, sizeof(buf), "DataOG-MachineTag-v1:%s", norm);
    uint8_t digest[32];
    dataog_sha256((const uint8_t *)buf, strlen(buf), digest);
    memcpy(out_tag, digest, 6u);
}

static void dataog_compute_ip_tag(const char *ip_str, uint8_t out_tag[4]) {
    char norm[64] = {0};
    size_t j = 0;
    for (size_t i = 0; ip_str && ip_str[i] && j + 1 < sizeof(norm); ++i) {
        char ch = ip_str[i];
        if (!isspace((unsigned char)ch)) {
            norm[j++] = ch;
        }
    }
    norm[j] = '\0';

    char buf[96] = {0};
    snprintf(buf, sizeof(buf), "DataOG-IpTag-v1:%s", norm);
    uint8_t digest[32];
    dataog_sha256((const uint8_t *)buf, strlen(buf), digest);
    memcpy(out_tag, digest, 4u);
}

static void dataog_build_payload(
    uint8_t scope,
    uint16_t duration_days,
    uint16_t expiry_day,
    const char *machine_code,
    const char *ip_str,
    uint8_t out_payload[16]
) {
    out_payload[0] = (uint8_t)DATAOG_LICENSE_VERSION;
    out_payload[1] = scope;
    dataog_store_be16(out_payload + 2, duration_days);
    dataog_store_be16(out_payload + 4, expiry_day);
    if (scope == DATAOG_SCOPE_GLOBAL) {
        memset(out_payload + 6, 0, 10u);
    } else {
        dataog_compute_machine_tag(machine_code, out_payload + 6);
        dataog_compute_ip_tag(ip_str, out_payload + 12);
    }
}

static int dataog_format_dog1_token(
    const uint8_t payload[16],
    const uint8_t sig[64],
    char *out_token,
    size_t out_cap
) {
    uint8_t raw[80];
    memcpy(raw, payload, 16u);
    memcpy(raw + 16, sig, 64u);
    char b32[160] = {0};
    int blen = dataog_b32_encode(raw, 80u, b32, sizeof(b32));
    if (blen != 128) return -1;

    /* Format: DOG1-XXXXXXXX-XXXXXXXX-... (16 groups of 8 chars) */
    size_t need = 5u + 128u + 15u + 1u;
    if (out_cap < need) return -1;
    size_t pos = 0;
    memcpy(out_token + pos, "DOG1-", 5u);
    pos += 5u;
    for (int g = 0; g < 16; ++g) {
        if (g > 0) out_token[pos++] = '-';
        memcpy(out_token + pos, b32 + g * 8, 8u);
        pos += 8u;
    }
    out_token[pos] = '\0';
    return 0;
}

static int dataog_decode_dog1_token(
    const char *code_str,
    uint8_t out_payload[16],
    uint8_t out_sig[64]
) {
    if (!code_str) return -1;
    char cleaned[256];
    size_t cpos = 0;
    for (size_t i = 0; code_str[i] != '\0'; ++i) {
        char ch = code_str[i];
        if (isspace((unsigned char)ch) || ch == '-') continue;
        if (cpos + 1 >= sizeof(cleaned)) return -1;
        cleaned[cpos++] = (char)toupper((unsigned char)ch);
    }
    cleaned[cpos] = '\0';
    if (strncmp(cleaned, "DOG1", 4) != 0) return -1;
    const char *b32_part = cleaned + 4;
    if (strlen(b32_part) != 128u) return -1;

    uint8_t raw[80];
    int dec_len = dataog_b32_decode(b32_part, raw, sizeof(raw));
    if (dec_len != 80) return -1;
    memcpy(out_payload, raw, 16u);
    memcpy(out_sig, raw + 16, 64u);
    return 0;
}

static int dataog_encode_request_code(
    const char *machine_code,
    const char *primary_ip,
    const char *hostname,
    char *out_req,
    size_t out_cap
) {
    char body[160] = {0};
    char safe_host[36] = {0};
    size_t hpos = 0;
    for (size_t i = 0; hostname && hostname[i] && hpos < 32; ++i) {
        char ch = hostname[i];
        if (ch == '|' || iscntrl((unsigned char)ch)) continue;
        safe_host[hpos++] = ch;
    }
    safe_host[hpos] = '\0';
    int n = snprintf(body, sizeof(body), "1|%s|%s|%s", machine_code, primary_ip, safe_host);
    if (n <= 0 || (size_t)n + 2 >= sizeof(body)) return -1;
    uint16_t crc = dataog_crc16_ccitt((const uint8_t *)body, (size_t)n);
    uint8_t raw[164];
    memcpy(raw, body, (size_t)n);
    dataog_store_be16(raw + n, crc);

    char b32[320] = {0};
    int blen = dataog_b32_encode(raw, (size_t)n + 2u, b32, sizeof(b32));
    if (blen <= 0) return -1;

    size_t pos = 0;
    if (out_cap < (size_t)blen + 32u) return -1;
    memcpy(out_req + pos, "REQ1-", 5u);
    pos += 5u;
    for (int i = 0; i < blen; ++i) {
        if (i > 0 && (i % 6) == 0) {
            if (pos + 2 >= out_cap) return -1;
            out_req[pos++] = '-';
        }
        if (pos + 2 >= out_cap) return -1;
        out_req[pos++] = b32[i];
    }
    out_req[pos] = '\0';
    return 0;
}

static int dataog_decode_request_code(
    const char *req_str,
    char *out_mc,
    size_t mc_cap,
    char *out_ip,
    size_t ip_cap,
    char *out_host,
    size_t host_cap
) {
    if (!req_str) return -1;
    char cleaned[384];
    size_t cpos = 0;
    for (size_t i = 0; req_str[i] != '\0'; ++i) {
        char ch = req_str[i];
        if (isspace((unsigned char)ch) || ch == '-') continue;
        if (cpos + 1 >= sizeof(cleaned)) return -1;
        cleaned[cpos++] = (char)toupper((unsigned char)ch);
    }
    cleaned[cpos] = '\0';
    if (strncmp(cleaned, "REQ1", 4) != 0) return -1;
    uint8_t raw[200];
    int dec_len = dataog_b32_decode(cleaned + 4, raw, sizeof(raw));
    if (dec_len < 12 || dec_len >= 196) return -1;

    uint16_t expected_crc = dataog_load_be16(raw + dec_len - 2);
    uint16_t actual_crc = dataog_crc16_ccitt(raw, (size_t)(dec_len - 2));
    if (expected_crc != actual_crc) return -1;

    char text[200];
    memcpy(text, raw, (size_t)(dec_len - 2));
    text[dec_len - 2] = '\0';

    /* Parse "1|MCH-...|IP|HOST" */
    char *p1 = strchr(text, '|');
    if (!p1) return -1;
    *p1 = '\0';
    if (strcmp(text, "1") != 0) return -1;
    char *mc = p1 + 1;
    char *p2 = strchr(mc, '|');
    if (!p2) return -1;
    *p2 = '\0';
    char *ip = p2 + 1;
    char *p3 = strchr(ip, '|');
    if (!p3) return -1;
    *p3 = '\0';
    char *host = p3 + 1;

    if (!mc[0] || !ip[0]) return -1;
    snprintf(out_mc, mc_cap, "%s", mc);
    snprintf(out_ip, ip_cap, "%s", ip);
    snprintf(out_host, host_cap, "%s", host);
    return 0;
}

static int dataog_verify_license_core(
    const char *code_str,
    const char *machine_code,
    const char *ips_csv,
    uint16_t today_day,
    const uint8_t pubkey[32],
    uint8_t *out_scope,
    uint16_t *out_duration_days,
    uint16_t *out_expiry_day
) {
    uint8_t payload[16];
    uint8_t sig[64];
    if (dataog_decode_dog1_token(code_str, payload, sig) != 0) {
        return DATAOG_STATUS_INVALID_FORMAT;
    }

    uint8_t ver = payload[0];
    uint8_t scope = payload[1];
    uint16_t duration_days = dataog_load_be16(payload + 2);
    uint16_t expiry_day = dataog_load_be16(payload + 4);

    if (ver != DATAOG_LICENSE_VERSION) {
        return DATAOG_STATUS_INVALID_FORMAT;
    }
    if (scope != DATAOG_SCOPE_MACHINE && scope != DATAOG_SCOPE_GLOBAL) {
        return DATAOG_STATUS_BAD_SCOPE;
    }
    if (duration_days == 0u) {
        return DATAOG_STATUS_INVALID_FORMAT;
    }

    uint8_t signed_msg[34];
    memcpy(signed_msg, DATAOG_SIG_DOMAIN, DATAOG_SIG_DOMAIN_LEN);
    memcpy(signed_msg + DATAOG_SIG_DOMAIN_LEN, payload, 16u);
    if (!dataog_ed25519_verify(signed_msg, sizeof(signed_msg), sig, pubkey)) {
        return DATAOG_STATUS_BAD_SIGNATURE;
    }

    if (out_scope) *out_scope = scope;
    if (out_duration_days) *out_duration_days = duration_days;
    if (out_expiry_day) *out_expiry_day = expiry_day;

    if (scope == DATAOG_SCOPE_MACHINE) {
        uint8_t expected_mc[6];
        dataog_compute_machine_tag(machine_code ? machine_code : "", expected_mc);
        if (memcmp(expected_mc, payload + 6, 6u) != 0) {
            return DATAOG_STATUS_DEVICE_MISMATCH;
        }
        int ip_matched = 0;
        if (ips_csv && ips_csv[0]) {
            char buf[512];
            snprintf(buf, sizeof(buf), "%s", ips_csv);
            char *cursor = buf;
            while (cursor && *cursor) {
                char *comma = strchr(cursor, ',');
                if (comma) *comma = '\0';
                uint8_t candidate_ip[4];
                dataog_compute_ip_tag(cursor, candidate_ip);
                if (memcmp(candidate_ip, payload + 12, 4u) == 0) {
                    ip_matched = 1;
                    break;
                }
                cursor = comma ? (comma + 1) : NULL;
            }
        }
        if (!ip_matched) {
            return DATAOG_STATUS_DEVICE_MISMATCH;
        }
    }

    if (today_day > expiry_day) {
        return DATAOG_STATUS_EXPIRED;
    }
    int32_t not_before = (int32_t)expiry_day - (int32_t)duration_days;
    if (not_before > 1 && (int32_t)today_day < (not_before - 1)) {
        return DATAOG_STATUS_NOT_YET_VALID;
    }

    return DATAOG_STATUS_OK;
}

#endif
