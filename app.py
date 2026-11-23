"""Data.O.G 多功能数据工程与数据治理一体化工作台 - FastAPI 微内核服务端入口.

启动后：
1. 自动禁用 Windows CMD 控制台快速编辑模式 (QuickEdit Mode)，防止鼠标点击黑窗口挂起服务进程
2. 初始化三层分离微内核底座 PlatformContext（应用无状态 + 混淆本地私有保险箱 + 可共享团队工作区，自动无损迁移历史数据）
3. 挂载平台公共路由 (/api/platform) 与四大高内聚业务插件模块 (/api/modules/*)
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
    """获取可写私有运行时存储目录（内部使用，严禁向用户界面或控制台暴露路径）."""
    from core.platform.storage_paths import resolve_private_storage_layout

    layout = resolve_private_storage_layout(create_dirs=True)
    return layout.private_root


def _is_test_runtime() -> bool:
    return "--test" in sys.argv or "unittest" in sys.modules


def _write_startup_crash_log(err_text: str) -> None:
    """将启动崩溃栈安全落盘至私有日志目录，不在终端暴露私有目录路径."""
    try:
        from core.platform.storage_paths import resolve_private_storage_layout

        layout = resolve_private_storage_layout(create_dirs=True)
        err_log_path = os.path.join(layout.logs_dir, "startup_error.log")
        with open(err_log_path, "w", encoding="utf-8") as f:
            f.write(err_text)
    except Exception:
        pass


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

    from core.platform.context import PlatformContext
    from core.platform.license_hub import AUTH_PUBLIC_EXEMPT_PREFIXES
    from core.platform.module_registry import mount_discovered_modules
    from core.platform.router import router as platform_router
    from core.platform.storage_paths import redact_private_storage_paths
except BaseException:
    err_text = traceback.format_exc()
    print("=" * 74)
    print("  [严重错误] Data.O.G 程序初始化导入依赖模块失败！")
    print("=" * 74)
    print(err_text)
    _write_startup_crash_log(err_text)
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


class _QuietImportProgressAccessFilter(logging.Filter):
    """过滤高频任务/导入进度轮询的正常 200 访问日志，防止控制台与日志文件刷屏，保留非 200 异常记录."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if (
                "/api/platform/import-progress/" in msg
                or "/api/platform/tasks/" in msg
            ) and '" 200' in msg:
                return False
        except Exception:
            pass
        return True


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

        if not _is_test_runtime() or bool((os.environ.get("DATAOG_HOME") or "").strip()):
            try:
                from core.platform.storage_paths import resolve_private_storage_layout

                layout = resolve_private_storage_layout(create_dirs=True)
                file_handler = logging.FileHandler(layout.service_log_path, encoding="utf-8")
                file_handler.setFormatter(formatter)
                log.addHandler(file_handler)
            except Exception:
                pass

    for uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "dws_migrator"):
        uv_log = logging.getLogger(uv_name)
        uv_log.handlers = log.handlers
        uv_log.setLevel(logging.INFO)
        uv_log.propagate = False
        if uv_name == "uvicorn.access" and not any(
            isinstance(f, _QuietImportProgressAccessFilter) for f in uv_log.filters
        ):
            uv_log.addFilter(_QuietImportProgressAccessFilter())

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
    *,
    private_root: Optional[str] = None,
    extra_modules: Optional[List[Any]] = None,
) -> FastAPI:
    if ctx is None:
        if db_path:
            ctx = PlatformContext(db_path=db_path)
        else:
            ctx = PlatformContext(private_root=private_root)
    store = ctx.store

    # 清理旧版本遗留的内置沙盒假数据，确保纯净初始化
    if store.get_meta("current_excel_name") == "内置示例_DWS贯标清单.xlsx":
        store.clear_items()

    app = FastAPI(
        title="Data.O.G",
        description="macOS 桌面级多功能数据工程与数据治理一体化工作台",
        version="2.9.1",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.platform_ctx = ctx
    app.state.store = store

    def _apply_security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", _SECURITY_CSP)
        return resp

    @app.middleware("http")
    async def security_headers_and_csrf_middleware(request: Request, call_next):
        req_path = request.url.path
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and req_path.startswith("/api/"):
            sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch_site and sec_fetch_site not in ("same-origin", "same-site", "none"):
                return _apply_security_headers(
                    JSONResponse(
                        status_code=403,
                        content={"ok": False, "error": "禁止跨站伪造请求 (Cross-Site Request Blocked)"},
                    )
                )

        # Gate 1: 全量 /api/* 请求离线非对称激活码鉴权拦截（仅放行公开认证与外观静态图片读取）
        if req_path.startswith("/api/") and not any(
            req_path.startswith(pref) for pref in AUTH_PUBLIC_EXEMPT_PREFIXES
        ):
            ok_auth, auth_info = ctx.license_hub.check_and_stamp_request(request)
            if not ok_auth:
                return _apply_security_headers(
                    JSONResponse(
                        status_code=401,
                        content={
                            "ok": False,
                            "code": "LICENSE_REQUIRED",
                            "license_status": auth_info.get("status", "UNACTIVATED"),
                            "error": auth_info.get("status_message")
                            or "系统尚未激活或授权已过期，请先输入有效激活码",
                        },
                    )
                )

        response = await call_next(request)
        # 全平台错误响应兜底脱敏：确保任意平台/模块路由报错均绝不向前端暴露本地私有保险箱物理路径或内部文件名
        if (
            req_path.startswith("/api/")
            and response.status_code >= 400
            and "application/json" in (response.headers.get("content-type") or "").lower()
        ):
            try:
                raw_chunks: List[bytes] = []
                if hasattr(response, "body_iterator") and response.body_iterator is not None:
                    async for chunk in response.body_iterator:
                        raw_chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
                    raw_body = b"".join(raw_chunks)
                else:
                    raw_body = bytes(getattr(response, "body", b"") or b"")
                raw_text = raw_body.decode("utf-8", errors="replace")
                redacted_text = redact_private_storage_paths(raw_text, ctx=ctx)
                passthrough_headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in ("content-length", "content-type")
                }
                response = Response(
                    content=redacted_text.encode("utf-8"),
                    status_code=response.status_code,
                    headers=passthrough_headers,
                    media_type="application/json",
                )
            except Exception:
                pass
        return _apply_security_headers(response)

    # 注册平台公共底座路由并自动发现挂载 modules/* 全部独立插件模块
    app.include_router(platform_router)
    mount_discovered_modules(app, ctx)
    if extra_modules:
        mount_discovered_modules(app, ctx, specs=extra_modules)

    # =========================================================================
    # 向后兼容 v1.0.0 历史 API 路由（共享同一底层 PlatformStateStore）
    # =========================================================================

    def _safe_legacy_conn_info() -> Dict[str, Any]:
        if store.is_split_storage:
            active_cid = str(store.get_meta("migrator_connection_id", "") or "")
            prof = ctx.connection_hub.get_profile(active_cid) if active_cid else None
            if prof and prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
                has_pwd = bool(prof.password)
                return {
                    "configured": bool(prof.host and prof.dbname and prof.user),
                    "host": prof.host,
                    "port": int(prof.port or (5432 if prof.db_type == "PGSQL" else 8000)),
                    "dbname": prof.dbname,
                    "user": prof.user,
                    "sslmode": prof.sslmode or "prefer",
                    "current_schema": prof.default_schema or "public",
                    "schemas": [],
                    "discovered_table_count": 0,
                    "discovered_tables": [],
                    "has_saved_password": has_pwd,
                    "password_masked": "******" if has_pwd else "",
                }
            return {
                "configured": False,
                "host": "",
                "port": 8000,
                "dbname": "",
                "user": "",
                "sslmode": "prefer",
                "current_schema": "public",
                "schemas": [],
                "discovered_table_count": 0,
                "discovered_tables": [],
                "has_saved_password": False,
                "password_masked": "",
            }
        from core.dws_engine import DWSMigratorEngine

        info = dict(DWSMigratorEngine(store).get_public_conn_info())
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
    async def api_bootstrap(request: Request) -> JSONResponse:
        ok, auth_info = ctx.license_hub.verify_request_capability(request)
        if not ok:
            return JSONResponse(
                status_code=401,
                content={
                    "ok": False,
                    "code": "LICENSE_REQUIRED",
                    "license_status": auth_info.get("status", "UNACTIVATED"),
                    "error": auth_info.get("status_message") or "系统尚未激活或授权已失效",
                },
            )
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
    print("  Data.O.G v2.9.1 — 多功能数据工程与数据治理一体化工作台")
    print("=" * 74)
    if port != args.port:
        print(f"  [!] 提示: 默认端口 {args.port} 已被占用，已自动切换至端口 {port}")
    print(f"  [*] 本地工作台访问地址 : {url}")
    print("  [*] 数据存储架构       : 本地私有保险箱与团队共享工作区已分离就绪")
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
        _write_startup_crash_log(crash_text)
        pause_on_windows_console()
        sys.exit(1)
