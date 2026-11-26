"""单库批量落标贯标执行台模块."""

from core.platform.module_registry import ModuleSpec
from modules.standard_migrator.changelog_mixin import ensure_migrator_changelog_tables
from modules.standard_migrator.router import router
from modules.standard_migrator.service import StandardMigratorService

MODULE_SPEC = ModuleSpec(
    module_id="standard_migrator",
    title="单库落标贯标台",
    subtitle="物理列序保序重建、Kahn 拓扑改名与多线程批量落标",
    api_prefix="/api/modules/standard_migrator",
    router=router,
    desktop_app_id="standard",
    workspace_tables=(
        "migration_items",
        "migration_change_logs",
        "migration_checkpoints",
    ),
    workspace_meta_keys=(
        "current_excel_name",
        "imported_at",
        "migrator_connection_id",
    ),
    init_hook=lambda ctx: ensure_migrator_changelog_tables(ctx.store),
    order=10,
)

__all__ = ["MODULE_SPEC", "StandardMigratorService", "router"]
