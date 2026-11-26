"""平台全局企业数据标准字典中枢子路由."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error as _err
from core.platform.schemas import StandardDictItemPayload

router = APIRouter()

# =========================================================================
# 全局数据标准字典中枢 (StandardCatalog)
# =========================================================================


@router.get("/standards")
def api_list_standards(
    keyword: str = Query(default=""),
    reg_category: str = Query(default=""),
    data_category: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    include_summary: bool = Query(default=True),
    ctx: PlatformContext = Depends(get_platform_context),
):
    page_data = ctx.standard_catalog.search_standards(
        keyword=keyword,
        reg_category=reg_category,
        data_category=data_category,
        page=page,
        page_size=page_size,
    )
    resp = {
        "ok": True,
        **page_data,
    }
    if include_summary:
        resp["summary"] = ctx.standard_catalog.get_summary()
        resp["format_profiles"] = ctx.standard_catalog.list_format_profiles()
    return resp


@router.get("/standards/lookup")
def api_lookup_standards(
    keyword: str = Query(default=""),
    limit: int = Query(default=30, ge=1, le=100),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return {
        "ok": True,
        "items": ctx.standard_catalog.lookup_standards(keyword=keyword, limit=limit),
    }


@router.get("/standards/template.xlsx")
def api_download_standard_dict_template(
    ctx: PlatformContext = Depends(get_platform_context),
):
    content = ctx.standard_catalog.generate_dictionary_template_bytes()
    fn = quote("DataOG_数据标准字典导入模板.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=standard_dict_template.xlsx; filename*=UTF-8''{fn}"
        },
    )


@router.get("/standards/export.xlsx")
def api_export_standard_dict(
    keyword: str = Query(default=""),
    reg_category: str = Query(default=""),
    data_category: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    content = ctx.standard_catalog.export_dictionary_excel_bytes(
        keyword=keyword,
        reg_category=reg_category,
        data_category=data_category,
    )
    fn = quote("DataOG_企业数据标准字典导出.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=standard_dict_export.xlsx; filename*=UTF-8''{fn}"
        },
    )


@router.post("/standards/upload")
async def api_upload_standard_dict(
    file: UploadFile = File(...),
    mode: str = Form(default="upsert"),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        ctx.import_progress.start(progress_id, "解析数据标准字典")
        reporter = ctx.import_progress.make_reporter(progress_id)
        res = await run_in_threadpool(
            ctx.standard_catalog.import_dictionary_excel,
            file.filename or "standard_dict.xlsx",
            raw,
            mode=mode,
            progress_cb=reporter,
        )
        msg = f"成功导入 {res['imported_count']} 条数据标准（{'全量覆盖' if res['mode'] == 'replace' else '增量合并'}）"
        ctx.import_progress.finish(
            progress_id,
            detail=msg,
            processed=int(res["imported_count"]),
            total=int(res["imported_count"]),
        )
        return {
            "ok": True,
            **res,
            "message": msg,
        }
    except Exception as exc:
        ctx.import_progress.fail(progress_id, str(exc))
        return _err(str(exc), 400)


@router.post("/standards/items")
def api_upsert_standard_item(
    payload: StandardDictItemPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.standard_catalog.upsert_item(payload.model_dump())
        return {
            "ok": True,
            "item": saved,
            "summary": ctx.standard_catalog.get_summary(),
            "message": f"已保存标准 [{saved['std_code']}]",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/standards/items/{std_code}")
def api_update_standard_item(
    std_code: str,
    payload: StandardDictItemPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.standard_catalog.update_item(std_code, payload.model_dump())
        return {
            "ok": True,
            "item": saved,
            "summary": ctx.standard_catalog.get_summary(),
            "message": f"已更新标准 [{saved['std_code']}]",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/standards/items/{std_code}")
def api_delete_standard_item(
    std_code: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    ok = ctx.standard_catalog.delete_item(std_code)
    if not ok:
        return _err(f"未找到数据项编号 [{std_code}]", 404)
    return {
        "ok": True,
        "summary": ctx.standard_catalog.get_summary(),
        "message": f"已删除标准 [{std_code}]",
    }


@router.post("/standards/clear")
def api_clear_standard_dict(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.standard_catalog.clear_dictionary()
    return {
        "ok": True,
        "summary": ctx.standard_catalog.get_summary(),
        "message": "已清空标准字典",
    }


