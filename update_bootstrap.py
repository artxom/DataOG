"""Data.O.G 增量更新引导器、数字签名校验与安全代码装载器 (update_bootstrap).

设计原则：
1. 纯 Python 标准库零第三方/零业务依赖（严禁导入 core.* 或 modules.*），在 app.py 启动极早期执行；
2. 采用 RFC 8032 Ed25519（独立签名域隔离前缀）+ BLAKE2b-512-CTR 加密混淆封装 + HMAC-SHA256 密封认证 (`DGP2`) + 全量 SHA-256 清单核验，彻底消除更新包内明文源码/脚本（兼容银行企业邮件网关安全扫描）；
3. 同二级版本累积增量兼容（如 3.5.0+ 均可直接应用 3.5.x 任意后续增量包），跨二级版本明确拦截引导全量包；
4. 通过 `sys.meta_path.insert(0, _VerifiedPatchMetaPathFinder)` + stdlib `zipimport` 优先挂载补丁 `code.zip`
   中的 `core.*` 与 `modules.*`，克服 PyInstaller `PyiFrozenImporter` 先于 `PathFinder` 的机制限制；
5. 内置启动健康守护 (`boot_guard.json`) 与异常自愈自动回退，补丁崩溃时无缝退回上一稳定补丁或内置基础底座。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import importlib
import importlib.abc
import io
import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import zipfile
import zipimport
import zlib

UPDATE_FORMAT_VERSION_V1 = 1
UPDATE_FORMAT_VERSION = 2
MIN_V2_ENFORCED_VERSION = (3, 5, 0)

DGP2_MAGIC = b"DGP2"
DGP2_BUNDLE_MAGIC = b"DGPB"
DGP2_BUNDLE_VERSION = 1
DGP2_CIPHER_BLAKE2B_CTR_HMAC = 1
DGP2_FLAG_ZLIB = 0x0001
DGP2_FLAG_ENCRYPTED = 0x0002
DGP2_HEADER_CORE_STRUCT = struct.Struct("!4sHHB3s24sII")
DGP2_HEADER_CORE_SIZE = DGP2_HEADER_CORE_STRUCT.size  # 44 bytes
DGP2_MAC_SIZE = 32
DGP2_SIG_SIZE = 64
DGP2_HEADER_SIZE = DGP2_HEADER_CORE_SIZE + DGP2_MAC_SIZE + DGP2_SIG_SIZE  # 140 bytes
DGP2_CIPHER_LABEL = "BLAKE2b-512-CTR+HMAC-SHA256"

UPDATE_SIG_DOMAIN_V1 = b"DataOG-Update-v1:"
UPDATE_SIG_DOMAIN_V2 = b"DataOG-Update-v2:"
UPDATE_SIG_DOMAIN_V2_MANIFEST = b"DataOG-Update-v2-Manifest:"

_ALLOWED_UPDATE_EXTS = (".dgp", ".zip", ".dat")

MAX_PACKAGE_BYTES = 32 * 1024 * 1024  # 32 MB
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024  # 80 MB
MAX_ZIP_ENTRIES = 2000
MAX_BOOT_FAILURES = 2
KEEP_RELEASE_COUNT = 3

_SEMVER_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UI_VERSION_META_RE = re.compile(r'<meta\s+name="dataog-ui-version"\s+content="([^"]+)"')
_FORBIDDEN_CODE_SEGMENTS = frozenset({"tests", "tools", "demo_sandbox", "__pycache__", ".keygen_secrets", ".git"})

# 与 core/platform/storage_paths.py 严格一致的混淆派生种子
_S_A = (0x7B, 0x2E, 0x91, 0x4C, 0xD3, 0x18, 0xA5, 0x60)
_S_B = (0x19, 0x4F, 0xF2, 0x28, 0xB1, 0x77, 0xC4, 0x03)
_S_C = (0x55, 0xA1, 0x3C, 0xE8, 0x12, 0x9F, 0x44, 0x6B)

# 与 core/platform/license_hub.py 严格一致的混淆拆分生产 Ed25519 公钥（零私钥）
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

# 混淆拆分的 DGP2 外壳加密密钥派生盐值 (Envelope Pepper)
_ENV_PERM: Tuple[int, ...] = (
    9, 10, 23, 1, 7, 17, 11, 21, 13, 15, 0, 12, 26, 14, 27, 16,
    3, 20, 8, 2, 5, 24, 6, 18, 28, 19, 30, 29, 22, 4, 31, 25,
)
_ENV_T0: Tuple[int, ...] = (
    0x1FDDD60E4897AEF4, 0x943281DA7EFB203E, 0x0BC334FDCF6C98F5, 0x6841D7FA306DFE7B
)
_ENV_T1: Tuple[int, ...] = (
    0x9290F2A3323CFE47, 0x72AFA8295D683C05, 0x4C70ED8AF6F844C6, 0x011FB8904300BEA3
)
_ENV_T2: Tuple[int, ...] = (
    0x0D56DC49FD664536, 0x55CDEFC91FA67E04, 0x62CA6159DDCA6864, 0x379390CBAFF9B2C5
)
_ENV_T3: Tuple[int, ...] = (
    0x2193F52D9A8FC0DC, 0x79EF9711A4709A72, 0xB005CA05E54BC5EB, 0x8EEAF8887FD5DDB9
)

# 纯 Python RFC 8032 Ed25519 常量与验签实现
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
    lhs = (-x * x + y * y) % _ED_P
    rhs = (1 + _ED_D * x * x * y * y) % _ED_P
    if lhs != rhs:
        return None
    return (x, y, 1, (x * y) % _ED_P)


def ed25519_verify(msg: bytes, sig: bytes, pk: bytes) -> bool:
    """RFC 8032 Ed25519 验签（纯标准库实现）."""
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


def _reconstruct_obfuscated_32(
    perm: Tuple[int, ...],
    t0: Tuple[int, ...],
    t1: Tuple[int, ...],
    t2: Tuple[int, ...],
    t3: Tuple[int, ...],
) -> bytes:
    """从四路异或分片与置换表还原 32 字节常量."""
    buf = bytearray(32)
    for i in range(32):
        q = i // 8
        sh = (i % 8) * 8
        val = (
            ((t0[q] >> sh) & 0xFF)
            ^ ((t1[q] >> sh) & 0xFF)
            ^ ((t2[q] >> sh) & 0xFF)
            ^ ((t3[q] >> sh) & 0xFF)
        )
        buf[perm[i]] = val
    return bytes(buf)


def reconstruct_prod_pubkey() -> bytes:
    """还原内嵌生产 Ed25519 公钥."""
    return _reconstruct_obfuscated_32(_PK_PERM, _PK_T0, _PK_T1, _PK_T2, _PK_T3)


def reconstruct_envelope_pepper() -> bytes:
    """还原 DGP2 加密混淆信封派生盐值."""
    return _reconstruct_obfuscated_32(_ENV_PERM, _ENV_T0, _ENV_T1, _ENV_T2, _ENV_T3)


def resolve_verifier_pubkey(verifier_pubkey: Optional[bytes] = None) -> bytes:
    """解析增量包验签公钥（冻结态严禁被环境变量覆盖）."""
    if verifier_pubkey is not None:
        if len(verifier_pubkey) != 32:
            raise ValueError("verifier_pubkey 必须为 32 字节")
        return verifier_pubkey
    if not getattr(sys, "frozen", False):
        hex_env = (os.environ.get("DATAOG_TEST_UPDATE_PUBKEY_HEX") or "").strip().lower()
        if hex_env:
            if not _HEX64_RE.match(hex_env):
                raise ValueError("DATAOG_TEST_UPDATE_PUBKEY_HEX 必须为 64 位十六进制公钥")
            return bytes.fromhex(hex_env)
    return reconstruct_prod_pubkey()


# =========================================================================
# 私有保险箱目录与安装目录定位（与 storage_paths.py 严格保持字节级一致）
# =========================================================================


def _derive_tag(slot: str, length: int = 8) -> str:
    seed = bytes(a ^ b ^ c for a, b, c in zip(_S_A, _S_B, _S_C))
    digest = hashlib.sha256(seed + b":" + slot.encode("utf-8")).hexdigest()
    return digest[:length]


def _apply_hidden_dir_attrs(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    if os.name == "nt":
        try:
            import ctypes

            file_attribute_hidden = 0x02
            invalid_file_attributes = 0xFFFFFFFF
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            get_attrs = kernel32.GetFileAttributesW
            get_attrs.argtypes = [ctypes.c_wchar_p]
            get_attrs.restype = ctypes.c_uint32
            set_attrs = kernel32.SetFileAttributesW
            set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            set_attrs.restype = ctypes.c_int

            attrs = int(get_attrs(str(path)))
            if attrs != invalid_file_attributes and not (attrs & file_attribute_hidden):
                set_attrs(str(path), attrs | file_attribute_hidden)
        except Exception:
            pass
    else:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass


def resolve_private_root(
    private_root_override: Optional[str] = None,
    *,
    os_name: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> str:
    """解析 Layer 2 本地私有保险箱根目录（与 core.platform.storage_paths 完全一致）."""
    if private_root_override:
        return os.path.abspath(private_root_override)
    env_home = (os.environ.get("DATAOG_HOME") or "").strip()
    if env_home:
        return os.path.abspath(env_home)

    seg_outer = _derive_tag("outer_ns", 6)
    seg_mid = _derive_tag("mid_bucket", 8)
    seg_inner = _derive_tag("inner_cell", 10)
    eff_os = os.name if os_name is None else os_name
    eff_plat = sys.platform if platform_name is None else platform_name

    if eff_os == "nt":
        base = (
            os.environ.get("LOCALAPPDATA", "").strip()
            or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
        return os.path.abspath(
            os.path.join(base, f".rtcache_{seg_outer}", f"c{seg_mid}", f"_{seg_inner}")
        )

    if eff_plat == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
        return os.path.abspath(
            os.path.join(base, f".com.sys.rt.{seg_outer}", f".{seg_mid}", f"_{seg_inner}")
        )

    base = (
        os.environ.get("XDG_DATA_HOME", "").strip()
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    )
    return os.path.abspath(
        os.path.join(base, f".sys-rt-{seg_outer}", f".{seg_mid}", f"_{seg_inner}")
    )


def resolve_updates_dir(private_root_override: Optional[str] = None) -> str:
    """返回增量补丁隔离存储目录 (<private_root>/rt_upd)."""
    return os.path.join(resolve_private_root(private_root_override), "rt_upd")


def resolve_install_dir(install_dir_override: Optional[str] = None) -> str:
    """返回应用程序安装目录（仅只读扫描 <install_dir>/updates/，冻结态忽略环境变量覆盖）."""
    if install_dir_override:
        return os.path.abspath(install_dir_override)
    if not getattr(sys, "frozen", False):
        env_dir = (os.environ.get("DATAOG_INSTALL_DIR") or "").strip()
        if env_dir:
            return os.path.abspath(env_dir)
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# =========================================================================
# SemVer 与同二级版本兼容性判定
# =========================================================================


def parse_semver(ver: str) -> Tuple[int, int, int]:
    """解析规范三段式语义化版本号."""
    raw = str(ver or "").strip()
    matched = _SEMVER_RE.match(raw)
    if not matched:
        raise ValueError(f"非法语义化版本号格式: {ver!r}")
    return int(matched.group(1)), int(matched.group(2)), int(matched.group(3))


def normalize_semver(ver: str) -> str:
    """规范化为不带前导 v 的 X.Y.Z 字符串."""
    maj, minor, patch = parse_semver(ver)
    return f"{maj}.{minor}.{patch}"


def format_series(ver: str) -> str:
    """返回二级版本系列标识 X.Y."""
    maj, minor, _ = parse_semver(ver)
    return f"{maj}.{minor}"


def compute_min_base_version(ver: str) -> str:
    """根据目标版本计算同二级版本系列最低底座版本 X.Y.0."""
    maj, minor, _ = parse_semver(ver)
    return f"{maj}.{minor}.0"


def check_version_compatibility(
    patch_version: str,
    base_version: str,
    min_base_version: Optional[str] = None,
) -> Tuple[bool, str]:
    """判定增量包是否适用于当前客户端底座版本（同二级版本 X.Y.0+ 累积兼容）."""
    try:
        p = parse_semver(patch_version)
        b = parse_semver(base_version)
        eff_min_str = min_base_version or compute_min_base_version(patch_version)
        m = parse_semver(eff_min_str)
    except ValueError as exc:
        return False, str(exc)

    p_str = f"{p[0]}.{p[1]}.{p[2]}"
    b_str = f"{b[0]}.{b[1]}.{b[2]}"
    m_str = f"{m[0]}.{m[1]}.{m[2]}"

    if (p[0], p[1]) != (m[0], m[1]) or m > p:
        return False, f"增量包最低底座声明 (v{m_str}) 与目标版本 (v{p_str}) 不一致"

    if (p[0], p[1]) != (b[0], b[1]):
        return (
            False,
            f"增量包系列 ({p[0]}.{p[1]}.x) 与当前客户端底座系列 ({b[0]}.{b[1]}.x) 不匹配，跨二级版本请下载完整客户端安装。",
        )

    if b < m:
        return (
            False,
            f"当前客户端底座版本 v{b_str} 低于增量包最低要求 v{m_str}，请先升级完整客户端。",
        )

    if p < b:
        return (
            False,
            f"增量包版本 v{p_str} 低于当前客户端底座版本 v{b_str}，无需降级安装。",
        )

    return True, ""


# =========================================================================
# 安全压缩包解析、防路径穿越、防 Zip-Bomb 与 Ed25519 双重校验
# =========================================================================


class UpdateVerificationError(ValueError):
    """增量包格式、完整性、数字签名或版本兼容性校验失败."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_zip_member_path(name: str, zinfo: zipfile.ZipInfo) -> str:
    """严格校验证明归档条目路径安全，阻断绝对路径、反斜杠、.. 穿越及符号链接."""
    if not name or "\x00" in name:
        raise UpdateVerificationError("PATH_TRAVERSAL", "增量包内存在非法空路径或 NUL 字节")
    if "\\" in name or name.startswith("/"):
        raise UpdateVerificationError("PATH_TRAVERSAL", f"增量包内存在非法路径分隔符或绝对路径: {name}")
    # 拦截 Windows 盘符伪装 (例如 C:foo/bar)
    first_seg = name.split("/", 1)[0]
    if ":" in first_seg:
        raise UpdateVerificationError("PATH_TRAVERSAL", f"增量包内存在非法盘符路径: {name}")
    mode = (zinfo.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode):
        raise UpdateVerificationError("PATH_TRAVERSAL", f"增量包严禁包含符号链接条目: {name}")
    parts = [p for p in name.split("/") if p != ""]
    if any(p in (".", "..") for p in parts):
        raise UpdateVerificationError("PATH_TRAVERSAL", f"增量包存在目录穿越条目: {name}")
    normalized = "/".join(parts)
    if zinfo.is_dir():
        return normalized + "/" if normalized else ""
    return normalized


