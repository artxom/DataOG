"""Data.O.G 同二级版本累积增量更新包签名构建工具 (build_update_pack.py).

产出结构：
- `<output_dir>/DataOG-Update-vX.Y.Z.zip` (~0.9~1.2 MB)
  ├── updates/DataOG-Update-vX.Y.Z.dgp   # Ed25519 签名载荷（manifest.json + manifest.sig + code.zip + static/）
  └── UPDATE_README.txt                  # 解压覆盖或应用内上传双渠道升级说明

安全约束：
1. 默认生产模式强制校验私钥推导出的公钥与客户端内嵌公钥 (`update_bootstrap.reconstruct_prod_pubkey()`) 完全一致；
2. 严禁将 `.keygen_secrets/`、`tests/`、`tools/`、`demo_sandbox/`、`.git/`、`__pycache__` 或原生二进制打入 `code.zip`；
3. 严禁向 stdout/stderr 打印签名私钥或种子全文；
4. 构建完成后立即调用 `update_bootstrap.verify_update_package_bytes()` 执行全包闭环自检后才原子落盘。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import zipfile

import update_bootstrap

_FIXED_ZIP_DT = (2025, 1, 1, 0, 0, 0)
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SKIP_CODE_DIRS = frozenset(
    {"__pycache__", ".keygen_secrets", ".git", "tests", "tools", "demo_sandbox", "_license_native_src"}
)
_SKIP_STATIC_NAMES = frozenset({".DS_Store", "Thumbs.db", "Desktop.ini"})


def _ensure_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_ensure_utf8_stdio()

# RFC 8032 Ed25519 签名原语（复用 update_bootstrap 曲线常量，零额外依赖）
_ED_L = update_bootstrap._ED_L
_ED_BASE = update_bootstrap._ED_BASE
_ed_scalarmult = update_bootstrap._ed_scalarmult
_ed_encodepoint = update_bootstrap._ed_encodepoint


def ed25519_derive_pubkey(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 私钥种子必须为 32 字节")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _ed_encodepoint(_ed_scalarmult(_ED_BASE, a))


def ed25519_sign_deterministic(msg: bytes, seed: bytes) -> bytes:
    """RFC 8032 确定性签名（与 license_hub / update_bootstrap 严格兼容）."""
    if len(seed) != 32:
        raise ValueError("Ed25519 私钥种子必须为 32 字节")
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


def _zip_writestr(
    zf: zipfile.ZipFile,
    arcname: str,
    data: bytes,
    *,
    compress_type: int = zipfile.ZIP_DEFLATED,
    compresslevel: Optional[int] = 9,
) -> None:
    zi = zipfile.ZipInfo(filename=arcname, date_time=_FIXED_ZIP_DT)
    zi.compress_type = compress_type
    zi.external_attr = (0o644 & 0xFFFF) << 16
    if compress_type == zipfile.ZIP_DEFLATED and compresslevel is not None:
        zf.writestr(zi, data, compress_type=compress_type, compresslevel=compresslevel)
    else:
        zf.writestr(zi, data, compress_type=compress_type)


def _detect_source_version(source_dir: str) -> str:
    kernel_path = os.path.join(source_dir, "core", "app_kernel.py")
    if os.path.isfile(kernel_path):
        with open(kernel_path, "r", encoding="utf-8") as fh:
            m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
        if m:
            return update_bootstrap.normalize_semver(m.group(1))
    app_path = os.path.join(source_dir, "app.py")
    if os.path.isfile(app_path):
        with open(app_path, "r", encoding="utf-8") as fh:
            m = re.search(r'^BASE_APP_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
        if m:
            return update_bootstrap.normalize_semver(m.group(1))
    raise ValueError(f"无法从 {source_dir} 解析默认版本号")


def resolve_signing_seed(
    *,
    seed: Optional[bytes] = None,
    seed_file: Optional[str] = None,
    source_dir: Optional[str] = None,
) -> bytes:
    """按优先级解析 32 字节 Ed25519 私钥种子（绝不输出任何密钥明文）."""
    raw_hex = ""
    if seed is not None:
        if len(seed) != 32:
            raise ValueError("传入 seed 参数必须为 32 字节")
        return seed

    if seed_file:
        if not os.path.isfile(seed_file):
            raise ValueError(f"找不到指定的签名种子文件: {seed_file}")
        with open(seed_file, "r", encoding="utf-8") as fh:
            raw_hex = fh.read().strip()
    elif (os.environ.get("DATAOG_UPDATE_SIGNING_KEY") or "").strip():
        raw_hex = os.environ["DATAOG_UPDATE_SIGNING_KEY"].strip()
    elif (os.environ.get("DATAOG_MASTER_SEED") or "").strip():
        raw_hex = os.environ["DATAOG_MASTER_SEED"].strip()
    else:
        base_dir = source_dir or os.path.dirname(os.path.abspath(__file__))
        local_key = os.path.join(base_dir, ".keygen_secrets", "master_ed25519.key")
        if os.path.isfile(local_key):
            with open(local_key, "r", encoding="utf-8") as fh:
                raw_hex = fh.read().strip()

    if not raw_hex:
        raise ValueError(
            "未检测到增量包签名私钥：请设置环境变量 DATAOG_UPDATE_SIGNING_KEY "
            "(64 位十六进制 Ed25519 种子) 或确保本地存在 .keygen_secrets/master_ed25519.key。"
        )
    if not _HEX64_RE.match(raw_hex):
        raise ValueError("签名私钥格式无效：必须为 64 位十六进制字符串")
    return bytes.fromhex(raw_hex)


def _collect_code_entries(source_dir: str) -> List[Tuple[str, bytes]]:
    entries: List[Tuple[str, bytes]] = []
    for root_pkg in ("core", "modules"):
        pkg_dir = os.path.join(source_dir, root_pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for cur_root, dirs, files in os.walk(pkg_dir, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_CODE_DIRS and not d.startswith("."))
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                full_path = os.path.join(cur_root, fname)
                if os.path.islink(full_path):
                    continue
                rel = os.path.relpath(full_path, source_dir).replace(os.sep, "/")
                with open(full_path, "rb") as fh:
                    entries.append((rel, fh.read()))
    entries.sort(key=lambda item: item[0])
    if not any(arc == "core/app_kernel.py" for arc, _ in entries):
        raise ValueError(f"源目录缺失核心内核文件: {os.path.join(source_dir, 'core', 'app_kernel.py')}")
    return entries


def _collect_static_entries(source_dir: str) -> List[Tuple[str, bytes]]:
    static_dir = os.path.join(source_dir, "static")
    if not os.path.isdir(static_dir):
        raise ValueError(f"找不到静态资源目录: {static_dir}")
    entries: List[Tuple[str, bytes]] = []
    for cur_root, dirs, files in os.walk(static_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            if fname in _SKIP_STATIC_NAMES or fname.startswith(".") or fname.endswith(".map"):
                continue
            full_path = os.path.join(cur_root, fname)
            if os.path.islink(full_path):
                continue
            rel = os.path.relpath(full_path, static_dir).replace(os.sep, "/")
            with open(full_path, "rb") as fh:
                entries.append((rel, fh.read()))
    entries.sort(key=lambda item: item[0])
    return entries


def _build_readme_text(manifest: Dict[str, Any]) -> str:
    ver = manifest["version"]
    series = manifest["series"]
    min_base = manifest["min_base_version"]
    built_at = manifest["built_at"]
    return (
        f"Data.O.G v{ver} 同系列累积增量更新包\n"
        f"============================================================\n"
        f"- 目标版本     : v{ver}\n"
        f"- 适用底座范围 : DataOG.exe v{min_base} 及以上同 {series}.x 系列客户端\n"
        f"                 （例如 {min_base} ~ {ver} 均可用本包直接平滑升级）\n"
        f"- 跨版本提示   : 若当前客户端低于 v{min_base}（例如 {series} 之前的老版本），请直接下载完整安装包\n"
        f"- 构建时间     : {built_at}\n"
        f"- 数字签名     : RFC 8032 Ed25519 + 全量 SHA-256 清单校验\n\n"
        f"升级方式（二选一）：\n"
        f"1. 【系统外部覆盖升级】将本压缩包中的 updates/ 文件夹直接解压覆盖到 DataOG 客户端目录\n"
        f"   （与 DataOG.exe 同级，无需替换 DataOG.exe 或 _internal/），下次启动 DataOG.exe 自动验签生效。\n"
        f"2. 【应用内界面上传】打开 Data.O.G -> 系统配置 -> 版本与升级 -> 点击「上传增量更新包」，\n"
        f"   直接选择本压缩包 DataOG-Update-v{ver}.zip（或内部 .dgp 文件），重启 DataOG 后生效。\n\n"
        f"说明：本升级包仅更新核心业务逻辑与前端界面，100% 保留原有激活授权、数据库连接、AI 密钥与工作区数据。\n"
    )


def build_update_pack(
    *,
    source_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    version: Optional[str] = None,
    min_base_version: Optional[str] = None,
    seed: Optional[bytes] = None,
    seed_file: Optional[str] = None,
    allow_custom_seed: bool = False,
    summary: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    """构建并自检签名增量更新包 DataOG-Update-vX.Y.Z.zip，返回构建统计元数据."""
    eff_source = os.path.abspath(source_dir or os.path.dirname(os.path.abspath(__file__)))
    eff_output = os.path.abspath(output_dir or os.path.join(eff_source, "dist"))
    os.makedirs(eff_output, exist_ok=True)

    ver_norm = update_bootstrap.normalize_semver(version or _detect_source_version(eff_source))
    series = update_bootstrap.format_series(ver_norm)
    min_base_norm = update_bootstrap.normalize_semver(
        min_base_version or update_bootstrap.compute_min_base_version(ver_norm)
    )
    v_tup = update_bootstrap.parse_semver(ver_norm)
    m_tup = update_bootstrap.parse_semver(min_base_norm)
    if (v_tup[0], v_tup[1]) != (m_tup[0], m_tup[1]) or m_tup > v_tup:
        raise ValueError(
            f"min_base_version (v{min_base_norm}) 必须与目标版本 (v{ver_norm}) 属于同一 X.Y 二级系列且 <= 目标版本"
        )

    eff_seed = resolve_signing_seed(seed=seed, seed_file=seed_file, source_dir=eff_source)
    derived_pk = ed25519_derive_pubkey(eff_seed)
    if not allow_custom_seed and derived_pk != update_bootstrap.reconstruct_prod_pubkey():
        raise ValueError(
            "签名私钥推导的公钥与客户端内嵌生产公钥不一致！请核对 DATAOG_UPDATE_SIGNING_KEY。"
        )

    code_entries = _collect_code_entries(eff_source)
    static_entries = _collect_static_entries(eff_source)

    index_entry = [data for rel, data in static_entries if rel == "index.html"]
    if not index_entry:
        raise ValueError("static 目录缺失 index.html")
    ui_ver = update_bootstrap._extract_ui_version_from_html(index_entry[0])
    if ui_ver != ver_norm:
        raise ValueError(
            f"static/index.html 中的 dataog-ui-version ({ui_ver or '未知'}) 与构建目标版本 v{ver_norm} 不一致"
        )

    code_buf = io.BytesIO()
    with zipfile.ZipFile(code_buf, "w") as czf:
        for arcname, raw in code_entries:
            _zip_writestr(czf, arcname, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    code_zip_bytes = code_buf.getvalue()

    static_hashes = {
        rel: hashlib.sha256(raw).hexdigest()
        for rel, raw in static_entries
    }
    manifest = {
        "format_version": update_bootstrap.UPDATE_FORMAT_VERSION,
        "version": ver_norm,
        "series": series,
        "min_base_version": min_base_norm,
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": (summary or f"Data.O.G v{ver_norm} 同系列累积增量更新包 (适用 v{min_base_norm}+)").strip(),
        "notes": (notes or "").strip(),
        "code_zip": {
            "sha256": hashlib.sha256(code_zip_bytes).hexdigest(),
            "size": len(code_zip_bytes),
            "file_count": len(code_entries),
        },
        "static_files": static_hashes,
    }
    manifest_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig_raw = ed25519_sign_deterministic(update_bootstrap.UPDATE_SIG_DOMAIN + manifest_raw, eff_seed)

    dgp_buf = io.BytesIO()
    with zipfile.ZipFile(dgp_buf, "w") as dzf:
        _zip_writestr(dzf, "manifest.json", manifest_raw)
        _zip_writestr(dzf, "manifest.sig", sig_raw)
        _zip_writestr(dzf, "code.zip", code_zip_bytes, compress_type=zipfile.ZIP_STORED, compresslevel=None)
        for rel, raw in static_entries:
            _zip_writestr(dzf, f"static/{rel}", raw)
    dgp_bytes = dgp_buf.getvalue()

    dgp_name = f"DataOG-Update-v{ver_norm}.dgp"
    readme_bytes = _build_readme_text(manifest).encode("utf-8")
    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, "w") as ozf:
        _zip_writestr(
            ozf,
            f"updates/{dgp_name}",
            dgp_bytes,
            compress_type=zipfile.ZIP_STORED,
            compresslevel=None,
        )
        _zip_writestr(ozf, "UPDATE_README.txt", readme_bytes)
    outer_bytes = outer_buf.getvalue()

    # 构建后闭环验签自检
    verified = update_bootstrap.verify_update_package_bytes(
        outer_bytes,
        base_version=min_base_norm,
        verifier_pubkey=derived_pk,
    )

    outer_filename = f"DataOG-Update-v{ver_norm}.zip"
    outer_path = os.path.join(eff_output, outer_filename)
    fd, tmp_out = tempfile.mkstemp(prefix=".tmp_upd_", suffix=".zip", dir=eff_output)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(outer_bytes)
        os.replace(tmp_out, outer_path)
    except Exception:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "version": ver_norm,
        "series": series,
        "min_base_version": min_base_norm,
        "zip_path": outer_path,
        "zip_filename": outer_filename,
        "zip_size": len(outer_bytes),
        "zip_sha256": hashlib.sha256(outer_bytes).hexdigest(),
        "dgp_sha256": verified["dgp_sha256"],
        "code_file_count": verified["code_file_count"],
        "static_file_count": verified["static_file_count"],
        "pubkey_hex": derived_pk.hex(),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 Data.O.G Ed25519 签名同系列累积增量更新包")
    parser.add_argument("--version", default="", help="目标版本号 (默认读取 core/app_kernel.py APP_VERSION)")
    parser.add_argument("--min-base-version", default="", help="最低兼容底座版本 (默认 X.Y.0)")
    parser.add_argument("--output-dir", default="dist", help="产物输出目录 (默认 dist)")
    parser.add_argument("--source-dir", default="", help="源代码根目录 (默认脚本所在目录)")
    parser.add_argument("--seed-file", default="", help="64 位十六进制 Ed25519 私钥种子文件路径")
    parser.add_argument("--allow-custom-seed", action="store_true", help="允许非生产测试公钥种子签名")
    parser.add_argument("--summary", default="", help="更新摘要说明")
    parser.add_argument("--notes", default="", help="补充发布说明")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    try:
        res = build_update_pack(
            source_dir=args.source_dir or None,
            output_dir=args.output_dir,
            version=args.version or None,
            min_base_version=args.min_base_version or None,
            seed_file=args.seed_file or None,
            allow_custom_seed=bool(args.allow_custom_seed),
            summary=args.summary,
            notes=args.notes,
        )
    except Exception as exc:
        print(f"[错误] 增量更新包构建失败: {exc}", file=sys.stderr)
        sys.exit(1)

    size_kb = res["zip_size"] / 1024.0
    print("=" * 72)
    print(f"[OK] Data.O.G v{res['version']} 同系列累积增量更新包构建并验签自检通过！")
    print(f"  [*] 产物路径     : {res['zip_path']}")
    print(f"  [*] 压缩包体积   : {size_kb:.1f} KB ({res['zip_size']} bytes)")
    print(f"  [*] 适用底座范围 : v{res['min_base_version']}+ (系列 {res['series']}.x)")
    print(f"  [*] 载荷统计     : core/modules {res['code_file_count']} 个 .py + static {res['static_file_count']} 项资源")
    print(f"  [*] 外层 ZIP SHA : {res['zip_sha256']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
