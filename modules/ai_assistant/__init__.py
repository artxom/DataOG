"""Data.O.G v3.0 智能数据助手后端独立插件模块 (modules/ai_assistant)."""

from __future__ import annotations

from core.platform.module_registry import ModuleSpec
from modules.ai_assistant.router import router
from modules.ai_assistant.service import AiAssistantService


def _init_module(ctx) -> None:
    AiAssistantService.ensure_schema(ctx.store)


MODULE_SPEC = ModuleSpec(
    module_id="ai_assistant",
    title="Data.O.G",
    subtitle="基于大模型的智能数据助手",
    api_prefix="/api/modules/ai_assistant",
    router=router,
    desktop_app_id="ai_assistant",
    private_tables=(
        "ai_assistant_sessions",
        "ai_assistant_messages",
        "ai_assistant_proposals",
        "ai_assistant_attachments",
        "ai_assistant_attachment_chunks",
        "ai_assistant_runs",
        "ai_assistant_run_events",
    ),
    init_hook=_init_module,
    order=50,
)
