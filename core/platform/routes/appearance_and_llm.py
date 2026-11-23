"""平台桌面外观资产与 AI 大模型接入中枢子路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error as _err
from core.platform.schemas import (
    AppearanceResetPayload,
    AppearanceUpdatePayload,
    LLMProviderPayload,
)

router = APIRouter()

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


