"""DataOG 微内核依赖注入上下文 (PlatformContext)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import Request

from core.platform.asset_store import AssetStore
from core.platform.connection_hub import ConnectionHub
from core.platform.driver_manager import DriverManager
from core.platform.llm_hub import LLMHub
from core.platform.metadata_hub import MetadataHub
from core.platform.query_engine import QueryEngine
from core.platform.storage import PlatformStateStore


class PlatformContext:
    """向全平台路由与独立业务模块统一注入公共底座能力."""

    def __init__(
        self,
        db_path: str,
        drivers_dir: Optional[str] = None,
        assets_dir: Optional[str] = None,
    ) -> None:
        abs_db = os.path.abspath(db_path)
        self.data_dir = os.path.dirname(abs_db)
        os.makedirs(self.data_dir, exist_ok=True)
        eff_drivers_dir = drivers_dir or os.path.join(self.data_dir, "drivers")
        eff_assets_dir = assets_dir or os.path.join(self.data_dir, "assets")
        os.makedirs(eff_drivers_dir, exist_ok=True)
        os.makedirs(eff_assets_dir, exist_ok=True)

        self.store = PlatformStateStore(abs_db)
        self.driver_manager = DriverManager(self.store, drivers_dir=eff_drivers_dir)
        self.connection_hub = ConnectionHub(self.store, self.driver_manager)
        self.metadata_hub = MetadataHub(self.store, self.connection_hub)
        self.query_engine = QueryEngine(self.store, self.connection_hub)
        self.asset_store = AssetStore(self.store, assets_dir=eff_assets_dir)
        self.llm_hub = LLMHub(self.store)
        self._registered_modules: List[Dict[str, str]] = []

    def register_module_manifest(
        self,
        *,
        module_id: str,
        title: str,
        subtitle: str = "",
        api_prefix: str = "",
    ) -> None:
        if any(m["module_id"] == module_id for m in self._registered_modules):
            return
        self._registered_modules.append(
            {
                "module_id": module_id,
                "title": title,
                "subtitle": subtitle,
                "api_prefix": api_prefix,
            }
        )

    def build_platform_bootstrap(self) -> Dict[str, Any]:
        connections = self.connection_hub.list_profiles()
        trees_summary: Dict[str, Any] = {}
        for c in connections:
            cid = c["connection_id"]
            cat = self.metadata_hub.get_catalog_tree(cid, auto_refresh=False)
            trees_summary[cid] = {
                "schema_count": len(cat["schemas"]),
                "table_count": len(cat["tables"]),
                "schemas": cat["schemas"],
                "tables": cat["tables"],
            }
        return {
            "app_name": "Data.O.G",
            "brand_name": "Data.O.G",
            "version": "2.1.0",
            "appearance": self.asset_store.get_public_appearance(),
            "connections": connections,
            "catalog_trees": trees_summary,
            "drivers": [d.to_dict() for d in self.store.list_jdbc_drivers()],
            "driver_templates": self.driver_manager.list_templates(),
            "java_runtime": self.driver_manager.get_java_runtime_status(),
            "llm_providers": self.llm_hub.list_providers(),
            "llm_models": self.llm_hub.list_available_models(),
            "modules": list(self._registered_modules),
            "logs": self.store.list_logs(limit=150),
        }


def get_platform_context(request: Request) -> PlatformContext:
    """FastAPI Depends 依赖注入入口."""
    ctx: Optional[PlatformContext] = getattr(request.app.state, "platform_ctx", None)
    if ctx is None:
        raise RuntimeError("PlatformContext 尚未绑定至 FastAPI 应用实例")
    return ctx
