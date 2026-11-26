"""Data.O.G 平台与独立业务模块共享 HTTP / 任务追踪 / 文件下载 SDK (core/platform/http_utils.py).

为所有平台路由与 `modules/*` 插件路由提供统一、极薄的 Web 胶水原语，消除各模块路由层的重复样板代码：
- `json_error`: 统一 JSON 错误响应格式 `{"ok": False, "error": ...}`
- `extract_task_id`: 统一提取并校验 Query / `X-Task-Id` 任务追踪标识
- `file_download_response`: 统一 RFC 5987 UTF-8 文件下载响应头与 `nosniff` 防护
- `run_tracked_task`: 统一包装支持 `task_id` 实时进度与取消生命周期的线程池异步端点
- `run_tracked_upload`: 统一包装支持 `progress_id` 导入进度追踪的文件上传异步端点
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext
from core.platform.diagnostics_hub import record_instant_metric, trace_span
from core.platform.import_progress import ImportProgressHub

MEDIA_TYPE_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
MEDIA_TYPE_HTML = "text/html; charset=utf-8"
MEDIA_TYPE_JSON = "application/json; charset=utf-8"


def json_error(message: str, status_code: int = 400) -> JSONResponse:
    """返回标准平台错误响应体."""
    return JSONResponse({"ok": False, "error": str(message)}, status_code=status_code)


def extract_task_id(request: Request, query_task_id: str = "") -> str:
    """从 Query 参数或 X-Task-Id 请求头提取合法任务追踪 ID."""
    candidate = (query_task_id or request.headers.get("x-task-id", "") or "").strip()
    return candidate if ImportProgressHub.is_valid_id(candidate) else ""


def file_download_response(
    content: bytes,
    filename: str,
    *,
    media_type: str = MEDIA_TYPE_XLSX,
    ascii_fallback: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Response:
    """构造带 UTF-8 安全文件名编码与 nosniff 防护的附件下载响应."""
    safe_name = (filename or "download.bin").replace("\r", "").replace("\n", "")
    encoded_name = quote(safe_name)
    if ascii_fallback:
        safe_ascii = ascii_fallback.replace("\r", "").replace("\n", "").replace('"', "")
        disp = f"attachment; filename={safe_ascii}; filename*=UTF-8''{encoded_name}"
    else:
        disp = f"attachment; filename*=UTF-8''{encoded_name}"
    headers: Dict[str, str] = {
        "Content-Disposition": disp,
        "X-Content-Type-Options": "nosniff",
    }
    if extra_headers:
        headers.update(extra_headers)
    return Response(content=content, media_type=media_type, headers=headers)


async def run_tracked_task(
    *,
    request: Request,
    task_id: str,
    ctx: PlatformContext,
    title: str,
    start_detail: str,
    fn: Callable[..., Any],
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    default_detail: str = "处理完成",
    finish_stage: str = "处理完成",
    detail_fn: Optional[Callable[[Any], str]] = None,
    count_fn: Optional[Callable[[Any], Tuple[int, int]]] = None,
    warning_fn: Optional[Callable[[Any], str]] = None,
    response_fn: Optional[Callable[[Any], Any]] = None,
    inject_progress_cb: bool = True,
    inject_cancel_check: bool = False,
    error_status: int = 400,
) -> Any:
    """统一包装支持 task_id 实时进度与取消生命周期的模块/平台端点."""
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(tid, title, detail=start_detail)
        call_kwargs = dict(kwargs or {})
        if inject_progress_cb:
            call_kwargs["progress_cb"] = ctx.task_progress.make_reporter(tid)
        if inject_cancel_check:
            call_kwargs["cancel_check"] = (
                (lambda: ctx.task_progress.is_cancelled(tid)) if tid else None
            )
        dispatch_t0 = time.perf_counter()

        def _timed_worker():
            queue_ms = max(0.0, (time.perf_counter() - dispatch_t0) * 1000.0)
            record_instant_metric(
                "threadpool", "queue_wait", queue_ms, {"title": title}, min_span_ms=2.0
            )
            with trace_span("task", title):
                return fn(*args, **call_kwargs)

        res = await run_in_threadpool(_timed_worker)
        if tid and not ctx.task_progress.is_cancelled(tid):
            processed, total = count_fn(res) if count_fn else (0, 0)
            if detail_fn is not None:
                detail = detail_fn(res)
            elif isinstance(res, dict):
                detail = str(res.get("message") or default_detail)
            else:
                detail = default_detail
            if warning_fn is not None:
                warning = warning_fn(res)
            elif isinstance(res, dict):
                warning = str(res.get("warning") or "")
            else:
                warning = ""
            ctx.task_progress.finish(
                tid,
                stage=finish_stage,
                detail=detail,
                warning=warning,
                processed=processed,
                total=total,
            )
        return response_fn(res) if response_fn is not None else res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return json_error(str(exc), error_status)


async def run_tracked_upload(
    *,
    ctx: PlatformContext,
    progress_id: str,
    title: str,
    fn: Callable[..., Dict[str, Any]],
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    default_detail: str = "导入完成",
    detail_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    count_fn: Optional[Callable[[Dict[str, Any]], Tuple[int, int]]] = None,
    error_status: int = 400,
) -> Any:
    """统一包装支持 progress_id 进度轮询的文件导入异步端点."""
    pid = (progress_id or "").strip()
    try:
        ctx.import_progress.start(pid, title)
        reporter = ctx.import_progress.make_reporter(pid)
        call_kwargs = dict(kwargs or {})
        call_kwargs["progress_cb"] = reporter
        dispatch_t0 = time.perf_counter()

        def _timed_upload_worker():
            queue_ms = max(0.0, (time.perf_counter() - dispatch_t0) * 1000.0)
            record_instant_metric(
                "threadpool", "queue_wait", queue_ms, {"title": title}, min_span_ms=2.0
            )
            with trace_span("task", title):
                return fn(*args, **call_kwargs)

        res = await run_in_threadpool(_timed_upload_worker)
        processed, total = count_fn(res) if count_fn else (0, 0)
        detail = (
            detail_fn(res)
            if detail_fn is not None
            else str(res.get("message") or default_detail)
        )
        ctx.import_progress.finish(
            pid,
            stage="导入完成",
            detail=detail,
            processed=processed,
            total=total,
        )
        return res
    except Exception as exc:
        ctx.import_progress.fail(pid, str(exc))
        return json_error(str(exc), error_status)
