/* Data.O.G Offline License Key Generator (Zero-Dependency Native Executable)
 * Build via tools/build_keygen.py (embeds obfuscated Ed25519 master seed only inside
 * the author's local keygen binary — NEVER bundled into DataOG client packages).
 */
#include "../../core/platform/_license_native_src/license_core.h"
#include "keygen_secret_tables.h"

static void dataog_reconstruct_seed(uint8_t out_seed[32]) {
    for (int i = 0; i < 32; ++i) {
        int q = i / 8;
        int sh = (i % 8) * 8;
        uint8_t b0 = (uint8_t)((DOG_SK_T0[q] >> sh) & 0xffu);
        uint8_t b1 = (uint8_t)((DOG_SK_T1[q] >> sh) & 0xffu);
        uint8_t b2 = (uint8_t)((DOG_SK_T2[q] >> sh) & 0xffu);
        uint8_t b3 = (uint8_t)((DOG_SK_T3[q] >> sh) & 0xffu);
        out_seed[DOG_SK_PERM[i]] = (uint8_t)(b0 ^ b1 ^ b2 ^ b3);
    }
}

static void dataog_issue_license(
    uint8_t scope,
    uint16_t duration_days,
    uint16_t expiry_day,
    const char *machine_code,
    const char *ip_str,
    char *out_token,
    size_t out_cap
) {
    uint8_t seed[32];
    uint8_t pk[32];
    uint8_t payload[16];
    uint8_t signed_msg[34];
    uint8_t sig[64];

    dataog_reconstruct_seed(seed);
    dataog_ed25519_pubkey(seed, pk);
    dataog_build_payload(scope, duration_days, expiry_day, machine_code, ip_str, payload);

    memcpy(signed_msg, DATAOG_SIG_DOMAIN, DATAOG_SIG_DOMAIN_LEN);
    memcpy(signed_msg + DATAOG_SIG_DOMAIN_LEN, payload, 16u);
    dataog_ed25519_sign(signed_msg, sizeof(signed_msg), seed, pk, sig);
    dataog_format_dog1_token(payload, sig, out_token, out_cap);

    dataog_secure_zero(seed, sizeof(seed));
    dataog_secure_zero(pk, sizeof(pk));
    dataog_secure_zero(signed_msg, sizeof(signed_msg));
    dataog_secure_zero(sig, sizeof(sig));
}

static void dataog_trim_line(char *s) {
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == '\n' || s[n - 1] == '\r' || s[n - 1] == ' ' || s[n - 1] == '\t')) {
        s[--n] = '\0';
    }
    size_t start = 0;
    while (s[start] == ' ' || s[start] == '\t') start++;
    if (start > 0) memmove(s, s + start, n - start + 1);
}

static void dataog_try_copy_clipboard(const char *text) {
#ifdef _WIN32
    FILE *p = _popen("clip", "w");
    if (p) {
        fputs(text, p);
        _pclose(p);
    }
#elif defined(__APPLE__)
    FILE *p = popen("pbcopy 2>/dev/null", "w");
    if (p) {
        fputs(text, p);
        pclose(p);
    }
#endif
}

static void print_usage(const char *prog) {
    printf("Data.O.G 离线激活码生成器 (独立零依赖版)\n");
    printf("用法:\n");
    printf("  %s                          启动交互式向导 (自动探测本机 IP 与机器码)\n", prog);
    printf("  %s --info [--json]          显示本机自动识别的机器码、IP 与申请码\n", prog);
    printf("  %s --local --days 90        为当前机器签发 90 天专属激活码\n", prog);
    printf("  %s --request REQ1-... --days 90  解析客户申请码签发专属激活码\n", prog);
    printf("  %s --machine MCH-... --ip 192.168.1.10 --days 30\n", prog);
    printf("  %s --global --days 90       签发全局通用激活码 (无视机器/IP，仅限时长)\n", prog);
    printf("  --until YYYY-MM-DD          替代 --days，指定精确截止日期\n");
    printf("  --quiet                     仅输出激活码字符串\n");
}

