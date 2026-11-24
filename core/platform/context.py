"""DataOG 微内核依赖注入上下文 (PlatformContext)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from core.platform.asset_store import AssetStore
from core.platform.connection_hub import ConnectionHub
from core.platform.diagnostics_hub import DiagnosticsHub, trace_span
from core.platform.driver_manager import DriverManager
from core.platform.import_progress import ImportProgressHub
from core.platform.knowledge_hub import KnowledgeHub
from core.platform.license_hub import LicenseHub
from core.platform.llm_hub import LLMHub
from core.platform.metadata_hub import MetadataHub
from core.platform.module_registry import ModuleRegistry
from core.platform.query_engine import QueryEngine
from core.platform.standard_catalog import StandardCatalog
from core.platform.storage import PlatformStateStore
from core.platform.storage_paths import resolve_private_storage_layout
from core.platform.workspace_hub import WorkspaceManager


class PlatformContext:
    """向全平台路由与独立业务模块统一注入公共底座能力."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        drivers_dir: Optional[str] = None,
        assets_dir: Optional[str] = None,
        verifier_pubkey: Optional[bytes] = None,
        *,
        private_root: Optional[str] = None,
        workspace_db_path: Optional[str] = None,
        legacy_data_dir: Optional[str] = None,
    ) -> None:
        if db_path is None:
            layout = resolve_private_storage_layout(
                private_root_override=private_root,
                create_dirs=True,
            )
            self.data_dir = layout.private_root
            eff_drivers_dir = drivers_dir or layout.drivers_dir
            eff_assets_dir = assets_dir or layout.assets_dir
            eff_workspaces_dir = layout.workspaces_dir
            eff_tmp_dir = layout.tmp_dir
            eff_clock_anchor = layout.clock_anchor_path
            eff_ws_db = (
                os.path.abspath(workspace_db_path)
                if workspace_db_path
                else layout.default_workspace_path
            )
            os.makedirs(eff_drivers_dir, exist_ok=True)
            os.makedirs(eff_assets_dir, exist_ok=True)
            os.makedirs(eff_workspaces_dir, exist_ok=True)
            os.makedirs(eff_tmp_dir, exist_ok=True)

            self.store = PlatformStateStore(
                layout.private_db_path,
                workspace_db_path=eff_ws_db,
                clock_anchor_path=eff_clock_anchor,
            )
            # 生产启动或显式指定 legacy_data_dir 时，自动无损迁移旧版 <exe_dir>/data/
            eff_legacy_dir = (
                legacy_data_dir
                if legacy_data_dir is not None
                else (None if layout.is_env_overridden else layout.legacy_app_data_dir)
            )
            if eff_legacy_dir:
                self.store.migrate_legacy_install_dir(
                    eff_legacy_dir,
                    assets_dir=eff_assets_dir,
                    drivers_dir=eff_drivers_dir,
                    clock_anchor_path=eff_clock_anchor,
                )
        else:
            abs_db = os.path.abspath(db_path)
            self.data_dir = os.path.dirname(abs_db)
            os.makedirs(self.data_dir, exist_ok=True)
            eff_drivers_dir = drivers_dir or os.path.join(self.data_dir, "drivers")
            eff_assets_dir = assets_dir or os.path.join(self.data_dir, "assets")
            eff_workspaces_dir = os.path.join(self.data_dir, "workspaces")
            eff_tmp_dir = os.path.join(self.data_dir, "tmp")
            eff_clock_anchor = os.path.join(self.data_dir, ".chk", "m_anchor.bin")
            os.makedirs(eff_drivers_dir, exist_ok=True)
            os.makedirs(eff_assets_dir, exist_ok=True)
            os.makedirs(eff_workspaces_dir, exist_ok=True)
            os.makedirs(eff_tmp_dir, exist_ok=True)

            self.store = PlatformStateStore(
                abs_db,
                workspace_db_path=workspace_db_path,
                clock_anchor_path=eff_clock_anchor,
            )

        self.license_hub = LicenseHub(
            self.store,
            verifier_pubkey=verifier_pubkey,
            anchor_file=eff_clock_anchor,
        )
        self.driver_manager = DriverManager(self.store, drivers_dir=eff_drivers_dir)
        self.connection_hub = ConnectionHub(self.store, self.driver_manager)
        self.metadata_hub = MetadataHub(self.store, self.connection_hub)
        self.query_engine = QueryEngine(self.store, self.connection_hub)
        self.asset_store = AssetStore(self.store, assets_dir=eff_assets_dir)
        self.llm_hub = LLMHub(self.store)
        self.knowledge_hub = KnowledgeHub(self.store, self.llm_hub)
        self.standard_catalog = StandardCatalog(self.store)
        self.workspace_hub = WorkspaceManager(
            self.store,
            self.connection_hub,
            self.metadata_hub,
            workspaces_dir=eff_workspaces_dir,
            tmp_dir=eff_tmp_dir,
        )
        self.import_progress = ImportProgressHub()
        self.task_progress = self.import_progress
        self.module_registry = ModuleRegistry()
        self._registered_modules: List[Dict[str, str]] = []
        self.app_version = "3.2.4"
        self.diagnostics_hub = DiagnosticsHub(self)

    def register_module_manifest(
        self,
        *,
        module_id: str,
        title: str,
        subtitle: str = "",
        api_prefix: str = "",
        desktop_app_id: str = "",
    ) -> None:
        if any(m["module_id"] == module_id for m in self._registered_modules):
            return
        entry: Dict[str, str] = {
            "module_id": module_id,
            "title": title,
            "subtitle": subtitle,
            "api_prefix": api_prefix,
        }
        if desktop_app_id:
            entry["desktop_app_id"] = desktop_app_id
        self._registered_modules.append(entry)

    def build_platform_bootstrap(self) -> Dict[str, Any]:
        with trace_span("bootstrap", "connections"):
            connections = self.connection_hub.list_profiles()
        trees_summary: Dict[str, Any] = {}
        with trace_span("bootstrap", "catalog_trees", {"connection_count": len(connections)}):
            for c in connections:
                cid = c["connection_id"]
                cat = self.metadata_hub.get_catalog_tree(cid, auto_refresh=False)
                trees_summary[cid] = {
                    "schema_count": len(cat["schemas"]),
                    "table_count": len(cat["tables"]),
                    "schemas": cat["schemas"],
                    "tables": cat["tables"],
                }
        with trace_span("bootstrap", "license"):
            license_info = self.license_hub.get_auth_status()
        with trace_span("bootstrap", "workspaces"):
            active_ws = self.workspace_hub.get_active_workspace_summary()
            workspaces = self.workspace_hub.list_workspaces()
        with trace_span("bootstrap", "appearance"):
            appearance = self.asset_store.get_public_appearance()
        with trace_span("bootstrap", "drivers"):
            drivers = [d.to_dict() for d in self.store.list_jdbc_drivers()]
            driver_templates = self.driver_manager.list_templates()
        with trace_span("bootstrap", "java_runtime"):
            java_runtime = self.driver_manager.get_java_runtime_status()
        with trace_span("bootstrap", "llm"):
            llm_providers = self.llm_hub.list_providers()
            llm_models = self.llm_hub.list_available_models()
        with trace_span("bootstrap", "knowledge"):
            knowledge_summary = self.knowledge_hub.get_summary()
        with trace_span("bootstrap", "standards"):
            standard_dict_summary = self.standard_catalog.get_summary()
        with trace_span("bootstrap", "logs"):
            logs = self.store.list_logs(limit=150)

        return {
            "app_name": "Data.O.G",
            "brand_name": "Data.O.G",
            "version": "3.2.4",
            "storage_mode": "SEPARATED" if self.store.is_split_storage else "COMPAT",
            "license": license_info,
            "active_workspace": active_ws,
            "workspaces": workspaces,
            "appearance": appearance,
            "connections": connections,
            "catalog_trees": trees_summary,
            "drivers": drivers,
            "driver_templates": driver_templates,
            "java_runtime": java_runtime,
            "llm_providers": llm_providers,
            "llm_models": llm_models,
            "knowledge_summary": knowledge_summary,
            "standard_dict_summary": standard_dict_summary,
            "modules": list(self._registered_modules),
            "logs": logs,
        }

    def close(self) -> None:
        """优雅释放连接池与底层 SQLite 持久连接句柄."""
        try:
            self.connection_hub.close_warm_connections()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass


def get_platform_context(request: Request) -> PlatformContext:
    """FastAPI Depends 依赖注入入口（附带 Gate 2 独立授权能力令牌二次核验）."""
    ctx: Optional[PlatformContext] = getattr(request.app.state, "platform_ctx", None)
    if ctx is None:
        raise RuntimeError("PlatformContext 尚未绑定至 FastAPI 应用实例")
    with trace_span("license", "gate2_capability"):
        ok, auth_info = ctx.license_hub.verify_request_capability(request)
    if not ok:
        raise HTTPException(
            status_code=401,
            detail={
                "ok": False,
                "code": "LICENSE_REQUIRED",
                "license_status": auth_info.get("status", "UNACTIVATED"),
                "error": auth_info.get("status_message") or "系统尚未激活或授权已失效，请先输入有效激活码",
            },
        )
    return ctx
