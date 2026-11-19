"""Data.O.G 离线非对称激活码中枢与 C 原生防反编译护盾 (LicenseHub).

安全架构：
1. 客户端仅内嵌混淆拆分的 32 字节 RFC 8032 Ed25519 公钥（零私钥），攻击者即使逆向二进制也无法伪造签名；
2. 优先编译并加载 C 原生编译护盾 (`_license_shield.{dylib,so,dll}`)，并通过 64 位随机挑战-应答 MAC 桥接防返回值补丁；
3. 确定性载荷包含 `(version=1, scope, duration_days, expiry_day, machine_tag_6B, ip_tag_4B)`：
   - `MACHINE_BOUND (0x01)`：严格校验本机硬件机器码 + 本地活动网卡 IPv4（拷贝状态库或更换 IP 立即失效）；
   - `GLOBAL (0x02)`：全局通用激活码，无视一切机器码与 IP 限制，仅校验时间有效性；
4. 离线防时钟回拨：在 `app_meta['license_clock_seal']` 与 `data/.sys_clock_anchor` 双位置存储硬件绑定密封的最高单调时间戳；
5. 双门禁能力令牌：Gate 1 (ASGI 中间件) 校验授权后向 request.state 签发仅当前请求有效的动态 HMAC 能力令牌，
   Gate 2 (`get_platform_context`) 独立二次核验，阻止绕过中间件的直接调用。
"""

from __future__ import annotations

import base64
import ctypes
import datetime
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from fastapi import Request

from core.platform.storage import PlatformStateStore

LICENSE_VERSION = 1
SCOPE_MACHINE = 1
SCOPE_GLOBAL = 2

EPOCH_START_DATE = datetime.date(2025, 1, 1)
CLOCK_ROLLBACK_TOLERANCE_SEC = 900.0  # 允许 15 分钟正常系统对时误差

_SIG_DOMAIN = b"DataOG-License-v1:"
_ACK_DOMAIN = b"DataOG-Shield-Ack-v1:"

# 混淆拆分的生产 Ed25519 公钥 (4 x 64-bit XOR 表 + 置换)
_PK_PERM: Tuple[int, ...] = (
    19, 3, 27, 8, 14, 30, 0, 22, 11, 5, 25, 16, 1, 29, 7, 18,
    24, 12, 2, 31, 9, 21, 15, 4, 28, 10, 6, 23, 17, 13, 20, 26,
)
_PK_T0: Tuple[int, ...] = (
    0xCC54E39780EC4C88, 0xC749369461C16E48, 0x73AE007D8A1BC8F3, 0x123FD22965138140
)
_PK_T1: Tuple[int, ...] = (
    0x55FACE3E43F89917, 0x8C849DEF8F715D54, 0xCAC285FC4A620A0E, 0xC6C48C360E722872
)
_PK_T2: Tuple[int, ...] = (
    0x2752608F76C25786, 0x7E230C3F2A8CF044, 0xB33B2BA174968631, 0x853CE39411333FA5
)
_PK_T3: Tuple[int, ...] = (
    0x64F8CC0815879303, 0xDF0A22D9CA183491, 0x6B7C02C9174D19E6, 0x37BB8EB57E0994DE
)

CONTACT_INFO: Dict[str, str] = {
    "owner": "莫政辉",
    "phone": "131-6111-4561",
    "email": "artxom@outlook.com",
    "notice": "如需激活码，烦请联系 莫政辉  手机 131-6111-4561  邮箱 artxom@outlook.com",
}

AUTH_PUBLIC_EXEMPT_PREFIXES: Tuple[str, ...] = (
    "/api/platform/auth/status",
    "/api/platform/auth/activate",
    "/api/platform/auth/lock",
    "/api/platform/assets/",
)

# =========================================================================
# 纯 Python RFC 8032 Ed25519（用于兜底校验、动态桥接交叉测试及单测签发）
# =========================================================================

_ED_P = (1 << 255) - 19
_ED_L = (1 << 252) + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P)
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if x % 2 != 0:
        x = _ED_P - x
    return x


_ED_BY = (4 * pow(5, _ED_P - 2, _ED_P)) % _ED_P
_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = (_ED_BX % _ED_P, _ED_BY % _ED_P, 1, (_ED_BX * _ED_BY) % _ED_P)
_ED_IDENT = (0, 1, 1, 0)


