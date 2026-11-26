"""Data.O.G 多功能数据工程与数据治理一体化工作台 - FastAPI 可热更新微内核引擎 (core/app_kernel.py).

职责：
1. 提供 `create_app()`、`run_smoke_test()`、`main()` 及静态资源解析（优先加载已验签增量补丁静态目录）
2. 初始化三层分离微内核底座 PlatformContext（应用无状态 + 混淆本地私有保险箱 + 可共享团队工作区，自动无损迁移历史数据）
3. 挂载平台公共路由 (/api/platform) 与全部独立业务插件模块 (/api/modules/*)
4. 严格绑定本地 127.0.0.1，注入安全响应头并在启动健康就绪后通知 update_bootstrap 清零启动故障计数
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

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
from starlette.concurrency import run_in_threadpool

import update_bootstrap
from core.platform.context import PlatformContext
from core.platform.license_hub import AUTH_PUBLIC_EXEMPT_PREFIXES
from core.platform.module_registry import mount_discovered_modules
from core.platform.router import router as platform_router
from core.platform.storage_paths import redact_private_storage_paths

APP_VERSION = "3.7.0"
_NO_CACHE_FRONTEND_EXTS = (".html", ".htm", ".js", ".mjs", ".css")
_UI_VERSION_META_RE = re.compile(r'<meta\s+name="dataog-ui-version"\s+content="([^"]+)"')


def _project_root_from_kernel() -> str:
    """定位仓库根目录（core/app_kernel.py 上溯两级目录）."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_bundle_dir() -> str:
    """获取静态资源所在目录（兼容 PyInstaller --onefile / --onedir 的 _MEIPASS 目录）."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return getattr(sys, "_MEIPASS")
    return _project_root_from_kernel()


def resolve_static_dir() -> str:
    """按优先级探测包含完整 index.html 与前端入口脚本的静态文件目录（优先激活态已验签增量补丁）."""
    candidates: List[str] = []
    patched_static = update_bootstrap.get_active_patch_static_dir()
    if patched_static:
        candidates.append(patched_static)
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            candidates.append(os.path.join(getattr(sys, "_MEIPASS"), "static"))
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "static"))
        candidates.append(os.path.join(exe_dir, "_internal", "static"))
    candidates.append(os.path.join(_project_root_from_kernel(), "static"))
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
    return "--test" in sys.argv or "--smoke-test" in sys.argv or "unittest" in sys.modules


def pause_on_windows_console(prompt_msg: str = "\n请按回车键 (Enter) 关闭此控制台窗口...") -> None:
    """在 Windows 控制台中暂停，防止双击启动 exe 时发生闪退无法看清报错."""
    if getattr(sys, "frozen", False) or os.name == "nt":
        try:
            input(prompt_msg)
        except Exception:
            time.sleep(10)


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
                or "/api/platform/diagnostics/ping" in msg
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


def check_static_ui_version_sync(static_dir: str, app_version: str = APP_VERSION) -> Tuple[bool, str]:
    """校验静态目录 index.html 声明的前端版本号是否与当前后端版本一致."""
    index_path = os.path.join(str(static_dir or ""), "index.html")
    if not os.path.isfile(index_path):
        return False, ""
    try:
        with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
            head_html = fh.read(4096)
    except OSError:
        return False, ""
    matched = _UI_VERSION_META_RE.search(head_html)
    if not matched:
        return False, ""
    ui_ver = matched.group(1).strip()
    return (ui_ver == app_version), ui_ver


def _apply_frontend_no_cache_headers(req_path: str, resp: Response) -> Response:
    """对非 API 的 HTML/JS/CSS 前端静态资源强制禁止浏览器陈旧缓存."""
    if not req_path.startswith("/api/") and (
        req_path == "/"
        or req_path.endswith("/")
        or req_path.lower().endswith(_NO_CACHE_FRONTEND_EXTS)
    ):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


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
    base_ver = update_bootstrap.get_runtime_base_version(fallback=APP_VERSION)
    if ctx is None:
        if db_path:
            ctx = PlatformContext(db_path=db_path, base_version=base_ver)
        else:
            ctx = PlatformContext(private_root=private_root, base_version=base_ver)
    store = ctx.store

    # 清理旧版本遗留的内置沙盒假数据，确保纯净初始化
    if store.get_meta("current_excel_name") == "内置示例_DWS贯标清单.xlsx":
        store.clear_items()

    app = FastAPI(
        title="Data.O.G",
        description="macOS 桌面级多功能数据工程与数据治理一体化工作台",
        version=APP_VERSION,
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
        diag_hub = ctx.diagnostics_hub
        trace_obj, trace_tok = diag_hub.begin_request(
            request.method,
            req_path,
            list(request.query_params.keys()),
            is_api=req_path.startswith("/api/"),
        )
        request.state.diag_trace = trace_obj
        finished = False
        status_code = 500
        auth_gate1_ms = 0.0
        handler_ms = 0.0
        error_redact_ms = 0.0
        response_bytes: Optional[int] = None
        error_text = ""

        def _finalize_and_attach_headers(resp: Response) -> Response:
            nonlocal finished, status_code, response_bytes
            status_code = int(getattr(resp, "status_code", status_code) or status_code)
            if response_bytes is None:
                body_attr = getattr(resp, "body", None)
                if isinstance(body_attr, (bytes, bytearray)):
                    response_bytes = len(body_attr)
                else:
                    cl_hdr = resp.headers.get("content-length")
                    if cl_hdr and cl_hdr.isdigit():
                        response_bytes = int(cl_hdr)
            if not finished:
                rec = diag_hub.finish_request(
                    trace_obj,
                    trace_tok,
                    status_code=status_code,
                    auth_gate1_ms=auth_gate1_ms,
                    handler_ms=handler_ms,
                    error_redact_ms=error_redact_ms,
                    response_bytes=response_bytes,
                    error_text=error_text,
                )
                finished = True
            else:
                rec = {
                    "trace_id": trace_obj.trace_id,
                    "total_ms": round((time.perf_counter() - trace_obj.start_mono) * 1000.0, 2),
                    "auth_gate1_ms": round(auth_gate1_ms, 2),
                    "handler_ms": round(handler_ms, 2),
                }
            resp.headers["X-DataOG-Trace-Id"] = str(rec["trace_id"])
            resp.headers["X-DataOG-Server-Ms"] = f"{float(rec['total_ms']):.2f}"
            resp.headers["X-DataOG-Auth-Ms"] = f"{float(rec['auth_gate1_ms']):.2f}"
            resp.headers["X-DataOG-Handler-Ms"] = f"{float(rec['handler_ms']):.2f}"
            resp.headers["Server-Timing"] = (
                f"auth;dur={float(rec['auth_gate1_ms']):.2f}, "
                f"handler;dur={float(rec['handler_ms']):.2f}, "
                f"total;dur={float(rec['total_ms']):.2f}"
            )
            return resp

        try:
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and req_path.startswith("/api/"):
                sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
                if sec_fetch_site and sec_fetch_site not in ("same-origin", "same-site", "none"):
                    error_text = "Cross-Site Request Blocked"
                    blocked_resp = JSONResponse(
                        status_code=403,
                        content={"ok": False, "error": "禁止跨站伪造请求 (Cross-Site Request Blocked)"},
                    )
                    return _apply_security_headers(_finalize_and_attach_headers(blocked_resp))

            # Gate 1: 全量 /api/* 请求离线非对称激活码鉴权拦截（仅放行公开认证与外观静态图片读取）
            if req_path.startswith("/api/") and not any(
                req_path.startswith(pref) for pref in AUTH_PUBLIC_EXEMPT_PREFIXES
            ):
                t_auth0 = time.perf_counter()
                fast_auth = ctx.license_hub.try_fast_stamp_request(request)
                if fast_auth is not None:
                    ok_auth, auth_info = fast_auth
                else:
                    ok_auth, auth_info = await run_in_threadpool(
                        ctx.license_hub.check_and_stamp_request, request
                    )
                auth_gate1_ms = (time.perf_counter() - t_auth0) * 1000.0
                if not ok_auth:
                    error_text = str(auth_info.get("status") or "LICENSE_REQUIRED")
                    unauth_resp = JSONResponse(
                        status_code=401,
                        content={
                            "ok": False,
                            "code": "LICENSE_REQUIRED",
                            "license_status": auth_info.get("status", "UNACTIVATED"),
                            "error": auth_info.get("status_message")
                            or "系统尚未激活或授权已过期，请先输入有效激活码",
                        },
                    )
                    return _apply_security_headers(_finalize_and_attach_headers(unauth_resp))

            t_handler0 = time.perf_counter()
            response = await call_next(request)
            handler_ms = (time.perf_counter() - t_handler0) * 1000.0

            # 全平台错误响应兜底脱敏：确保任意平台/模块路由报错均绝不向前端暴露本地私有保险箱物理路径或内部文件名
            if (
                req_path.startswith("/api/")
                and response.status_code >= 400
                and "application/json" in (response.headers.get("content-type") or "").lower()
            ):
                t_red0 = time.perf_counter()
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
                    redacted_bytes = redacted_text.encode("utf-8")
                    response_bytes = len(redacted_bytes)
                    error_text = redacted_text[:300]
                    passthrough_headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in ("content-length", "content-type")
                    }
                    response = Response(
                        content=redacted_bytes,
                        status_code=response.status_code,
                        headers=passthrough_headers,
                        media_type="application/json",
                    )
                except Exception:
                    pass
                error_redact_ms = (time.perf_counter() - t_red0) * 1000.0

            return _apply_security_headers(
                _apply_frontend_no_cache_headers(req_path, _finalize_and_attach_headers(response))
            )
        except BaseException as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if not finished:
                diag_hub.finish_request(
                    trace_obj,
                    trace_tok,
                    status_code=status_code,
                    auth_gate1_ms=auth_gate1_ms,
                    handler_ms=handler_ms,
                    error_redact_ms=error_redact_ms,
                    response_bytes=response_bytes,
                    error_text=error_text,
                )

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


async def _asgi_smoke_get(app_inst: Any, path: str) -> Tuple[int, Dict[str, str], bytes]:
    """零网络/零 httpx 依赖的内联 ASGI GET 探活器（供冻结态 --smoke-test 极速验证）."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:18080"),
            (b"sec-fetch-site", b"same-origin"),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 18080),
    }
    sent = False
    response_complete = asyncio.Event()
    status_code = 500
    resp_headers: List[Tuple[bytes, bytes]] = []
    resp_chunks: List[bytes] = []

    async def receive() -> Dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(msg: Dict[str, Any]) -> None:
        nonlocal status_code, resp_headers
        if msg["type"] == "http.response.start":
            status_code = int(msg["status"])
            resp_headers = list(msg.get("headers") or [])
        elif msg["type"] == "http.response.body":
            resp_chunks.append(msg.get("body") or b"")
            if not msg.get("more_body", False):
                response_complete.set()

    try:
        await app_inst(scope, receive, send)
    finally:
        response_complete.set()

    headers_map = {
        k.decode("latin-1").lower(): v.decode("latin-1") for k, v in resp_headers
    }
    return status_code, headers_map, b"".join(resp_chunks)


