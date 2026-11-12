"""DataOG 多功能数据工程与数据治理一体化工作台 - FastAPI 微内核服务端入口.

启动后：
1. 自动禁用 Windows CMD 控制台快速编辑模式 (QuickEdit Mode)，防止鼠标点击黑窗口挂起服务进程
2. 初始化全局共享微内核底座 PlatformContext（自动无损迁移旧版 dws_migrator_state.db 至 dataog_state.db）
3. 挂载平台公共路由 (/api/platform) 与三大高内聚业务插件模块 (/api/modules/*)
4. 严格绑定本地 127.0.0.1，注入安全响应头并自动打开默认浏览器
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from typing import Any, Dict, List, Optional


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


ensure_utf8_console_streams()
disable_windows_console_quick_edit()


def get_bundle_dir() -> str:
    """获取静态资源所在目录（兼容 PyInstaller --onefile / --onedir 的 _MEIPASS 目录）."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return getattr(sys, "_MEIPASS")
    return os.path.dirname(os.path.abspath(__file__))


def resolve_static_dir() -> str:
    """按优先级探测包含完整 index.html 与前端入口脚本的静态文件目录，避免空目录导致 404."""
    candidates: List[str] = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            candidates.append(os.path.join(getattr(sys, "_MEIPASS"), "static"))
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "static"))
        candidates.append(os.path.join(exe_dir, "_internal", "static"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
    candidates.append(os.path.join(os.getcwd(), "static"))

    for cand in candidates:
        has_entry_js = os.path.isfile(os.path.join(cand, "js", "main.js")) or os.path.isfile(
            os.path.join(cand, "app.js")
        )
        if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "index.html")) and has_entry_js:
            return cand

    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return candidates[0]


def get_runtime_data_dir() -> str:
    """获取可写运行时数据目录（在 Windows exe 模式下保存在 exe 同级 data 目录）."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def pause_on_windows_console(prompt_msg: str = "\n请按回车键 (Enter) 关闭此控制台窗口...") -> None:
    """在 Windows 控制台中暂停，防止双击启动 exe 时发生闪退无法看清报错."""
    if getattr(sys, "frozen", False) or os.name == "nt":
        try:
            input(prompt_msg)
        except Exception:
            time.sleep(10)


try:
    import anyio
    import anyio._backends._asyncio  # noqa: F401
    import anyio.to_thread
    import h11  # noqa: F401
    import multipart  # noqa: F401
    import openpyxl  # noqa: F401
    import psycopg2  # noqa: F401
    import pymysql  # noqa: F401
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response
    from fastapi.staticfiles import StaticFiles

    from core.dws_engine import DWSMigratorEngine
    from core.platform.context import PlatformContext
    from core.platform.router import router as platform_router
    from modules.cross_comparator.router import router as cross_comparator_router
    from modules.data_explorer.router import router as data_explorer_router
    from modules.ddl_translator.router import router as ddl_translator_router
    from modules.standard_migrator.router import router as standard_migrator_router
except BaseException:
    err_text = traceback.format_exc()
    print("=" * 74)
    print("  [严重错误] DataOG 程序初始化导入依赖模块失败！")
    print("=" * 74)
    print(err_text)
    try:
        err_log_path = os.path.join(get_runtime_data_dir(), "startup_error.log")
        with open(err_log_path, "w", encoding="utf-8") as f:
            f.write(err_text)
        print(f"  [*] 详细错误日志已写入: {err_log_path}")
    except Exception:
        pass
    pause_on_windows_console()
    sys.exit(1)


class SafeConsoleStreamHandler(logging.StreamHandler):
    """防阻塞控制台日志处理器：及时 flush 并忽略控制台管道异常."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
            self.flush()
        except Exception:
            pass


