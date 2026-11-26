"""Data.O.G 4.0 外置插件加密签名封包、解包与契约校验核心 (core/platform/plugin_package.py).

设计目标：
1. 独立于 `DGP2` 系统增量更新协议：采用 `DGAP` 魔数头与独立域隔离前缀 (`DataOG-Plugin-v1:`)，
   复用 `update_bootstrap` 纯标准库 Ed25519 / BLAKE2b-512-CTR / HMAC-SHA256 / DGPB 原语，严禁跨协议重放；
2. 零明文源码/脚本外露：加密后 `.dogapp` 不含 ZIP/明文文件头或源码特征，外层 `.zip` 仅含
   `plugins/DataOG-Plugin-<id>-v<ver>.dogapp` 与 `PLUGIN_README.txt`，适配企业/银行邮件网关传输；
3. 严禁编译态扩展与二进制载荷（`.pyd`/`.so`/`.dylib`/`.dll`/`.exe` 等），保证单一 `.dogapp` 跨平台通用。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import struct
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import zipfile
import zlib

from core.platform.storage.schema import PRIVATE_ONLY_TABLES, WORKSPACE_BUSINESS_TABLES, WORKSPACE_META_KEYS
import update_bootstrap

PLATFORM_ABI_VERSION = "4.0"
DGP_PLUGIN_MAGIC = b"DGAP"
PLUGIN_FORMAT_VERSION = 1
PLUGIN_HEADER_CORE_STRUCT = struct.Struct("!4sHHB3s24sII")
PLUGIN_HEADER_CORE_SIZE = PLUGIN_HEADER_CORE_STRUCT.size  # 44 bytes
PLUGIN_MAC_SIZE = 32
PLUGIN_SIG_SIZE = 64
PLUGIN_HEADER_SIZE = PLUGIN_HEADER_CORE_SIZE + PLUGIN_MAC_SIZE + PLUGIN_SIG_SIZE  # 140 bytes
PLUGIN_CIPHER_LABEL = "BLAKE2b-512-CTR+HMAC-SHA256"

PLUGIN_SIG_DOMAIN = b"DataOG-Plugin-v1:"
PLUGIN_MANIFEST_SIG_DOMAIN = b"DataOG-Plugin-v1-Manifest:"

MAX_PLUGIN_PACKAGE_BYTES = 32 * 1024 * 1024  # 32 MB
MAX_PLUGIN_UNCOMPRESSED_BYTES = 80 * 1024 * 1024  # 80 MB
MAX_PLUGIN_ENTRIES = 2000

_SAFE_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_SAFE_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_SAFE_SQL_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_FIXED_ZIP_DT = (2025, 1, 1, 0, 0, 0)

RESERVED_PLUGIN_IDS = frozenset(
    {
        "standard_migrator",
        "cross_comparator",
        "ddl_translator",
        "data_explorer",
        "ai_assistant",
        "settings",
        "explorer",
        "standard",
        "polyglot",
        "platform",
        "core",
        "modules",
        "plugins",
    }
)

FORBIDDEN_BINARY_EXTS = frozenset(
    {
        ".pyd",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".vbs",
        ".jar",
        ".pyc",
        ".pyo",
        ".com",
        ".msi",
    }
)

_PROTECTED_BUILTIN_TABLES = frozenset(
    set(PRIVATE_ONLY_TABLES)
    | set(WORKSPACE_BUSINESS_TABLES)
    | {
        "kb_documents",
        "kb_chunks",
        "kb_embeddings",
        "ai_assistant_sessions",
        "ai_assistant_messages",
        "ai_assistant_proposals",
        "ai_assistant_attachments",
        "ai_assistant_attachment_chunks",
        "ai_assistant_runs",
        "ai_assistant_run_events",
    }
)

_ALLOWED_PLUGIN_EXTS = (".dogapp", ".zip", ".dat")


class PluginVerificationError(ValueError):
    """外置插件包格式、签名、完整性、二进制禁用或版本兼容性校验失败."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_dgap_bytes(raw_bytes: bytes) -> bool:
    return isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) >= 4 and bytes(raw_bytes[:4]) == DGP_PLUGIN_MAGIC


def derive_plugin_envelope_keys(pubkey: bytes, nonce: bytes) -> Tuple[bytes, bytes]:
    """基于独立插件域盐值、验签公钥与随机 24B Nonce 派生密钥."""
    if len(pubkey) != 32:
        raise ValueError("pubkey 必须为 32 字节")
    if len(nonce) != 24:
        raise ValueError("nonce 必须为 24 字节")
    pepper = update_bootstrap.reconstruct_envelope_pepper()
    ikm = pepper + b"DataOG-Plugin-v1:" + pubkey + nonce
    enc_key = hashlib.blake2b(ikm, person=b"DGAP-ENC-V01\x00\x00\x00\x00", digest_size=32).digest()
    mac_key = hashlib.blake2b(ikm, person=b"DGAP-MAC-V01\x00\x00\x00\x00", digest_size=32).digest()
    return enc_key, mac_key


def _dgap_envelope_sign_message(header_core: bytes, ciphertext: bytes) -> bytes:
    return PLUGIN_SIG_DOMAIN + header_core + hashlib.sha256(ciphertext).digest()


