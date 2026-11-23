"""Data.O.G 独立插件模块：AI 只读智能数据探查会话 (modules/data_explorer).

严格遵循插件隔离规范：仅依赖 `core.platform` 公共底座，与其他 `modules/*` 严格零互引。
"""

from core.platform.module_registry import ModuleSpec
from modules.data_explorer.router import router
from modules.data_explorer.service import DataExplorerService

MODULE_SPEC = ModuleSpec(
    module_id="data_explorer",
    title="AI 只读智能数据探查",
    subtitle="硬件级只读沙箱与多轮自动采样画像会话",
    api_prefix="/api/modules/data_explorer",
    router=router,
    desktop_app_id="explorer",
    private_tables=("explorer_sessions", "explorer_messages"),
    order=40,
)

__all__ = ["DataExplorerService", "MODULE_SPEC", "router"]