def run_smoke_test(require_native_shield: Optional[bool] = None) -> Dict[str, Any]:
    """2 秒极速启动冒烟自检：验证冻结态核心依赖、原生护盾、静态资源、插件挂载与 ASGI 路由."""
    t0 = time.perf_counter()

    # 1. 核心运行时依赖可导入性校验
    required_runtime_modules = (
        "fastapi",
        "pydantic_core",
        "uvicorn",
        "h11",
        "anyio",
        "openpyxl",
        "et_xmlfile",
        "multipart",
        "psycopg2",
        "pymysql",
        "sqlite3",
    )
    for mod_name in required_runtime_modules:
        importlib.import_module(mod_name)

    # 2. 静态前端目录与版本号一致校验
    static_dir = resolve_static_dir()
    if not os.path.isdir(static_dir):
        raise RuntimeError(f"静态资源目录不存在: {static_dir}")
    for rel_asset in (
        "index.html",
        os.path.join("js", "main.js"),
        os.path.join("css", "workbench.css"),
        "dataog.ico",
    ):
        asset_path = os.path.join(static_dir, rel_asset)
        if not os.path.isfile(asset_path):
            raise RuntimeError(f"缺失关键静态资源文件: {rel_asset} ({static_dir})")

    ui_ok, ui_ver = check_static_ui_version_sync(static_dir, APP_VERSION)
    if not ui_ok:
        raise RuntimeError(
            f"前端版本声明 ({ui_ver or '未知'}) 与后端 APP_VERSION (v{APP_VERSION}) 不一致"
        )

    # 3. 隔离临时保险箱实例化 FastAPI 微内核并验证插件/知识库/原生护盾
    smoke_tmp = tempfile.mkdtemp(prefix="dataog_smoke_")
    ctx = None
    try:
        vault_root = os.path.join(smoke_tmp, "vault")
        app_inst = create_app(private_root=vault_root)
        if getattr(app_inst, "version", "") != APP_VERSION:
            raise RuntimeError(
                f"FastAPI app.version ({getattr(app_inst, 'version', '')}) != {APP_VERSION}"
            )
        ctx = getattr(getattr(app_inst, "state", None), "platform_ctx", None)
        if ctx is None:
            raise RuntimeError("PlatformContext 未正确挂载至 app.state.platform_ctx")

        expected_modules = {
            "standard_migrator",
            "cross_comparator",
            "ddl_translator",
            "data_explorer",
            "ai_assistant",
        }
        loaded_modules = {m["module_id"] for m in ctx.module_registry.list_manifests()}
        missing_modules = sorted(expected_modules - loaded_modules)
        if missing_modules:
            raise RuntimeError(f"自动发现插件模块缺失: {missing_modules}")

        builtin_kb_docs = len(ctx.knowledge_hub.list_documents())
        if builtin_kb_docs < 1:
            raise RuntimeError("内置方言参考知识库未自动种子化")

        auth_status = ctx.license_hub.get_auth_status()
        has_shield = bool(auth_status.get("native_shield"))
        must_shield = (
            bool(getattr(sys, "frozen", False))
            if require_native_shield is None
            else bool(require_native_shield)
        )
        if must_shield and not has_shield:
            raise RuntimeError("原生防反编译护盾 (_license_shield) 未能加载")

        # 4. 内存 ASGI 四路探活（静态首页防缓存 + 主模块 + 鉴权开放路由 + Gate 1 拦截）
        async def _run_probes() -> None:
            st_root, hdr_root, body_root = await _asgi_smoke_get(app_inst, "/")
            if st_root != 200:
                raise RuntimeError(f"GET / 返回异常状态码: {st_root}")
            if "no-store" not in hdr_root.get("cache-control", ""):
                raise RuntimeError(f"GET / 缺少防缓存响应头: {hdr_root.get('cache-control')}")
            if "dataog-ui-version" not in body_root.decode("utf-8", "replace"):
                raise RuntimeError("GET / 响应体缺少 dataog-ui-version 元标签")

            st_js, _, body_js = await _asgi_smoke_get(app_inst, "/js/main.js")
            if st_js != 200 or "bootstrapWorkbench" not in body_js.decode("utf-8", "replace"):
                raise RuntimeError(f"GET /js/main.js 探活失败: status={st_js}")

            st_auth, hdr_auth, body_auth = await _asgi_smoke_get(
                app_inst, "/api/platform/auth/status"
            )
            if st_auth != 200:
                raise RuntimeError(f"GET /api/platform/auth/status 返回异常状态码: {st_auth}")
            if "x-dataog-trace-id" not in hdr_auth or "server-timing" not in hdr_auth:
                raise RuntimeError("诊断响应头 X-DataOG-Trace-Id / Server-Timing 缺失")
            auth_json = json.loads(body_auth.decode("utf-8"))
            if (
                auth_json.get("ok") is not True
                or auth_json.get("authorized") is not False
                or not auth_json.get("login_modes")
            ):
                raise RuntimeError(f"/api/platform/auth/status 响应负载异常: {auth_json}")

            st_boot, _, _ = await _asgi_smoke_get(app_inst, "/api/platform/bootstrap")
            if st_boot != 401:
                raise RuntimeError(
                    f"Gate 1 授权拦截失效: 未登录访问 /api/platform/bootstrap 返回 {st_boot}"
                )

        asyncio.run(_run_probes())
    finally:
        if ctx is not None:
            ctx.close()
        for attempt in range(4):
            shutil.rmtree(smoke_tmp, ignore_errors=True)
            if not os.path.exists(smoke_tmp):
                break
            time.sleep(0.1 * (attempt + 1))

    update_bootstrap.mark_boot_healthy()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "version": APP_VERSION,
        "base_version": update_bootstrap.get_runtime_base_version(fallback=APP_VERSION),
        "patch_active": update_bootstrap.is_patch_active(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "native_shield": has_shield,
        "modules": sorted(loaded_modules),
        "builtin_kb_docs": builtin_kb_docs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DataOG 多功能数据工程与数据治理一体化工作台")
    parser.add_argument(
        "--port", type=int, default=18080, help="本地 Web 监听端口 (默认 18080)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="启动时不自动打开浏览器"
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="运行极速启动冒烟自检（验证冻结态依赖、原生护盾、静态资源、插件挂载与 ASGI 路由）并退出",
    )
    parser.add_argument(
        "--test", action="store_true", help="运行内置自动化单元测试并退出"
    )
    args = parser.parse_args()

    if args.smoke_test:
        try:
            summary = run_smoke_test()
        except Exception as exc:
            print(f"[SMOKE] FAILED: {exc}")
            traceback.print_exc()
            sys.exit(1)
        mods_csv = ",".join(summary["modules"])
        print(
            f"[SMOKE] OK | version=v{summary['version']} "
            f"| base=v{summary['base_version']} "
            f"| patch_active={summary['patch_active']} "
            f"| modules={mods_csv} "
            f"| native_shield={summary['native_shield']} "
            f"| builtin_kb={summary['builtin_kb_docs']} "
            f"| elapsed={summary['elapsed_ms']}ms"
        )
        sys.exit(0)

    if args.test:
        tests_dir = os.path.join(get_bundle_dir(), "tests")
        if not os.path.isdir(tests_dir):
            print(
                "[INFO] 当前客户发行包未携带开发期单元测试目录 (tests/)；"
                "如需验证打包运行完整性请使用 --smoke-test，"
                '或请在源码仓库中运行 `python3 -m unittest discover -s tests -p "test_*.py" -v`。'
            )
            sys.exit(1)
        import unittest

        suite = unittest.defaultTestLoader.discover(
            start_dir=tests_dir,
            pattern="test_*.py",
        )
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    host = "127.0.0.1"
    port = find_available_port(host, args.port)
    url = f"http://{host}:{port}"

    print("=" * 74)
    print(f"  Data.O.G v{APP_VERSION} — 多功能数据工程与数据治理一体化工作台")
    print("=" * 74)
    if update_bootstrap.is_patch_active():
        base_ver = update_bootstrap.get_runtime_base_version(fallback=APP_VERSION)
        print(f"  [*] 运行增量补丁       : v{APP_VERSION} (底座 v{base_ver}，数字签名校验通过)")
    else:
        patch_state = update_bootstrap.get_active_patch_state()
        if patch_state.get("boot_message"):
            print(f"  [!] 增量状态提示       : {patch_state['boot_message']}")
    if port != args.port:
        print(f"  [!] 提示: 默认端口 {args.port} 已被占用，已自动切换至端口 {port}")
    print(f"  [*] 本地工作台访问地址 : {url}")
    print("  [*] 数据存储架构       : 本地私有保险箱与团队共享工作区已分离就绪")
    print("  [*] 按 Ctrl + C 可安全停止服务并退出。")
    print("=" * 74)
    ui_ok, ui_ver = check_static_ui_version_sync(resolve_static_dir(), APP_VERSION)
    if not ui_ok:
        detected = ui_ver or "旧版/未知"
        print(f"  [!] 警告: 检测到前端静态资源版本 ({detected}) 与程序版本 (v{APP_VERSION}) 不一致！")
        print("  [!] 提示: 请将压缩包中的 DataOG.exe 与 _internal 文件夹一起完整解压覆盖后重试。")

    app_instance = create_app()
    update_bootstrap.mark_boot_healthy()

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
    try:
        server.run()
    finally:
        try:
            app_instance.state.platform_ctx.close()
        except Exception:
            pass