def _ed_add(p1: Tuple[int, int, int, int], p2: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = ((y1 - x1) * (y2 - x2)) % _ED_P
    b = ((y1 + x1) * (y2 + x2)) % _ED_P
    c = (2 * _ED_D * t1 * t2) % _ED_P
    d = (2 * z1 * z2) % _ED_P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return ((e * f) % _ED_P, (g * h) % _ED_P, (f * g) % _ED_P, (e * h) % _ED_P)


def _ed_scalarmult(pt: Tuple[int, int, int, int], e: int) -> Tuple[int, int, int, int]:
    res = _ED_IDENT
    cur = pt
    while e > 0:
        if e & 1:
            res = _ed_add(res, cur)
        cur = _ed_add(cur, cur)
        e >>= 1
    return res


def _ed_encodepoint(pt: Tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, _ED_P - 2, _ED_P)
    xn = (x * zi) % _ED_P
    yn = (y * zi) % _ED_P
    return (yn | ((xn & 1) << 255)).to_bytes(32, "little")


def _ed_decodepoint(s: bytes) -> Optional[Tuple[int, int, int, int]]:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    if y >= _ED_P:
        return None
    x = _ed_xrecover(y)
    if (x & 1) != sign:
        x = _ED_P - x
    pt = (x, y, 1, (x * y) % _ED_P)
    # Verify curve equation -x^2 + y^2 == 1 + d*x^2*y^2 (mod p)
    lhs = (-x * x + y * y) % _ED_P
    rhs = (1 + _ED_D * x * x * y * y) % _ED_P
    if lhs != rhs:
        return None
    return pt


def ed25519_derive_pubkey(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed 必须为 32 字节")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _ed_encodepoint(_ed_scalarmult(_ED_BASE, a))


def ed25519_sign_deterministic(msg: bytes, seed: bytes) -> bytes:
    """RFC 8032 确定性签名（与 C 端 dataog_ed25519_sign 严格一致，主要供自动化测试使用）."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed 必须为 32 字节")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:64]
    pk = _ed_encodepoint(_ed_scalarmult(_ED_BASE, a))

    r_digest = hashlib.sha512(prefix + msg).digest()
    r = int.from_bytes(r_digest, "little") % _ED_L
    r_bytes = _ed_encodepoint(_ed_scalarmult(_ED_BASE, r))

    k_digest = hashlib.sha512(r_bytes + pk + msg).digest()
    k = int.from_bytes(k_digest, "little") % _ED_L
    s = (r + k * a) % _ED_L
    return r_bytes + s.to_bytes(32, "little")


def ed25519_verify(msg: bytes, sig: bytes, pk: bytes) -> bool:
    if len(sig) != 64 or len(pk) != 32:
        return False
    r_bytes = sig[:32]
    s_int = int.from_bytes(sig[32:], "little")
    if s_int >= _ED_L:
        return False
    a_pt = _ed_decodepoint(pk)
    r_pt = _ed_decodepoint(r_bytes)
    if a_pt is None or r_pt is None:
        return False
    k_digest = hashlib.sha512(r_bytes + pk + msg).digest()
    k = int.from_bytes(k_digest, "little") % _ED_L
    lhs = _ed_scalarmult(_ED_BASE, s_int)
    rhs = _ed_add(r_pt, _ed_scalarmult(a_pt, k))
    return _ed_encodepoint(lhs) == _ed_encodepoint(rhs)


def _reconstruct_prod_pubkey() -> bytes:
    buf = bytearray(32)
    for i in range(32):
        q = i // 8
        sh = (i % 8) * 8
        val = (
            ((_PK_T0[q] >> sh) & 0xFF)
            ^ ((_PK_T1[q] >> sh) & 0xFF)
            ^ ((_PK_T2[q] >> sh) & 0xFF)
            ^ ((_PK_T3[q] >> sh) & 0xFF)
        )
        buf[_PK_PERM[i]] = val
    return bytes(buf)


# =========================================================================
# 编解码、日期换算与硬件指纹探针（与 C 端 license_core.h 100% 字节级对拍）
# =========================================================================


def _b32_encode_nopad(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _b32_decode_nopad(text: str) -> bytes:
    cleaned = "".join(ch for ch in text.upper() if not ch.isspace() and ch != "-")
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ("=" * pad), casefold=True)


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def date_to_epoch_day(d: datetime.date) -> int:
    diff = (d - EPOCH_START_DATE).days
    return max(0, min(65535, diff))


def epoch_day_to_date(day_idx: int) -> datetime.date:
    return EPOCH_START_DATE + datetime.timedelta(days=int(day_idx))


def compute_machine_tag(machine_code: str) -> bytes:
    norm = "".join(ch for ch in (machine_code or "").strip().upper() if not ch.isspace())
    return hashlib.sha256(f"DataOG-MachineTag-v1:{norm}".encode("utf-8")).digest()[:6]


def compute_ip_tag(ip_str: str) -> bytes:
    norm = "".join(ch for ch in (ip_str or "").strip() if not ch.isspace())
    return hashlib.sha256(f"DataOG-IpTag-v1:{norm}".encode("utf-8")).digest()[:4]


def issue_activation_code_for_seed(
    seed: bytes,
    *,
    scope: int,
    duration_days: int,
    expiry_date: datetime.date,
    machine_code: str = "",
    ip_str: str = "",
) -> str:
    """确定性生成标准 `DOG1-...` 激活码（主要用于测试或自动化对拍验证）."""
    if duration_days < 1 or duration_days > 65535:
        raise ValueError("duration_days 必须位于 1..65535")
    exp_day = date_to_epoch_day(expiry_date)
    if scope == SCOPE_GLOBAL:
        mtag = b"\x00" * 6
        itag = b"\x00" * 4
    elif scope == SCOPE_MACHINE:
        mtag = compute_machine_tag(machine_code)
        itag = compute_ip_tag(ip_str)
    else:
        raise ValueError("不支持的 scope")

    payload = struct.pack(">BBHH", LICENSE_VERSION, scope, duration_days, exp_day) + mtag + itag
    sig = ed25519_sign_deterministic(_SIG_DOMAIN + payload, seed)
    b32 = _b32_encode_nopad(payload + sig)
    groups = [b32[i : i + 8] for i in range(0, len(b32), 8)]
    return "DOG1-" + "-".join(groups)


def decode_activation_token(code_str: str) -> Tuple[bytes, bytes, int, int, int, int]:
    cleaned = "".join(ch for ch in (code_str or "").upper() if not ch.isspace() and ch != "-")
    if not cleaned.startswith("DOG1"):
        raise ValueError("激活码格式无效：必须以 DOG1- 开头")
    b32_part = cleaned[4:]
    if len(b32_part) != 128:
        raise ValueError("激活码长度无效，请确认已完整复制全部字符")
    try:
        raw = _b32_decode_nopad(b32_part)
    except Exception as exc:
        raise ValueError(f"激活码字符校验失败: {exc}") from exc
    if len(raw) != 80:
        raise ValueError("激活码载荷长度异常")
    payload = raw[:16]
    sig = raw[16:80]
    ver, scope, duration_days, expiry_day = struct.unpack(">BBHH", payload[:6])
    return payload, sig, ver, scope, duration_days, expiry_day


def encode_request_code(machine_code: str, primary_ip: str, hostname: str) -> str:
    safe_host = "".join(ch for ch in (hostname or "LOCAL") if ch != "|" and ord(ch) >= 32)[:32]
    body = f"1|{machine_code}|{primary_ip}|{safe_host}".encode("utf-8")
    crc = _crc16_ccitt(body)
    b32 = _b32_encode_nopad(body + struct.pack(">H", crc))
    groups = [b32[i : i + 6] for i in range(0, len(b32), 6)]
    return "REQ1-" + "-".join(groups)


def decode_request_code(req_code: str) -> Dict[str, str]:
    cleaned = "".join(ch for ch in (req_code or "").upper() if not ch.isspace() and ch != "-")
    if not cleaned.startswith("REQ1"):
        raise ValueError("设备申请码格式无效")
    raw = _b32_decode_nopad(cleaned[4:])
    if len(raw) < 12:
        raise ValueError("设备申请码长度不足")
    body, crc_bytes = raw[:-2], raw[-2:]
    if struct.unpack(">H", crc_bytes)[0] != _crc16_ccitt(body):
        raise ValueError("设备申请码 CRC 校验失败")
    parts = body.decode("utf-8", errors="replace").split("|")
    if len(parts) != 4 or parts[0] != "1":
        raise ValueError("设备申请码版本不受支持")
    return {"machine_code": parts[1], "primary_ip": parts[2], "hostname": parts[3]}


def _read_raw_machine_id_py() -> str:
    sys_name = platform.system()
    if sys_name == "Darwin":
        try:
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            for line in out.splitlines():
                if "IOPlatformUUID" in line and "=" in line:
                    rhs = line.split("=", 1)[1].strip().strip('"').strip()
                    if rhs:
                        return rhs.upper()
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "kern.uuid"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
            if out:
                return out.upper()
        except Exception:
            pass
    elif os.name == "nt":
        try:
            import winreg  # type: ignore

            for access in (
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
                winreg.KEY_READ,
            ):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Cryptography",
                        0,
                        access,
                    ) as key:
                        val, _ = winreg.QueryValueEx(key, "MachineGuid")
                        if val:
                            return str(val).strip().upper()
                except OSError:
                    continue
        except Exception:
            pass
    else:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        val = fh.read().strip()
                        if val:
                            return val.upper()
                except OSError:
                    pass
    host = socket.gethostname().strip()
    return f"HOST-{host}".upper() if host else "DATAOG-OFFLINE-NODE"


def get_machine_code_py() -> str:
    raw_id = _read_raw_machine_id_py()
    digest = hashlib.sha256(f"DataOG-MachineCode-v1:{raw_id}".encode("utf-8")).digest()[:10]
    b32 = _b32_encode_nopad(digest)
    return f"MCH-{b32[0:4]}-{b32[4:8]}-{b32[8:12]}-{b32[12:16]}"


def _is_usable_ipv4_py(ip_str: str) -> bool:
    try:
        packed = socket.inet_aton(ip_str)
    except OSError:
        return False
    b0, b1 = packed[0], packed[1]
    if b0 in (0, 127) or (b0 == 169 and b1 == 254) or (b0 == 198 and b1 in (18, 19)) or b0 >= 224:
        return False
    return True


def _ip_priority_py(ip_str: str) -> Tuple[int, str]:
    packed = socket.inet_aton(ip_str)
    b0, b1 = packed[0], packed[1]
    if b0 == 192 and b1 == 168:
        prio = 0
    elif b0 == 10:
        prio = 1
    elif b0 == 172 and 16 <= b1 <= 31:
        prio = 2
    else:
        prio = 3
    return (prio, ip_str)


def collect_local_ips_py() -> Tuple[str, List[str]]:
    candidates: set[str] = set()
    # 1. UDP routing probe (no actual packet sent)
    for target in ("192.168.1.1", "10.0.0.1", "172.16.0.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 9))
                ip = s.getsockname()[0]
                if _is_usable_ipv4_py(ip):
                    candidates.add(ip)
        except OSError:
            pass
    # 2. Hostname resolution
    try:
        _, _, ip_list = socket.gethostbyname_ex(socket.gethostname())
        for ip in ip_list:
            if _is_usable_ipv4_py(ip):
                candidates.add(ip)
    except OSError:
        pass
    # 3. addrinfo
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_usable_ipv4_py(ip):
                candidates.add(ip)
    except OSError:
        pass

    sorted_ips = sorted(candidates, key=_ip_priority_py)
    if not sorted_ips:
        return "127.0.0.1", ["127.0.0.1"]
    return sorted_ips[0], sorted_ips


# =========================================================================
# C 原生编译护盾动态库管理与挑战-应答桥接
# =========================================================================


def _shield_lib_filename() -> str:
    if os.name == "nt":
        return "_license_shield.dll"
    if sys.platform == "darwin":
        return "_license_shield.dylib"
    return "_license_shield.so"


def _candidate_shield_paths() -> List[str]:
    fname = _shield_lib_filename()
    paths: List[str] = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            paths.append(os.path.join(getattr(sys, "_MEIPASS"), "core", "platform", fname))
            paths.append(os.path.join(getattr(sys, "_MEIPASS"), fname))
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        paths.append(os.path.join(exe_dir, "_internal", "core", "platform", fname))
        paths.append(os.path.join(exe_dir, "core", "platform", fname))
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, fname))
    return paths


LAST_SHIELD_COMPILE_ERROR: str = ""


def _unload_cdll_handle(lib: Any) -> None:
    handle = getattr(lib, "_handle", None)
    if not handle:
        return
    try:
        import _ctypes

        if os.name == "nt" and hasattr(_ctypes, "FreeLibrary"):
            _ctypes.FreeLibrary(handle)
        elif hasattr(_ctypes, "dlclose"):
            _ctypes.dlclose(handle)
    except Exception:
        pass


def _verify_loadable_shield_binary(lib_path: str) -> Tuple[bool, str]:
    """测试加载动态库并校验导出符号与位数/运行时依赖，校验后立即释放句柄防 Windows 文件锁."""
    if not os.path.isfile(lib_path):
        return False, "产物文件不存在"
    lib = None
    try:
        lib = ctypes.CDLL(lib_path)
        _ = lib.dataog_shield_probe_device
        _ = lib.dataog_shield_verify
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        if lib is not None:
            _unload_cdll_handle(lib)


def _build_shield_compiler_commands(
    kind: str,
    compiler: str,
    src_c: str,
    tmp_lib: str,
    tmp_dir: str,
) -> List[List[str]]:
    if kind == "cl":
        tmp_obj = os.path.join(tmp_dir, "license_shield.obj")
        tmp_imp = os.path.join(tmp_dir, "license_shield.lib")
        return [
            [
                compiler,
                "/nologo",
                "/O2",
                "/MT",
                "/LD",
                src_c,
                f"/Fo:{tmp_obj}",
                f"/Fe:{tmp_lib}",
                "ws2_32.lib",
                "iphlpapi.lib",
                "advapi32.lib",
                "/link",
                f"/IMPLIB:{tmp_imp}",
            ]
        ]
    if sys.platform == "darwin":
        return [
            [
                compiler,
                "-O2",
                "-std=c11",
                "-dynamiclib",
                "-fvisibility=hidden",
                src_c,
                "-o",
                tmp_lib,
            ]
        ]
    if os.name == "nt":
        if kind == "clang":
            return [
                [
                    compiler,
                    "-O2",
                    "-std=c11",
                    "-shared",
                    "-D_MT",
                    "-Xclang",
                    "--dependent-lib=libcmt",
                    src_c,
                    "-o",
                    tmp_lib,
                    "-lws2_32",
                    "-liphlpapi",
                    "-ladvapi32",
                ],
                [
                    compiler,
                    "-O2",
                    "-std=c11",
                    "-shared",
                    src_c,
                    "-o",
                    tmp_lib,
                    "-lws2_32",
                    "-liphlpapi",
                    "-ladvapi32",
                ],
            ]
        return [
            [
                compiler,
                "-O2",
                "-std=c11",
                "-shared",
                "-static",
                "-static-libgcc",
                "-fno-stack-protector",
                src_c,
                "-o",
                tmp_lib,
                "-lws2_32",
                "-liphlpapi",
                "-ladvapi32",
            ],
            [
                compiler,
                "-O2",
                "-std=c11",
                "-shared",
                "-static-libgcc",
                "-fno-stack-protector",
                src_c,
                "-o",
                tmp_lib,
                "-lws2_32",
                "-liphlpapi",
                "-ladvapi32",
            ],
        ]
    return [
        [
            compiler,
            "-O2",
            "-std=c11",
            "-shared",
            "-fPIC",
            "-fvisibility=hidden",
            src_c,
            "-o",
            tmp_lib,
            "-Wl,-s",
        ]
    ]


def _install_compiled_shield_binary(tmp_lib: str, out_lib: str, src_mtime: float) -> Tuple[bool, str]:
    """原子落盘新编译的原生动态库；兼容 Windows 跨盘符移动与进程内存映射旧 DLL 文件锁."""
    out_dir = os.path.dirname(out_lib)
    out_base = os.path.basename(out_lib)
    try:
        for fn in os.listdir(out_dir):
            if fn.startswith(out_base + ".old_"):
                try:
                    os.remove(os.path.join(out_dir, fn))
                except OSError:
                    pass
    except OSError:
        pass

    try:
        os.replace(tmp_lib, out_lib)
        return True, ""
    except PermissionError as perm_exc:
        first_err: Exception = perm_exc
    except OSError:
        try:
            shutil.copyfile(tmp_lib, out_lib)
            return True, ""
        except OSError as copy_exc:
            first_err = copy_exc

    # 若目标动态库已被当前或其他进程映射且时间戳已不旧于源码，直接复用现有文件
    try:
        if os.path.isfile(out_lib) and os.path.getmtime(out_lib) >= src_mtime:
            return True, ""
    except OSError:
        pass

    # Windows 允许对已内存映射的旧版 DLL 执行同目录改名腾位后再写入新版 DLL
    if os.path.isfile(out_lib):
        stale_path = f"{out_lib}.old_{os.getpid()}_{int(time.time() * 1000)}"
        try:
            os.replace(out_lib, stale_path)
            shutil.copyfile(tmp_lib, out_lib)
            try:
                os.remove(stale_path)
            except OSError:
                pass
            return True, ""
        except OSError as rename_exc:
            return False, f"无法覆盖已锁定的原生护盾动态库 ({out_lib}): {rename_exc} (原始错误: {first_err})"

    return False, f"无法写入原生护盾动态库 ({out_lib}): {first_err}"


def compile_native_license_shield(force: bool = False) -> Optional[str]:
    """在非冻结开发/打包环境中将 `license_shield.c` 编译为本地原生动态库."""
    global LAST_SHIELD_COMPILE_ERROR
    if getattr(sys, "frozen", False):
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    src_c = os.path.join(here, "_license_native_src", "license_shield.c")
    src_h = os.path.join(here, "_license_native_src", "license_core.h")
    if not os.path.isfile(src_c) or not os.path.isfile(src_h):
        return None

    out_lib = os.path.join(here, _shield_lib_filename())
    src_mtime = max(os.path.getmtime(src_c), os.path.getmtime(src_h))
    if not force and os.path.isfile(out_lib):
        if os.path.getmtime(out_lib) >= src_mtime:
            return out_lib

    candidates: List[Tuple[str, str]] = []
    for cand in (("gcc", "clang", "cl") if os.name == "nt" else ("clang", "gcc", "cc")):
        found = shutil.which(cand)
        if found:
            candidates.append((cand, found))
    if not candidates:
        LAST_SHIELD_COMPILE_ERROR = "未在 PATH 中检测到可用的 C 编译器 (gcc / clang / cl)"
        return out_lib if os.path.isfile(out_lib) else None

    err_logs: List[str] = []
    with tempfile.TemporaryDirectory(prefix="dog_shield_bld_") as tmp_dir:
        tmp_lib = os.path.join(tmp_dir, _shield_lib_filename())
        for kind, compiler in candidates:
            for cmd in _build_shield_compiler_commands(kind, compiler, src_c, tmp_lib, tmp_dir):
                try:
                    if os.path.exists(tmp_lib):
                        os.remove(tmp_lib)
                except OSError:
                    pass
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if proc.returncode != 0 or not os.path.isfile(tmp_lib):
                    detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                    err_logs.append(f"[{kind}] rc={proc.returncode}: {detail}")
                    continue

                if sys.platform == "darwin":
                    cs = shutil.which("codesign")
                    if cs:
                        subprocess.run(
                            [cs, "--force", "--sign", "-", tmp_lib],
                            capture_output=True,
                            check=False,
                        )

                ok_load, load_err = _verify_loadable_shield_binary(tmp_lib)
                if not ok_load:
                    err_logs.append(f"[{kind}] 动态库加载自检未通过: {load_err}")
                    continue

                ok_inst, inst_err = _install_compiled_shield_binary(tmp_lib, out_lib, src_mtime)
                if not ok_inst:
                    err_logs.append(f"[{kind}] {inst_err}")
                    LAST_SHIELD_COMPILE_ERROR = "\n".join(err_logs)
                    return out_lib if (not force and os.path.isfile(out_lib)) else None

                LAST_SHIELD_COMPILE_ERROR = ""
                return out_lib

    LAST_SHIELD_COMPILE_ERROR = "\n".join(err_logs)
    return out_lib if (not force and os.path.isfile(out_lib)) else None


class NativeLicenseShield:
    """加载 `_license_shield` 原生动态库并提供带挑战 MAC 的验签与设备采集."""

    def __init__(self) -> None:
        self._lib = None
        self._load_lib()

    def _load_lib(self) -> None:
        if not getattr(sys, "frozen", False):
            try:
                compile_native_license_shield(force=False)
            except Exception:
                pass
        for cand in _candidate_shield_paths():
            if not os.path.isfile(cand):
                continue
            try:
                lib = ctypes.CDLL(cand)
                lib.dataog_shield_probe_device.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                ]
                lib.dataog_shield_probe_device.restype = ctypes.c_int

                lib.dataog_shield_verify.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_uint16,
                    ctypes.c_uint64,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint8),
                    ctypes.POINTER(ctypes.c_uint16),
                    ctypes.POINTER(ctypes.c_uint16),
                    ctypes.POINTER(ctypes.c_uint64),
                ]
                lib.dataog_shield_verify.restype = ctypes.c_int
                self._lib = lib
                return
            except Exception:
                continue

    @property
    def is_loaded(self) -> bool:
        return self._lib is not None

    def probe_device(self) -> Optional[Tuple[str, str, List[str]]]:
        if self._lib is None:
            return None
        try:
            mc_buf = ctypes.create_string_buffer(64)
            pri_buf = ctypes.create_string_buffer(64)
            csv_buf = ctypes.create_string_buffer(512)
            rc = self._lib.dataog_shield_probe_device(mc_buf, 64, pri_buf, 64, csv_buf, 512)
            if rc != 0:
                return None
            mc = mc_buf.value.decode("utf-8", errors="ignore").strip()
            pri = pri_buf.value.decode("utf-8", errors="ignore").strip()
            csv = csv_buf.value.decode("utf-8", errors="ignore").strip()
            ips = [x.strip() for x in csv.split(",") if x.strip()]
            if pri and pri not in ips:
                ips.insert(0, pri)
            if not mc:
                return None
            return mc, (pri or (ips[0] if ips else "127.0.0.1")), (ips or ["127.0.0.1"])
        except Exception:
            return None

    def verify_with_challenge(
        self,
        *,
        code_str: str,
        machine_code: str,
        ips: List[str],
        today_day: int,
        effective_pubkey: bytes,
        using_custom_pubkey: bool,
    ) -> Optional[Tuple[int, int, int, int]]:
        """返回 `(status_code, scope, duration_days, expiry_day)`；若挑战应答 MAC 失败则返回无效标记."""
        if self._lib is None:
            return None
        nonce = secrets.randbits(64)
        out_scope = ctypes.c_uint8(0)
        out_dur = ctypes.c_uint16(0)
        out_exp = ctypes.c_uint16(0)
        out_mac = ctypes.c_uint64(0)

        pk_ptr = None
        pk_buf = None
        if using_custom_pubkey:
            pk_buf = (ctypes.c_uint8 * 32).from_buffer_copy(effective_pubkey)
            pk_ptr = ctypes.cast(pk_buf, ctypes.c_void_p)

        ips_csv = ",".join(ips)
        st = int(
            self._lib.dataog_shield_verify(
                code_str.encode("utf-8"),
                machine_code.encode("utf-8"),
                ips_csv.encode("utf-8"),
                ctypes.c_uint16(today_day),
                ctypes.c_uint64(nonce),
                pk_ptr,
                ctypes.byref(out_scope),
                ctypes.byref(out_dur),
                ctypes.byref(out_exp),
                ctypes.byref(out_mac),
            )
        )

        # 验证 C 护盾挑战-应答 MAC，彻底阻断二进制返回值补丁 (mov eax, 0; ret)
        ack_msg = (
            _ACK_DOMAIN
            + struct.pack(">Q", nonce)
            + effective_pubkey
            + struct.pack(
                ">BBHH",
                st & 0xFF,
                out_scope.value,
                out_dur.value,
                out_exp.value,
            )
        )
        expected_mac = struct.unpack(">Q", hashlib.sha512(ack_msg).digest()[:8])[0]
        if out_mac.value != expected_mac:
            return -99, 0, 0, 0
        return st, int(out_scope.value), int(out_dur.value), int(out_exp.value)


# =========================================================================
# LicenseHub 主授权中枢
# =========================================================================


class LicenseHub:
    """Data.O.G 离线授权验证、防时钟回拨、登录拦截与会话能力令牌中枢."""

    _STATUS_LABELS: Dict[str, str] = {
        "ACTIVE": "已激活",
        "UNACTIVATED": "未激活",
        "LOCKED": "已手动锁定",
        "EXPIRED": "授权已过期",
        "DEVICE_MISMATCH": "设备或 IP 不匹配",
        "CLOCK_ROLLBACK": "检测到系统时间异常回拨",
        "INVALID": "激活码无效",
    }

    def __init__(
        self,
        store: PlatformStateStore,
        *,
        verifier_pubkey: Optional[bytes] = None,
        clock_fn: Optional[Callable[[], float]] = None,
        device_probe_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._prod_pubkey = _reconstruct_prod_pubkey()
        if verifier_pubkey is not None:
            if len(verifier_pubkey) != 32:
                raise ValueError("verifier_pubkey 必须为 32 字节")
            self._verifier_pubkey = bytes(verifier_pubkey)
            self._using_custom_pubkey = True
        else:
            self._verifier_pubkey = self._prod_pubkey
            self._using_custom_pubkey = False

        self._clock_fn = clock_fn or time.time
        self._custom_device_probe_fn = device_probe_fn
        self._shield = NativeLicenseShield()

        self._runtime_secret = secrets.token_bytes(32)
        self._session_epoch = secrets.token_hex(8)
        self._cached_device: Optional[Dict[str, Any]] = None
        self._cached_device_ts: float = 0.0
        self._failed_attempts: Deque[float] = deque()

        data_dir = os.path.dirname(os.path.abspath(self.store.db_path))
        self._anchor_file = os.path.join(data_dir, ".sys_clock_anchor")

    def _bind_test_pubkey(self, pubkey: bytes) -> None:
        """仅供自动化单元测试在进程内注入临时测试公钥，不对外暴露任何外部配置或环境变量."""
        if len(pubkey) != 32:
            raise ValueError("pubkey 必须为 32 字节")
        with self._lock:
            self._verifier_pubkey = bytes(pubkey)
            self._using_custom_pubkey = True

    def _now_ts(self) -> float:
        return float(self._clock_fn())

    def _today_date(self) -> datetime.date:
        return datetime.datetime.fromtimestamp(self._now_ts()).date()

    def get_device_info(self, *, force_refresh: bool = False) -> Dict[str, Any]:
        if self._custom_device_probe_fn is not None:
            info = dict(self._custom_device_probe_fn())
            mc = str(info.get("machine_code") or get_machine_code_py())
            all_ips = list(info.get("all_ips") or [])
            pri = str(info.get("primary_ip") or (all_ips[0] if all_ips else "127.0.0.1"))
            if pri and pri not in all_ips:
                all_ips.insert(0, pri)
            host = str(info.get("hostname") or socket.gethostname() or "LOCAL-HOST")
            return {
                "machine_code": mc,
                "primary_ip": pri,
                "all_ips": all_ips or ["127.0.0.1"],
                "hostname": host,
                "request_code": encode_request_code(mc, pri, host),
            }

        now_mono = time.monotonic()
        with self._lock:
            if (
                not force_refresh
                and self._cached_device is not None
                and (now_mono - self._cached_device_ts) < 20.0
            ):
                return dict(self._cached_device)

        probed = self._shield.probe_device()
        if probed is not None:
            mc, pri, all_ips = probed
        else:
            mc = get_machine_code_py()
            pri, all_ips = collect_local_ips_py()

        host = socket.gethostname() or "LOCAL-HOST"
        device = {
            "machine_code": mc,
            "primary_ip": pri,
            "all_ips": all_ips,
            "hostname": host,
            "request_code": encode_request_code(mc, pri, host),
        }
        with self._lock:
            self._cached_device = dict(device)
            self._cached_device_ts = time.monotonic()
        return device

    def _clock_seal_hmac(self, max_ts: float, machine_code: str) -> str:
        msg = f"{int(max_ts)}|{machine_code}".encode("utf-8")
        key = hashlib.sha256(b"DataOG-ClockSeal-v1:" + self._verifier_pubkey).digest()
        return hmac.new(key, msg, hashlib.sha256).hexdigest()

    def _read_clock_watermark(self, machine_code: str) -> Optional[float]:
        candidates: List[float] = []
        raw_meta = self.store.get_meta("license_clock_seal", None)
        if isinstance(raw_meta, dict):
            try:
                ts_val = float(raw_meta.get("ts", 0))
                sig = str(raw_meta.get("sig", ""))
                if ts_val > 0 and hmac.compare_digest(sig, self._clock_seal_hmac(ts_val, machine_code)):
                    candidates.append(ts_val)
            except (TypeError, ValueError):
                pass
        if os.path.isfile(self._anchor_file):
            try:
                with open(self._anchor_file, "r", encoding="utf-8") as fh:
                    obj = json.loads(fh.read())
                ts_val = float(obj.get("ts", 0))
                sig = str(obj.get("sig", ""))
                if ts_val > 0 and hmac.compare_digest(sig, self._clock_seal_hmac(ts_val, machine_code)):
                    candidates.append(ts_val)
            except Exception:
                pass
        # 仅在系统真实运行态参考状态库 mtime 防同时删库锚点，但测试传入自定义 clock_fn 时不受真实文件时间戳干扰
        if self._clock_fn is time.time and os.path.isfile(self.store.db_path):
            try:
                candidates.append(float(os.path.getmtime(self.store.db_path)))
            except OSError:
                pass
        return max(candidates) if candidates else None

    def _advance_clock_watermark(self, now_ts: float, machine_code: str) -> None:
        existing = self._read_clock_watermark(machine_code) or 0.0
        target_ts = max(existing, now_ts)
        seal_obj = {
            "ts": int(target_ts),
            "sig": self._clock_seal_hmac(target_ts, machine_code),
        }
        self.store.save_meta("license_clock_seal", seal_obj)
        try:
            with open(self._anchor_file, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(seal_obj))
        except OSError:
            pass

    @staticmethod
    def _mask_activation_code(code_str: str) -> str:
        s = (code_str or "").strip().upper()
        if len(s) <= 20:
            return s
        return f"{s[:13]}••••••••{s[-8:]}"

    def _verify_token_raw(
        self,
        code_str: str,
        device: Dict[str, Any],
        today: datetime.date,
    ) -> Tuple[str, int, int, int, str]:
        """返回 `(status_code, scope, duration_days, expiry_day, error_message)`."""
        try:
            payload, sig, ver, scope, duration_days, expiry_day = decode_activation_token(code_str)
        except ValueError as exc:
            return "INVALID", 0, 0, 0, str(exc)

        if ver != LICENSE_VERSION or duration_days == 0:
            return "INVALID", 0, 0, 0, "激活码版本或有效天数格式异常"
        if scope not in (SCOPE_MACHINE, SCOPE_GLOBAL):
            return "INVALID", 0, 0, 0, "未知的激活码授权类型"

        today_day = date_to_epoch_day(today)
        py_sig_ok = ed25519_verify(_SIG_DOMAIN + payload, sig, self._verifier_pubkey)

        shield_res = self._shield.verify_with_challenge(
            code_str=code_str,
            machine_code=device["machine_code"],
            ips=device["all_ips"],
            today_day=today_day,
            effective_pubkey=self._verifier_pubkey,
            using_custom_pubkey=self._using_custom_pubkey,
        )
        if shield_res is not None:
            st_c, s_scope, s_dur, s_exp = shield_res
            if st_c == -99:
                return "INVALID", 0, 0, 0, "安全校验模块完整性异常"
            if not py_sig_ok:
                return "INVALID", 0, 0, 0, "激活码数字签名无效"
            if st_c == -1 or st_c == -2 or st_c == -6:
                return "INVALID", s_scope, s_dur, s_exp, "激活码数字签名或格式校验失败"
            if st_c == -5:
                return (
                    "DEVICE_MISMATCH",
                    s_scope,
                    s_dur,
                    s_exp,
                    "当前设备机器码或本机 IP 与该专属激活码不匹配，请联系管理员重新签发或使用全局通用激活码",
                )
            if st_c == -3:
                exp_str = epoch_day_to_date(s_exp).isoformat()
                return (
                    "EXPIRED",
                    s_scope,
                    s_dur,
                    s_exp,
                    f"激活码已于 {exp_str} 过期，请联系管理员获取续期激活码",
                )
            if st_c == -4:
                return (
                    "CLOCK_ROLLBACK",
                    s_scope,
                    s_dur,
                    s_exp,
                    "当前系统日期早于该激活码生效时间，请校准本机系统时间后重试",
                )
        else:
            if not py_sig_ok:
                return "INVALID", 0, 0, 0, "激活码数字签名校验失败，请确认未被篡改"

        # Python 侧独立再次校验证书范围与日期（纵深防御）
        if scope == SCOPE_MACHINE:
            mc_tag = compute_machine_tag(device["machine_code"])
            if mc_tag != payload[6:12]:
                return (
                    "DEVICE_MISMATCH",
                    scope,
                    duration_days,
                    expiry_day,
                    "当前设备机器码与激活码绑定设备不一致",
                )
            ip_tags = {compute_ip_tag(ip) for ip in device["all_ips"]}
            if payload[12:16] not in ip_tags:
                return (
                    "DEVICE_MISMATCH",
                    scope,
                    duration_days,
                    expiry_day,
                    "当前设备 IP 与激活码绑定 IP 不一致",
                )

        if today_day > expiry_day:
            exp_str = epoch_day_to_date(expiry_day).isoformat()
            return "EXPIRED", scope, duration_days, expiry_day, f"激活码已于 {exp_str} 过期"
        not_before = expiry_day - duration_days
        if not_before > 1 and today_day < (not_before - 1):
            return (
                "CLOCK_ROLLBACK",
                scope,
                duration_days,
                expiry_day,
                "检测到当前系统日期早于激活码签发有效期，请校准系统时间",
            )

        return "ACTIVE", scope, duration_days, expiry_day, ""

    def get_auth_status(self, *, force_refresh_ip: bool = False) -> Dict[str, Any]:
        device = self.get_device_info(force_refresh=force_refresh_ip)
        now_ts = self._now_ts()
        today = self._today_date()

        saved_code = str(self.store.get_meta("license_activation_code", "") or "").strip()
        is_locked = str(self.store.get_meta("license_locked", "0") or "0") == "1"

        watermark = self._read_clock_watermark(device["machine_code"])
        clock_rollback = watermark is not None and (now_ts + CLOCK_ROLLBACK_TOLERANCE_SEC) < watermark

        status = "UNACTIVATED"
        authorized = False
        scope = 0
        duration_days = 0
        expiry_day = 0
        expires_date = ""
        days_remaining = 0
        status_message = "系统当前处于离线试用期且未激活，请输入激活码进入工作台。"

        if saved_code:
            st, scope, duration_days, expiry_day, err_msg = self._verify_token_raw(
                saved_code, device, today
            )
            if expiry_day > 0:
                exp_dt = epoch_day_to_date(expiry_day)
                expires_date = exp_dt.isoformat()
                days_remaining = max(0, (exp_dt - today).days + 1)
            if clock_rollback:
                status = "CLOCK_ROLLBACK"
                status_message = "检测到本机系统时间被大幅回调，请校准系统日期时间后再激活或解锁。"
            elif st != "ACTIVE":
                self._advance_clock_watermark(now_ts, device["machine_code"])
                status = st
                status_message = err_msg
            elif is_locked:
                self._advance_clock_watermark(now_ts, device["machine_code"])
                status = "LOCKED"
                status_message = "工作台已手动锁定，请重新输入有效激活码解锁进入。"
            else:
                status = "ACTIVE"
                authorized = True
                self._advance_clock_watermark(now_ts, device["machine_code"])
                status_message = f"当前激活码可用日期至 {expires_date}"
        elif clock_rollback:
            status = "CLOCK_ROLLBACK"
            status_message = "检测到本机系统时间异常回调，请校准系统时间。"
        else:
            self._advance_clock_watermark(now_ts, device["machine_code"])

        scope_label = (
            "全局通用激活码（不限机器与 IP）"
            if scope == SCOPE_GLOBAL
            else ("设备专属激活码（绑定本机机器码与 IP）" if scope == SCOPE_MACHINE else "")
        )

        return {
            "authorized": authorized,
            "locked": is_locked,
            "status": status,
            "status_label": self._STATUS_LABELS.get(status, status),
            "status_message": status_message,
            "login_mode_default": "activation_code",
            "login_modes": [
                {
                    "id": "activation_code",
                    "label": "激活码模式",
                    "enabled": True,
                    "description": "离线验证本机授权激活码与有效期",
                },
                {
                    "id": "account",
                    "label": "账号密码登录",
                    "enabled": False,
                    "description": "当前处于试用期且不联网，账号密码登录暂未开放，请使用激活码模式",
                },
            ],
            "scope": "GLOBAL" if scope == SCOPE_GLOBAL else ("MACHINE" if scope == SCOPE_MACHINE else ""),
            "scope_label": scope_label,
            "expires_date": expires_date,
            "days_remaining": days_remaining,
            "duration_days": duration_days,
            "masked_code": self._mask_activation_code(saved_code) if saved_code else "",
            "device": device,
            "contact": dict(CONTACT_INFO),
            "native_shield": self._shield.is_loaded,
        }

    def activate(self, activation_code: str, login_mode: str = "activation_code") -> Dict[str, Any]:
        mode = (login_mode or "activation_code").strip().lower()
        if mode != "activation_code":
            raise ValueError("当前处于试用期且不联网，账号密码登录暂未开放，请使用「激活码模式」进入")

        now_mono = time.monotonic()
        with self._lock:
            while self._failed_attempts and (now_mono - self._failed_attempts[0]) > 60.0:
                self._failed_attempts.popleft()
            if len(self._failed_attempts) >= 8:
                raise ValueError("激活码连续校验失败次数过多，请 1 分钟后再试")

        cleaned_code = (activation_code or "").strip().upper()
        if not cleaned_code:
            raise ValueError("请输入激活码")

        device = self.get_device_info(force_refresh=True)
        now_ts = self._now_ts()
        today = self._today_date()

        watermark = self._read_clock_watermark(device["machine_code"])
        if watermark is not None and (now_ts + CLOCK_ROLLBACK_TOLERANCE_SEC) < watermark:
            raise ValueError("检测到本机系统时间异常回拨，请校准系统日期时间后再激活")

        st, scope, duration_days, expiry_day, err_msg = self._verify_token_raw(
            cleaned_code, device, today
        )
        if st != "ACTIVE":
            with self._lock:
                self._failed_attempts.append(time.monotonic())
            raise ValueError(err_msg or "激活码无效")

        with self._lock:
            self._failed_attempts.clear()
            self.store.save_meta("license_activation_code", cleaned_code)
            self.store.save_meta("license_locked", "0")
            self._advance_clock_watermark(now_ts, device["machine_code"])
            self._session_epoch = secrets.token_hex(8)

        exp_date = epoch_day_to_date(expiry_day).isoformat()
        scope_text = "全局通用激活码" if scope == SCOPE_GLOBAL else "设备专属激活码"
        self.store.add_log(
            "INFO",
            "安全授权",
            f"激活成功 ({scope_text})，可用日期至 {exp_date}，激活码={self._mask_activation_code(cleaned_code)}",
        )
        return self.get_auth_status()

    def lock(self) -> Dict[str, Any]:
        with self._lock:
            self.store.save_meta("license_locked", "1")
            self._session_epoch = secrets.token_hex(8)
        self.store.add_log("INFO", "安全授权", "用户主动锁定工作台并退回登录页")
        return self.get_auth_status()

    def issue_session_cookie_value(self) -> str:
        exp_ts = int(self._now_ts()) + 86400
        raw = f"{self._session_epoch}:{exp_ts}".encode("utf-8")
        sig = hmac.new(self._runtime_secret, raw, hashlib.sha256).hexdigest()[:24]
        return f"{self._session_epoch}:{exp_ts}:{sig}"

    def _check_session_cookie(self, cookie_val: str) -> bool:
        if not cookie_val:
            return False
        parts = cookie_val.split(":")
        if len(parts) != 3:
            return False
        epoch, exp_str, sig = parts
        if not hmac.compare_digest(epoch, self._session_epoch):
            return False
        try:
            exp_ts = int(exp_str)
        except ValueError:
            return False
        if self._now_ts() > exp_ts:
            return False
        expected = hmac.new(
            self._runtime_secret, f"{epoch}:{exp_ts}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:24]
        return hmac.compare_digest(sig, expected)

    def check_and_stamp_request(self, request: Request) -> Tuple[bool, Dict[str, Any]]:
        """Gate 1: 中间件调用。校验授权并在 request.state 上加盖请求级动态 HMAC 能力令牌."""
        auth = self.get_auth_status()
        if not auth["authorized"]:
            return False, auth
        nonce = secrets.token_hex(12)
        path = request.url.path
        mac = hmac.new(
            self._runtime_secret,
            f"{self._session_epoch}:{nonce}:{path}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        request.state.license_cap = (nonce, path, mac)
        return True, auth

    def verify_request_capability(self, request: Request) -> Tuple[bool, Dict[str, Any]]:
        """Gate 2: FastAPI Depends 二次防线校验."""
        path = request.url.path
        if any(path.startswith(pref) for pref in AUTH_PUBLIC_EXEMPT_PREFIXES):
            return True, {}
        cap = getattr(request.state, "license_cap", None)
        if not isinstance(cap, tuple) or len(cap) != 3:
            return False, self.get_auth_status()
        nonce, cap_path, mac = cap
        if cap_path != path:
            return False, self.get_auth_status()
        expected = hmac.new(
            self._runtime_secret,
            f"{self._session_epoch}:{nonce}:{cap_path}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return False, self.get_auth_status()
        auth = self.get_auth_status()
        return bool(auth["authorized"]), auth
