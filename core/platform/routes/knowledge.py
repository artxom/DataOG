"""平台级离线轻量化知识库子路由 (/api/platform/knowledge/*)."""

from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import (
    file_download_response,
    json_error as _err,
    run_tracked_task,
    run_tracked_upload,
)
from core.platform.schemas import (
    KnowledgeDocUpdatePayload,
    KnowledgeEmbeddingConfigPayload,
    KnowledgeSearchPayload,
    KnowledgeTextDocPayload,
)

router = APIRouter()


@router.get("/knowledge/summary")
def api_kb_summary(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "summary": ctx.knowledge_hub.get_summary(),
        "embedding_config": ctx.knowledge_hub.get_embedding_config(),
    }


@router.get("/knowledge/documents")
def api_kb_list_documents(
    dialect: str = Query(default=""),
    include_builtin: bool = Query(default=True),
    keyword: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return {
        "ok": True,
        "documents": ctx.knowledge_hub.list_documents(
            dialect=dialect,
            include_builtin=include_builtin,
            keyword=keyword,
        ),
        "summary": ctx.knowledge_hub.get_summary(),
    }


@router.get("/knowledge/documents/{doc_id}")
def api_kb_get_document(
    doc_id: str,
    include_chunks: bool = Query(default=True),
    ctx: PlatformContext = Depends(get_platform_context),
):
    doc = ctx.knowledge_hub.get_document(doc_id, include_chunks=include_chunks)
    if doc is None:
        return _err("未找到指定的知识文档", 404)
    return {"ok": True, "document": doc}


@router.post("/knowledge/documents/upload")
async def api_kb_upload_document(
    file: UploadFile = File(...),
    dialect: str = Form(default="GENERIC"),
    title: str = Form(default=""),
    tags: str = Form(default=""),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    raw_bytes = await file.read()
    parsed_tags: List[str] = []
    if tags.strip():
        try:
            maybe = json.loads(tags)
            if isinstance(maybe, list):
                parsed_tags = [str(x).strip() for x in maybe if str(x).strip()]
            else:
                parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]
        except Exception:
            parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

    return await run_tracked_upload(
        ctx=ctx,
        progress_id=progress_id,
        title="导入知识库参考资料",
        fn=ctx.knowledge_hub.ingest_uploaded_file,
        kwargs={
            "filename": file.filename or "reference.md",
            "content": raw_bytes,
            "dialect": dialect,
            "title": title,
            "tags": parsed_tags,
        },
        count_fn=lambda r: (
            int((r.get("document") or {}).get("chunk_count") or 0),
            int((r.get("document") or {}).get("chunk_count") or 0),
        ),
    )


@router.post("/knowledge/documents/text")
def api_kb_create_text_document(
    payload: KnowledgeTextDocPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        res = ctx.knowledge_hub.ingest_text_document(
            title=payload.title,
            content=payload.content,
            dialect=payload.dialect,
            tags=payload.tags,
        )
        return {**res, "summary": ctx.knowledge_hub.get_summary()}
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/knowledge/documents/{doc_id}")
def api_kb_update_document(
    doc_id: str,
    payload: KnowledgeDocUpdatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        updated = ctx.knowledge_hub.update_document(
            doc_id,
            title=payload.title,
            dialect=payload.dialect,
            tags=payload.tags,
            enabled=payload.enabled,
        )
        if updated is None:
            return _err("未找到指定的知识文档", 404)
        return {
            "ok": True,
            "document": updated,
            "summary": ctx.knowledge_hub.get_summary(),
            "message": "知识文档已更新",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/knowledge/documents/{doc_id}")
def api_kb_delete_document(
    doc_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        ok = ctx.knowledge_hub.delete_document(doc_id)
        if not ok:
            return _err("未找到要删除的知识文档", 404)
        return {
            "ok": True,
            "summary": ctx.knowledge_hub.get_summary(),
            "message": "知识文档已删除",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/knowledge/search")
def api_kb_search(
    payload: KnowledgeSearchPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.knowledge_hub.search(
            payload.query,
            dialect=payload.dialect,
            top_k=payload.top_k,
            include_builtin=payload.include_builtin,
            use_embeddings=payload.use_embeddings,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/knowledge/chunks/{chunk_id}")
def api_kb_get_chunk_section(
    chunk_id: str,
    window: int = Query(default=1, ge=0, le=4),
    ctx: PlatformContext = Depends(get_platform_context),
):
    res = ctx.knowledge_hub.read_section(chunk_id, window=window)
    if res is None:
        return _err("未找到指定的知识库章节片段", 404)
    return res


@router.get("/knowledge/export")
def api_kb_export_pack(
    dialect: str = Query(default=""),
    doc_ids: Optional[str] = Query(default=None),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        id_list = [x.strip() for x in (doc_ids or "").split(",") if x.strip()]
        gz_bytes, filename = ctx.knowledge_hub.export_pack_bytes(
            dialect=dialect,
            doc_ids=id_list or None,
        )
        return file_download_response(
            gz_bytes,
            filename,
            media_type="application/gzip",
            ascii_fallback="DataOG_Knowledge_Pack.json.gz",
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/knowledge/import")
async def api_kb_import_pack(
    file: UploadFile = File(...),
    overwrite_existing: bool = Form(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw_bytes = await file.read()
        res = ctx.knowledge_hub.import_pack_bytes(
            raw_bytes,
            overwrite_existing=overwrite_existing,
        )
        return {**res, "summary": ctx.knowledge_hub.get_summary()}
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/knowledge/embedding-config")
def api_kb_get_embedding_config(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "embedding_config": ctx.knowledge_hub.get_embedding_config(),
    }


@router.put("/knowledge/embedding-config")
def api_kb_save_embedding_config(
    payload: KnowledgeEmbeddingConfigPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        cfg = ctx.knowledge_hub.save_embedding_config(payload.model_dump())
        return {
            "ok": True,
            "embedding_config": cfg,
            "summary": ctx.knowledge_hub.get_summary(),
            "message": "Embedding 检索配置已保存",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/knowledge/embeddings/reindex")
async def api_kb_reindex_embeddings(
    request: Request,
    doc_id: str = Query(default=""),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="重建知识库向量索引",
        start_detail="正在准备读取知识切片并计算语义向量...",
        fn=ctx.knowledge_hub.reindex_embeddings,
        kwargs={"doc_id": doc_id},
        finish_stage="重建完成",
        count_fn=lambda r: (
            int(r.get("embedded_chunks") or 0),
            int(r.get("embedded_chunks") or 0),
        ),
    )
