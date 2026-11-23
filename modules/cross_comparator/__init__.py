"""跨环境双列拟物书签比对中心模块."""

from core.platform.module_registry import ModuleSpec
from modules.cross_comparator.router import router
from modules.cross_comparator.service import CrossComparatorService

MODULE_SPEC = ModuleSpec(
    module_id="cross_comparator",
    title="跨环境双列拟物书签比对",
    subtitle="来源库 × 目标库 双列拖拽配对、拟物数标书签与闭环推送",
    api_prefix="/api/modules/cross_comparator",
    router=router,
    desktop_app_id="standard",
    workspace_tables=("cross_table_bindings", "cross_column_bindings"),
    workspace_meta_keys=(
        "cross_standard_filename",
        "cross_standard_imported_at",
        "cross_source_connection_id",
        "cross_target_connection_id",
    ),
    order=20,
)

__all__ = ["CrossComparatorService", "MODULE_SPEC", "router"]
