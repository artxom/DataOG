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

    _install_resilient_rmtree()
    run_pre_build_tests(project_dir)

    try:
        import PyInstaller.__main__
    except ImportError:
        print(
            "[错误] 当前 Python 环境未安装 pyinstaller，请先执行: python -m pip install -r requirements.txt"
        )
        sys.exit(1)

    static_src = os.path.join(project_dir, "static")
    tests_src = os.path.join(project_dir, "tests")
    if not os.path.isdir(static_src):
        print(f"[错误] 找不到前端静态资源目录: {static_src}")
        sys.exit(1)

    add_static_arg = f"{static_src}{os.pathsep}static"
    add_tests_arg = f"{tests_src}{os.pathsep}tests"
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
    os.makedirs(work_path, exist_ok=True)
    os.makedirs(spec_path, exist_ok=True)
    os.makedirs(cache_path, exist_ok=True)
    os.environ["PYINSTALLER_CONFIG_DIR"] = cache_path

    excluded_modules = [
        "tkinter",
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
        "--collect-all=pydantic_core",
        "--collect-submodules=starlette",
        "--collect-submodules=uvicorn",
        "--collect-submodules=anyio",
        "--collect-submodules=openpyxl",
        "--collect-submodules=multipart",
        "--collect-submodules=pymysql",
        "--collect-all=psycopg2",
        "--collect-submodules=core",
        "--collect-submodules=modules",
        "--hidden-import=h11",
        "--hidden-import=et_xmlfile",
        "--hidden-import=unittest.mock",
    ]
    if importlib.util.find_spec("httpx") is not None:
        pyinstaller_args.append("--collect-submodules=httpx")
    if importlib.util.find_spec("jpype") is not None:
        pyinstaller_args.append("--collect-all=jpype")

    for mod in excluded_modules:
        pyinstaller_args.append(f"--exclude-module={mod}")

    print(
        "[2/3] 正在调用 PyInstaller 打包 DataOG 独立可执行程序 (包含完整 Python 环境与模块化静态资源) ..."
    )
    try:
        PyInstaller.__main__.run(pyinstaller_args)
    finally:
        _safe_rmtree(temp_build_root)

    dist_bundle_dir = os.path.join(project_dir, "dist", exe_name)
    sample_excel = os.path.join(project_dir, "示例_DWS字段贯标清单.xlsx")
    if os.path.isdir(dist_bundle_dir):
        if os.path.isfile(sample_excel):
            shutil.copy2(
                sample_excel, os.path.join(dist_bundle_dir, "示例_DWS字段贯标清单.xlsx")
            )
        target_static = os.path.join(dist_bundle_dir, "static")
        if os.path.exists(target_static):
            shutil.rmtree(target_static, ignore_errors=True)
        shutil.copytree(static_src, target_static)
        os.makedirs(os.path.join(dist_bundle_dir, "data", "drivers"), exist_ok=True)

    exe_ext = ".exe" if os.name == "nt" else ""
    output_exe = os.path.join(dist_bundle_dir, f"{exe_name}{exe_ext}")

    print("\n" + "=" * 74)
    print("[3/3] DataOG 打包完成！")
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
