"""Data.O.G 多功能数据工程与数据治理一体化工作台 - 冻结态极薄启动壳 (app.py).

职责：
1. 自动禁用 Windows CMD 快速编辑模式并启用 UTF-8 控制台输出流；
2. 通过纯标准库引导器 `update_bootstrap` 扫描/验签并优先挂载已签名同系列增量补丁；
3. 从 `core.app_kernel` 加载并重导出 FastAPI 微内核全部公开入口，保持 100% 向后兼容；
4. 若增量补丁导入或初始化异常，自动执行崩溃自愈回退并无缝切回内置基础版本底座运行。
"""

from __future__ import annotations

import os
import sys
import time
import traceback

BASE_APP_VERSION = "3.6.0"


def disable_windows_console_quick_edit() -> None:
    """在 Windows 下自动关闭 CMD 控制台窗口的『快速编辑模式』(QuickEdit Mode)，
    彻底防止用户鼠标点击控制台窗口进入『选择状态』导致 stdout 与整个 Web 服务被系统挂起假死."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
        if h_stdin and h_stdin != -1:
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
                new_mode = (mode.value & ~0x0040 & ~0x0020) | 0x0080
                kernel32.SetConsoleMode(h_stdin, new_mode)
    except Exception:
        pass


def ensure_utf8_console_streams() -> None:
    """确保 Windows / CI 重定向控制台下标准输出流支持 UTF-8 中文输出而不抛 UnicodeEncodeError."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _write_startup_crash_log_fallback(err_text: str) -> None:
    """启动崩溃日志兜底落盘（不依赖核心业务包，不向终端打印私有物理路径）."""
    try:
        import update_bootstrap as _ub

        logs_dir = os.path.join(_ub.resolve_private_root(), "rt_log")
        os.makedirs(logs_dir, exist_ok=True)
        with open(os.path.join(logs_dir, "startup_error.log"), "w", encoding="utf-8") as fh:
            fh.write(err_text)
    except Exception:
        pass


def pause_on_windows_console(prompt_msg: str = "\n请按回车键 (Enter) 关闭此控制台窗口...") -> None:
    """在 Windows 控制台中暂停，防止双击启动 exe 时发生闪退无法看清报错."""
    if getattr(sys, "frozen", False) or os.name == "nt":
        try:
            input(prompt_msg)
        except Exception:
            time.sleep(10)


ensure_utf8_console_streams()
disable_windows_console_quick_edit()

import update_bootstrap

update_bootstrap.bootstrap_runtime_patch(base_version=BASE_APP_VERSION)

try:
    import core.app_kernel as _kernel
except BaseException as _patch_exc:
    if update_bootstrap.is_patch_active():
        _failed_ver = update_bootstrap.rollback_active_patch_and_unload(
            f"{type(_patch_exc).__name__}: {_patch_exc}"
        )
        print(
            f"[!] 增量补丁 v{_failed_ver or '?'} 加载失败，已自动回退至内置基础版本 "
            f"v{BASE_APP_VERSION}: {_patch_exc}"
        )
        try:
            import core.app_kernel as _kernel
        except BaseException:
            _err_text = traceback.format_exc()
            print("=" * 74)
            print("  [严重错误] Data.O.G 程序初始化导入内置核心内核失败！")
            print("=" * 74)
            print(_err_text)
            _write_startup_crash_log_fallback(_err_text)
            pause_on_windows_console()
            sys.exit(1)
    else:
        _err_text = traceback.format_exc()
        print("=" * 74)
        print("  [严重错误] Data.O.G 程序初始化导入依赖模块失败！")
        print("=" * 74)
        print(_err_text)
        _write_startup_crash_log_fallback(_err_text)
        pause_on_windows_console()
        sys.exit(1)

APP_VERSION = _kernel.APP_VERSION
FAVICON_SVG = _kernel.FAVICON_SVG
SafeConsoleStreamHandler = _kernel.SafeConsoleStreamHandler
_QuietImportProgressAccessFilter = _kernel._QuietImportProgressAccessFilter
check_static_ui_version_sync = _kernel.check_static_ui_version_sync
create_app = _kernel.create_app
find_available_port = _kernel.find_available_port
get_bundle_dir = _kernel.get_bundle_dir
get_runtime_data_dir = _kernel.get_runtime_data_dir
logger = _kernel.logger
main = _kernel.main
resolve_static_dir = _kernel.resolve_static_dir
run_smoke_test = _kernel.run_smoke_test
setup_console_logging = _kernel.setup_console_logging


if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] 已收到退出指令，服务已安全停止。")
    except SystemExit as sys_exit:
        if (
            sys_exit.code not in (0, None)
            and "--test" not in sys.argv
            and "--smoke-test" not in sys.argv
        ):
            pause_on_windows_console()
        raise
    except BaseException as _run_exc:
        if update_bootstrap.is_patch_active() and not update_bootstrap.get_active_patch_state().get("marked_healthy"):
            _failed_ver = update_bootstrap.rollback_active_patch_and_unload(
                f"{type(_run_exc).__name__}: {_run_exc}"
            )
            print(
                f"\n[!] 增量补丁 v{_failed_ver or '?'} 启动异常，已自动回退至内置基础版本，"
                "正在使用内置底座重新启动..."
            )
            import core.app_kernel as _fallback_kernel

            _fallback_kernel.main()
        else:
            crash_text = traceback.format_exc()
            print("\n" + "=" * 74)
            print("  [程序运行异常] 服务未能正常启动或运行中断，详细错误如下：")
            print("=" * 74)
            print(crash_text)
            _write_startup_crash_log_fallback(crash_text)
            pause_on_windows_console()
            sys.exit(1)
