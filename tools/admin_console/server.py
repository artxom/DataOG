"""管理员总控台 FastAPI 应用与路由装配 (`tools/admin_console/server.py`)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import anyio
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tools.admin_console.auth import (
    AdminAuthManager,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
)
from tools.admin_console.service import AdminConsoleService, ConflictError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_ADMIN_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

_PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/status",
        "/api/auth/setup",
        "/api/auth/login",
    }
)

_CONSOLE_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="ag" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0%" stop-color="#0a84ff"/><stop offset="100%" stop-color="#30d158"/>'
    "</linearGradient></defs>"
    '<rect width="64" height="64" rx="15" fill="#141821"/>'
    '<rect x="4" y="4" width="56" height="56" rx="12" fill="none" stroke="url(#ag)" stroke-width="4"/>'
    '<text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" '
    'fill="#f5f7fa" font-family="-apple-system,sans-serif" font-weight="800" font-size="20">ADM</text>'
    "</svg>"
)


class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class ChangePasswordReq(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=6, max_length=256)


class OneClickDemoReq(BaseModel):
    registry_prefix: str = ""


class SandboxActionReq(BaseModel):
    action: str = Field(..., min_length=1, max_length=40)
    registry_prefix: str = ""


class DecodeReqCodeReq(BaseModel):
    request_code: str = Field(..., min_length=4, max_length=1024)


class IssueLicenseReq(BaseModel):
    mode: str = "local"
    days: Optional[int] = 90
    until_date: str = ""
    request_code: str = ""
    machine_code: str = ""
    ip_addr: str = ""
    note: str = ""


class ApplyLicenseReq(BaseModel):
    activation_code: str = Field(..., min_length=8, max_length=512)
    target: str = "demo"


def _apply_security_headers(resp: Response, *, is_api: bool = False) -> Response:
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", _ADMIN_CSP)
    if is_api:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )


def create_admin_app(
    auth_manager: Optional[AdminAuthManager] = None,
    service: Optional[AdminConsoleService] = None,
) -> FastAPI:
    auth = auth_manager or AdminAuthManager()
    svc = service or AdminConsoleService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            svc.shutdown_all()

    app = FastAPI(
        title="Data.O.G 管理员总控台",
        version="2.8.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.auth_manager = auth
    app.state.admin_service = svc

    @app.middleware("http")
    async def security_and_session_middleware(request: Request, call_next):
        req_path = request.url.path
        is_api = req_path.startswith("/api/")

        if is_api and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch_site and sec_fetch_site not in ("same-origin", "same-site", "none"):
                return _apply_security_headers(
                    JSONResponse(
                        status_code=403,
                        content={"ok": False, "error": "拒绝跨站写请求 (CSRF 校验拦截)"},
                    ),
                    is_api=True,
                )

        if is_api and req_path not in _PUBLIC_API_PATHS:
            token = request.cookies.get(SESSION_COOKIE_NAME)
            session = auth.validate_session(token)
            if not session:
                return _apply_security_headers(
                    JSONResponse(
                        status_code=401,
                        content={
                            "ok": False,
                            "code": "ADMIN_AUTH_REQUIRED",
                            "error": "请先登录管理员总控台",
                        },
                    ),
                    is_api=True,
                )
            request.state.admin_session = session
        else:
            request.state.admin_session = None

        response = await call_next(request)
        return _apply_security_headers(response, is_api=is_api)

    # =========================================================================
    # 健康检查与管理员认证
    # =========================================================================

    @app.get("/api/health")
    async def api_health() -> Dict[str, Any]:
        return {"ok": True, "service": "dataog-admin-console", "version": "2.8.0"}

    @app.get("/api/auth/status")
    async def api_auth_status(request: Request) -> Dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        sess = auth.validate_session(token)
        return {
            "ok": True,
            "initialized": auth.is_initialized(),
            "authenticated": bool(sess),
            "username": sess["username"] if sess else "",
        }

    @app.post("/api/auth/setup")
    async def api_auth_setup(body: LoginReq) -> Response:
        if auth.is_initialized():
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "管理员账号已存在，请直接登录"},
            )
        try:
            meta = await anyio.to_thread.run_sync(
                auth.set_credentials, body.username, body.password
            )
            ok, msg, token, status_code = await anyio.to_thread.run_sync(
                lambda: auth.verify_login(body.username, body.password, client_key="setup")
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

        if not ok or not token:
            return JSONResponse(status_code=status_code, content={"ok": False, "error": msg})
        resp = JSONResponse({"ok": True, "username": meta["username"]})
        _set_session_cookie(resp, token)
        return resp

    @app.post("/api/auth/login")
    async def api_auth_login(body: LoginReq, request: Request) -> Response:
        client_ip = request.client.host if request.client else "local"
        ok, msg, token, status_code = await anyio.to_thread.run_sync(
            lambda: auth.verify_login(body.username, body.password, client_key=client_ip)
        )
        if not ok or not token:
            return JSONResponse(
                status_code=status_code,
                content={"ok": False, "error": msg},
            )
        sess = auth.validate_session(token) or {}
        resp = JSONResponse({"ok": True, "username": sess.get("username", body.username)})
        _set_session_cookie(resp, token)
        return resp

    @app.post("/api/auth/logout")
    async def api_auth_logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        auth.logout(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
        return resp

    @app.post("/api/auth/change-password")
    async def api_auth_change_password(
        body: ChangePasswordReq, request: Request
    ) -> Response:
        sess = getattr(request.state, "admin_session", None) or {}
        username = str(sess.get("username") or "")
        try:
            new_token = await anyio.to_thread.run_sync(
                auth.change_password, username, body.old_password, body.new_password
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        resp = JSONResponse({"ok": True, "message": "管理员密码已更新"})
        _set_session_cookie(resp, new_token)
        return resp

    # =========================================================================
    # 总览与一键编排
    # =========================================================================

    @app.get("/api/overview")
    async def api_overview() -> Dict[str, Any]:
        data = await anyio.to_thread.run_sync(svc.get_overview)
        return {"ok": True, **data}

    @app.post("/api/one-click-demo")
    async def api_one_click_demo(body: OneClickDemoReq) -> JSONResponse:
        try:
            job = await anyio.to_thread.run_sync(
                lambda: svc.start_one_click_demo_job(registry_prefix=body.registry_prefix)
            )
            return JSONResponse({"ok": True, "job": job})
        except ConflictError as exc:
            return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/api/jobs")
    async def api_list_jobs() -> Dict[str, Any]:
        jobs = await anyio.to_thread.run_sync(svc.list_jobs)
        return {"ok": True, "jobs": jobs}

    @app.get("/api/jobs/{job_id}")
    async def api_get_job(job_id: str) -> Response:
        job = await anyio.to_thread.run_sync(svc.get_job, job_id)
        if not job:
            return JSONResponse(status_code=404, content={"ok": False, "error": "任务不存在"})
        return JSONResponse({"ok": True, "job": job})

    # =========================================================================
    # 工作台启停与日志
    # =========================================================================

    @app.get("/api/workbenches")
    async def api_workbenches() -> Dict[str, Any]:
        workbenches = await anyio.to_thread.run_sync(svc.get_workbenches_status)
        return {"ok": True, "workbenches": workbenches}

    @app.post("/api/workbenches/{mode}/start")
    async def api_start_workbench(mode: str) -> JSONResponse:
        try:
            info = await anyio.to_thread.run_sync(svc.start_workbench, mode)
            return JSONResponse({"ok": True, "workbench": info})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    @app.post("/api/workbenches/{mode}/stop")
    async def api_stop_workbench(mode: str) -> JSONResponse:
        try:
            info = await anyio.to_thread.run_sync(svc.stop_workbench, mode)
            return JSONResponse({"ok": True, "workbench": info})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/api/workbenches/{mode}/logs")
    async def api_workbench_logs(mode: str) -> JSONResponse:
        try:
            logs = await anyio.to_thread.run_sync(svc.get_workbench_logs, mode)
            return JSONResponse({"ok": True, "logs": logs})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    # =========================================================================
    # 演示沙箱、DBeaver 与演练剧本
    # =========================================================================

    @app.get("/api/sandbox/status")
    async def api_sandbox_status(verify: bool = False) -> Dict[str, Any]:
        status = await anyio.to_thread.run_sync(
            lambda: svc.get_sandbox_status(include_anomalies=verify)
        )
        return {"ok": True, "sandbox": status}

    @app.post("/api/sandbox/action")
    async def api_sandbox_action(body: SandboxActionReq) -> JSONResponse:
        try:
            job = await anyio.to_thread.run_sync(
                lambda: svc.start_sandbox_action(
                    body.action, registry_prefix=body.registry_prefix
                )
            )
            return JSONResponse({"ok": True, "job": job})
        except ConflictError as exc:
            return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/api/sandbox/dbeaver")
    async def api_sandbox_dbeaver() -> Dict[str, Any]:
        items = await anyio.to_thread.run_sync(svc.get_dbeaver_connections)
        return {"ok": True, "connections": items}

    @app.get("/api/sandbox/prompts")
    async def api_sandbox_prompts() -> Dict[str, Any]:
        prompts = await anyio.to_thread.run_sync(svc.get_demo_prompts)
        return {"ok": True, "prompts": prompts}

    # =========================================================================
    # 激活码签发中心
    # =========================================================================

    @app.get("/api/license/info")
    async def api_license_info() -> Dict[str, Any]:
        info = await anyio.to_thread.run_sync(svc.get_license_info)
        return {"ok": True, **info}

    @app.post("/api/license/decode-request")
    async def api_decode_request(body: DecodeReqCodeReq) -> JSONResponse:
        try:
            decoded = await anyio.to_thread.run_sync(
                svc.decode_request_code, body.request_code
            )
            return JSONResponse({"ok": True, "device": decoded})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.post("/api/license/issue")
    async def api_issue_license(body: IssueLicenseReq) -> JSONResponse:
        try:
            record = await anyio.to_thread.run_sync(
                lambda: svc.issue_license(
                    mode=body.mode,
                    days=body.days,
                    until_date=body.until_date,
                    request_code=body.request_code,
                    machine_code=body.machine_code,
                    ip_addr=body.ip_addr,
                    note=body.note,
                )
            )
            return JSONResponse({"ok": True, "record": record})
        except (ValueError, FileNotFoundError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    @app.post("/api/license/apply")
    async def api_apply_license(body: ApplyLicenseReq) -> JSONResponse:
        try:
            res = await anyio.to_thread.run_sync(
                lambda: svc.apply_license_to_target(
                    body.activation_code, target=body.target
                )
            )
            return JSONResponse({"ok": True, **res})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    @app.post("/api/license/build-keygen")
    async def api_build_keygen() -> JSONResponse:
        try:
            job = await anyio.to_thread.run_sync(svc.start_build_keygen_job)
            return JSONResponse({"ok": True, "job": job})
        except ConflictError as exc:
            return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
        except (ValueError, FileNotFoundError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/api/license/history")
    async def api_license_history() -> Dict[str, Any]:
        items = await anyio.to_thread.run_sync(svc.list_history)
        return {"ok": True, "items": items}

    @app.delete("/api/license/history/{item_id}")
    async def api_delete_license_history(item_id: str) -> JSONResponse:
        deleted = await anyio.to_thread.run_sync(svc.delete_history_item, item_id)
        if not deleted:
            return JSONResponse(status_code=404, content={"ok": False, "error": "记录不存在"})
        return JSONResponse({"ok": True})

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    async def route_favicon() -> Response:
        return Response(content=_CONSOLE_FAVICON_SVG, media_type="image/svg+xml")

    if os.path.isdir(STATIC_DIR):
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:
        raise HTTPException(status_code=500, detail="管理员总控台前端静态目录缺失")

    return app
