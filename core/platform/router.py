"""DataOG 平台底座公共 API 路由聚合入口 (/api/platform).

所有具体端点按领域拆分为 `core/platform/routes/*` 八个高内聚子路由模块。
"""

from __future__ import annotations

from fastapi import APIRouter

from core.platform.http_utils import extract_task_id
from core.platform.routes.appearance_and_llm import router as appearance_and_llm_router
from core.platform.routes.auth_and_system import router as auth_and_system_router
from core.platform.routes.data_sources import router as data_sources_router
from core.platform.routes.diagnostics import router as diagnostics_router
from core.platform.routes.knowledge import router as knowledge_router
from core.platform.routes.standards import router as standards_router
from core.platform.routes.updates import router as updates_router
from core.platform.routes.workspaces import router as workspaces_router

router = APIRouter(prefix="/api/platform", tags=["platform"])
router.include_router(auth_and_system_router)
router.include_router(data_sources_router)
router.include_router(appearance_and_llm_router)
router.include_router(knowledge_router)
router.include_router(standards_router)
router.include_router(workspaces_router)
router.include_router(diagnostics_router)
router.include_router(updates_router)

__all__ = ["extract_task_id", "router"]