def _ed25519_derive_pubkey(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 私钥种子必须为 32 字节")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return update_bootstrap._ed_encodepoint(update_bootstrap._ed_scalarmult(update_bootstrap._ED_BASE, a))


def _ed25519_sign_deterministic(msg: bytes, seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 私钥种子必须为 32 字节")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:64]
    pk = update_bootstrap._ed_encodepoint(update_bootstrap._ed_scalarmult(update_bootstrap._ED_BASE, a))
    r_digest = hashlib.sha512(prefix + msg).digest()
    r = int.from_bytes(r_digest, "little") % update_bootstrap._ED_L
    r_bytes = update_bootstrap._ed_encodepoint(update_bootstrap._ed_scalarmult(update_bootstrap._ED_BASE, r))
    k_digest = hashlib.sha512(r_bytes + pk + msg).digest()
    k = int.from_bytes(k_digest, "little") % update_bootstrap._ED_L
    s = (r + k * a) % update_bootstrap._ED_L
    return r_bytes + s.to_bytes(32, "little")


def _validate_rel_entry_path(rel_path: str) -> str:
    norm = str(rel_path or "").strip()
    if not norm or "\x00" in norm:
        raise PluginVerificationError("PATH_TRAVERSAL", "插件包包含非法空路径或 NUL 字节")
    if "\\" in norm or norm.startswith("/") or norm.endswith("/"):
        raise PluginVerificationError("PATH_TRAVERSAL", f"插件包包含非法路径格式: {norm}")
    first_seg = norm.split("/", 1)[0]
    if ":" in first_seg:
        raise PluginVerificationError("PATH_TRAVERSAL", f"插件包包含非法盘符路径: {norm}")
    parts = norm.split("/")
    if any(not p or p in (".", "..") for p in parts):
        raise PluginVerificationError("PATH_TRAVERSAL", f"插件包包含目录穿越路径: {norm}")
    low = norm.lower()
    for ext in FORBIDDEN_BINARY_EXTS:
        if low.endswith(ext):
            raise PluginVerificationError("FORBIDDEN_CONTENT", f"插件包严禁携带可执行或编译态二进制文件: {norm}")
    return "/".join(parts)


def check_platform_compatibility(
    min_platform_version: str,
    max_platform_version: Optional[str],
    platform_version: str,
) -> Tuple[bool, str]:
    try:
        cur_t = update_bootstrap.parse_semver(platform_version)
        min_t = update_bootstrap.parse_semver(min_platform_version)
        max_t = update_bootstrap.parse_semver(max_platform_version) if max_platform_version else None
    except ValueError as exc:
        return False, str(exc)

    if cur_t < min_t:
        return False, f"当前 Data.O.G 版本 (v{platform_version}) 低于插件最低要求 (v{min_platform_version})"
    if max_t is not None and cur_t > max_t:
        return False, f"当前 Data.O.G 版本 (v{platform_version}) 高于插件兼容上限 (v{max_platform_version})"
    return True, ""


def validate_plugin_manifest(
    manifest: Any,
    *,
    platform_version: Optional[str] = None,
) -> Dict[str, Any]:
    """严格校验 `plugin.json` 契约与保留 ID/表名隔离边界."""
    if not isinstance(manifest, dict):
        raise PluginVerificationError("INVALID_MANIFEST", "plugin.json 必须为 JSON 对象")

    schema_ver = manifest.get("schema_version")
    if schema_ver != 1:
        raise PluginVerificationError("INVALID_MANIFEST", f"不支持的 plugin.json schema_version: {schema_ver!r}")

    api_ver = str(manifest.get("api_version") or "").strip()
    if api_ver != PLATFORM_ABI_VERSION:
        raise PluginVerificationError(
            "INCOMPATIBLE_ABI",
            f"插件契约版本 ({api_ver or '空'}) 与当前平台 ABI ({PLATFORM_ABI_VERSION}) 不匹配",
        )

    plugin_id = str(manifest.get("plugin_id") or "").strip()
    if not _SAFE_PLUGIN_ID_RE.match(plugin_id):
        raise PluginVerificationError("INVALID_MANIFEST", f"非法的 plugin_id: {plugin_id!r}（需 ^[a-z][a-z0-9_]{{1,47}}$）")
    if plugin_id in RESERVED_PLUGIN_IDS:
        raise PluginVerificationError("RESERVED_ID", f"plugin_id={plugin_id!r} 为平台保留标识，禁止使用")

    name = str(manifest.get("name") or "").strip()
    if not name:
        raise PluginVerificationError("INVALID_MANIFEST", "plugin.json 缺少非空插件名称 name")

    try:
        version = update_bootstrap.normalize_semver(str(manifest.get("version") or ""))
        min_plat = update_bootstrap.normalize_semver(str(manifest.get("min_platform_version") or "4.0.0"))
        raw_max = str(manifest.get("max_platform_version") or "").strip()
        max_plat = update_bootstrap.normalize_semver(raw_max) if raw_max else ""
    except ValueError as exc:
        raise PluginVerificationError("INVALID_MANIFEST", f"plugin.json 版本号声明无效: {exc}") from exc

    if max_plat and update_bootstrap.parse_semver(max_plat) < update_bootstrap.parse_semver(min_plat):
        raise PluginVerificationError("INVALID_MANIFEST", "max_platform_version 不得小于 min_platform_version")

    if platform_version:
        ok_compat, compat_msg = check_platform_compatibility(min_plat, max_plat or None, platform_version)
        if not ok_compat:
            raise PluginVerificationError("INCOMPATIBLE_VERSION", compat_msg)

    desktop_app = manifest.get("desktop_app")
    if not isinstance(desktop_app, dict):
        raise PluginVerificationError("INVALID_MANIFEST", "plugin.json 缺少 desktop_app 对象")
    app_id = str(desktop_app.get("app_id") or "").strip().lower()
    if not _SAFE_APP_ID_RE.match(app_id) or app_id in RESERVED_PLUGIN_IDS:
        raise PluginVerificationError("INVALID_MANIFEST", f"非法的或保留的 desktop_app.app_id: {app_id!r}")
    app_title = str(desktop_app.get("title") or name).strip()
    entry_js = _validate_rel_entry_path(str(desktop_app.get("entry_js") or "register.js"))
    raw_styles = desktop_app.get("stylesheets") or []
    if not isinstance(raw_styles, list):
        raise PluginVerificationError("INVALID_MANIFEST", "desktop_app.stylesheets 必须为数组")
    stylesheets = [_validate_rel_entry_path(str(s)) for s in raw_styles if str(s or "").strip()]

    backend = manifest.get("backend")
    if not isinstance(backend, dict):
        raise PluginVerificationError("INVALID_MANIFEST", "plugin.json 缺少 backend 对象")
    expected_prefix = f"/api/modules/{plugin_id}"
    api_prefix = str(backend.get("api_prefix") or expected_prefix).strip()
    if api_prefix != expected_prefix:
        raise PluginVerificationError(
            "INVALID_MANIFEST",
            f"backend.api_prefix 必须为 {expected_prefix!r}，当前为 {api_prefix!r}",
        )
    backend_entry = str(backend.get("entry") or "backend/__init__.py").strip()
    if backend_entry != "backend/__init__.py":
        raise PluginVerificationError("INVALID_MANIFEST", f"backend.entry 必须为 'backend/__init__.py'")

    storage = manifest.get("storage") or {}
    if not isinstance(storage, dict):
        raise PluginVerificationError("INVALID_MANIFEST", "plugin.json storage 必须为对象")

    def _clean_tables(key: str) -> List[str]:
        vals = storage.get(key) or []
        if not isinstance(vals, list):
            raise PluginVerificationError("INVALID_MANIFEST", f"storage.{key} 必须为数组")
        cleaned: List[str] = []
        for item in vals:
            t = str(item or "").strip()
            if not t:
                continue
            if not _SAFE_SQL_IDENT_RE.match(t) or t.lower().startswith("sqlite_") or t in _PROTECTED_BUILTIN_TABLES:
                raise PluginVerificationError("FORBIDDEN_CONTENT", f"storage.{key} 包含非法或受保护的表名: {t!r}")
            if t not in cleaned:
                cleaned.append(t)
        return cleaned

    ws_tables = _clean_tables("workspace_tables")
    priv_tables = _clean_tables("private_tables")
    overlap = set(ws_tables) & set(priv_tables)
    if overlap:
        raise PluginVerificationError("INVALID_MANIFEST", f"插件表不得同时归属工作区与私有库: {sorted(overlap)}")

    raw_meta_keys = storage.get("workspace_meta_keys") or []
    if not isinstance(raw_meta_keys, list):
        raise PluginVerificationError("INVALID_MANIFEST", "storage.workspace_meta_keys 必须为数组")
    ws_meta_keys: List[str] = []
    for mk in raw_meta_keys:
        clean_mk = str(mk or "").strip()
        if not clean_mk:
            continue
        if clean_mk in WORKSPACE_META_KEYS:
            raise PluginVerificationError("FORBIDDEN_CONTENT", f"storage.workspace_meta_keys 禁止占用系统元键: {clean_mk!r}")
        if clean_mk not in ws_meta_keys:
            ws_meta_keys.append(clean_mk)

    normalized = dict(manifest)
    normalized["schema_version"] = 1
    normalized["api_version"] = PLATFORM_ABI_VERSION
    normalized["plugin_id"] = plugin_id
    normalized["name"] = name
    normalized["version"] = version
    normalized["summary"] = str(manifest.get("summary") or "").strip()
    normalized["author"] = str(manifest.get("author") or "").strip()
    normalized["min_platform_version"] = min_plat
    normalized["max_platform_version"] = max_plat
    normalized["desktop_app"] = {
        "app_id": app_id,
        "title": app_title,
        "default_summary": str(desktop_app.get("default_summary") or normalized["summary"] or app_title).strip(),
        "entry_js": entry_js,
        "stylesheets": stylesheets,
        "order": int(desktop_app.get("order") or 100),
    }
    normalized["backend"] = {
        "entry": "backend/__init__.py",
        "api_prefix": expected_prefix,
    }
    normalized["storage"] = {
        "workspace_tables": ws_tables,
        "private_tables": priv_tables,
        "workspace_meta_keys": ws_meta_keys,
    }
    return normalized


def seal_plugin_bundle(
    raw_bundle: bytes,
    *,
    seed: bytes,
    nonce: Optional[bytes] = None,
) -> bytes:
    """使用 DGAP + Ed25519 + HMAC-SHA256 + BLAKE2b-512-CTR 加密混淆打包插件."""
    if len(seed) != 32:
        raise ValueError("Ed25519 私钥种子必须为 32 字节")
    if nonce is None:
        eff_nonce = secrets.token_bytes(24)
    else:
        if len(nonce) != 24:
            raise ValueError("DGAP Nonce 必须为 24 字节")
        eff_nonce = bytes(nonce)

    compressed = zlib.compress(raw_bundle, level=9)
    pubkey = _ed25519_derive_pubkey(seed)
    enc_key, mac_key = derive_plugin_envelope_keys(pubkey, eff_nonce)
    ciphertext = update_bootstrap.dgp2_ctr_xor(compressed, enc_key, eff_nonce)

    header_core = PLUGIN_HEADER_CORE_STRUCT.pack(
        DGP_PLUGIN_MAGIC,
        PLUGIN_FORMAT_VERSION,
        update_bootstrap.DGP2_FLAG_ZLIB | update_bootstrap.DGP2_FLAG_ENCRYPTED,
        update_bootstrap.DGP2_CIPHER_BLAKE2B_CTR_HMAC,
        b"\x00\x00\x00",
        eff_nonce,
        len(raw_bundle),
        len(ciphertext),
    )
    envelope_mac = hmac.new(mac_key, header_core + ciphertext, hashlib.sha256).digest()
    envelope_sig = _ed25519_sign_deterministic(_dgap_envelope_sign_message(header_core, ciphertext), seed)
    return header_core + envelope_mac + envelope_sig + ciphertext


def unseal_plugin_bytes(dogapp_bytes: bytes, *, pubkey: bytes) -> Dict[str, bytes]:
    """严格按『先 Ed25519 验签 -> 再 HMAC-SHA256 认证 -> 后解密 -> 最后限流解压』解封 DGAP 信封."""
    if not dogapp_bytes:
        raise PluginVerificationError("INVALID_ARCHIVE", "插件包内容为空")
    if len(dogapp_bytes) > MAX_PLUGIN_PACKAGE_BYTES:
        raise PluginVerificationError("SIZE_LIMIT", "插件包体积超出 32 MB 安全上限")
    if len(dogapp_bytes) < PLUGIN_HEADER_SIZE:
        raise PluginVerificationError("INVALID_ARCHIVE", "插件包头部不完整")

    header_core = bytes(dogapp_bytes[:PLUGIN_HEADER_CORE_SIZE])
    magic, fmt_ver, flags, cipher_id, reserved, nonce, plaintext_len, ciphertext_len = (
        PLUGIN_HEADER_CORE_STRUCT.unpack(header_core)
    )
    if (
        magic != DGP_PLUGIN_MAGIC
        or fmt_ver != PLUGIN_FORMAT_VERSION
        or flags != (update_bootstrap.DGP2_FLAG_ZLIB | update_bootstrap.DGP2_FLAG_ENCRYPTED)
        or cipher_id != update_bootstrap.DGP2_CIPHER_BLAKE2B_CTR_HMAC
        or reserved != b"\x00\x00\x00"
    ):
        raise PluginVerificationError("INVALID_ARCHIVE", "无效的 Data.O.G 插件加密信封头 (需 DGAP 格式)")
    if ciphertext_len <= 0 or PLUGIN_HEADER_SIZE + ciphertext_len != len(dogapp_bytes):
        raise PluginVerificationError("INVALID_ARCHIVE", "插件包密文长度与文件尺寸不匹配")

    mac_start = PLUGIN_HEADER_CORE_SIZE
    sig_start = mac_start + PLUGIN_MAC_SIZE
    envelope_mac = bytes(dogapp_bytes[mac_start:sig_start])
    envelope_sig = bytes(dogapp_bytes[sig_start:PLUGIN_HEADER_SIZE])
    ciphertext = bytes(dogapp_bytes[PLUGIN_HEADER_SIZE:])

    if not update_bootstrap.ed25519_verify(_dgap_envelope_sign_message(header_core, ciphertext), envelope_sig, pubkey):
        raise PluginVerificationError("BAD_SIGNATURE", "插件包数字签名校验失败，来源不可信或已被篡改")

    enc_key, mac_key = derive_plugin_envelope_keys(pubkey, nonce)
    expected_mac = hmac.new(mac_key, header_core + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(envelope_mac, expected_mac):
        raise PluginVerificationError("BAD_SIGNATURE", "插件包完整性 MAC 校验失败，密文已被篡改")

    if plaintext_len <= 0 or plaintext_len > MAX_PLUGIN_UNCOMPRESSED_BYTES:
        raise PluginVerificationError("SIZE_LIMIT", "插件包声明解压体积非法或超出 80 MB 上限")

    compressed = update_bootstrap.dgp2_ctr_xor(ciphertext, enc_key, nonce)
    try:
        raw_bundle = update_bootstrap._decompress_zlib_limited(compressed, plaintext_len)
        files = update_bootstrap.unpack_dgp2_bundle(raw_bundle)
    except update_bootstrap.UpdateVerificationError as exc:
        raise PluginVerificationError(exc.code, exc.message) from exc
    return files


def extract_inner_dogapp_bytes(raw_bytes: bytes) -> Tuple[bytes, str]:
    """从直接 `.dogapp`/`.dat` 或外层 `.zip` 中提取唯一的 `DGAP` 插件加密信封."""
    if not raw_bytes:
        raise PluginVerificationError("INVALID_ARCHIVE", "插件包内容为空")
    if len(raw_bytes) > MAX_PLUGIN_PACKAGE_BYTES:
        raise PluginVerificationError("SIZE_LIMIT", "插件包体积超出 32 MB 安全上限")
    if is_dgap_bytes(raw_bytes):
        return bytes(raw_bytes), "dogapp"
    if update_bootstrap.is_dgp2_bytes(raw_bytes):
        raise PluginVerificationError("WRONG_PACKAGE_TYPE", "检测到系统增量更新包 (DGP2)，请前往「版本与升级」安装")

    try:
        zf = update_bootstrap._open_and_scan_zip(raw_bytes, max_raw=MAX_PLUGIN_PACKAGE_BYTES)
    except update_bootstrap.UpdateVerificationError as exc:
        raise PluginVerificationError(exc.code, exc.message) from exc

    with zf:
        infos = [zi for zi in zf.infolist() if not zi.is_dir()]
        dogapp_infos: List[zipfile.ZipInfo] = []
        for zi in infos:
            try:
                norm = update_bootstrap._validate_zip_member_path(zi.filename, zi)
            except update_bootstrap.UpdateVerificationError as exc:
                raise PluginVerificationError(exc.code, exc.message) from exc
            low = norm.lower()
            if (norm.startswith("updates/") or "/" not in norm) and low.endswith(".dgp"):
                raise PluginVerificationError("WRONG_PACKAGE_TYPE", "检测到系统增量更新包 (.dgp)，请前往「版本与升级」安装")
            in_root_or_plugins = "/" not in norm or (
                norm.startswith("plugins/") and "/" not in norm[len("plugins/") :]
            )
            if not in_root_or_plugins:
                continue
            if low.endswith(".dogapp") or (low.endswith(".dat") and zi.file_size >= PLUGIN_HEADER_SIZE):
                dogapp_infos.append(zi)
            elif not low.endswith((".txt", ".md")) and zi.file_size >= PLUGIN_HEADER_SIZE:
                with zf.open(zi, "r") as handle:
                    head = handle.read(4)
                if head == DGP_PLUGIN_MAGIC:
                    dogapp_infos.append(zi)

        if len(dogapp_infos) != 1:
            raise PluginVerificationError(
                "INVALID_ARCHIVE",
                "未识别为有效的 Data.O.G 插件包（需包含唯一 plugins/*.dogapp 或有效的 DGAP 加密载荷）",
            )
        try:
            dogapp_bytes = update_bootstrap._read_zip_member_limited(zf, dogapp_infos[0], MAX_PLUGIN_PACKAGE_BYTES)
        except update_bootstrap.UpdateVerificationError as exc:
            raise PluginVerificationError(exc.code, exc.message) from exc
        if update_bootstrap.is_dgp2_bytes(dogapp_bytes):
            raise PluginVerificationError("WRONG_PACKAGE_TYPE", "检测到系统增量更新包 (DGP2)，请前往「版本与升级」安装")
        return dogapp_bytes, "outer_zip"


def _validate_signed_package_manifest_dict(
    pkg_meta: Any,
    manifest: Dict[str, Any],
) -> Dict[str, str]:
    if not isinstance(pkg_meta, dict):
        raise PluginVerificationError("INVALID_ARCHIVE", "package_manifest.json 格式必须为对象")
    if str(pkg_meta.get("plugin_id") or "") != manifest["plugin_id"]:
        raise PluginVerificationError("INVALID_ARCHIVE", "package_manifest plugin_id 与 plugin.json 不一致")
    if str(pkg_meta.get("version") or "") != manifest["version"]:
        raise PluginVerificationError("INVALID_ARCHIVE", "package_manifest version 与 plugin.json 不一致")
    raw_files = pkg_meta.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise PluginVerificationError("INVALID_ARCHIVE", "package_manifest 缺失 files 哈希清单")
    clean: Dict[str, str] = {}
    for rel, sha in raw_files.items():
        norm_rel = _validate_rel_entry_path(str(rel))
        sha_s = str(sha or "").strip().lower()
        if not _HEX64_RE.match(sha_s):
            raise PluginVerificationError("INVALID_ARCHIVE", f"package_manifest 文件哈希无效: {norm_rel}")
        clean[norm_rel] = sha_s
    if "plugin.json" not in clean or "backend/__init__.py" not in clean:
        raise PluginVerificationError("INVALID_ARCHIVE", "插件包缺失核心文件 plugin.json 或 backend/__init__.py")
    entry_js_rel = f"frontend/{manifest['desktop_app']['entry_js']}"
    if entry_js_rel not in clean:
        raise PluginVerificationError("INVALID_ARCHIVE", f"插件包缺失声明的前端入口 {entry_js_rel}")
    return clean


def _verify_payload_map(
    files_map: Dict[str, bytes],
    *,
    platform_version: Optional[str],
    pubkey: bytes,
) -> Dict[str, Any]:
    for req in ("plugin.json", "package_manifest.json", "package_manifest.sig"):
        if req not in files_map:
            raise PluginVerificationError("INVALID_ARCHIVE", f"插件包缺失必要元数据文件: {req}")

    pkg_manifest_raw = files_map["package_manifest.json"]
    sig_raw = files_map["package_manifest.sig"]
    if len(pkg_manifest_raw) > 2 * 1024 * 1024:
        raise PluginVerificationError("SIZE_LIMIT", "package_manifest.json 体积超出安全上限")
    if len(sig_raw) != 64:
        raise PluginVerificationError("BAD_SIGNATURE", "插件签名 package_manifest.sig 长度无效")
    if not update_bootstrap.ed25519_verify(PLUGIN_MANIFEST_SIG_DOMAIN + pkg_manifest_raw, sig_raw, pubkey):
        raise PluginVerificationError("BAD_SIGNATURE", "插件清单二级签名验证失败")

    try:
        manifest_obj = json.loads(files_map["plugin.json"].decode("utf-8"))
        pkg_meta_obj = json.loads(pkg_manifest_raw.decode("utf-8"))
    except Exception as exc:
        raise PluginVerificationError("INVALID_ARCHIVE", f"插件清单 JSON 解析失败: {exc}") from exc

    manifest = validate_plugin_manifest(manifest_obj, platform_version=platform_version)
    expected_hashes = _validate_signed_package_manifest_dict(pkg_meta_obj, manifest)

    actual_payload_keys = {
        _validate_rel_entry_path(k)
        for k in files_map.keys()
        if k not in ("package_manifest.json", "package_manifest.sig")
    }
    expected_keys = set(expected_hashes.keys())
    if actual_payload_keys != expected_keys:
        missing = sorted(expected_keys - actual_payload_keys)
        extra = sorted(actual_payload_keys - expected_keys)
        raise PluginVerificationError(
            "CHECKSUM_MISMATCH",
            f"插件文件清单不一致（缺失={missing[:3]} 多余={extra[:3]}）",
        )

    for rel in sorted(expected_keys):
        if rel != "plugin.json" and not rel.startswith(("backend/", "frontend/", "README", "CHANGELOG", "LICENSE")):
            raise PluginVerificationError("FORBIDDEN_CONTENT", f"插件包包含未授权路径: {rel}")
        data = files_map[rel]
        if hashlib.sha256(data).hexdigest() != expected_hashes[rel]:
            raise PluginVerificationError("CHECKSUM_MISMATCH", f"插件文件 {rel} SHA-256 校验失败")

    return {
        "manifest": manifest,
        "package_manifest": pkg_meta_obj,
        "package_manifest_raw": pkg_manifest_raw,
        "sig_raw": sig_raw,
        "file_count": len(expected_keys),
    }


def verify_plugin_package_bytes(
    raw_bytes: bytes,
    *,
    platform_version: Optional[str] = None,
    verifier_pubkey: Optional[bytes] = None,
    include_payload: bool = False,
) -> Dict[str, Any]:
    """完整验签、解封与校验 `.dogapp`/`.zip`/`.dat` 插件包."""
    dogapp_bytes, container_type = extract_inner_dogapp_bytes(raw_bytes)
    dogapp_sha256 = hashlib.sha256(dogapp_bytes).hexdigest()
    pubkey = update_bootstrap.resolve_verifier_pubkey(verifier_pubkey)

    files_map = unseal_plugin_bytes(dogapp_bytes, pubkey=pubkey)
    checked = _verify_payload_map(files_map, platform_version=platform_version, pubkey=pubkey)

    res: Dict[str, Any] = {
        "manifest": checked["manifest"],
        "package_manifest": checked["package_manifest"],
        "dogapp_bytes": dogapp_bytes,
        "dogapp_sha256": dogapp_sha256,
        "container_type": container_type,
        "format_version": PLUGIN_FORMAT_VERSION,
        "encrypted": True,
        "cipher": PLUGIN_CIPHER_LABEL,
        "package_size": len(raw_bytes),
        "dogapp_size": len(dogapp_bytes),
        "file_count": checked["file_count"],
    }
    if include_payload:
        res["payload_files"] = files_map
    return res


def verify_installed_plugin_dir(
    pkg_dir: str,
    *,
    platform_version: Optional[str] = None,
    verifier_pubkey: Optional[bytes] = None,
) -> Dict[str, Any]:
    """启动或回退前复核已落盘插件目录的签名与全量文件 SHA-256."""
    if not pkg_dir or not os.path.isdir(pkg_dir) or os.path.islink(pkg_dir):
        raise PluginVerificationError("INVALID_ARCHIVE", "插件安装目录不存在或无效")

    pubkey = update_bootstrap.resolve_verifier_pubkey(verifier_pubkey)
    files_map: Dict[str, bytes] = {}
    abs_root = os.path.abspath(pkg_dir)
    real_root = os.path.realpath(abs_root)
    total = 0
    for cur_root, dirs, files in os.walk(abs_root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
        for fname in sorted(files):
            if fname.startswith(".") or fname.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(cur_root, fname)
            if os.path.islink(full):
                raise PluginVerificationError("PATH_TRAVERSAL", f"插件目录严禁包含符号链接: {fname}")
            if not os.path.realpath(full).startswith(real_root + os.sep):
                raise PluginVerificationError("PATH_TRAVERSAL", f"插件目录存在越界文件: {fname}")
            rel = os.path.relpath(full, abs_root).replace(os.sep, "/")
            _validate_rel_entry_path(rel)
            size = os.path.getsize(full)
            total += size
            if total > MAX_PLUGIN_UNCOMPRESSED_BYTES:
                raise PluginVerificationError("SIZE_LIMIT", "已安装插件体积超出安全上限")
            with open(full, "rb") as fh:
                files_map[rel] = fh.read()

    checked = _verify_payload_map(files_map, platform_version=platform_version, pubkey=pubkey)
    return {
        "manifest": checked["manifest"],
        "package_manifest": checked["package_manifest"],
        "pkg_dir": abs_root,
        "backend_dir": os.path.join(abs_root, "backend"),
        "frontend_dir": os.path.join(abs_root, "frontend"),
    }


def assert_gateway_safe_plugin_package(
    outer_zip_bytes: bytes,
    dogapp_bytes: bytes,
    plugin_id: str,
    version: str,
) -> None:
    """构建期强校验：确保密文零明文特征且外层 ZIP 符合企业邮件网关安全规范."""
    if not dogapp_bytes.startswith(DGP_PLUGIN_MAGIC):
        raise PluginVerificationError("INVALID_ARCHIVE", ".dogapp 载荷缺少 DGAP 加密魔数头")
    if zipfile.is_zipfile(io.BytesIO(dogapp_bytes)) or dogapp_bytes[:2] in (b"PK", b"MZ", b"\x1f\x8b", b"\x78\x9c"):
        raise PluginVerificationError("INVALID_ARCHIVE", ".dogapp 密文不允许包含 ZIP/压缩明文特征头")
    for marker in (b"plugin.json", b"backend/__init__.py", b"package_manifest.json", b"registerDesktopApp", b"MODULE_SPEC"):
        if marker in dogapp_bytes:
            raise PluginVerificationError("FORBIDDEN_CONTENT", f".dogapp 密文检测到未加密明文特征: {marker!r}")

    expected_entry = f"plugins/DataOG-Plugin-{plugin_id}-v{version}.dogapp"
    with zipfile.ZipFile(io.BytesIO(outer_zip_bytes), "r") as zf:
        names = [zi.filename for zi in zf.infolist() if not zi.is_dir()]
        if set(names) != {expected_entry, "PLUGIN_README.txt"}:
            raise PluginVerificationError("INVALID_ARCHIVE", f"外层插件 ZIP 条目集合非预期: {names}")


def collect_plugin_source_entries(plugin_dir: str) -> Tuple[Dict[str, Any], List[Tuple[str, bytes]]]:
    """扫描插件工作区目录，校验并收集待封包文件条目."""
    abs_dir = os.path.abspath(plugin_dir)
    manifest_path = os.path.join(abs_dir, "plugin.json")
    if not os.path.isfile(manifest_path):
        raise PluginVerificationError("INVALID_MANIFEST", f"找不到 plugin.json: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = validate_plugin_manifest(json.load(fh))

    entries: List[Tuple[str, bytes]] = []
    with open(manifest_path, "rb") as fh:
        entries.append(("plugin.json", fh.read()))

    for top_dir in ("backend", "frontend"):
        sub_path = os.path.join(abs_dir, top_dir)
        if not os.path.isdir(sub_path):
            raise PluginVerificationError("INVALID_ARCHIVE", f"插件目录缺失 {top_dir}/ 子目录")
        for cur_root, dirs, files in os.walk(sub_path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            for fname in sorted(files):
                if fname.startswith(".") or fname in (".DS_Store", "Thumbs.db", "Desktop.ini"):
                    continue
                full = os.path.join(cur_root, fname)
                if os.path.islink(full):
                    raise PluginVerificationError("PATH_TRAVERSAL", f"插件禁止包含符号链接: {full}")
                rel = os.path.relpath(full, abs_dir).replace(os.sep, "/")
                norm_rel = _validate_rel_entry_path(rel)
                with open(full, "rb") as fh:
                    entries.append((norm_rel, fh.read()))

    entries.sort(key=lambda x: x[0])
    return manifest, entries


def build_plugin_package(
    plugin_dir: str,
    *,
    output_dir: str,
    seed: Optional[bytes] = None,
    seed_file: Optional[str] = None,
    allow_custom_seed: bool = False,
    nonce: Optional[bytes] = None,
) -> Dict[str, Any]:
    """将插件源码目录加密混淆并 Ed25519 签名为 `.dogapp` 与银行网关安全外层 `.zip`."""
    if seed is None:
        raw_hex = ""
        if seed_file:
            with open(seed_file, "r", encoding="utf-8") as fh:
                raw_hex = fh.read().strip()
        elif (os.environ.get("DATAOG_UPDATE_SIGNING_KEY") or "").strip():
            raw_hex = os.environ["DATAOG_UPDATE_SIGNING_KEY"].strip()
        elif (os.environ.get("DATAOG_MASTER_SEED") or "").strip():
            raw_hex = os.environ["DATAOG_MASTER_SEED"].strip()
        if not raw_hex:
            raise ValueError("未提供插件签名私钥：请通过 --sign-key 或环境变量 DATAOG_UPDATE_SIGNING_KEY 传入 64 位十六进制 Ed25519 私钥种子")
        if not _HEX64_RE.match(raw_hex):
            raise ValueError("签名私钥格式无效：必须为 64 位十六进制字符串")
        eff_seed = bytes.fromhex(raw_hex)
    else:
        if len(seed) != 32:
            raise ValueError("seed 必须为 32 字节")
        eff_seed = bytes(seed)

    pubkey = _ed25519_derive_pubkey(eff_seed)
    prod_pk = update_bootstrap.reconstruct_prod_pubkey()
    if not allow_custom_seed and pubkey != prod_pk:
        raise ValueError("签名私钥推导出的公钥与 Data.O.G 内嵌生产验签公钥不一致（若用于测试请显式传 allow_custom_seed=True）")

    manifest, payload_entries = collect_plugin_source_entries(plugin_dir)
    plugin_id = manifest["plugin_id"]
    version = manifest["version"]

    file_hashes = {rel: hashlib.sha256(data).hexdigest() for rel, data in payload_entries}
    pkg_meta = {
        "format_version": PLUGIN_FORMAT_VERSION,
        "plugin_id": plugin_id,
        "version": version,
        "abi_version": PLATFORM_ABI_VERSION,
        "built_at": _utc_now_iso(),
        "files": file_hashes,
    }
    pkg_meta_raw = json.dumps(pkg_meta, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    sig_raw = _ed25519_sign_deterministic(PLUGIN_MANIFEST_SIG_DOMAIN + pkg_meta_raw, eff_seed)

    bundle_entries = list(payload_entries) + [
        ("package_manifest.json", pkg_meta_raw),
        ("package_manifest.sig", sig_raw),
    ]
    raw_bundle = update_bootstrap.pack_dgp2_bundle(bundle_entries)
    dogapp_bytes = seal_plugin_bundle(raw_bundle, seed=eff_seed, nonce=nonce)

    readme_text = (
        f"Data.O.G 4.0 外置应用插件包：{manifest['name']} ({plugin_id} v{version})\n"
        f"================================================================\n"
        f"- 适配底座版本 : Data.O.G v{manifest['min_platform_version']}+ (ABI v{PLATFORM_ABI_VERSION})\n"
        f"- 加密封装机制 : DGAP (BLAKE2b-512-CTR + HMAC-SHA256) + Ed25519 双重数字签名\n"
        f"- 安装方式（二选一）：\n"
        f"  1. 应用内安装：打开 Data.O.G → 系统配置 → 应用插件 → 上传本 .zip 或内部 .dogapp 文件，重启后生效。\n"
        f"  2. 目录放置：将本压缩包内 plugins/ 文件夹解压到 DataOG.exe 同级目录，启动时自动验签吸纳。\n"
    ).encode("utf-8")

    outer_buf = io.BytesIO()
    inner_name = f"plugins/DataOG-Plugin-{plugin_id}-v{version}.dogapp"
    with zipfile.ZipFile(outer_buf, "w") as zf:
        for arc, data in ((inner_name, dogapp_bytes), ("PLUGIN_README.txt", readme_text)):
            zi = zipfile.ZipInfo(filename=arc, date_time=_FIXED_ZIP_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o644 & 0xFFFF) << 16
            zf.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    outer_zip_bytes = outer_buf.getvalue()

    assert_gateway_safe_plugin_package(outer_zip_bytes, dogapp_bytes, plugin_id, version)
    verify_plugin_package_bytes(
        outer_zip_bytes,
        verifier_pubkey=pubkey,
    )

    os.makedirs(output_dir, exist_ok=True)
    zip_out = os.path.join(output_dir, f"DataOG-Plugin-{plugin_id}-v{version}.zip")
    dogapp_out = os.path.join(output_dir, f"DataOG-Plugin-{plugin_id}-v{version}.dogapp")
    with open(zip_out, "wb") as fh:
        fh.write(outer_zip_bytes)
    with open(dogapp_out, "wb") as fh:
        fh.write(dogapp_bytes)

    return {
        "ok": True,
        "plugin_id": plugin_id,
        "version": version,
        "name": manifest["name"],
        "zip_path": zip_out,
        "dogapp_path": dogapp_out,
        "zip_size": len(outer_zip_bytes),
        "dogapp_size": len(dogapp_bytes),
        "file_count": len(payload_entries),
        "dogapp_sha256": hashlib.sha256(dogapp_bytes).hexdigest(),
    }