def _read_zip_member_limited(
    zf: zipfile.ZipFile,
    zinfo: zipfile.ZipInfo,
    remaining_budget: int,
) -> bytes:
    """按流读取压缩条目并强制校验未压缩尺寸与预算上限，防止伪造头部 Zip-Bomb."""
    if zinfo.file_size < 0 or zinfo.file_size > remaining_budget:
        raise UpdateVerificationError("SIZE_LIMIT", "增量包解压后体积超出安全上限")
    chunks: List[bytes] = []
    total = 0
    with zf.open(zinfo, "r") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > remaining_budget:
                raise UpdateVerificationError("SIZE_LIMIT", "增量包解压体积超出安全上限 (防解压膨胀拦截)")
            chunks.append(chunk)
    return b"".join(chunks)


def _open_and_scan_zip(raw_bytes: bytes, *, max_raw: int = MAX_PACKAGE_BYTES) -> zipfile.ZipFile:
    if not raw_bytes:
        raise UpdateVerificationError("INVALID_ARCHIVE", "增量包内容为空")
    if len(raw_bytes) > max_raw:
        raise UpdateVerificationError(
            "SIZE_LIMIT", f"增量包体积超出 {max_raw // (1024 * 1024)} MB 安全上限"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise UpdateVerificationError("INVALID_ARCHIVE", f"无效的 ZIP/DGP 更新包文件: {exc}") from exc

    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        zf.close()
        raise UpdateVerificationError("SIZE_LIMIT", "增量包条目数量超出安全上限")

    declared_total = 0
    for zi in infos:
        _validate_zip_member_path(zi.filename, zi)
        if not zi.is_dir():
            if zi.file_size < 0:
                zf.close()
                raise UpdateVerificationError("INVALID_ARCHIVE", "增量包包含异常负数文件尺寸")
            declared_total += int(zi.file_size)
            if declared_total > MAX_UNCOMPRESSED_BYTES:
                zf.close()
                raise UpdateVerificationError("SIZE_LIMIT", "增量包声明解压体积超出 80 MB 安全上限")
    return zf


def is_dgp2_bytes(raw_bytes: bytes) -> bool:
    """判断字节流是否以 DGP2 加密混淆更新信封魔数头起始."""
    return isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) >= 4 and bytes(raw_bytes[:4]) == DGP2_MAGIC


def derive_dgp2_keys(pubkey: bytes, nonce: bytes) -> Tuple[bytes, bytes]:
    """基于内嵌混淆盐值、验签公钥与随机 24B Nonce 派生独立加密密钥与 HMAC 密钥."""
    if len(pubkey) != 32:
        raise ValueError("pubkey 必须为 32 字节")
    if len(nonce) != 24:
        raise ValueError("nonce 必须为 24 字节")
    pepper = reconstruct_envelope_pepper()
    ikm = pepper + pubkey + nonce
    enc_key = hashlib.blake2b(ikm, person=b"DG2-ENC-KEY-V02\x00", digest_size=32).digest()
    mac_key = hashlib.blake2b(ikm, person=b"DG2-MAC-KEY-V02\x00", digest_size=32).digest()
    return enc_key, mac_key


