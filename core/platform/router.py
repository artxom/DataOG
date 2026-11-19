"""DataOG 平台底座公共 API 路由 (/api/platform)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.import_progress import ImportProgressHub
from core.platform.schemas import (
    AppearanceResetPayload,
    AppearanceUpdatePayload,
    AuthActivatePayload,
    ConnectionPayload,
    ConnectionStandardFormatPayload,
    LLMProviderPayload,
    MetadataRefreshPayload,
    QueryExecutePayload,
    QueryExportPayload,
    ReadOnlyQueryExecutePayload,
    StandardDictItemPayload,
)

router = APIRouter(prefix="/api/platform", tags=["platform"])


def _err(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def extract_task_id(request: Request, query_task_id: str = "") -> str:
    candidate = (query_task_id or request.headers.get("x-task-id", "") or "").strip()
    return candidate if ImportProgressHub.is_valid_id(candidate) else ""


# =========================================================================
# 离线激活码与登录鉴权路由 (/api/platform/auth/*，免前置拦截)
# =========================================================================


@router.get("/auth/status")
def api_auth_status(ctx: PlatformContext = Depends(get_platform_context)):
    status = ctx.license_hub.get_auth_status(force_refresh_ip=True)
    return {
        "ok": True,
        "license": status,
        "appearance": ctx.asset_store.get_public_appearance(),
        **status,
    }


@router.post("/auth/activate")
def api_auth_activate(
    payload: AuthActivatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        status = ctx.license_hub.activate(
            activation_code=payload.activation_code,
            login_mode=payload.login_mode,
        )
        exp_date = status.get("expires_date") or ""
        msg = f"激活成功！当前激活码可用日期至 {exp_date}" if exp_date else "激活成功！"
        resp = JSONResponse(
            content={
                "ok": True,
                "license": status,
                "message": msg,
            }
        )
        resp.set_cookie(
            key="dataog_session",
            value=ctx.license_hub.issue_session_cookie_value(),
            httponly=True,
            samesite="strict",
            path="/",
            max_age=86400,
        )
        return resp
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/auth/lock")
def api_auth_lock(ctx: PlatformContext = Depends(get_platform_context)):
    status = ctx.license_hub.lock()
    resp = JSONResponse(
        content={
            "ok": True,
            "license": status,
            "message": "工作台已安全锁定",
        }
    )
    resp.delete_cookie(key="dataog_session", path="/")
    return resp


@router.get("/bootstrap")
def api_platform_bootstrap(ctx: PlatformContext = Depends(get_platform_context)):
    return {"ok": True, **ctx.build_platform_bootstrap()}


# =========================================================================
# 自定义 JDBC .jar 驱动管理中心
# =========================================================================


@router.get("/drivers")
def api_list_drivers(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "drivers": [d.to_dict() for d in ctx.store.list_jdbc_drivers()],
        "templates": ctx.driver_manager.list_templates(),
        "java_runtime": ctx.driver_manager.get_java_runtime_status(),
    }


@router.post("/drivers/upload")
async def api_upload_driver_jar(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    db_type_hint: str = Form(default="CUSTOM_JDBC"),
    driver_class_hint: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw_bytes = await file.read()
        pkg = ctx.driver_manager.register_uploaded_jar(
            filename=file.filename or "driver.jar",
            content=raw_bytes,
            name=name,
            db_type_hint=db_type_hint,
            driver_class_hint=driver_class_hint,
        )
        return {
            "ok": True,
            "driver": pkg.to_dict(),
            "drivers": [d.to_dict() for d in ctx.store.list_jdbc_drivers()],
            "message": f"成功上传并校验驱动包 [{pkg.name}] (SHA-256: {pkg.sha256[:16]}...)",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/drivers/{driver_id}")
def api_delete_driver_jar(
    driver_id: str, ctx: PlatformContext = Depends(get_platform_context)
):
    ok = ctx.driver_manager.delete_driver(driver_id)
    if not ok:
        return _err("未找到要删除的驱动档案", 404)
    return {
        "ok": True,
        "drivers": [d.to_dict() for d in ctx.store.list_jdbc_drivers()],
        "message": "驱动包已安全删除",
    }


# =========================================================================
# 全局命名数据源连接中心 (ConnectionHub)
# =========================================================================


@router.get("/connections")
def api_list_connections(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "connections": ctx.connection_hub.list_profiles(),
    }


@router.post("/connections")
def api_create_or_save_connection(
    payload: ConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.connection_hub.save_profile(
            payload.model_dump(exclude_unset=True),
            connection_id=payload.connection_id,
        )
        tree = None
        warning = ""
        if payload.auto_refresh_metadata:
            try:
                tree = ctx.metadata_hub.refresh_catalog(saved.connection_id)
            except Exception as refresh_exc:
                warning = f"连接档案已保存，但自动采集元数据失败: {refresh_exc}"
        return {
            "ok": True,
            "connection": saved.to_public_dict(),
            "connections": ctx.connection_hub.list_profiles(),
            "catalog_tree": tree,
            "warning": warning,
            "message": f"数据源 [{saved.name}] 已保存",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/connections/{connection_id}")
def api_update_connection(
    connection_id: str,
    payload: ConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.connection_hub.save_profile(
            payload.model_dump(exclude_unset=True),
            connection_id=connection_id,
        )
        tree = None
        if payload.auto_refresh_metadata:
            tree = ctx.metadata_hub.refresh_catalog(saved.connection_id)
        return {
            "ok": True,
            "connection": saved.to_public_dict(),
            "connections": ctx.connection_hub.list_profiles(),
            "catalog_tree": tree,
            "message": f"数据源 [{saved.name}] 已更新",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/connections/{connection_id}/standard-format")
def api_update_connection_standard_format(
    connection_id: str,
    payload: ConnectionStandardFormatPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.connection_hub.update_standard_format(
            connection_id, payload.standard_format
        )
        return {
            "ok": True,
            "connection": saved.to_public_dict(),
            "connections": ctx.connection_hub.list_profiles(),
            "message": f"数据源 [{saved.name}] 的落标标准格式已保存为 [{saved.standard_format}]",
        }
    except LookupError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/connections/{connection_id}")
def api_delete_connection(
    connection_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    ok = ctx.connection_hub.delete_profile(connection_id)
    if not ok:
        return _err("未找到指定的数据源连接档案", 404)
    return {
        "ok": True,
        "connections": ctx.connection_hub.list_profiles(),
        "message": "数据源连接档案已删除",
    }


@router.post("/connections/test")
def api_test_unsaved_connection(
    payload: ConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        profile = ctx.connection_hub.build_profile_from_payload(
            payload.model_dump(exclude_unset=True),
            connection_id=payload.connection_id,
        )
        res = ctx.connection_hub.test_profile(profile)
        return res
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/connections/{connection_id}/test")
def api_test_saved_connection(
    connection_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.connection_hub.test_connection_by_id(connection_id)
    except Exception as exc:
        return _err(str(exc), 400)


# =========================================================================
# 全局持久化元数据中枢 (MetadataHub)
# =========================================================================


@router.get("/metadata/{connection_id}/tree")
def api_get_metadata_tree(
    connection_id: str,
    auto_refresh: bool = Query(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        tree = ctx.metadata_hub.get_catalog_tree(
            connection_id, auto_refresh=auto_refresh
        )
        return {"ok": True, **tree}
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/metadata/{connection_id}/refresh")
async def api_refresh_metadata_tree(
    connection_id: str,
    request: Request,
    payload: Optional[MetadataRefreshPayload] = None,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        schema_name = payload.schema_name if payload else None
        if tid:
            ctx.task_progress.start(
                tid,
                "刷新数据源目录缓存",
                detail=f"准备扫描 {schema_name or '全部模式'} 表与视图清单...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        tree = await run_in_threadpool(
            ctx.metadata_hub.refresh_catalog,
            connection_id,
            schema_name=schema_name,
            progress_cb=reporter,
        )
        msg = f"成功刷新元数据缓存（共 {len(tree['schemas'])} 个模式、{len(tree['tables'])} 个对象）"
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="目录刷新完成",
                detail=msg,
                processed=len(tree["tables"]),
                total=len(tree["tables"]),
            )
        return {
            "ok": True,
            **tree,
            "message": msg,
        }
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/metadata/{connection_id}/table")
async def api_get_table_detail(
    connection_id: str,
    request: Request,
    table_name: str = Query(...),
    schema_name: str = Query(default=""),
    force_refresh: bool = Query(default=False),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        target_label = f"{schema_name}.{table_name}" if schema_name else table_name
        if tid:
            ctx.task_progress.start(
                tid,
                "加载库表元数据",
                detail=f"正在读取 {target_label} 表结构...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        meta = await run_in_threadpool(
            ctx.metadata_hub.get_table_metadata,
            connection_id,
            schema_name=schema_name,
            table_name=table_name,
            force_refresh=force_refresh,
            penetrate_views=True,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            detail_msg = (
                f"已加载 {meta.schema_name}.{meta.table_name}（共 {len(meta.columns)} 列）"
                if meta.exists
                else f"对象 {meta.schema_name}.{meta.table_name} 不存在"
            )
            ctx.task_progress.finish(
                tid,
                stage="结构加载完成",
                detail=detail_msg,
                processed=len(meta.columns),
                total=len(meta.columns),
            )
        return {"ok": True, "table": meta.to_dict()}
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/metadata/search")
def api_search_metadata(
    keyword: str = Query(default=""),
    connection_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: PlatformContext = Depends(get_platform_context),
):
    hits = ctx.metadata_hub.search_objects(
        keyword=keyword, connection_id=connection_id or None, limit=limit
    )
    return {"ok": True, "hits": hits}


# =========================================================================
# 通用简单查询与表数据预览中心 (QueryEngine)
# =========================================================================


@router.post("/query/execute")
async def api_execute_query(
    payload: QueryExecutePayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "执行 SQL 查询",
                detail=f"正在准备执行 SQL（上限 {payload.limit} 行）...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        result = await run_in_threadpool(
            ctx.query_engine.execute_sql,
            payload.connection_id,
            payload.sql,
            limit=payload.limit,
            timeout_sec=payload.timeout_sec,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="查询完成",
                detail=result.message or f"返回 {result.row_count} 行",
                processed=result.row_count,
                total=result.row_count,
            )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/query/execute-readonly")
def api_execute_readonly_query(
    payload: ReadOnlyQueryExecutePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        result = ctx.query_engine.execute_readonly_sql(
            payload.connection_id,
            payload.sql,
            limit=payload.limit,
            max_limit_cap=200,
            timeout_sec=payload.timeout_sec,
        )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/query/preview")
async def api_preview_table(
    request: Request,
    connection_id: str = Query(...),
    table_name: str = Query(...),
    schema_name: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=2000),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        target_label = f"{schema_name}.{table_name}" if schema_name else table_name
        if tid:
            ctx.task_progress.start(
                tid,
                "预览表数据",
                detail=f"正在采样 {target_label}（最多 {limit} 行）...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        result = await run_in_threadpool(
            ctx.query_engine.preview_table,
            connection_id,
            schema_name=schema_name,
            table_name=table_name,
            limit=limit,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="数据预览完成",
                detail=result.message or f"返回 {result.row_count} 行",
                processed=result.row_count,
                total=result.row_count,
            )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/query/export")
def api_export_query_result(
    payload: QueryExportPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        columns = list(payload.columns)
        rows = list(payload.rows)
        if not columns and payload.connection_id and payload.sql:
            q_res = ctx.query_engine.execute_sql(
                payload.connection_id, payload.sql, limit=payload.limit
            )
            columns = [c.name for c in q_res.columns]
            rows = q_res.rows

        if not columns:
            return _err("没有可导出的查询结果列", 400)

        content, media_type = ctx.query_engine.export_result_bytes(
            columns=columns,
            rows=rows,
            fmt=payload.format,
            title=payload.title,
        )
        ext = "xlsx" if payload.format.strip().lower() in ("xlsx", "excel") else "csv"
        safe_fn = quote(f"{payload.title or 'query_result'}.{ext}")
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={ext}_export.{ext}; filename*=UTF-8''{safe_fn}"
            },
        )
    except Exception as exc:
        return _err(str(exc), 400)


# =========================================================================
# 全局任务与 SQL 日志抽屉接口
# =========================================================================


@router.get("/logs")
def api_platform_logs(
    limit: int = Query(default=150, ge=1, le=1000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return {"ok": True, "logs": ctx.store.list_logs(limit=limit)}


@router.post("/logs/clear")
def api_platform_clear_logs(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.store.clear_logs()
    return {"ok": True, "logs": []}


@router.get("/import-progress/{progress_id}")
async def api_platform_import_progress(
    progress_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    prog = ctx.import_progress.get(progress_id)
    return JSONResponse(
        content={
            "ok": True,
            "found": prog is not None,
            "progress": prog,
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/tasks/{task_id}")
async def api_platform_task_status(
    task_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    prog = ctx.task_progress.get(task_id)
    return JSONResponse(
        content={
            "ok": True,
            "found": prog is not None,
            "task": prog,
            "progress": prog,
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.post("/tasks/{task_id}/cancel")
async def api_platform_task_cancel(
    task_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    cancelled = ctx.task_progress.cancel(task_id)
    return JSONResponse(
        content={"ok": True, "cancelled": cancelled},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/tasks/{task_id}/stream")
async def api_platform_task_stream(
    task_id: str,
    request: Request,
    max_wait_ms: int = Query(default=25000, ge=0, le=120000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    async def _event_generator():
        def _encode_event(snap_obj: Optional[dict]) -> str:
            payload = {
                "ok": True,
                "found": snap_obj is not None,
                "task": snap_obj,
                "progress": snap_obj,
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        initial = ctx.task_progress.get(task_id)
        if max_wait_ms == 0 or (
            initial is not None
            and initial.get("status") in ("completed", "failed", "cancelled")
        ):
            yield _encode_event(initial)
            return

        last_seq = 0
        if initial is not None:
            yield _encode_event(initial)
            last_seq = int(initial.get("seq", 1))

        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        while True:
            if await request.is_disconnected():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            snap = await run_in_threadpool(
                ctx.task_progress.wait_for_change,
                task_id,
                last_seq,
                min(0.35, remaining),
            )
            if snap is None:
                if last_seq > 0:
                    break
                rem_after = deadline - time.monotonic()
                if rem_after <= 0:
                    break
                await asyncio.sleep(min(0.05, rem_after))
                continue

            snap_seq = int(snap.get("seq", 1))
            if snap_seq > last_seq:
                yield _encode_event(snap)
                last_seq = snap_seq
                if snap.get("status") in ("completed", "failed", "cancelled"):
                    break
            elif snap.get("status") in ("completed", "failed", "cancelled"):
                break

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


# =========================================================================
# 外观壁纸与软件/模块图标本地持久化中心 (AssetStore)
# =========================================================================


@router.get("/appearance")
def api_get_appearance(ctx: PlatformContext = Depends(get_platform_context)):
    return {"ok": True, "appearance": ctx.asset_store.get_public_appearance()}


@router.put("/appearance")
def api_update_appearance(
    payload: AppearanceUpdatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        appearance = ctx.asset_store.update_preferences(
            wallpaper_id=payload.wallpaper_id,
            wallpaper_fit=payload.wallpaper_fit,
        )
        return {"ok": True, "appearance": appearance}
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/appearance/upload")
async def api_upload_appearance_asset(
    file: UploadFile = File(...),
    slot: str = Form(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw_bytes = await file.read()
        appearance = ctx.asset_store.save_uploaded_asset(
            slot=slot,
            filename=file.filename or "",
            content=raw_bytes,
        )
        return {
            "ok": True,
            "appearance": appearance,
            "message": "图片已保存至本地",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/appearance/reset")
def api_reset_appearance_slot(
    payload: AppearanceResetPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        appearance = ctx.asset_store.reset_slot(payload.slot)
        return {
            "ok": True,
            "appearance": appearance,
            "message": "已恢复默认外观设置",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/assets/{asset_name}")
def api_serve_local_asset(
    asset_name: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    found = ctx.asset_store.read_asset_bytes(asset_name)
    if found is None:
        return JSONResponse({"ok": False, "error": "资源不存在"}, status_code=404)
    data, mime = found
    ext = asset_name.rsplit(".", 1)[-1] if "." in asset_name else "bin"
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="asset.{ext}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "private, max-age=86400",
        },
    )


# =========================================================================
# 全局 AI 大模型统一接入中心 (LLMHub — OpenAI 兼容协议)
# =========================================================================


@router.get("/llm/providers")
def api_list_llm_providers(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "providers": ctx.llm_hub.list_providers(),
        "available_models": ctx.llm_hub.list_available_models(),
    }


@router.post("/llm/providers")
def api_save_llm_provider(
    payload: LLMProviderPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.llm_hub.save_provider(
            payload.model_dump(exclude_unset=True),
            provider_id=payload.provider_id,
        )
        return {
            "ok": True,
            "provider": saved.to_public_dict(),
            "providers": ctx.llm_hub.list_providers(),
            "available_models": ctx.llm_hub.list_available_models(),
            "message": f"已保存 {saved.name} 配置",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/llm/providers/{provider_id}")
def api_update_llm_provider(
    provider_id: str,
    payload: LLMProviderPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.llm_hub.save_provider(
            payload.model_dump(exclude_unset=True),
            provider_id=provider_id,
        )
        return {
            "ok": True,
            "provider": saved.to_public_dict(),
            "providers": ctx.llm_hub.list_providers(),
            "available_models": ctx.llm_hub.list_available_models(),
            "message": f"已保存 {saved.name} 配置",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/llm/providers/{provider_id}")
def api_delete_llm_provider(
    provider_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        ok = ctx.llm_hub.delete_provider(provider_id)
        if not ok:
            return _err("未找到指定的 AI 服务商档案", 404)
        return {
            "ok": True,
            "providers": ctx.llm_hub.list_providers(),
            "available_models": ctx.llm_hub.list_available_models(),
            "message": "服务商配置已删除",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/llm/test")
def api_test_llm_provider(
    payload: LLMProviderPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.llm_hub.test_provider(payload.model_dump(exclude_unset=True))
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/llm/models")
def api_fetch_llm_models(
    payload: LLMProviderPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.llm_hub.fetch_models(payload.model_dump(exclude_unset=True))
    except Exception as exc:
        return _err(str(exc), 400)


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
    ctx: PlatformContext = Depends(get_platform_context),
):
    page_data = ctx.standard_catalog.search_standards(
        keyword=keyword,
        reg_category=reg_category,
        data_category=data_category,
        page=page,
        page_size=page_size,
    )
    return {
        "ok": True,
        **page_data,
        "summary": ctx.standard_catalog.get_summary(),
        "format_profiles": ctx.standard_catalog.list_format_profiles(),
    }


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

