"""DataOG Windows 独立可执行程序 (DataOG.exe) 自动化打包脚本.

通过 Python 直接调用 PyInstaller API，避免 Windows .bat 批处理文件因换行符 (CRLF/LF)、
多行转义符 (^) 或 GBK/UTF-8 编码差异导致打包参数丢失或构建出闪退程序。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest


def _ensure_utf8_stdio() -> None:
    """确保在 Windows GitHub Actions (cp1252) 或 GBK 控制台中输出中文提示与测试日志不触发 UnicodeEncodeError."""
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
_ORIG_RMTREE = shutil.rmtree

# 仅限开发期与本机内部使用的测试文件（依赖已排除的 tools / demo_sandbox），严禁打入客户版安装包
EXCLUDED_CUSTOMER_TEST_FILES = frozenset(
    {
        "test_admin_console.py",
        "test_demo_sandbox.py",
    }
)


def stage_customer_tests_dir(tests_src: str, staging_dir: str) -> str:
    """构建仅包含客户运行时自检测试的纯净暂存目录（剔除 dev-only 测试与 __pycache__）."""
    _safe_rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    for fname in sorted(os.listdir(tests_src)):
        if fname in EXCLUDED_CUSTOMER_TEST_FILES:
            continue
        if not fname.endswith(".py"):
            continue
        src_file = os.path.join(tests_src, fname)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, os.path.join(staging_dir, fname))
    return staging_dir


def _clear_readonly_tree(root_path: str) -> None:
    """递归解除目录与文件的只读限制."""
    try:
        os.chmod(root_path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(root_path):
        for d in dirnames:
            try:
                os.chmod(os.path.join(dirpath, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
        for f in filenames:
            try:
                os.chmod(os.path.join(dirpath, f), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass


def _safe_rmtree(target_path: str, retries: int = 5, delay: float = 0.25) -> None:
    """Windows 兼容的强韧目录清理函数，解决只读属性、杀毒软件短暂句柄锁或 OneDrive 权限导致 [WinError 5] 拒绝访问."""
    if not os.path.exists(target_path):
        return

    for attempt in range(retries):
        _ORIG_RMTREE(target_path, ignore_errors=True)
        if not os.path.exists(target_path):
            return
        _clear_readonly_tree(target_path)
        time.sleep(delay * (attempt + 1))
        _ORIG_RMTREE(target_path, ignore_errors=True)
        if not os.path.exists(target_path):
            return

    if os.path.exists(target_path):
        try:
            trash_path = f"{target_path}.old_{os.getpid()}_{int(time.time() * 1000)}"
            os.rename(target_path, trash_path)
            _ORIG_RMTREE(trash_path, ignore_errors=True)
        except OSError:
            pass


def _install_resilient_rmtree() -> None:
    """拦截 PyInstaller 内部的裸 shutil.rmtree 调用，防止 Windows 下清理 localpycs 等目录时抛出 PermissionError."""

    def _patched_rmtree(path, ignore_errors=False, onerror=None, **kwargs):
        _safe_rmtree(os.fsdecode(path))

    shutil.rmtree = _patched_rmtree


def run_pre_build_tests(project_dir: str) -> None:
    print("[1/3] 正在运行打包前 DataOG 全量自动化单元测试校验 ...")
    suite = unittest.defaultTestLoader.discover(
        start_dir=os.path.join(project_dir, "tests"),
        pattern="test_*.py",
    )
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if not result.wasSuccessful():
        print("[错误] 单元测试未通过，已中止打包。")
        sys.exit(1)
    print("[OK] 单元测试全部通过！\n")


def build_executable() -> None:
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)

    # 先于单元测试编译 C 原生激活校验防反编译护盾（避免 Windows 下 unittest 预先通过 ctypes 映射旧 DLL 造成文件锁）
    shield_bin_path = None
    has_c_compiler = any(shutil.which(c) for c in ("gcc", "clang", "cl", "cc"))
    try:
        import core.platform.license_hub as _lh

        shield_bin_path = _lh.compile_native_license_shield(force=True)
        if shield_bin_path and os.path.isfile(shield_bin_path):
            print(f"[预检] C 原生防反编译护盾已编译就绪: {shield_bin_path}")
        elif has_c_compiler:
            print("[错误] 检测到 C 编译器但编译 _license_shield 原生护盾失败：")
            if getattr(_lh, "LAST_SHIELD_COMPILE_ERROR", ""):
                print(_lh.LAST_SHIELD_COMPILE_ERROR)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        if has_c_compiler:
            print(f"[错误] 编译原生防反编译护盾发生未预期异常: {exc}")
            sys.exit(1)
        print(f"[警告] 编译原生护盾出现异常: {exc}")
        shield_bin_path = None

    run_pre_build_tests(project_dir)
    _install_resilient_rmtree()

    try:
        import PyInstaller.__main__
    except ImportError:
        print(
            "[错误] 当前 Python 环境未安装 pyinstaller，请先执行: python -m pip install -r requirements.txt pyinstaller"
        )
        sys.exit(1)

    static_src = os.path.join(project_dir, "static")
    tests_src = os.path.join(project_dir, "tests")
    icon_src = os.path.join(static_src, "dataog.ico")
    if not os.path.isdir(static_src):
        print(f"[错误] 找不到前端静态资源目录: {static_src}")
        sys.exit(1)
    if not os.path.isfile(icon_src):
        print(f"[错误] 找不到可执行程序图标文件: {icon_src}")
        sys.exit(1)

    add_static_arg = f"{static_src}{os.pathsep}static"
    exe_name = "DataOG"

    # 若 Windows 下有正在运行的同名进程，先尝试关闭以释放 dist 目录文件锁
    if os.name == "nt":
        for proc_name in (f"{exe_name}.exe", "DWS字段贯标工具.exe"):
            subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    _safe_rmtree(os.path.join(project_dir, "build"))
    _safe_rmtree(os.path.join(project_dir, "dist", exe_name))

    temp_build_root = tempfile.mkdtemp(prefix="dataog_pyi_build_")
    work_path = os.path.join(temp_build_root, "work")
    spec_path = os.path.join(temp_build_root, "spec")
    cache_path = os.path.join(temp_build_root, "cache")
    staged_tests_dir = stage_customer_tests_dir(
        tests_src, os.path.join(temp_build_root, "staged_tests")
    )
    add_tests_arg = f"{staged_tests_dir}{os.pathsep}tests"
    os.makedirs(work_path, exist_ok=True)
    os.makedirs(spec_path, exist_ok=True)
    os.makedirs(cache_path, exist_ok=True)
    os.environ["PYINSTALLER_CONFIG_DIR"] = cache_path

    excluded_modules = [
        # 大体积科学计算与 GUI 库
        "tkinter",
        "turtle",
        "turtledemo",
        "idlelib",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "IPython",
        "jedi",
        "PIL",
        "pywebview",
        "streamlit",
        "altair",
        "pyarrow",
        "lxml",
        "cryptography",
        "objc",
        "AppKit",
        "Foundation",
        "Cocoa",
        "uvloop",
        "watchfiles",
        "websockets",
        "wsproto",
        "gunicorn",
        # 已由标准库替代的重型 SDK 与开发期工具包
        "openai",
        "jiter",
        "httpx",
        "httpcore",
        "certifi",
        "tqdm",
        "distro",
        "pytest",
        "pip",
        "setuptools",
        "wheel",
        "venv",
        "ensurepip",
        "lib2to3",
        "pydoc",
        "doctest",
        "curses",
        # 运行时零引用的框架子包
        "psycopg2.tests",
        "pydantic.mypy",
        "fastapi.cli",
        "uvicorn.workers",
        "uvicorn.protocols.websockets",
        "anyio._backends._trio",
        "anyio.pytest_plugin",
        # 严禁将激活码生成器构建工具与本地演示沙箱打入客户版安装包
        "tools",
        "demo_sandbox",
    ]

    pyinstaller_args = [
        "app.py",
        "--noconfirm",
        "--console",
        "--onedir",
        f"--name={exe_name}",
        f"--workpath={work_path}",
        f"--specpath={spec_path}",
        f"--add-data={add_static_arg}",
        f"--add-data={add_tests_arg}",
        "--collect-submodules=fastapi",
        "--collect-submodules=pydantic",
        "--collect-binaries=pydantic_core",
        "--hidden-import=pydantic_core",
        "--collect-submodules=starlette",
        "--collect-submodules=uvicorn",
        "--collect-submodules=anyio",
        "--collect-submodules=openpyxl",
        "--collect-submodules=multipart",
        "--collect-submodules=pymysql",
        "--collect-binaries=psycopg2",
        "--collect-submodules=psycopg2",
        "--collect-submodules=core",
        "--collect-submodules=modules",
        "--hidden-import=h11",
        "--hidden-import=et_xmlfile",
        "--hidden-import=http.server",
        "--hidden-import=unittest.mock",
    ]
    if shield_bin_path and os.path.isfile(shield_bin_path):
        pyinstaller_args.append(
            f"--add-binary={shield_bin_path}{os.pathsep}core/platform"
        )
    if importlib.util.find_spec("jpype") is not None:
        pyinstaller_args.append("--collect-all=jpype")

    if os.name == "nt" or importlib.util.find_spec("PIL") is not None:
        pyinstaller_args.append(f"--icon={icon_src}")

    for mod in excluded_modules:
        pyinstaller_args.append(f"--exclude-module={mod}")

    print(
        "[2/3] 正在调用 PyInstaller 轻量化打包 Data.O.G 独立可执行程序 ..."
    )
    try:
        PyInstaller.__main__.run(pyinstaller_args)
    finally:
        _safe_rmtree(temp_build_root)

    dist_bundle_dir = os.path.join(project_dir, "dist", exe_name)
    bundle_size_mb = 0.0
    if os.path.isdir(dist_bundle_dir):
        internal_dir = os.path.join(dist_bundle_dir, "_internal")
        if os.path.isdir(internal_dir):
            for root, dirs, files in os.walk(internal_dir, topdown=False):
                for fname in files:
                    if fname.endswith((".pyi", ".pxd", ".c", ".h")):
                        try:
                            os.remove(os.path.join(root, fname))
                        except OSError:
                            pass
                for dname in dirs:
                    if dname == "__pycache__":
                        _safe_rmtree(os.path.join(root, dname))

        # Layer 1 无状态程序包铁律：绝不在打包产物目录中创建或携带 data/ 目录，确保全量覆盖升级绝不丢失用户数据
        legacy_dist_data = os.path.join(dist_bundle_dir, "data")
        if os.path.exists(legacy_dist_data):
            _safe_rmtree(legacy_dist_data)
        if shield_bin_path and os.path.isfile(shield_bin_path):
            shield_fname = os.path.basename(shield_bin_path)
            bundled_shield = any(
                shield_fname in fs for _, _, fs in os.walk(dist_bundle_dir)
            )
            if not bundled_shield:
                print(f"[错误] 打包产物中缺失原生防反编译护盾文件: {shield_fname}")
                sys.exit(1)
        # 校验 dev-only 测试未泄露进客户发行包，避免 DataOG.exe --test 因 tools/demo_sandbox 排除而失败
        leaked_dev_tests = [
            fname
            for _, _, fs in os.walk(dist_bundle_dir)
            for fname in fs
            if fname in EXCLUDED_CUSTOMER_TEST_FILES
        ]
        if leaked_dev_tests:
            print(f"[错误] 客户发行包中泄露了开发期测试文件: {sorted(set(leaked_dev_tests))}")
            sys.exit(1)
        total_bytes = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(dist_bundle_dir)
            for f in fs
        )
        bundle_size_mb = total_bytes / (1024 * 1024)

    exe_ext = ".exe" if os.name == "nt" else ""
    output_exe = os.path.join(dist_bundle_dir, f"{exe_name}{exe_ext}")

    print("\n" + "=" * 74)
    print(f"[3/3] Data.O.G 轻量化打包完成！（解压态总体积: {bundle_size_mb:.2f} MB）")
    print(f"  [*] 独立运行包目录 : {dist_bundle_dir}")
    print(f"  [*] 可执行程序路径 : {output_exe}")
    print(
        "  [*] 使用说明       : 直接双击 DataOG.exe 即可展开控制台窗口并自动打开 http://127.0.0.1:18080"
    )
    print(
        f"                       迁移到无 Python 环境的电脑时，请将整个 [{exe_name}] 文件夹一起复制。"
    )
    print("=" * 74)


if __name__ == "__main__":
    build_executable()
