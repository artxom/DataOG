"""多邻国模块：智能数仓词根命名 + 跨方言建表 DDL 翻译与四层穿透器 API 路由 (/api/modules/ddl_translator)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import run_tracked_task
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
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="智能标准命名",
        start_detail="正在准备词根解析与标准英文命名转换...",
        fn=DdlTranslatorService(ctx).smart_naming,
        args=(payload.model_dump(),),
        finish_stage="命名完成",
        detail_fn=lambda r: (
            f"已完成 {len(r.get('items') or [])} 项标准命名 ({r.get('engine_label', '')})"
        ),
        count_fn=lambda r: (len(r.get("items") or []), len(r.get("items") or [])),
    )


@router.post("/translate")
async def api_ddl_translate(
    payload: TranslateDdlPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="跨方言 DDL 翻译",
        start_detail=f"正在准备转换至目标方言 {payload.target_dialect}...",
        fn=DdlTranslatorService(ctx).translate,
        args=(payload.model_dump(),),
        finish_stage="方言翻译完成",
        detail_fn=lambda r: (
            f"已完成 {r.get('source_dialect', '')} → {r.get('target_dialect', '')} ({r.get('engine_mode', 'AST')})"
        ),
        count_fn=lambda r: (
            int(r.get("column_count") or 0),
            int(r.get("column_count") or 0),
        ),
    )