def setup_console_logging() -> logging.Logger:
    """配置控制台与文件双通道日志输出."""
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    log = logging.getLogger("dataog")
    log.setLevel(logging.INFO)
    if not log.handlers:
        console_handler = SafeConsoleStreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        log.addHandler(console_handler)

        try:
            log_file = os.path.join(get_runtime_data_dir(), "dataog_service.log")
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            log.addHandler(file_handler)
        except Exception:
            pass

    for uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "dws_migrator"):
        uv_log = logging.getLogger(uv_name)
        uv_log.handlers = log.handlers
        uv_log.setLevel(logging.INFO)
        uv_log.propagate = False

    return log


logger = setup_console_logging()

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0%" stop-color="#0071e3"/><stop offset="100%" stop-color="#00c6ff"/>'
    "</linearGradient></defs>"
    '<rect width="64" height="64" rx="15" fill="url(#g)"/>'
    '<text x="50%" y="55%" dominant-baseline="middle" text-anchor="middle" '
    'fill="#ffffff" font-family="-apple-system,sans-serif" font-weight="800" font-size="22">OG</text>'
    "</svg>"
)

_SECURITY_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def find_available_port(host: str = "127.0.0.1", preferred_port: int = 18080) -> int:
    """自动检测可用端口."""
    for candidate in range(preferred_port, preferred_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def create_app(
    db_path: Optional[str] = None,
    ctx: Optional[PlatformContext] = None,
) -> FastAPI:
    if ctx is None:
        if not db_path:
            db_path = os.path.join(get_runtime_data_dir(), "dataog_state.db")
        ctx = PlatformContext(db_path=db_path)
    ctx.register_module_manifest(
        module_id="standard_migrator",
        title="单库落标贯标台",
        subtitle="物理列序保序重建、Kahn 拓扑改名与多线程批量落标",
        api_prefix="/api/modules/standard_migrator",
    )
    ctx.register_module_manifest(
        module_id="cross_comparator",
        title="跨环境三栏数标比对",
        subtitle="来源库 × 数据标准 × 目标库 16:9 三方映射与闭环推送",
        api_prefix="/api/modules/cross_comparator",
    )
    ctx.register_module_manifest(
        module_id="ddl_translator",
        title="跨方言 DDL 与视图穿透",
        subtitle="多方言建表 DDL 互译与上游视图四层血缘自动穿透",
        api_prefix="/api/modules/ddl_translator",
    )
    ctx.register_module_manifest(
        module_id="data_explorer",
        title="AI 只读智能数据探查",
        subtitle="硬件级只读沙箱与多轮自动采样画像会话",
        api_prefix="/api/modules/data_explorer",
    )

    store = ctx.store
    engine = DWSMigratorEngine(store)

    # 清理旧版本遗留的内置沙盒假数据，确保纯净初始化
    if store.get_meta("current_excel_name") == "内置示例_DWS贯标清单.xlsx":
        store.clear_items()

    app = FastAPI(
        title="Data.O.G",
        description="macOS 桌面级多功能数据工程与数据治理一体化工作台",
        version="2.2.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.platform_ctx = ctx
    app.state.store = store
    app.state.engine = engine

    @app.middleware("http")
    async def security_headers_and_csrf_middleware(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
            sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch_site and sec_fetch_site not in ("same-origin", "same-site", "none"):
                return JSONResponse(
                    status_code=403,
                    content={"ok": False, "error": "禁止跨站伪造请求 (Cross-Site Request Blocked)"},
                )

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _SECURITY_CSP)
        return response

    # 注册平台公共底座路由与四大独立插件模块路由
    app.include_router(platform_router)
    app.include_router(standard_migrator_router)
    app.include_router(cross_comparator_router)
    app.include_router(ddl_translator_router)
    app.include_router(data_explorer_router)

    # =========================================================================
    # 向后兼容 v1.0.0 历史 API 路由（共享同一底层 PlatformStateStore）
    # =========================================================================

    def _safe_legacy_conn_info() -> Dict[str, Any]:
        info = dict(engine.get_public_conn_info())
        has_pwd = bool(info.pop("password", ""))
        info["has_saved_password"] = has_pwd
        info["password_masked"] = "******" if has_pwd else ""
        return info

    def _build_full_state_dict(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "connection": _safe_legacy_conn_info(),
            "excel_meta": {
                "filename": store.get_meta("current_excel_name", "未导入"),
                "imported_at": store.get_meta("imported_at", ""),
            },
            "summary": store.get_summary(),
            "items": store.list_items(),
            "logs": store.list_logs(limit=150),
        }
        if extra:
            payload.update(extra)
        return payload

    @app.get("/api/bootstrap", include_in_schema=False)
    async def api_bootstrap() -> JSONResponse:
        data = await anyio.to_thread.run_sync(_build_full_state_dict)
        return JSONResponse(data)

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    async def route_favicon() -> Response:
        custom_fav = await anyio.to_thread.run_sync(ctx.asset_store.read_brand_favicon)
        if custom_fav is not None:
            raw_bytes, mime = custom_fav
            return Response(
                content=raw_bytes,
                media_type=mime,
                headers={"X-Content-Type-Options": "nosniff"},
            )
        return Response(content=FAVICON_SVG, media_type="image/svg+xml")

    @app.api_route("/.well-known/{rest_of_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def route_well_known(rest_of_path: str) -> Response:
        _ = rest_of_path
        return Response(status_code=204)

    static_dir = resolve_static_dir()
    if not os.path.isdir(static_dir):
        raise RuntimeError(
            f"未找到前端静态资源目录 static ({static_dir})，请确保使用 python build_exe.py 完整打包！"
        )

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="DataOG 多功能数据工程与数据治理一体化工作台")
    parser.add_argument(
        "--port", type=int, default=18080, help="本地 Web 监听端口 (默认 18080)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="启动时不自动打开浏览器"
    )
    parser.add_argument(
        "--test", action="store_true", help="运行内置自动化单元测试并退出"
    )
    args = parser.parse_args()

    if args.test:
        import unittest

        sys.modules.setdefault("app", sys.modules[__name__])
        suite = unittest.defaultTestLoader.discover(
            start_dir=os.path.join(get_bundle_dir(), "tests"),
            pattern="test_*.py",
        )
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    host = "127.0.0.1"
    port = find_available_port(host, args.port)
    url = f"http://{host}:{port}"

    print("=" * 74)
    print("  Data.O.G v2.2.0 — 多功能数据工程与数据治理一体化工作台")
    print("=" * 74)
    if port != args.port:
        print(f"  [!] 提示: 默认端口 {args.port} 已被占用，已自动切换至端口 {port}")
    print(f"  [*] 本地 IDE 工作台地址 : {url}")
    print(
        f"  [*] 本地状态库存储路径  : {os.path.join(get_runtime_data_dir(), 'dataog_state.db')}"
    )
    print("  [*] 按 Ctrl + C 可安全停止服务并退出。")
    print("=" * 74)

    app_instance = create_app()

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    config = uvicorn.Config(
        app=app_instance,
        host=host,
        port=port,
        log_level="info",
        log_config=None,
        loop="asyncio",
        http="h11",
        ws="none",
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] 已收到退出指令，服务已安全停止。")
    except SystemExit as sys_exit:
        if sys_exit.code not in (0, None) and "--test" not in sys.argv:
            pause_on_windows_console()
        raise
    except BaseException:
        crash_text = traceback.format_exc()
        print("\n" + "=" * 74)
        print("  [程序运行异常] 服务未能正常启动或运行中断，详细错误如下：")
        print("=" * 74)
        print(crash_text)
        try:
            crash_path = os.path.join(get_runtime_data_dir(), "startup_error.log")
            with open(crash_path, "w", encoding="utf-8") as f:
                f.write(crash_text)
            print(f"  [*] 错误详情已保存至: {crash_path}")
        except Exception:
            pass
        pause_on_windows_console()
        sys.exit(1)