def dgp2_ctr_xor(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
    """纯标准库 BLAKE2b-512-CTR 流加密/解密原语."""
    if len(enc_key) != 32:
        raise ValueError("enc_key 必须为 32 字节")
    if len(nonce) != 24:
        raise ValueError("nonce 必须为 24 字节")
    n = len(data)
    if n == 0:
        return b""

    base_h = hashlib.blake2b(key=enc_key, person=b"DG2-CTR-STR-V02\x00", digest_size=64)
    base_h.update(nonce)
    out = bytearray(n)
    chunk_bytes = 1024 * 64  # 每次批量异或 64 KiB
    blk_idx = 0
    pos = 0
    while pos < n:
        end = min(pos + chunk_bytes, n)
        need = end - pos
        num_blks = (need + 63) // 64
        ks_parts: List[bytes] = []
        for i in range(num_blks):
            h_blk = base_h.copy()
            h_blk.update((blk_idx + i).to_bytes(8, "big"))
            ks_parts.append(h_blk.digest())
        ks = b"".join(ks_parts)[:need]
        xored = int.from_bytes(data[pos:end], "little") ^ int.from_bytes(ks, "little")
        out[pos:end] = xored.to_bytes(need, "little")
        pos = end
        blk_idx += num_blks
    return bytes(out)


def dgp2_compute_mac(mac_key: bytes, header_core: bytes, ciphertext: bytes) -> bytes:
    """计算 DGP2 外壳头与密文的 HMAC-SHA256 完整性认证标签."""
    return hmac.new(mac_key, header_core + ciphertext, hashlib.sha256).digest()


def dgp2_envelope_sign_message(header_core: bytes, ciphertext: bytes) -> bytes:
    """构造 DGP2 外壳独立签名域的 Ed25519 签名消息."""
    return UPDATE_SIG_DOMAIN_V2 + header_core + hashlib.sha256(ciphertext).digest()


def _validate_bundle_entry_path(name: str) -> str:
    """严格校验 DGPB 二进制包内部相对路径，阻断任何穿越、绝对路径或非法字符."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise UpdateVerificationError("PATH_TRAVERSAL", "加密包内存在非法空路径或 NUL 字节")
    if "\\" in name or name.startswith("/") or name.endswith("/"):
        raise UpdateVerificationError("PATH_TRAVERSAL", f"加密包内存在非法路径格式: {name}")
    if len(name.encode("utf-8")) > 512:
        raise UpdateVerificationError("PATH_TRAVERSAL", "加密包内条目路径长度超出安全上限")
    first_seg = name.split("/", 1)[0]
    if ":" in first_seg:
        raise UpdateVerificationError("PATH_TRAVERSAL", f"加密包内存在非法盘符路径: {name}")
    parts = name.split("/")
    if any(not p or p in (".", "..") for p in parts):
        raise UpdateVerificationError("PATH_TRAVERSAL", f"加密包内存在目录穿越或空段路径: {name}")
    return "/".join(parts)


def pack_dgp2_bundle(entries: Sequence[Tuple[str, bytes]]) -> bytes:
    """将多文件映射确定性打包为无明文目录特征的 DGPB 紧凑二进制流."""
    norm_items: List[Tuple[str, bytes, bytes]] = []
    seen: set = set()
    for raw_name, raw_data in entries:
        norm = _validate_bundle_entry_path(str(raw_name))
        if norm in seen:
            raise UpdateVerificationError("INVALID_ARCHIVE", f"加密包存在重复条目: {norm}")
        seen.add(norm)
        data_b = bytes(raw_data)
        norm_items.append((norm, norm.encode("utf-8"), data_b))
    if not norm_items:
        raise ValueError("DGP2 打包条目不能为空")
    norm_items.sort(key=lambda item: item[0])

    buf = io.BytesIO()
    buf.write(struct.pack("!4sHI", DGP2_BUNDLE_MAGIC, DGP2_BUNDLE_VERSION, len(norm_items)))
    for _, name_b, data_b in norm_items:
        buf.write(struct.pack("!HI", len(name_b), len(data_b)))
        buf.write(name_b)
        buf.write(data_b)
    return buf.getvalue()


def unpack_dgp2_bundle(raw_bundle: bytes) -> Dict[str, bytes]:
    """严格边界校验并解包 DGPB 二进制流."""
    total_len = len(raw_bundle)
    if total_len > MAX_UNCOMPRESSED_BYTES:
        raise UpdateVerificationError("SIZE_LIMIT", "增量包解包体积超出 80 MB 安全上限")
    if total_len < 10:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内部载荷结构不完整")

    magic, ver, count = struct.unpack_from("!4sHI", raw_bundle, 0)
    if magic != DGP2_BUNDLE_MAGIC or ver != DGP2_BUNDLE_VERSION:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内部载荷魔数或版本无效")
    if count <= 0:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内部不包含任何文件条目")
    if count > MAX_ZIP_ENTRIES:
        raise UpdateVerificationError("SIZE_LIMIT", "加密增量包内部条目数量超出安全上限")

    pos = 10
    remaining = MAX_UNCOMPRESSED_BYTES
    files: Dict[str, bytes] = {}
    for _ in range(count):
        if pos + 6 > total_len:
            raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内部条目头截断")
        name_len, data_len = struct.unpack_from("!HI", raw_bundle, pos)
        pos += 6
        if name_len <= 0 or name_len > 512:
            raise UpdateVerificationError("PATH_TRAVERSAL", "加密增量包条目路径长度异常")
        if pos + name_len + data_len > total_len:
            raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内部条目数据越界")
        if data_len > remaining:
            raise UpdateVerificationError("SIZE_LIMIT", "加密增量包解压体积超出安全上限")
        remaining -= data_len
        try:
            raw_name = raw_bundle[pos : pos + name_len].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpdateVerificationError("INVALID_ARCHIVE", f"加密增量包条目名编码无效: {exc}") from exc
        norm = _validate_bundle_entry_path(raw_name)
        if norm in files:
            raise UpdateVerificationError("INVALID_ARCHIVE", f"加密增量包存在重复条目: {norm}")
        data_start = pos + name_len
        files[norm] = bytes(raw_bundle[data_start : data_start + data_len])
        pos = data_start + data_len

    if pos != total_len:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内部载荷存在未预期尾部冗余字节")
    return files


def _decompress_zlib_limited(comp_bytes: bytes, expected_plain_len: int) -> bytes:
    """在内存上限硬约束下解压 zlib 载荷（先验签认证后调用）."""
    if expected_plain_len <= 0:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包声明明文长度无效")
    if expected_plain_len > MAX_UNCOMPRESSED_BYTES:
        raise UpdateVerificationError("SIZE_LIMIT", "增量包解压后体积超出 80 MB 安全上限")
    decomp = zlib.decompressobj()
    try:
        plain = decomp.decompress(comp_bytes, MAX_UNCOMPRESSED_BYTES + 1)
        if len(plain) > MAX_UNCOMPRESSED_BYTES or decomp.unconsumed_tail:
            raise UpdateVerificationError("SIZE_LIMIT", "增量包解压体积超出安全上限 (防解压膨胀拦截)")
        tail = decomp.flush(MAX_UNCOMPRESSED_BYTES + 1 - len(plain))
        plain += tail
    except UpdateVerificationError:
        raise
    except zlib.error as exc:
        raise UpdateVerificationError("INVALID_ARCHIVE", f"加密增量包解压缩失败: {exc}") from exc

    if len(plain) > MAX_UNCOMPRESSED_BYTES:
        raise UpdateVerificationError("SIZE_LIMIT", "增量包解压体积超出安全上限 (防解压膨胀拦截)")
    if not decomp.eof or decomp.unused_data or len(plain) != expected_plain_len:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包解压后长度与信封声明不一致")
    return plain


def unseal_dgp2_bytes(dgp_bytes: bytes, *, pubkey: bytes) -> Dict[str, bytes]:
    """严格按『先 Ed25519 验签 -> 再 HMAC-SHA256 认证 -> 后解密 -> 最后限流解压』解封 DGP2 信封."""
    if not dgp_bytes:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包内容为空")
    if len(dgp_bytes) > MAX_PACKAGE_BYTES:
        raise UpdateVerificationError(
            "SIZE_LIMIT", f"增量包体积超出 {MAX_PACKAGE_BYTES // (1024 * 1024)} MB 安全上限"
        )
    if len(dgp_bytes) < DGP2_HEADER_SIZE:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包头部不完整")

    header_core = bytes(dgp_bytes[:DGP2_HEADER_CORE_SIZE])
    magic, fmt_ver, flags, cipher_id, reserved, nonce, plaintext_len, ciphertext_len = (
        DGP2_HEADER_CORE_STRUCT.unpack(header_core)
    )
    if (
        magic != DGP2_MAGIC
        or fmt_ver != UPDATE_FORMAT_VERSION
        or flags != (DGP2_FLAG_ZLIB | DGP2_FLAG_ENCRYPTED)
        or cipher_id != DGP2_CIPHER_BLAKE2B_CTR_HMAC
        or reserved != b"\x00\x00\x00"
    ):
        raise UpdateVerificationError("INVALID_ARCHIVE", "无效的 DGP2 加密增量包头部标识")
    if ciphertext_len <= 0 or DGP2_HEADER_SIZE + ciphertext_len != len(dgp_bytes):
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包密文长度与文件尺寸不匹配")

    mac_start = DGP2_HEADER_CORE_SIZE
    sig_start = mac_start + DGP2_MAC_SIZE
    envelope_mac = bytes(dgp_bytes[mac_start:sig_start])
    envelope_sig = bytes(dgp_bytes[sig_start:DGP2_HEADER_SIZE])
    ciphertext = bytes(dgp_bytes[DGP2_HEADER_SIZE:])

    # 1. 先校验外壳 Ed25519 数字签名，未过验签绝不解密或解压
    if not ed25519_verify(dgp2_envelope_sign_message(header_core, ciphertext), envelope_sig, pubkey):
        raise UpdateVerificationError(
            "BAD_SIGNATURE",
            "加密增量包数字签名校验失败，文件可能已被篡改或来源不可信。",
        )

    # 2. 校验外壳 HMAC-SHA256 认证标签
    enc_key, mac_key = derive_dgp2_keys(pubkey, nonce)
    expected_mac = dgp2_compute_mac(mac_key, header_core, ciphertext)
    if not hmac.compare_digest(envelope_mac, expected_mac):
        raise UpdateVerificationError(
            "BAD_SIGNATURE",
            "加密增量包完整性 MAC 校验失败，密文或信封参数已被篡改。",
        )

    # 3. 强边界校验明文尺寸后才执行解密、限流解压与解包
    if plaintext_len <= 0:
        raise UpdateVerificationError("INVALID_ARCHIVE", "加密增量包声明明文长度无效")
    if plaintext_len > MAX_UNCOMPRESSED_BYTES:
        raise UpdateVerificationError("SIZE_LIMIT", "增量包声明解压体积超出 80 MB 安全上限")

    compressed = dgp2_ctr_xor(ciphertext, enc_key, nonce)
    raw_bundle = _decompress_zlib_limited(compressed, plaintext_len)
    return unpack_dgp2_bundle(raw_bundle)


def extract_inner_dgp_bytes(raw_bytes: bytes) -> Tuple[bytes, str]:
    """从外层发布包 (.zip) 或直接包 (.dgp / .dat) 中提取核心 .dgp 载荷字节."""
    if not raw_bytes:
        raise UpdateVerificationError("INVALID_ARCHIVE", "增量包内容为空")
    if len(raw_bytes) > MAX_PACKAGE_BYTES:
        raise UpdateVerificationError(
            "SIZE_LIMIT", f"增量包体积超出 {MAX_PACKAGE_BYTES // (1024 * 1024)} MB 安全上限"
        )
    if is_dgp2_bytes(raw_bytes):
        return bytes(raw_bytes), "dgp"

    with _open_and_scan_zip(raw_bytes) as zf:
        infos = [zi for zi in zf.infolist() if not zi.is_dir()]
        names = {zi.filename for zi in infos}
        if "manifest.json" in names and "manifest.sig" in names:
            return raw_bytes, "dgp"

        dgp_infos: List[zipfile.ZipInfo] = []
        candidate_infos: List[Tuple[str, zipfile.ZipInfo]] = []
        for zi in infos:
            norm = _validate_zip_member_path(zi.filename, zi)
            lower = norm.lower()
            in_root_or_updates = "/" not in norm or (
                norm.startswith("updates/") and "/" not in norm[len("updates/"):]
            )
            if not in_root_or_updates:
                continue
            candidate_infos.append((lower, zi))
            if lower.endswith(".dgp"):
                dgp_infos.append(zi)

        if not dgp_infos:
            for lower, zi in candidate_infos:
                if lower.endswith((".txt", ".md")) or zi.file_size < DGP2_HEADER_SIZE:
                    continue
                try:
                    with zf.open(zi, "r") as handle:
                        head = handle.read(4)
                except Exception:
                    head = b""
                if head == DGP2_MAGIC:
                    dgp_infos.append(zi)

        if len(dgp_infos) != 1:
            raise UpdateVerificationError(
                "INVALID_ARCHIVE",
                "未识别为有效的 Data.O.G 增量更新包（需包含唯一 updates/*.dgp 或有效 DGP2 载荷）",
            )
        dgp_bytes = _read_zip_member_limited(zf, dgp_infos[0], MAX_PACKAGE_BYTES)
        return dgp_bytes, "outer_zip"


def _inspect_code_zip_bytes(code_zip_bytes: bytes) -> int:
    """校验内嵌 code.zip 严格仅含白名单 .py 业务代码并包含 core/app_kernel.py."""
    with _open_and_scan_zip(code_zip_bytes, max_raw=MAX_PACKAGE_BYTES) as zf:
        file_infos = [zi for zi in zf.infolist() if not zi.is_dir()]
        if not file_infos:
            raise UpdateVerificationError("FORBIDDEN_CONTENT", "增量包 code.zip 不含任何 Python 源码")

        seen_paths: set = set()
        remaining = MAX_UNCOMPRESSED_BYTES
        for zi in file_infos:
            norm = _validate_zip_member_path(zi.filename, zi)
            parts = norm.split("/")
            if parts[0] not in ("core", "modules"):
                raise UpdateVerificationError(
                    "FORBIDDEN_CONTENT", f"增量代码包仅允许包含 core/ 或 modules/ 目录: {norm}"
                )
            if any(seg in _FORBIDDEN_CODE_SEGMENTS for seg in parts):
                raise UpdateVerificationError(
                    "FORBIDDEN_CONTENT", f"增量代码包包含禁止打包目录: {norm}"
                )
            if not norm.endswith(".py"):
                raise UpdateVerificationError(
                    "FORBIDDEN_CONTENT", f"增量代码包仅允许纯 .py 源码模块: {norm}"
                )
            if norm in seen_paths:
                raise UpdateVerificationError("INVALID_ARCHIVE", f"code.zip 存在重复条目: {norm}")
            seen_paths.add(norm)
            data = _read_zip_member_limited(zf, zi, remaining)
            remaining -= len(data)

        if "core/app_kernel.py" not in seen_paths:
            raise UpdateVerificationError("FORBIDDEN_CONTENT", "增量代码包缺失核心内核入口 core/app_kernel.py")
        return len(seen_paths)


def _extract_ui_version_from_html(html_bytes: bytes) -> str:
    head = html_bytes[:8192].decode("utf-8", errors="replace")
    matched = _UI_VERSION_META_RE.search(head)
    return matched.group(1).strip() if matched else ""


def _validate_manifest_dict(
    manifest: Any,
    *,
    expected_format_version: Optional[int] = None,
) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise UpdateVerificationError("INVALID_ARCHIVE", "manifest.json 格式必须为 JSON 对象")
    fmt_ver = manifest.get("format_version")
    if fmt_ver not in (UPDATE_FORMAT_VERSION_V1, UPDATE_FORMAT_VERSION):
        raise UpdateVerificationError(
            "INVALID_ARCHIVE",
            f"不支持的增量包格式版本: {fmt_ver!r}",
        )
    if expected_format_version is not None and fmt_ver != expected_format_version:
        raise UpdateVerificationError(
            "INVALID_ARCHIVE",
            f"manifest format_version ({fmt_ver}) 与包信封格式 ({expected_format_version}) 不匹配",
        )
    try:
        ver = normalize_semver(str(manifest.get("version") or ""))
        min_base = normalize_semver(str(manifest.get("min_base_version") or compute_min_base_version(ver)))
    except ValueError as exc:
        raise UpdateVerificationError("INVALID_ARCHIVE", f"manifest 版本号声明无效: {exc}") from exc

    series = str(manifest.get("series") or "").strip()
    if series != format_series(ver):
        raise UpdateVerificationError(
            "INVALID_ARCHIVE",
            f"manifest 系列号 ({series}) 与版本 ({ver}) 不匹配",
        )
    p_tup = parse_semver(ver)
    m_tup = parse_semver(min_base)
    if p_tup >= MIN_V2_ENFORCED_VERSION and fmt_ver < UPDATE_FORMAT_VERSION:
        raise UpdateVerificationError(
            "INVALID_ARCHIVE",
            f"v{ver} 及以上增量更新包必须采用加密混淆格式 (DGP2)",
        )
    if (p_tup[0], p_tup[1]) != (m_tup[0], m_tup[1]) or m_tup > p_tup:
        raise UpdateVerificationError(
            "INVALID_ARCHIVE",
            f"manifest min_base_version ({min_base}) 不属于同二级系列或高于目标版本 ({ver})",
        )

    code_zip_meta = manifest.get("code_zip")
    if not isinstance(code_zip_meta, dict):
        raise UpdateVerificationError("INVALID_ARCHIVE", "manifest 缺失 code_zip 校验描述符")
    cz_sha = str(code_zip_meta.get("sha256") or "").strip().lower()
    if not _HEX64_RE.match(cz_sha):
        raise UpdateVerificationError("INVALID_ARCHIVE", "manifest code_zip.sha256 非法")
    try:
        cz_size = int(code_zip_meta.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateVerificationError("INVALID_ARCHIVE", "manifest code_zip.size 非法") from exc
    if cz_size <= 0:
        raise UpdateVerificationError("INVALID_ARCHIVE", "manifest code_zip.size 必须大于 0")

    static_files = manifest.get("static_files")
    if not isinstance(static_files, dict) or not static_files:
        raise UpdateVerificationError("INVALID_ARCHIVE", "manifest 缺失 static_files 校验映射")
    clean_static: Dict[str, str] = {}
    for rel, sha_val in static_files.items():
        rel_str = str(rel or "").strip()
        sha_str = str(sha_val or "").strip().lower()
        if (
            not rel_str
            or rel_str.startswith("/")
            or "\\" in rel_str
            or ".." in rel_str.split("/")
            or not _HEX64_RE.match(sha_str)
        ):
            raise UpdateVerificationError("INVALID_ARCHIVE", f"manifest static_files 包含非法条目: {rel_str}")
        clean_static[rel_str] = sha_str

    if "index.html" not in clean_static or "js/main.js" not in clean_static:
        raise UpdateVerificationError("FORBIDDEN_CONTENT", "manifest static_files 缺失前端核心入口")

    normalized = dict(manifest)
    normalized["format_version"] = int(fmt_ver)
    normalized["version"] = ver
    normalized["series"] = series
    normalized["min_base_version"] = min_base
    normalized["code_zip"] = {
        "sha256": cz_sha,
        "size": cz_size,
        "file_count": int(code_zip_meta.get("file_count") or 0),
    }
    normalized["static_files"] = clean_static
    return normalized


def _check_payload_file_names(names: Sequence[str] | Any) -> None:
    name_set = set(names)
    for req in ("manifest.json", "manifest.sig", "code.zip"):
        if req not in name_set:
            raise UpdateVerificationError("INVALID_ARCHIVE", f"增量包缺失核心文件: {req}")
    unexpected = [
        name
        for name in name_set
        if name not in ("manifest.json", "manifest.sig", "code.zip")
        and not name.startswith("static/")
    ]
    if unexpected:
        raise UpdateVerificationError(
            "FORBIDDEN_CONTENT", f"增量包包含未授权顶层文件: {unexpected[0]}"
        )


def _verify_manifest_and_sig(
    manifest_raw: bytes,
    sig_raw: bytes,
    *,
    expected_format_version: int,
    sig_domain: bytes,
    base_version: Optional[str],
    pubkey: bytes,
) -> Dict[str, Any]:
    if len(manifest_raw) > 2 * 1024 * 1024:
        raise UpdateVerificationError("SIZE_LIMIT", "manifest.json 体积超出安全上限")
    if len(sig_raw) != 64:
        raise UpdateVerificationError("BAD_SIGNATURE", "增量包签名文件 manifest.sig 长度无效")
    if not ed25519_verify(sig_domain + manifest_raw, sig_raw, pubkey):
        raise UpdateVerificationError(
            "BAD_SIGNATURE",
            "增量包数字签名校验失败，文件可能已被篡改或来源不可信。",
        )
    try:
        manifest_raw_obj = json.loads(manifest_raw.decode("utf-8"))
    except Exception as exc:
        raise UpdateVerificationError("INVALID_ARCHIVE", f"manifest.json 解析失败: {exc}") from exc
    manifest = _validate_manifest_dict(
        manifest_raw_obj,
        expected_format_version=expected_format_version,
    )
    if base_version:
        ok_compat, compat_msg = check_version_compatibility(
            manifest["version"],
            base_version,
            manifest["min_base_version"],
        )
        if not ok_compat:
            raise UpdateVerificationError("INCOMPATIBLE_VERSION", compat_msg)
    return manifest


def _verify_payload_contents(
    manifest: Dict[str, Any],
    code_zip_bytes: bytes,
    static_bytes_map: Dict[str, bytes],
) -> Tuple[int, int]:
    if len(code_zip_bytes) != manifest["code_zip"]["size"]:
        raise UpdateVerificationError("CHECKSUM_MISMATCH", "code.zip 字节长度与签名清单不一致")
    if hashlib.sha256(code_zip_bytes).hexdigest() != manifest["code_zip"]["sha256"]:
        raise UpdateVerificationError("CHECKSUM_MISMATCH", "code.zip SHA-256 校验失败，内容已被修改")
    code_file_count = _inspect_code_zip_bytes(code_zip_bytes)

    expected_static_hashes = manifest["static_files"]
    actual_keys = set(static_bytes_map.keys())
    expected_keys = set(expected_static_hashes.keys())
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        detail_parts = []
        if missing:
            detail_parts.append(f"缺失 {', '.join(missing[:3])}")
        if extra:
            detail_parts.append(f"存在未签名多余文件 {', '.join(extra[:3])}")
        raise UpdateVerificationError(
            "CHECKSUM_MISMATCH",
            f"增量包静态资源清单不一致（{'; '.join(detail_parts)}）",
        )

    for rel in sorted(expected_keys):
        sbytes = static_bytes_map[rel]
        if hashlib.sha256(sbytes).hexdigest() != expected_static_hashes[rel]:
            raise UpdateVerificationError(
                "CHECKSUM_MISMATCH", f"静态资源 {rel} SHA-256 校验失败"
            )

    ui_ver = _extract_ui_version_from_html(static_bytes_map.get("index.html", b""))
    if ui_ver != manifest["version"]:
        raise UpdateVerificationError(
            "UI_VERSION_MISMATCH",
            f"增量包前端版本声明 ({ui_ver or '未知'}) 与清单版本 (v{manifest['version']}) 不一致",
        )
    return code_file_count, len(expected_static_hashes)


def verify_update_package_bytes(
    raw_bytes: bytes,
    *,
    base_version: Optional[str] = None,
    verifier_pubkey: Optional[bytes] = None,
    include_payload: bool = False,
) -> Dict[str, Any]:
    """完整校验 .zip / .dgp / .dat 增量更新包的 DGP2 加密认证信封、Ed25519 数字签名、文件白名单、SHA-256 与版本兼容性."""
    dgp_bytes, container_type = extract_inner_dgp_bytes(raw_bytes)
    dgp_sha256 = hashlib.sha256(dgp_bytes).hexdigest()
    pubkey = resolve_verifier_pubkey(verifier_pubkey)

    if is_dgp2_bytes(dgp_bytes):
        payload_files = unseal_dgp2_bytes(dgp_bytes, pubkey=pubkey)
        _check_payload_file_names(payload_files.keys())
        manifest_raw = payload_files["manifest.json"]
        sig_raw = payload_files["manifest.sig"]
        manifest = _verify_manifest_and_sig(
            manifest_raw,
            sig_raw,
            expected_format_version=UPDATE_FORMAT_VERSION,
            sig_domain=UPDATE_SIG_DOMAIN_V2_MANIFEST,
            base_version=base_version,
            pubkey=pubkey,
        )
        code_zip_bytes = payload_files["code.zip"]
        static_bytes_map = {
            name[len("static/"):]: data
            for name, data in payload_files.items()
            if name.startswith("static/")
        }
        code_file_count, static_file_count = _verify_payload_contents(
            manifest, code_zip_bytes, static_bytes_map
        )
        fmt_ver = UPDATE_FORMAT_VERSION
        is_encrypted = True
        cipher_label = DGP2_CIPHER_LABEL
    else:
        payload_files = {}
        with _open_and_scan_zip(dgp_bytes, max_raw=MAX_PACKAGE_BYTES) as zf:
            file_map: Dict[str, zipfile.ZipInfo] = {}
            for zi in zf.infolist():
                norm = _validate_zip_member_path(zi.filename, zi)
                if zi.is_dir():
                    continue
                if norm in file_map:
                    raise UpdateVerificationError("INVALID_ARCHIVE", f"增量包存在重复条目: {norm}")
                file_map[norm] = zi

            _check_payload_file_names(file_map.keys())
            remaining = MAX_UNCOMPRESSED_BYTES
            manifest_raw = _read_zip_member_limited(
                zf, file_map["manifest.json"], min(remaining, 2 * 1024 * 1024)
            )
            remaining -= len(manifest_raw)
            sig_raw = _read_zip_member_limited(zf, file_map["manifest.sig"], min(remaining, 1024))
            remaining -= len(sig_raw)

            manifest = _verify_manifest_and_sig(
                manifest_raw,
                sig_raw,
                expected_format_version=UPDATE_FORMAT_VERSION_V1,
                sig_domain=UPDATE_SIG_DOMAIN_V1,
                base_version=base_version,
                pubkey=pubkey,
            )

            code_zip_bytes = _read_zip_member_limited(zf, file_map["code.zip"], remaining)
            remaining -= len(code_zip_bytes)

            static_bytes_map = {}
            for name, zi in sorted(file_map.items()):
                if not name.startswith("static/"):
                    continue
                rel = name[len("static/"):]
                sbytes = _read_zip_member_limited(zf, zi, remaining)
                remaining -= len(sbytes)
                static_bytes_map[rel] = sbytes

            code_file_count, static_file_count = _verify_payload_contents(
                manifest, code_zip_bytes, static_bytes_map
            )
            if include_payload:
                payload_files = {
                    "manifest.json": manifest_raw,
                    "manifest.sig": sig_raw,
                    "code.zip": code_zip_bytes,
                }
                for rel, sbytes in static_bytes_map.items():
                    payload_files[f"static/{rel}"] = sbytes

        fmt_ver = UPDATE_FORMAT_VERSION_V1
        is_encrypted = False
        cipher_label = "none"

    result: Dict[str, Any] = {
        "manifest": manifest,
        "manifest_raw": manifest_raw,
        "sig_raw": sig_raw,
        "dgp_bytes": dgp_bytes,
        "dgp_sha256": dgp_sha256,
        "container_type": container_type,
        "format_version": fmt_ver,
        "encrypted": is_encrypted,
        "cipher": cipher_label,
        "package_size": len(raw_bytes),
        "dgp_size": len(dgp_bytes),
        "code_file_count": code_file_count,
        "static_file_count": static_file_count,
    }
    if include_payload:
        result["payload_files"] = payload_files
    return result


def verify_installed_release_dir(
    release_dir: str,
    *,
    base_version: Optional[str] = None,
    verifier_pubkey: Optional[bytes] = None,
) -> Dict[str, Any]:
    """启动及回退前对已落盘补丁目录进行全量重验签与文件完整性核验."""
    if not release_dir or not os.path.isdir(release_dir):
        raise UpdateVerificationError("INVALID_ARCHIVE", "补丁发行目录不存在")

    manifest_path = os.path.join(release_dir, "manifest.json")
    sig_path = os.path.join(release_dir, "manifest.sig")
    code_zip_path = os.path.join(release_dir, "code.zip")
    static_dir = os.path.join(release_dir, "static")

    for req_path, label in (
        (manifest_path, "manifest.json"),
        (sig_path, "manifest.sig"),
        (code_zip_path, "code.zip"),
    ):
        if not os.path.isfile(req_path) or os.path.islink(req_path):
            raise UpdateVerificationError("INVALID_ARCHIVE", f"已安装补丁缺失或含非法链接: {label}")
    if not os.path.isdir(static_dir) or os.path.islink(static_dir):
        raise UpdateVerificationError("INVALID_ARCHIVE", "已安装补丁 static 目录无效")

    with open(manifest_path, "rb") as fh:
        manifest_raw = fh.read(2 * 1024 * 1024 + 1)
    if len(manifest_raw) > 2 * 1024 * 1024:
        raise UpdateVerificationError("SIZE_LIMIT", "已安装 manifest.json 体积异常")

    with open(sig_path, "rb") as fh:
        sig_raw = fh.read(128)
    if len(sig_raw) != 64:
        raise UpdateVerificationError("BAD_SIGNATURE", "已安装 manifest.sig 长度无效")

    pubkey = resolve_verifier_pubkey(verifier_pubkey)
    if ed25519_verify(UPDATE_SIG_DOMAIN_V2_MANIFEST + manifest_raw, sig_raw, pubkey):
        verified_fmt = UPDATE_FORMAT_VERSION
    elif ed25519_verify(UPDATE_SIG_DOMAIN_V1 + manifest_raw, sig_raw, pubkey):
        verified_fmt = UPDATE_FORMAT_VERSION_V1
    else:
        raise UpdateVerificationError("BAD_SIGNATURE", "已安装补丁数字签名核验失败")

    try:
        manifest = _validate_manifest_dict(
            json.loads(manifest_raw.decode("utf-8")),
            expected_format_version=verified_fmt,
        )
    except UpdateVerificationError:
        raise
    except Exception as exc:
        raise UpdateVerificationError("INVALID_ARCHIVE", f"已安装 manifest.json 解析失败: {exc}") from exc

    if base_version:
        ok_compat, compat_msg = check_version_compatibility(
            manifest["version"],
            base_version,
            manifest["min_base_version"],
        )
        if not ok_compat:
            raise UpdateVerificationError("INCOMPATIBLE_VERSION", compat_msg)

    with open(code_zip_path, "rb") as fh:
        code_zip_bytes = fh.read()
    if len(code_zip_bytes) != manifest["code_zip"]["size"]:
        raise UpdateVerificationError("CHECKSUM_MISMATCH", "已安装 code.zip 长度不匹配")
    if hashlib.sha256(code_zip_bytes).hexdigest() != manifest["code_zip"]["sha256"]:
        raise UpdateVerificationError("CHECKSUM_MISMATCH", "已安装 code.zip SHA-256 校验失败")
    code_file_count = _inspect_code_zip_bytes(code_zip_bytes)

    expected_static = manifest["static_files"]
    actual_rel_paths: set = set()
    index_bytes = b""
    for cur_root, dirs, files in os.walk(static_dir, followlinks=False):
        for d in list(dirs):
            full_d = os.path.join(cur_root, d)
            if os.path.islink(full_d):
                raise UpdateVerificationError("PATH_TRAVERSAL", f"static 包含非法符号链接目录: {d}")
        for fname in files:
            full_f = os.path.join(cur_root, fname)
            if os.path.islink(full_f):
                raise UpdateVerificationError("PATH_TRAVERSAL", f"static 包含非法符号链接文件: {fname}")
            rel = os.path.relpath(full_f, static_dir).replace(os.sep, "/")
            actual_rel_paths.add(rel)

    if actual_rel_paths != set(expected_static.keys()):
        raise UpdateVerificationError("CHECKSUM_MISMATCH", "已安装补丁静态文件集合与签名清单不一致")

    for rel in sorted(expected_static.keys()):
        full_f = os.path.join(static_dir, *rel.split("/"))
        h = hashlib.sha256()
        total = 0
        chunks: List[bytes] = []
        with open(full_f, "rb") as fh:
            while True:
                blk = fh.read(65536)
                if not blk:
                    break
                total += len(blk)
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise UpdateVerificationError("SIZE_LIMIT", f"静态文件过大: {rel}")
                h.update(blk)
                if rel == "index.html":
                    chunks.append(blk)
        if h.hexdigest() != expected_static[rel]:
            raise UpdateVerificationError("CHECKSUM_MISMATCH", f"已安装静态文件 {rel} 被修改或损坏")
        if rel == "index.html":
            index_bytes = b"".join(chunks)

    ui_ver = _extract_ui_version_from_html(index_bytes)
    if ui_ver != manifest["version"]:
        raise UpdateVerificationError("UI_VERSION_MISMATCH", "已安装前端版本号与清单不一致")

    return {
        "manifest": manifest,
        "format_version": manifest["format_version"],
        "encrypted": manifest["format_version"] >= UPDATE_FORMAT_VERSION,
        "code_zip_path": code_zip_path,
        "static_dir": static_dir,
        "code_file_count": code_file_count,
        "static_file_count": len(expected_static),
    }


# =========================================================================
# Layer 2 状态持久化、原子落盘与历史裁剪
# =========================================================================


def _default_state() -> Dict[str, Any]:
    return {
        "active_version": None,
        "previous_version": None,
        "history": [],
        "ingested_hashes": {},
        "retired_reason": None,
        "last_rollback_reason": None,
        "updated_at": None,
    }


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    _apply_hidden_dir_attrs(parent)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: str, data: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".bin", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_update_state(updates_dir: str) -> Dict[str, Any]:
    state_path = os.path.join(updates_dir, "state.json")
    base = _default_state()
    if not os.path.isfile(state_path):
        return base
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            base.update(raw)
    except Exception:
        return _default_state()
    if not isinstance(base.get("history"), list):
        base["history"] = []
    if not isinstance(base.get("ingested_hashes"), dict):
        base["ingested_hashes"] = {}
    return base


def save_update_state(updates_dir: str, state: Dict[str, Any]) -> None:
    os.makedirs(updates_dir, exist_ok=True)
    _apply_hidden_dir_attrs(updates_dir)
    payload = dict(_default_state())
    payload.update(state)
    payload["updated_at"] = _utc_now_iso()
    _atomic_write_json(os.path.join(updates_dir, "state.json"), payload)


def load_boot_guard(updates_dir: str) -> Dict[str, Any]:
    guard_path = os.path.join(updates_dir, "boot_guard.json")
    default = {
        "version": None,
        "consecutive_failures": 0,
        "last_attempt_at": None,
        "healthy": True,
    }
    if not os.path.isfile(guard_path):
        return default
    try:
        with open(guard_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            default.update(raw)
    except Exception:
        pass
    return default


def save_boot_guard(updates_dir: str, guard: Dict[str, Any]) -> None:
    if not os.path.isdir(updates_dir):
        os.makedirs(updates_dir, exist_ok=True)
        _apply_hidden_dir_attrs(updates_dir)
    _atomic_write_json(os.path.join(updates_dir, "boot_guard.json"), dict(guard))


def _record_history_entry(state: Dict[str, Any], manifest: Dict[str, Any], *, source: str, dgp_sha256: str) -> None:
    ver = manifest["version"]
    fmt_ver = int(manifest.get("format_version") or UPDATE_FORMAT_VERSION_V1)
    history = [h for h in (state.get("history") or []) if isinstance(h, dict) and h.get("version") != ver]
    history.insert(
        0,
        {
            "version": ver,
            "series": manifest["series"],
            "min_base_version": manifest["min_base_version"],
            "format_version": fmt_ver,
            "encrypted": fmt_ver >= UPDATE_FORMAT_VERSION,
            "built_at": manifest.get("built_at") or "",
            "summary": manifest.get("summary") or "",
            "notes": manifest.get("notes") or "",
            "code_file_count": int((manifest.get("code_zip") or {}).get("file_count") or 0),
            "static_file_count": len(manifest.get("static_files") or {}),
            "installed_at": _utc_now_iso(),
            "source": source,
            "dgp_sha256": dgp_sha256,
        },
    )
    state["history"] = history[:20]


def prune_old_releases(
    updates_dir: str,
    state: Dict[str, Any],
    *,
    keep: int = KEEP_RELEASE_COUNT,
    protected_versions: Sequence[Optional[str]] = (),
) -> List[str]:
    """清理超出保留数量的旧版补丁缓存（绝不删除当前激活、回退备份或运行中补丁）."""
    releases_root = os.path.join(updates_dir, "releases")
    if not os.path.isdir(releases_root):
        return []
    protected = {
        str(v)
        for v in (
            state.get("active_version"),
            state.get("previous_version"),
            _RUNTIME_STATE.get("active_version"),
            *protected_versions,
        )
        if v
    }
    candidates: List[Tuple[Tuple[int, int, int], str]] = []
    for entry in os.listdir(releases_root):
        full = os.path.join(releases_root, entry)
        if not os.path.isdir(full):
            continue
        try:
            ver_tup = parse_semver(entry)
        except ValueError:
            ver_tup = (0, 0, 0)
        candidates.append((ver_tup, entry))

    candidates.sort(key=lambda item: item[0], reverse=True)
    kept_names = {name for _, name in candidates[: max(1, keep)]}
    removed: List[str] = []
    for _, name in candidates:
        if name in kept_names or name in protected:
            continue
        target = os.path.join(releases_root, name)
        shutil.rmtree(target, ignore_errors=True)
        if not os.path.exists(target):
            removed.append(name)
    return removed


def install_verified_update_bytes(
    raw_bytes: bytes,
    *,
    updates_dir: str,
    base_version: str,
    verifier_pubkey: Optional[bytes] = None,
    source: str = "upload",
    source_filename: str = "",
) -> Dict[str, Any]:
    """验签并原子安装增量包至 Layer 2 (<private_root>/rt_upd/releases/<version>/)."""
    verified = verify_update_package_bytes(
        raw_bytes,
        base_version=base_version,
        verifier_pubkey=verifier_pubkey,
        include_payload=True,
    )
    manifest = verified["manifest"]
    ver = manifest["version"]
    dgp_sha256 = verified["dgp_sha256"]
    payload_files = verified["payload_files"]

    os.makedirs(updates_dir, exist_ok=True)
    _apply_hidden_dir_attrs(updates_dir)
    releases_root = os.path.join(updates_dir, "releases")
    os.makedirs(releases_root, exist_ok=True)
    _apply_hidden_dir_attrs(releases_root)

    stage_dir = os.path.join(updates_dir, f".stage_{ver}_{secrets.token_hex(4)}")
    target_dir = os.path.join(releases_root, ver)
    backup_dir = os.path.join(updates_dir, f".old_{ver}_{secrets.token_hex(4)}")

    # 原子替换目标目录（短退避重试容忍 Windows 杀软实时扫描短暂句柄锁）
    def _resilient_dir_replace(src: str, dst: str, retries: int = 4) -> None:
        for attempt in range(retries):
            try:
                os.replace(src, dst)
                return
            except OSError:
                if attempt >= retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))

    try:
        os.makedirs(stage_dir, exist_ok=True)
        _atomic_write_bytes(os.path.join(stage_dir, "manifest.json"), verified["manifest_raw"])
        _atomic_write_bytes(os.path.join(stage_dir, "manifest.sig"), verified["sig_raw"])
        _atomic_write_bytes(os.path.join(stage_dir, "code.zip"), payload_files["code.zip"])

        for rel in sorted(manifest["static_files"].keys()):
            sbytes = payload_files[f"static/{rel}"]
            dest = os.path.join(stage_dir, "static", *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as out_fh:
                out_fh.write(sbytes)

        # 落盘后整目录二次重验签，确保解压期间零损坏
        verify_installed_release_dir(
            stage_dir,
            base_version=base_version,
            verifier_pubkey=verifier_pubkey,
        )

        if os.path.exists(target_dir):
            _resilient_dir_replace(target_dir, backup_dir)
        _resilient_dir_replace(stage_dir, target_dir)
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        if os.path.exists(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
        if not os.path.exists(target_dir) and os.path.exists(backup_dir):
            try:
                _resilient_dir_replace(backup_dir, target_dir)
            except OSError:
                pass
        raise

    state = load_update_state(updates_dir)
    old_active = state.get("active_version")
    if old_active and old_active != ver:
        state["previous_version"] = old_active
    state["active_version"] = ver
    state["retired_reason"] = None
    state["last_rollback_reason"] = None
    _record_history_entry(state, manifest, source=source, dgp_sha256=dgp_sha256)

    ingested = dict(state.get("ingested_hashes") or {})
    ingested[dgp_sha256] = {
        "version": ver,
        "filename": os.path.basename(source_filename or f"DataOG-Update-v{ver}.dgp"),
        "ingested_at": _utc_now_iso(),
        "source": source,
    }
    state["ingested_hashes"] = ingested
    save_update_state(updates_dir, state)
    save_boot_guard(
        updates_dir,
        {
            "version": ver,
            "consecutive_failures": 0,
            "last_attempt_at": None,
            "healthy": False,
        },
    )
    prune_old_releases(updates_dir, state)

    return {
        "ok": True,
        "version": ver,
        "series": manifest["series"],
        "min_base_version": manifest["min_base_version"],
        "format_version": verified["format_version"],
        "encrypted": verified["encrypted"],
        "cipher": verified.get("cipher") or "",
        "summary": manifest.get("summary") or "",
        "dgp_sha256": dgp_sha256,
        "code_file_count": verified["code_file_count"],
        "static_file_count": verified["static_file_count"],
        "container_type": verified["container_type"],
        "state": state,
    }


def scan_and_ingest_install_dir_updates(
    *,
    updates_dir: str,
    install_dir: str,
    base_version: str,
    verifier_pubkey: Optional[bytes] = None,
) -> Dict[str, Any]:
    """只读扫描 <install_dir>/updates/ 下的 .dgp/.zip/.dat 更新包，自动吸纳最高版本的新包."""
    scan_dir = os.path.join(install_dir, "updates")
    if not os.path.isdir(scan_dir):
        return {
            "found_count": 0,
            "installed_version": None,
            "already_ingested_count": 0,
            "skipped": [],
        }

    candidates = []
    for fname in sorted(os.listdir(scan_dir)):
        low = fname.lower()
        if not low.endswith(_ALLOWED_UPDATE_EXTS):
            continue
        full = os.path.join(scan_dir, fname)
        if os.path.isfile(full) and not os.path.islink(full):
            candidates.append((fname, full))

    if not candidates:
        return {
            "found_count": 0,
            "installed_version": None,
            "already_ingested_count": 0,
            "skipped": [],
        }

    state = load_update_state(updates_dir)
    ingested_hashes = dict(state.get("ingested_hashes") or {})
    valid_new: List[Tuple[Tuple[int, int, int], str, bytes, Dict[str, Any]]] = []
    already_ingested_count = 0
    skipped: List[Dict[str, str]] = []

    for fname, full in candidates:
        try:
            fsize = os.path.getsize(full)
            if fsize > MAX_PACKAGE_BYTES:
                skipped.append({"filename": fname, "reason": "文件体积超出 32 MB 安全上限"})
                continue
            with open(full, "rb") as fh:
                raw = fh.read(MAX_PACKAGE_BYTES + 1)
            verified = verify_update_package_bytes(
                raw,
                base_version=base_version,
                verifier_pubkey=verifier_pubkey,
            )
            dgp_sha = verified["dgp_sha256"]
            ver = verified["manifest"]["version"]
            if dgp_sha in ingested_hashes and os.path.isdir(os.path.join(updates_dir, "releases", ver)):
                already_ingested_count += 1
                continue
            valid_new.append((parse_semver(ver), fname, raw, verified))
        except UpdateVerificationError as exc:
            skipped.append({"filename": fname, "reason": str(exc)})
        except Exception as exc:
            skipped.append({"filename": fname, "reason": str(exc)})

    installed_version: Optional[str] = None
    if valid_new:
        valid_new.sort(key=lambda item: item[0], reverse=True)
        _, best_name, best_raw, _ = valid_new[0]
        res = install_verified_update_bytes(
            best_raw,
            updates_dir=updates_dir,
            base_version=base_version,
            verifier_pubkey=verifier_pubkey,
            source="install_dir",
            source_filename=best_name,
        )
        installed_version = res["version"]

    return {
        "found_count": len(candidates),
        "installed_version": installed_version,
        "already_ingested_count": already_ingested_count,
        "skipped": skipped,
    }


def count_pending_install_dir_updates(
    *,
    updates_dir: str,
    install_dir: str,
    base_version: str,
    verifier_pubkey: Optional[bytes] = None,
) -> int:
    """轻量统计 <install_dir>/updates/ 中尚未吸纳的合法新包数量."""
    scan_dir = os.path.join(install_dir, "updates")
    if not os.path.isdir(scan_dir):
        return 0
    state = load_update_state(updates_dir)
    ingested_hashes = state.get("ingested_hashes") or {}
    pending = 0
    for fname in sorted(os.listdir(scan_dir)):
        low = fname.lower()
        if not low.endswith(_ALLOWED_UPDATE_EXTS):
            continue
        full = os.path.join(scan_dir, fname)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        try:
            if os.path.getsize(full) > MAX_PACKAGE_BYTES:
                continue
            with open(full, "rb") as fh:
                raw = fh.read(MAX_PACKAGE_BYTES + 1)
            verified = verify_update_package_bytes(
                raw, base_version=base_version, verifier_pubkey=verifier_pubkey
            )
            ver = verified["manifest"]["version"]
            if verified["dgp_sha256"] not in ingested_hashes or not os.path.isdir(
                os.path.join(updates_dir, "releases", ver)
            ):
                pending += 1
        except Exception:
            continue
    return pending


# =========================================================================
# sys.meta_path[0] zipimport 补丁加载器与启动崩溃自动回退守护
# =========================================================================


class _VerifiedPatchMetaPathFinder(importlib.abc.MetaPathFinder):
    """优先从已验签 code.zip 通过 zipimport 加载 core.* 与 modules.*（先于 PyiFrozenImporter）."""

    def __init__(self, code_zip_path: str) -> None:
        self.code_zip_path = code_zip_path
        self._importer = zipimport.zipimporter(code_zip_path)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None):
        if fullname not in ("core", "modules") and not fullname.startswith(("core.", "modules.")):
            return None
        try:
            return self._importer.find_spec(fullname, target)
        except Exception:
            return None

    def invalidate_caches(self) -> None:
        try:
            self._importer.invalidate_caches()
        except Exception:
            pass


_RUNTIME_STATE: Dict[str, Any] = {
    "enabled": False,
    "active": False,
    "base_version": "",
    "active_version": None,
    "active_static_dir": None,
    "active_code_zip": None,
    "updates_dir": "",
    "install_dir": "",
    "verifier_pubkey": None,
    "finder": None,
    "boot_message": "",
    "rolled_back_from": None,
    "retired_reason": None,
    "ingested_on_boot": None,
    "marked_healthy": False,
}


def _unload_finder_and_modules() -> None:
    finder = _RUNTIME_STATE.get("finder")
    if finder is not None:
        sys.meta_path[:] = [f for f in sys.meta_path if f is not finder]
    else:
        sys.meta_path[:] = [
            f for f in sys.meta_path if not isinstance(f, _VerifiedPatchMetaPathFinder)
        ]
    _RUNTIME_STATE["finder"] = None
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("core", "modules") or mod_name.startswith(("core.", "modules.")):
            sys.modules.pop(mod_name, None)
    importlib.invalidate_caches()


def _select_fallback_target(
    updates_dir: str,
    *,
    failed_version: str,
    previous_version: Optional[str],
    base_version: str,
    verifier_pubkey: Optional[bytes],
) -> Optional[str]:
    if not previous_version or previous_version == failed_version:
        return None
    prev_dir = os.path.join(updates_dir, "releases", previous_version)
    if not os.path.isdir(prev_dir):
        return None
    try:
        verify_installed_release_dir(
            prev_dir,
            base_version=base_version,
            verifier_pubkey=verifier_pubkey,
        )
        return previous_version
    except Exception:
        return None


def bootstrap_runtime_patch(
    *,
    base_version: str,
    updates_dir: Optional[str] = None,
    install_dir: Optional[str] = None,
    verifier_pubkey: Optional[bytes] = None,
    force_enable: Optional[bool] = None,
) -> Dict[str, Any]:
    """在 app.py 启动极早期执行增量包扫描、验签激活与防崩溃回退守护.

    非冻结开发/单元测试模式下默认关闭（仅当 force_enable=True 或设置 DATAOG_ENABLE_PATCH_LOADER=1 时启用），
    避免研发态单元测试意外加载本机私有保险箱中的客户补丁。
    """
    base_norm = normalize_semver(base_version)
    eff_updates_dir = os.path.abspath(updates_dir) if updates_dir else resolve_updates_dir()
    eff_install_dir = resolve_install_dir(install_dir)
    is_enabled = (
        bool(force_enable)
        if force_enable is not None
        else (bool(getattr(sys, "frozen", False)) or os.environ.get("DATAOG_ENABLE_PATCH_LOADER") == "1")
    )

    _RUNTIME_STATE.update(
        {
            "enabled": is_enabled,
            "active": False,
            "base_version": base_norm,
            "active_version": None,
            "active_static_dir": None,
            "active_code_zip": None,
            "updates_dir": eff_updates_dir,
            "install_dir": eff_install_dir,
            "verifier_pubkey": verifier_pubkey,
            "boot_message": "",
            "rolled_back_from": None,
            "retired_reason": None,
            "ingested_on_boot": None,
            "marked_healthy": False,
        }
    )

    if not is_enabled:
        return dict(_RUNTIME_STATE)

    # 1. 只读扫描 <install_dir>/updates/ 吸纳最新签名更新包
    try:
        scan_res = scan_and_ingest_install_dir_updates(
            updates_dir=eff_updates_dir,
            install_dir=eff_install_dir,
            base_version=base_norm,
            verifier_pubkey=verifier_pubkey,
        )
        _RUNTIME_STATE["ingested_on_boot"] = scan_res.get("installed_version")
    except Exception as exc:
        _RUNTIME_STATE["boot_message"] = f"扫描 updates 目录出现异常（已忽略并继续启动）: {exc}"

    # 若尚未存在 rt_upd 目录与任何安装补丁，保持零磁盘写入（守护干净启动隐私断言）
    if not os.path.exists(os.path.join(eff_updates_dir, "state.json")):
        return dict(_RUNTIME_STATE)

    state = load_update_state(eff_updates_dir)
    target_ver = state.get("active_version")
    if not target_ver:
        return dict(_RUNTIME_STATE)

    # 2. 检查活动补丁与当前基础客户端底座版本兼容性（若用户覆盖安装更新版 exe，自动退役旧补丁）
    try:
        target_norm = normalize_semver(str(target_ver))
        ok_compat, compat_msg = check_version_compatibility(target_norm, base_norm)
    except ValueError as exc:
        ok_compat, compat_msg = False, str(exc)
        target_norm = str(target_ver)

    if not ok_compat:
        retire_note = f"当前底座已为 v{base_norm}，自动退役历史增量补丁 v{target_norm}（{compat_msg}）"
        state["active_version"] = None
        if state.get("previous_version") == target_norm:
            state["previous_version"] = None
        state["retired_reason"] = retire_note
        save_update_state(eff_updates_dir, state)
        _RUNTIME_STATE["retired_reason"] = retire_note
        _RUNTIME_STATE["boot_message"] = retire_note
        return dict(_RUNTIME_STATE)

    # 3. 连续启动失败超过阈值时自动回退
    guard = load_boot_guard(eff_updates_dir)
    if (
        guard.get("version") == target_norm
        and not bool(guard.get("healthy", False))
        and int(guard.get("consecutive_failures") or 0) >= MAX_BOOT_FAILURES
    ):
        fb = _select_fallback_target(
            eff_updates_dir,
            failed_version=target_norm,
            previous_version=state.get("previous_version"),
            base_version=base_norm,
            verifier_pubkey=verifier_pubkey,
        )
        rb_reason = f"增量补丁 v{target_norm} 连续 {MAX_BOOT_FAILURES} 次启动异常，已自动回退至{' v' + fb if fb else '内置基础版本 v' + base_norm}"
        state["active_version"] = fb
        state["last_rollback_reason"] = rb_reason
        if state.get("previous_version") == target_norm:
            state["previous_version"] = None
        save_update_state(eff_updates_dir, state)
        save_boot_guard(
            eff_updates_dir,
            {"version": fb, "consecutive_failures": 0, "last_attempt_at": _utc_now_iso(), "healthy": True},
        )
        _RUNTIME_STATE["rolled_back_from"] = target_norm
        _RUNTIME_STATE["boot_message"] = rb_reason
        target_norm = fb or ""
        if not target_norm:
            return dict(_RUNTIME_STATE)

    # 4. 启动前核验已落盘补丁 Ed25519 签名与全部资源 SHA-256
    release_dir = os.path.join(eff_updates_dir, "releases", target_norm)
    try:
        verified_dir = verify_installed_release_dir(
            release_dir,
            base_version=base_norm,
            verifier_pubkey=verifier_pubkey,
        )
    except Exception as exc:
        fb = _select_fallback_target(
            eff_updates_dir,
            failed_version=target_norm,
            previous_version=state.get("previous_version"),
            base_version=base_norm,
            verifier_pubkey=verifier_pubkey,
        )
        rb_reason = f"增量补丁 v{target_norm} 启动核验失败（{exc}），已自动回退至{' v' + fb if fb else '内置基础版本 v' + base_norm}"
        state["active_version"] = fb
        state["last_rollback_reason"] = rb_reason
        if state.get("previous_version") == target_norm:
            state["previous_version"] = None
        save_update_state(eff_updates_dir, state)
        _RUNTIME_STATE["rolled_back_from"] = target_norm
        _RUNTIME_STATE["boot_message"] = rb_reason
        if not fb:
            return dict(_RUNTIME_STATE)
        target_norm = fb
        release_dir = os.path.join(eff_updates_dir, "releases", target_norm)
        try:
            verified_dir = verify_installed_release_dir(
                release_dir,
                base_version=base_norm,
                verifier_pubkey=verifier_pubkey,
            )
        except Exception:
            state["active_version"] = None
            save_update_state(eff_updates_dir, state)
            return dict(_RUNTIME_STATE)

    # 5. 记录本次启动尝试计数（mark_boot_healthy 成功后清零）并挂载 sys.meta_path[0]
    save_boot_guard(
        eff_updates_dir,
        {
            "version": target_norm,
            "consecutive_failures": (
                int(guard.get("consecutive_failures") or 0) + 1
                if guard.get("version") == target_norm
                else 1
            ),
            "last_attempt_at": _utc_now_iso(),
            "healthy": False,
        },
    )

    _unload_finder_and_modules()
    finder = _VerifiedPatchMetaPathFinder(verified_dir["code_zip_path"])
    sys.meta_path.insert(0, finder)

    _RUNTIME_STATE.update(
        {
            "active": True,
            "active_version": target_norm,
            "active_static_dir": verified_dir["static_dir"],
            "active_code_zip": verified_dir["code_zip_path"],
            "finder": finder,
        }
    )
    return dict(_RUNTIME_STATE)


def mark_boot_healthy() -> None:
    """应用与插件成功初始化后清除启动失败计数，标记当前补丁为健康稳定版."""
    if not _RUNTIME_STATE.get("active") or _RUNTIME_STATE.get("marked_healthy"):
        return
    updates_dir = str(_RUNTIME_STATE.get("updates_dir") or "")
    ver = _RUNTIME_STATE.get("active_version")
    if updates_dir and ver:
        try:
            save_boot_guard(
                updates_dir,
                {
                    "version": ver,
                    "consecutive_failures": 0,
                    "last_attempt_at": _utc_now_iso(),
                    "healthy": True,
                },
            )
        except Exception:
            pass
    _RUNTIME_STATE["marked_healthy"] = True


def rollback_active_patch_and_unload(reason: str = "") -> Optional[str]:
    """当补丁导入或启动初始化失败时，立即卸载 Finder/模块并将 state 回退至上一稳定版或内置底座."""
    rolled_back_from = _RUNTIME_STATE.get("active_version")
    updates_dir = str(_RUNTIME_STATE.get("updates_dir") or "")
    base_version = str(_RUNTIME_STATE.get("base_version") or "0.0.0")
    verifier_pubkey = _RUNTIME_STATE.get("verifier_pubkey")

    _unload_finder_and_modules()

    _RUNTIME_STATE["active"] = False
    _RUNTIME_STATE["active_version"] = None
    _RUNTIME_STATE["active_static_dir"] = None
    _RUNTIME_STATE["active_code_zip"] = None
    _RUNTIME_STATE["marked_healthy"] = False
    _RUNTIME_STATE["rolled_back_from"] = rolled_back_from

    if updates_dir and os.path.isdir(updates_dir) and rolled_back_from:
        try:
            state = load_update_state(updates_dir)
            fb = _select_fallback_target(
                updates_dir,
                failed_version=str(rolled_back_from),
                previous_version=state.get("previous_version"),
                base_version=base_version,
                verifier_pubkey=verifier_pubkey,
            )
            detail = (reason or "启动异常").strip()
            rb_msg = f"增量补丁 v{rolled_back_from} 启动失败（{detail}），已自动回退至{' v' + fb if fb else '内置基础版本 v' + base_version}"
            state["active_version"] = fb
            state["last_rollback_reason"] = rb_msg
            if state.get("previous_version") == rolled_back_from:
                state["previous_version"] = None
            save_update_state(updates_dir, state)
            save_boot_guard(
                updates_dir,
                {
                    "version": fb,
                    "consecutive_failures": 0,
                    "last_attempt_at": _utc_now_iso(),
                    "healthy": True,
                },
            )
            _RUNTIME_STATE["boot_message"] = rb_msg
        except Exception:
            pass
    return rolled_back_from


def get_active_patch_state() -> Dict[str, Any]:
    """返回当前进程增量补丁激活状态只读副本（不含 finder 实例）."""
    out = dict(_RUNTIME_STATE)
    out.pop("finder", None)
    out.pop("verifier_pubkey", None)
    return out


def get_active_patch_static_dir() -> Optional[str]:
    """若当前运行在已验证补丁模式下，返回补丁静态资源目录路径."""
    if _RUNTIME_STATE.get("active"):
        cand = _RUNTIME_STATE.get("active_static_dir")
        if cand and os.path.isdir(cand):
            return str(cand)
    return None


def get_runtime_base_version(fallback: str = "") -> str:
    """返回当前底座可执行程序版本."""
    base = str(_RUNTIME_STATE.get("base_version") or "").strip()
    return base or str(fallback or "").strip()


def is_patch_active() -> bool:
    """返回当前进程是否由增量补丁驱动运行."""
    return bool(_RUNTIME_STATE.get("active"))
