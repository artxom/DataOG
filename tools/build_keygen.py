"""Data.O.G 零依赖独立激活码生成器编译脚本 (tools/build_keygen.py).

安全原则：
1. 主私钥种子默认从本地 `.keygen_secrets/master_ed25519.key`（或 `--seed-file` / `DATAOG_MASTER_SEED`）读取，
   该文件被 `.gitignore` 严格排除，绝不进入 Git 仓库或客户端安装包；
2. 编译期间将私钥拆分为 4 组 XOR+置换查找表写入临时目录头文件，编译完成立即销毁临时源文件；
3. 输出零环境依赖单文件原生可执行程序至 `keygen/DataOG_Keygen`（Windows 下为 `DataOG_Keygen.exe`）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与客户端内置公钥保持一致（仅用于校验本地私钥种子是否匹配）
_PROD_PUBKEY_HEX = "049da21161f75be4a0e902c95d332e2b0e3eea1a7cacda042a246651498581a3"

_SECRET_PERM: Tuple[int, ...] = (
    22, 5, 13, 30, 1, 18, 9, 27, 4, 25, 11, 16, 0, 29, 7, 20,
    14, 3, 31, 8, 24, 15, 2, 19, 28, 6, 21, 12, 26, 10, 17, 23,
)


def _derive_ed25519_pubkey(seed: bytes) -> bytes:
    """Pure Python RFC 8032 Ed25519 public key derivation from 32B seed."""
    p = (1 << 255) - 19
    d = (-121665 * pow(121666, p - 2, p)) % p
    i_const = pow(2, (p - 1) // 4, p)

    def _xrecover(y: int) -> int:
        xx = (y * y - 1) * pow(d * y * y + 1, p - 2, p)
        x = pow(xx, (p + 3) // 8, p)
        if (x * x - xx) % p != 0:
            x = (x * i_const) % p
        if x % 2 != 0:
            x = p - x
        return x

    by = (4 * pow(5, p - 2, p)) % p
    bx = _xrecover(by)
    base = (bx % p, by % p, 1, (bx * by) % p)

    def _add(pt1: Tuple[int, int, int, int], pt2: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        x1, y1, z1, t1 = pt1
        x2, y2, z2, t2 = pt2
        a = ((y1 - x1) * (y2 - x2)) % p
        b = ((y1 + x1) * (y2 + x2)) % p
        c = (2 * d * t1 * t2) % p
        dd = (2 * z1 * z2) % p
        e = b - a
        f = dd - c
        g = dd + c
        h = b + a
        return ((e * f) % p, (g * h) % p, (f * g) % p, (e * h) % p)

    def _smul(pt: Tuple[int, int, int, int], exp: int) -> Tuple[int, int, int, int]:
        res = (0, 1, 1, 0)
        cur = pt
        while exp > 0:
            if exp & 1:
                res = _add(res, cur)
            cur = _add(cur, cur)
            exp >>= 1
        return res

    digest = hashlib.sha512(seed).digest()
    a_int = int.from_bytes(digest[:32], "little")
    a_int &= (1 << 254) - 8
    a_int |= 1 << 254
    x, y, z, _ = _smul(base, a_int)
    zi = pow(z, p - 2, p)
    xn = (x * zi) % p
    yn = (y * zi) % p
    return (yn | ((xn & 1) << 255)).to_bytes(32, "little")


def _load_seed(seed_file: Optional[str]) -> bytes:
    env_seed = os.environ.get("DATAOG_MASTER_SEED", "").strip()
    if env_seed:
        raw = env_seed.encode("utf-8")
    else:
        path = seed_file or os.path.join(PROJECT_ROOT, ".keygen_secrets", "master_ed25519.key")
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"未找到本地私钥种子文件: {path}（请先生成私钥或通过 --seed-file / DATAOG_MASTER_SEED 指定）"
            )
        with open(path, "rb") as fh:
            raw = fh.read().strip()

    if len(raw) == 64:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except Exception:
            pass
    if len(raw) == 32:
        return raw
    raise ValueError("Ed25519 主私钥种子长度无效，必须为 32 字节原始密钥或 64 位十六进制字符串")


def _build_secret_header(seed: bytes) -> str:
    m0 = secrets.token_bytes(32)
    m1 = secrets.token_bytes(32)
    m2 = secrets.token_bytes(32)
    m3 = bytes(seed[i] ^ m0[i] ^ m1[i] ^ m2[i] for i in range(32))
    tables = []
    for tbl in (m0, m1, m2, m3):
        permuted = bytes(tbl[_SECRET_PERM[i]] for i in range(32))
        tables.append(list(struct.unpack("<4Q", permuted)))

    lines = [
        "#ifndef DATAOG_KEYGEN_SECRET_TABLES_H",
        "#define DATAOG_KEYGEN_SECRET_TABLES_H",
        "#include <stdint.h>",
        "static const uint8_t DOG_SK_PERM[32] = {",
        "    " + ", ".join(str(x) for x in _SECRET_PERM),
        "};",
    ]
    for idx, qwords in enumerate(tables):
        vals = ", ".join(f"0x{q:016x}ULL" for q in qwords)
        lines.append(f"static const uint64_t DOG_SK_T{idx}[4] = {{{vals}}};")
    lines.append("#endif")
    return "\n".join(lines) + "\n"


def _find_c_compilers() -> List[Tuple[str, str]]:
    cands: List[Tuple[str, str]] = []
    order = ("gcc", "clang", "cl") if os.name == "nt" else ("clang", "gcc", "cc")
    for cand in order:
        p = shutil.which(cand)
        if p:
            cands.append((cand, p))
    if not cands:
        raise RuntimeError("未检测到可用的 C 编译器 (clang / gcc / cl)")
    return cands


def build_keygen(
    *,
    output_path: Optional[str] = None,
    seed_file: Optional[str] = None,
    allow_custom_seed: bool = False,
) -> str:
    seed = _load_seed(seed_file)
    pub = _derive_ed25519_pubkey(seed)
    if not allow_custom_seed and pub.hex() != _PROD_PUBKEY_HEX:
        raise ValueError(
            "私钥种子与 Data.O.G 客户端内置公钥不匹配！若用于测试自定义密钥，请附加 --allow-custom-seed"
        )

    exe_name = "DataOG_Keygen.exe" if os.name == "nt" else "DataOG_Keygen"
    out_bin = os.path.abspath(output_path or os.path.join(PROJECT_ROOT, "keygen", exe_name))
    os.makedirs(os.path.dirname(out_bin), exist_ok=True)

    keygen_c = os.path.join(PROJECT_ROOT, "tools", "keygen_src", "keygen.c")
    native_inc = os.path.join(PROJECT_ROOT, "core", "platform", "_license_native_src")
    compilers = _find_c_compilers()

    with tempfile.TemporaryDirectory(prefix="dog_keygen_bld_") as tmpdir:
        hdr_path = os.path.join(tmpdir, "keygen_secret_tables.h")
        with open(hdr_path, "w", encoding="utf-8") as fh:
            fh.write(_build_secret_header(seed))

        err_logs: List[str] = []
        built = False
        for kind, compiler in compilers:
            if kind == "cl":
                cmd_variants: List[List[str]] = [
                    [
                        compiler,
                        "/nologo",
                        "/O2",
                        "/MT",
                        f"/I{tmpdir}",
                        f"/I{native_inc}",
                        keygen_c,
                        f"/Fo:{os.path.join(tmpdir, 'keygen.obj')}",
                        f"/Fe:{out_bin}",
                        "ws2_32.lib",
                        "iphlpapi.lib",
                        "advapi32.lib",
                    ]
                ]
            elif os.name == "nt" and kind == "clang":
                base_cmd = [
                    compiler,
                    "-O2",
                    "-std=c11",
                    f"-I{tmpdir}",
                    f"-I{native_inc}",
                    keygen_c,
                    "-o",
                    out_bin,
                ]
                cmd_variants = [
                    base_cmd + ["-D_MT", "-Xclang", "--dependent-lib=libcmt", "-lws2_32", "-liphlpapi", "-ladvapi32"],
                    base_cmd + ["-lws2_32", "-liphlpapi", "-ladvapi32"],
                ]
            elif os.name == "nt":
                base_cmd = [
                    compiler,
                    "-O2",
                    "-std=c11",
                    "-fno-stack-protector",
                    f"-I{tmpdir}",
                    f"-I{native_inc}",
                    keygen_c,
                    "-o",
                    out_bin,
                ]
                cmd_variants = [
                    base_cmd + ["-static", "-static-libgcc", "-lws2_32", "-liphlpapi", "-ladvapi32"],
                    base_cmd + ["-static-libgcc", "-lws2_32", "-liphlpapi", "-ladvapi32"],
                ]
            else:
                cmd = [
                    compiler,
                    "-O2",
                    "-std=c11",
                    f"-I{tmpdir}",
                    f"-I{native_inc}",
                    keygen_c,
                    "-o",
                    out_bin,
                ]
                if sys.platform != "darwin":
                    cmd.append("-Wl,-s")
                cmd_variants = [cmd]

            for cmd in cmd_variants:
                try:
                    if os.path.exists(out_bin):
                        os.remove(out_bin)
                except OSError:
                    pass
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if proc.returncode == 0 and os.path.isfile(out_bin):
                    built = True
                    break
                err_logs.append(f"[{kind}] {proc.stdout}\n{proc.stderr}")
            if built:
                break

        if not built:
            raise RuntimeError("编译激活码生成器失败:\n" + "\n".join(err_logs))

        if os.name != "nt":
            strip_bin = shutil.which("strip")
            if strip_bin:
                subprocess.run([strip_bin, out_bin], capture_output=True, check=False)
            os.chmod(out_bin, stat.S_IRWXU)
            if sys.platform == "darwin":
                codesign_bin = shutil.which("codesign")
                if codesign_bin:
                    subprocess.run(
                        [codesign_bin, "--force", "--sign", "-", out_bin],
                        capture_output=True,
                        check=False,
                    )

    return out_bin


def main() -> None:
    parser = argparse.ArgumentParser(description="编译 Data.O.G 独立零依赖激活码生成器")
    parser.add_argument("--output", default=None, help="输出可执行文件路径")
    parser.add_argument("--seed-file", default=None, help="Ed25519 私钥种子路径")
    parser.add_argument("--allow-custom-seed", action="store_true", help="允许使用非生产公钥对应的测试种子")
    args = parser.parse_args()

    out_path = build_keygen(
        output_path=args.output,
        seed_file=args.seed_file,
        allow_custom_seed=args.allow_custom_seed,
    )
    print(f"[OK] 独立激活码生成器已编译完成: {out_path}")


if __name__ == "__main__":
    main()
