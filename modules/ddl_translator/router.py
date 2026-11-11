"""多邻国模块：智能数仓词根命名 + 跨方言建表 DDL 翻译与四层穿透器 API 路由 (/api/modules/ddl_translator)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
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
def api_smart_naming(
    payload: SmartNamingPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        svc = DdlTranslatorService(ctx)
        return svc.smart_naming(payload.model_dump())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/translate")
def api_ddl_translate(
    payload: TranslateDdlPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        svc = DdlTranslatorService(ctx)
        return svc.translate(payload.model_dump())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
