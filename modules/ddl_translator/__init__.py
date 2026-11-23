"""跨方言建表 DDL 翻译与四层视图血缘穿透器模块."""

from core.platform.module_registry import ModuleSpec
from modules.ddl_translator.router import router
from modules.ddl_translator.service import DdlTranslatorService

MODULE_SPEC = ModuleSpec(
    module_id="ddl_translator",
    title="跨方言 DDL 与视图穿透",
    subtitle="多方言建表 DDL 互译与上游视图四层血缘自动穿透",
    api_prefix="/api/modules/ddl_translator",
    router=router,
    desktop_app_id="polyglot",
    order=30,
)

__all__ = ["DdlTranslatorService", "MODULE_SPEC", "router"]
