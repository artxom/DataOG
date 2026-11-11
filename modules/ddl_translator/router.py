"""跨方言建表 DDL 翻译与视图四层穿透器 API 路由 (/api/modules/ddl_translator)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from modules.ddl_translator.schemas import TranslateDdlPayload
from modules.ddl_translator.service import DdlTranslatorService

router = APIRouter(prefix="/api/modules/ddl_translator", tags=["ddl_translator"])


@router.get("/options")
def api_ddl_translator_options(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "connections": ctx.connection_hub.list_profiles(),
        "standard_tables": ctx.store.list_cross_tables(),
        "target_dialects": [
            {"value": "DWS", "label": "华为云 GaussDB(DWS)"},
            {"value": "PGSQL", "label": "PostgreSQL / openGauss"},
            {"value": "INCEPTOR", "label": "星环 Transwarp Inceptor"},
            {"value": "MYSQL", "label": "MySQL / OceanBase / Doris / StarRocks"},
            {"value": "DM8", "label": "达梦数据库 DM8"},
            {"value": "ORACLE", "label": "Oracle Database"},
            {"value": "SQLITE", "label": "SQLite 3"},
        ],
        "storage_modes": [
            {"value": "AUTO", "label": "智能推荐默认存储格式 (AUTO)"},
            {"value": "DWS_ROW", "label": "华为 DWS 行存表 (orientation=row)"},
            {"value": "DWS_COLUMN", "label": "华为 DWS 列存压缩表 (orientation=column)"},
            {"value": "INCEPTOR_ORC_TXN", "label": "星环 Inceptor ORC 事务表 (CRUD)"},
            {"value": "INCEPTOR_ORC", "label": "星环 Inceptor 标准 ORC 分析表"},
            {"value": "INCEPTOR_HOLODESK", "label": "星环 Inceptor Holodesk 内存列存表"},
            {"value": "INCEPTOR_TEXTFILE", "label": "星环 Inceptor TEXTFILE 文本表"},
        ],
    }


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
