"""多邻国模块：智能数仓词根命名 + 跨方言建表 DDL 翻译与四层穿透器 API 路由 (/api/modules/ddl_translator)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.router import extract_task_id
from modules.ddl_translator.schemas import SmartNamingPayload, TranslateDdlPayload
from modules.ddl_translator.service import DdlTranslatorService

router = APIRouter(prefix="/api/modules/ddl_translator", tags=["ddl_translator"])


@router.get("/options")
def api_ddl_translator_options(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "connections": ctx.connection_hub.list_profiles(),
        "standard_tables": ctx.store.list_cross_tables(),
        "llm_models": ctx.llm_hub.list_available_models(),
        "target_dialects": [
            {"value": "DWS", "label": "GaussDB(DWS)"},
            {"value": "PGSQL", "label": "PostgreSQL / openGauss"},
            {"value": "INCEPTOR", "label": "Transwarp Inceptor"},
            {"value": "MYSQL", "label": "MySQL / OceanBase / StarRocks"},
            {"value": "DM8", "label": "达梦 DM8"},
            {"value": "ORACLE", "label": "Oracle"},
            {"value": "SQLITE", "label": "SQLite"},
        ],
        "storage_modes": [
            {"value": "AUTO", "label": "AUTO 默认存储"},
            {"value": "DWS_ROW", "label": "DWS 行存 (ROW)"},
            {"value": "DWS_COLUMN", "label": "DWS 列存 (COLUMN)"},
            {"value": "INCEPTOR_ORC_TXN", "label": "Inceptor ORC 事务表"},
            {"value": "INCEPTOR_ORC", "label": "Inceptor 标准 ORC"},
            {"value": "INCEPTOR_HOLODESK", "label": "Inceptor Holodesk"},
            {"value": "INCEPTOR_TEXTFILE", "label": "Inceptor TEXTFILE"},
        ],
    }


@router.post("/naming")
async def api_smart_naming(
    payload: SmartNamingPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "智能标准命名",
                detail="正在准备词根解析与标准英文命名转换...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        svc = DdlTranslatorService(ctx)
        res = await run_in_threadpool(
            svc.smart_naming,
            payload.model_dump(),
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            items_cnt = len(res.get("items") or [])
            ctx.task_progress.finish(
                tid,
                stage="命名完成",
                detail=f"已完成 {items_cnt} 项标准命名 ({res.get('engine_label', '')})",
                warning=str(res.get("warning") or ""),
                processed=items_cnt,
                total=items_cnt,
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/translate")
async def api_ddl_translate(
    payload: TranslateDdlPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "跨方言 DDL 翻译",
                detail=f"正在准备转换至目标方言 {payload.target_dialect}...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        svc = DdlTranslatorService(ctx)
        res = await run_in_threadpool(
            svc.translate,
            payload.model_dump(),
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            col_cnt = int(res.get("column_count") or 0)
            ctx.task_progress.finish(
                tid,
                stage="方言翻译完成",
                detail=f"已完成 {res.get('source_dialect', '')} → {res.get('target_dialect', '')} ({res.get('engine_mode', 'AST')})",
                warning=str(res.get("warning") or ""),
                processed=col_cnt,
                total=col_cnt,
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
