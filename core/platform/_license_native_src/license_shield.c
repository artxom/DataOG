/* Data.O.G Native C Verification Shield (_license_shield)
 * Compiled to native shared library (.dylib / .so / .dll) so critical
 * signature checks & obfuscated public key reconstruction live in native machine code.
 */
#include "license_core.h"

#ifdef _WIN32
#  define DOG_EXPORT __declspec(dllexport)
#else
#  define DOG_EXPORT __attribute__((visibility("default")))
#endif

static const uint8_t DOG_PK_PERM[32] = {
    19, 3, 27, 8, 14, 30, 0, 22, 11, 5, 25, 16, 1, 29, 7, 18,
    24, 12, 2, 31, 9, 21, 15, 4, 28, 10, 6, 23, 17, 13, 20, 26
};

static const uint64_t DOG_PK_T0[4] = {
    0xcc54e39780ec4c88ULL, 0xc749369461c16e48ULL, 0x73ae007d8a1bc8f3ULL, 0x123fd22965138140ULL
};
static const uint64_t DOG_PK_T1[4] = {
    0x55face3e43f89917ULL, 0x8c849def8f715d54ULL, 0xcac285fc4a620a0eULL, 0xc6c48c360e722872ULL
};
static const uint64_t DOG_PK_T2[4] = {
    0x2752608f76c25786ULL, 0x7e230c3f2a8cf044ULL, 0xb33b2ba174968631ULL, 0x853ce39411333fa5ULL
};
static const uint64_t DOG_PK_T3[4] = {
    0x64f8cc0815879303ULL, 0xdf0a22d9ca183491ULL, 0x6b7c02c9174d19e6ULL, 0x37bb8eb57e0994deULL
};

static void dataog_reconstruct_prod_pubkey(uint8_t out_pk[32]) {
    for (int i = 0; i < 32; ++i) {
        int q = i / 8;
        int sh = (i % 8) * 8;
        uint8_t b0 = (uint8_t)((DOG_PK_T0[q] >> sh) & 0xffu);
        uint8_t b1 = (uint8_t)((DOG_PK_T1[q] >> sh) & 0xffu);
        uint8_t b2 = (uint8_t)((DOG_PK_T2[q] >> sh) & 0xffu);
        uint8_t b3 = (uint8_t)((DOG_PK_T3[q] >> sh) & 0xffu);
        out_pk[DOG_PK_PERM[i]] = (uint8_t)(b0 ^ b1 ^ b2 ^ b3);
    }
}

static uint64_t dataog_compute_ack_mac(
    uint64_t challenge_nonce,
    const uint8_t eff_pk[32],
    int status_code,
    uint8_t scope,
    uint16_t duration_days,
    uint16_t expiry_day
) {
    static const char prefix[] = "DataOG-Shield-Ack-v1:";
    uint8_t buf[21 + 8 + 32 + 1 + 1 + 2 + 2];
    size_t pos = 0;
    memcpy(buf + pos, prefix, 21u); pos += 21u;
    dataog_store_be64(buf + pos, challenge_nonce); pos += 8u;
    memcpy(buf + pos, eff_pk, 32u); pos += 32u;
    buf[pos++] = (uint8_t)(status_code & 0xff);
    buf[pos++] = scope;
    dataog_store_be16(buf + pos, duration_days); pos += 2u;
    dataog_store_be16(buf + pos, expiry_day); pos += 2u;

    uint8_t digest[64];
    dataog_sha512(buf, pos, digest);
    uint64_t mac = dataog_load_be64(digest);
    dataog_secure_zero(digest, sizeof(digest));
    dataog_secure_zero(buf, sizeof(buf));
    return mac;
}

DOG_EXPORT int dataog_shield_probe_device(
    char *out_machine_code,
    int mc_cap,
    char *out_primary_ip,
    int ip_cap,
    char *out_all_ips_csv,
    int csv_cap
) {
    char mc[32] = {0};
    char pri[32] = {0};
    char csv[512] = {0};
    dataog_get_machine_code(mc);
    dataog_collect_local_ips(pri, sizeof(pri), csv, sizeof(csv));
    if (out_machine_code && mc_cap > 0) snprintf(out_machine_code, (size_t)mc_cap, "%s", mc);
    if (out_primary_ip && ip_cap > 0) snprintf(out_primary_ip, (size_t)ip_cap, "%s", pri);
    if (out_all_ips_csv && csv_cap > 0) snprintf(out_all_ips_csv, (size_t)csv_cap, "%s", csv);
    return 0;
}

DOG_EXPORT int dataog_shield_verify(
    const char *code_str,
    const char *machine_code,
    const char *ips_csv,
    uint16_t today_day,
    uint64_t challenge_nonce,
    const uint8_t *override_pubkey_or_null,
    uint8_t *out_scope,
    uint16_t *out_duration_days,
    uint16_t *out_expiry_day,
    uint64_t *out_response_mac
) {
    uint8_t eff_pk[32];
    if (override_pubkey_or_null != NULL) {
        memcpy(eff_pk, override_pubkey_or_null, 32u);
    } else {
        dataog_reconstruct_prod_pubkey(eff_pk);
    }

    uint8_t scope = 0;
    uint16_t duration_days = 0;
    uint16_t expiry_day = 0;
    int st = dataog_verify_license_core(
        code_str,
        machine_code,
        ips_csv,
        today_day,
        eff_pk,
        &scope,
        &duration_days,
        &expiry_day
    );

    if (out_scope) *out_scope = scope;
    if (out_duration_days) *out_duration_days = duration_days;
    if (out_expiry_day) *out_expiry_day = expiry_day;
    if (out_response_mac) {
        *out_response_mac = dataog_compute_ack_mac(
            challenge_nonce, eff_pk, st, scope, duration_days, expiry_day
        );
    }
    dataog_secure_zero(eff_pk, sizeof(eff_pk));
    return st;
}
