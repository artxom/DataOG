"""平台全链路性能诊断、全栈基准压测与脱敏诊断日志包导出子路由."""

from __future__ import annotations

import datetime
import json
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import (
    MEDIA_TYPE_JSON,
    file_download_response,
    json_error as _err,
)
from core.platform.schemas import (
    DiagnosticsBenchmarkPayload,
    DiagnosticsConfigPayload,
    DiagnosticsExportPayload,
)

router = APIRouter()


@router.get("/diagnostics/ping")
async def api_diagnostics_ping(ctx: PlatformContext = Depends(get_platform_context)):
    _ = ctx
    return JSONResponse(
        content={"ok": True, "ts_ms": round(time.time() * 1000.0, 2)},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/diagnostics/overview")
async def api_diagnostics_overview(ctx: PlatformContext = Depends(get_platform_context)):
    overview = await run_in_threadpool(ctx.diagnostics_hub.get_overview)
    return {"ok": True, **overview}


@router.get("/diagnostics/traces/{trace_id}")
def api_diagnostics_trace_detail(
    trace_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    trace = ctx.diagnostics_hub.get_trace(trace_id)
    if trace is None:
        return _err("未找到对应的请求 Trace 记录", 404)
    return {"ok": True, "trace": trace}


@router.post("/diagnostics/benchmark")
async def api_diagnostics_run_benchmark(
    payload: DiagnosticsBenchmarkPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        bench = await ctx.diagnostics_hub.run_benchmark_async(
            include_connections=payload.include_connections,
            include_llm=payload.include_llm,
        )
        overview = await run_in_threadpool(ctx.diagnostics_hub.get_overview)
        return {
            "ok": True,
            "benchmark": bench,
            "auto_findings": overview["auto_findings"],
            "environment": overview["environment"],
            "sqlite_stats": overview["sqlite_stats"],
            "pool_stats": overview["pool_stats"],
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/diagnostics/export")
async def api_diagnostics_export(
    payload: DiagnosticsExportPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        bundle = await run_in_threadpool(
            lambda: ctx.diagnostics_hub.build_export_bundle(
                client_snapshot=payload.client_snapshot,
                include_service_log=payload.include_service_log,
                include_execution_logs=payload.include_execution_logs,
            )
        )
        raw_bytes = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"DataOG_Diagnostics_{stamp}.json"
        return file_download_response(
            raw_bytes,
            filename,
            media_type=MEDIA_TYPE_JSON,
            ascii_fallback="DataOG_Diagnostics.json",
            extra_headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/diagnostics/clear")
def api_diagnostics_clear(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.diagnostics_hub.clear()
    return {"ok": True, "message": "内存性能采样记录已清空"}


@router.put("/diagnostics/config")
def api_diagnostics_update_config(
    payload: DiagnosticsConfigPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    val = ctx.diagnostics_hub.update_config(slow_threshold_ms=payload.slow_threshold_ms)
    return {"ok": True, "slow_threshold_ms": val}