int main(int argc, char **argv) {
#ifdef _WIN32
    SetConsoleOutputCP(65001);
    SetConsoleCP(65001);
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif

    char local_mc[32] = {0};
    char local_primary_ip[32] = {0};
    char local_all_ips[512] = {0};
    char hostname[128] = {0};
    char local_req[384] = {0};

    dataog_get_machine_code(local_mc);
    dataog_collect_local_ips(local_primary_ip, sizeof(local_primary_ip), local_all_ips, sizeof(local_all_ips));
    if (gethostname(hostname, sizeof(hostname) - 1) != 0 || hostname[0] == '\0') {
        snprintf(hostname, sizeof(hostname), "LOCAL-HOST");
    }
    dataog_encode_request_code(local_mc, local_primary_ip, hostname, local_req, sizeof(local_req));

    uint16_t today_day = dataog_today_epoch_day();
    char today_str[16] = {0};
    dataog_epoch_day_to_str(today_day, today_str);

    int flag_info = 0, flag_json = 0, flag_local = 0, flag_global = 0, flag_quiet = 0;
    const char *arg_req = NULL, *arg_mc = NULL, *arg_ip = NULL, *arg_until = NULL;
    int arg_days = -1;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            print_usage(argv[0]);
            return 0;
        } else if (strcmp(argv[i], "--info") == 0) {
            flag_info = 1;
        } else if (strcmp(argv[i], "--json") == 0) {
            flag_json = 1;
        } else if (strcmp(argv[i], "--local") == 0) {
            flag_local = 1;
        } else if (strcmp(argv[i], "--global") == 0) {
            flag_global = 1;
        } else if (strcmp(argv[i], "--quiet") == 0 || strcmp(argv[i], "-q") == 0) {
            flag_quiet = 1;
        } else if (strcmp(argv[i], "--request") == 0 && i + 1 < argc) {
            arg_req = argv[++i];
        } else if (strcmp(argv[i], "--machine") == 0 && i + 1 < argc) {
            arg_mc = argv[++i];
        } else if (strcmp(argv[i], "--ip") == 0 && i + 1 < argc) {
            arg_ip = argv[++i];
        } else if (strcmp(argv[i], "--days") == 0 && i + 1 < argc) {
            arg_days = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--until") == 0 && i + 1 < argc) {
            arg_until = argv[++i];
        } else {
            fprintf(stderr, "[错误] 未知参数: %s\n", argv[i]);
            return 2;
        }
    }

    if (flag_info) {
        if (flag_json) {
            printf(
                "{\"machine_code\":\"%s\",\"primary_ip\":\"%s\",\"all_ips\":\"%s\","
                "\"hostname\":\"%s\",\"request_code\":\"%s\",\"today\":\"%s\"}\n",
                local_mc, local_primary_ip, local_all_ips, hostname, local_req, today_str
            );
        } else {
            printf("本机信息自动采集结果:\n");
            printf("  主机名     : %s\n", hostname);
            printf("  本机主 IP  : %s\n", local_primary_ip);
            printf("  全部 IP    : %s\n", local_all_ips);
            printf("  机器码     : %s\n", local_mc);
            printf("  设备申请码 : %s\n", local_req);
            printf("  当前日期   : %s\n", today_str);
        }
        return 0;
    }

    uint8_t scope = DATAOG_SCOPE_MACHINE;
    char target_mc[64] = {0};
    char target_ip[64] = {0};
    char target_host[128] = {0};
    uint16_t duration_days = 0;
    uint16_t expiry_day = 0;

    if (argc > 1 && (flag_local || flag_global || arg_req || (arg_mc && arg_ip))) {
        if (flag_global) {
            scope = DATAOG_SCOPE_GLOBAL;
        } else if (arg_req) {
            scope = DATAOG_SCOPE_MACHINE;
            if (dataog_decode_request_code(
                    arg_req, target_mc, sizeof(target_mc), target_ip, sizeof(target_ip),
                    target_host, sizeof(target_host)) != 0) {
                fprintf(stderr, "[错误] 无效的设备申请码 (REQ1-...)\n");
                return 1;
            }
        } else if (arg_mc && arg_ip) {
            scope = DATAOG_SCOPE_MACHINE;
            snprintf(target_mc, sizeof(target_mc), "%s", arg_mc);
            snprintf(target_ip, sizeof(target_ip), "%s", arg_ip);
        } else {
            scope = DATAOG_SCOPE_MACHINE;
            snprintf(target_mc, sizeof(target_mc), "%s", local_mc);
            snprintf(target_ip, sizeof(target_ip), "%s", local_primary_ip);
        }

        if (arg_until) {
            int y = 0, m = 0, d = 0;
            if (!dataog_parse_date_str(arg_until, &y, &m, &d)) {
                fprintf(stderr, "[错误] 无效的截止日期格式，请使用 YYYY-MM-DD\n");
                return 1;
            }
            expiry_day = dataog_date_to_epoch_day(y, m, d);
            if (expiry_day < today_day) {
                fprintf(stderr, "[错误] 截止日期 %s 不能早于今天 %s\n", arg_until, today_str);
                return 1;
            }
            uint32_t diff = (uint32_t)(expiry_day - today_day) + 1u;
            duration_days = (diff > 65535u) ? 65535u : (uint16_t)diff;
        } else {
            int dval = (arg_days > 0) ? arg_days : 90;
            if (dval < 1 || dval > 36500) {
                fprintf(stderr, "[错误] 有效天数须在 1 ~ 36500 之间\n");
                return 1;
            }
            uint32_t exp_u = (uint32_t)today_day + (uint32_t)dval - 1u;
            if (exp_u > 65535u) exp_u = 65535u;
            expiry_day = (uint16_t)exp_u;
            duration_days = (uint16_t)dval;
        }
    } else if (argc > 1) {
        fprintf(stderr, "[错误] 请指定 --local、--global、--request 或 --machine+--ip\n");
        print_usage(argv[0]);
        return 2;
    } else {
        /* Interactive Wizard Mode */
        printf("======================================================================\n");
        printf("  Data.O.G 离线激活码生成器 (独立零依赖版本)\n");
        printf("======================================================================\n");
        printf("  自动采集本机信息：\n");
        printf("    主机名称   : %s\n", hostname);
        printf("    本机主 IP  : %s  (全部网卡 IP: %s)\n", local_primary_ip, local_all_ips);
        printf("    本机机器码 : %s\n", local_mc);
        printf("    设备申请码 : %s\n", local_req);
        printf("    当前日期   : %s\n", today_str);
        printf("----------------------------------------------------------------------\n");
        printf("  请选择签发模式:\n");
        printf("    [1] 本机专属激活码 (自动绑定上述本机 IP 与机器码) [默认]\n");
        printf("    [2] 远端客户专属激活码 (粘贴 REQ1-... 申请码 或 手动输入机器码+IP)\n");
        printf("    [3] 全局通用激活码 (无视任何机器与 IP 限制，仅校验有效期)\n");
        printf("  请输入选项 [1/2/3] (回车默认 1): ");
        fflush(stdout);

        char line[512] = {0};
        if (!fgets(line, sizeof(line), stdin)) return 1;
        dataog_trim_line(line);
        int mode = (line[0] == '\0') ? 1 : atoi(line);

        if (mode == 3) {
            scope = DATAOG_SCOPE_GLOBAL;
        } else if (mode == 2) {
            scope = DATAOG_SCOPE_MACHINE;
            printf("  请粘贴用户发来的设备申请码 (REQ1-...)，或直接回车改为手动输入机器码与 IP: ");
            fflush(stdout);
            if (!fgets(line, sizeof(line), stdin)) return 1;
            dataog_trim_line(line);
            if (line[0] != '\0') {
                if (dataog_decode_request_code(
                        line, target_mc, sizeof(target_mc), target_ip, sizeof(target_ip),
                        target_host, sizeof(target_host)) != 0) {
                    fprintf(stderr, "[错误] 设备申请码校验失败，请检查是否复制完整。\n");
                    return 1;
                }
                printf("  -> 已解析设备: 主机=%s, 机器码=%s, IP=%s\n", target_host, target_mc, target_ip);
            } else {
                printf("  请输入目标机器码 (如 MCH-XXXX-XXXX-XXXX-XXXX): ");
                fflush(stdout);
                if (!fgets(target_mc, sizeof(target_mc), stdin)) return 1;
                dataog_trim_line(target_mc);
                printf("  请输入目标 IPv4 地址 (如 192.168.1.100): ");
                fflush(stdout);
                if (!fgets(target_ip, sizeof(target_ip), stdin)) return 1;
                dataog_trim_line(target_ip);
                if (!target_mc[0] || !target_ip[0]) {
                    fprintf(stderr, "[错误] 机器码与 IP 不能为空。\n");
                    return 1;
                }
            }
        } else {
            scope = DATAOG_SCOPE_MACHINE;
            snprintf(target_mc, sizeof(target_mc), "%s", local_mc);
            snprintf(target_ip, sizeof(target_ip), "%s", local_primary_ip);
        }

        printf("\n  请选择激活码有效时长:\n");
        printf("    [1] 7 天 (短期体验)\n");
        printf("    [2] 15 天\n");
        printf("    [3] 30 天 (1 个月)\n");
        printf("    [4] 90 天 (1 季度) [默认]\n");
        printf("    [5] 180 天 (半年)\n");
        printf("    [6] 365 天 (1 年)\n");
        printf("    [7] 自定义天数\n");
        printf("    [8] 指定精确截止日期 (YYYY-MM-DD)\n");
        printf("  请输入选项 [1-8] (回车默认 4): ");
        fflush(stdout);

        if (!fgets(line, sizeof(line), stdin)) return 1;
        dataog_trim_line(line);
        int dur_opt = (line[0] == '\0') ? 4 : atoi(line);
        int days_val = 90;
        if (dur_opt == 1) days_val = 7;
        else if (dur_opt == 2) days_val = 15;
        else if (dur_opt == 3) days_val = 30;
        else if (dur_opt == 4) days_val = 90;
        else if (dur_opt == 5) days_val = 180;
        else if (dur_opt == 6) days_val = 365;
        else if (dur_opt == 7) {
            printf("  请输入有效天数 (1 ~ 36500): ");
            fflush(stdout);
            if (!fgets(line, sizeof(line), stdin)) return 1;
            dataog_trim_line(line);
            days_val = atoi(line);
            if (days_val < 1 || days_val > 36500) {
                fprintf(stderr, "[错误] 天数范围无效。\n");
                return 1;
            }
        } else if (dur_opt == 8) {
            printf("  请输入截止日期 (YYYY-MM-DD): ");
            fflush(stdout);
            if (!fgets(line, sizeof(line), stdin)) return 1;
            dataog_trim_line(line);
            int y = 0, m = 0, d = 0;
            if (!dataog_parse_date_str(line, &y, &m, &d)) {
                fprintf(stderr, "[错误] 日期格式无效。\n");
                return 1;
            }
            expiry_day = dataog_date_to_epoch_day(y, m, d);
            if (expiry_day < today_day) {
                fprintf(stderr, "[错误] 截止日期不能早于今天 %s。\n", today_str);
                return 1;
            }
            uint32_t diff = (uint32_t)(expiry_day - today_day) + 1u;
            duration_days = (diff > 65535u) ? 65535u : (uint16_t)diff;
            days_val = -1;
        }
        if (days_val > 0) {
            uint32_t exp_u = (uint32_t)today_day + (uint32_t)days_val - 1u;
            if (exp_u > 65535u) exp_u = 65535u;
            expiry_day = (uint16_t)exp_u;
            duration_days = (uint16_t)days_val;
        }
    }

    char token[256] = {0};
    char exp_str[16] = {0};
    dataog_issue_license(scope, duration_days, expiry_day, target_mc, target_ip, token, sizeof(token));
    dataog_epoch_day_to_str(expiry_day, exp_str);

    if (flag_quiet) {
        printf("%s\n", token);
        return 0;
    }

    if (argc <= 1) {
        dataog_try_copy_clipboard(token);
    }
    printf("\n======================================================================\n");
    printf("  激活码签发成功！（唯一确定性签名）\n");
    printf("----------------------------------------------------------------------\n");
    printf("  授权类型   : %s\n", scope == DATAOG_SCOPE_GLOBAL ? "全局通用激活码 (无视机器/IP限制)" : "设备专属激活码 (严格绑定机器码 + IP)");
    if (scope == DATAOG_SCOPE_MACHINE) {
        printf("  绑定机器码 : %s\n", target_mc);
        printf("  绑定 IP    : %s\n", target_ip);
    }
    printf("  签发日期   : %s\n", today_str);
    printf("  可用时长   : %u 天\n", (unsigned)duration_days);
    printf("  可用日期至 : %s (含当日 23:59:59)\n", exp_str);
    printf("----------------------------------------------------------------------\n");
    printf("  激活码:\n  %s\n", token);
    printf("======================================================================\n");
    return 0;
}
