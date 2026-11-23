"""Data.O.G 后端插件微内核注册中心与自动发现机制 (core/platform/module_registry.py).

设计目标（v2.9.0 / 3.0 模块化基石）：
1. 新增任意业务插件模块时，只需在 `modules/<module_id>/__init__.py` 中声明并导出 `MODULE_SPEC = ModuleSpec(...)`；
2. 无需修改 `app.py`、`asset_store.py` 或 `storage` 主流程即可自动完成：
   - 平台清单注册 (`ctx.register_module_manifest`)
   - 私有表 / 共享工作区表 / 工作区元数据键声明注册
   - 桌面应用自定义图标槽位注册 (`ctx.asset_store.register_app_id`)
   - 可选模块初始化钩子执行 (`spec.init_hook(ctx)`)
   - FastAPI 路由自动挂载 (`app.include_router(spec.router)`)
3. 严格兼容源码运行态与 PyInstaller 冻结态 (`--collect-submodules=modules`).
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import pkgutil
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("dataog")

_SAFE_MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


@dataclass(frozen=True)
class ModuleSpec:
    """独立插件模块的声明式自描述清单."""

    module_id: str
    title: str
    subtitle: str = ""
    api_prefix: str = ""
    router: Any = None
    desktop_app_id: str = ""
    private_tables: Tuple[str, ...] = ()
    workspace_tables: Tuple[str, ...] = ()
    workspace_meta_keys: Tuple[str, ...] = ()
    init_hook: Optional[Callable[[Any], None]] = None
    order: int = 100

    def __post_init__(self) -> None:
        if not _SAFE_MODULE_ID_RE.match(self.module_id or ""):
            raise ValueError(f"非法的模块标识 module_id: {self.module_id!r}")
        if not self.api_prefix:
            object.__setattr__(self, "api_prefix", f"/api/modules/{self.module_id}")

    def to_manifest_dict(self) -> Dict[str, Any]:
        return {
            "module_id": self.module_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "api_prefix": self.api_prefix,
            "desktop_app_id": self.desktop_app_id,
        }


class ModuleRegistry:
    """维护已挂载插件模块的声明式注册表."""

    def __init__(self) -> None:
        self._specs: Dict[str, ModuleSpec] = {}

    def register(self, spec: ModuleSpec) -> None:
        self._specs[spec.module_id] = spec

    def get(self, module_id: str) -> Optional[ModuleSpec]:
        return self._specs.get(module_id)

    def list_specs(self) -> List[ModuleSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.order, s.module_id))

    def list_manifests(self) -> List[Dict[str, Any]]:
        return [s.to_manifest_dict() for s in self.list_specs()]


def _iter_child_package_names(root_pkg: Any, package_name: str) -> List[str]:
    """兼容源码态与 PyInstaller 冻结态枚举直接子插件包名."""
    names: List[str] = []
    seen: set[str] = set()
    pkg_paths = getattr(root_pkg, "__path__", None)

    if pkg_paths:
        for mod_info in pkgutil.iter_modules(pkg_paths):
            if mod_info.ispkg and not mod_info.name.startswith("_") and mod_info.name not in seen:
                seen.add(mod_info.name)
                names.append(mod_info.name)

    # PyInstaller 冻结态兜底：若 iter_modules 未返回，则扫描 PyiFrozenImporter.toc 与 walk_packages
    if not names:
        prefix = f"{package_name}."
        loader = getattr(root_pkg, "__loader__", None)
        toc = getattr(loader, "toc", None)
        is_pkg_fn = getattr(loader, "is_package", None)
        if isinstance(toc, (set, frozenset, list, tuple)):
            for entry in sorted(e for e in toc if isinstance(e, str)):
                if not entry.startswith(prefix):
                    continue
                rest = entry[len(prefix) :]
                if "." in rest:
                    child = rest.split(".", 1)[0]
                else:
                    # 兼容单文件插件包 (modules/<pkg>/__init__.py，toc 中仅含 "modules.<pkg>")
                    is_single_pkg = False
                    if callable(is_pkg_fn):
                        try:
                            is_single_pkg = bool(is_pkg_fn(entry))
                        except Exception:
                            is_single_pkg = False
                    if not is_single_pkg:
                        continue
                    child = rest
                if child and not child.startswith("_") and child not in seen:
                    seen.add(child)
                    names.append(child)

    if not names and pkg_paths:
        prefix = f"{package_name}."
        for mod_info in pkgutil.walk_packages(pkg_paths, prefix):
            rest = mod_info.name[len(prefix) :]
            if "." not in rest and mod_info.ispkg and not rest.startswith("_") and rest not in seen:
                seen.add(rest)
                names.append(rest)

    return names


def discover_module_specs(package_name: str = "modules") -> List[ModuleSpec]:
    """扫描 `modules/` 下的全部顶层插件子包，按 `(order, module_id)` 返回所有合法 `ModuleSpec`."""
    root_pkg = importlib.import_module(package_name)
    child_names = _iter_child_package_names(root_pkg, package_name)
    if not child_names:
        return []

    discovered: List[ModuleSpec] = []
    seen_ids = set()

    for child_name in child_names:
        full_name = f"{package_name}.{child_name}"
        mod_obj = importlib.import_module(full_name)
        spec = getattr(mod_obj, "MODULE_SPEC", None)
        if spec is None:
            continue
        if not isinstance(spec, ModuleSpec):
            raise TypeError(
                f"模块 {full_name}.MODULE_SPEC 必须是 ModuleSpec 实例，当前为 {type(spec).__name__}"
            )
        if spec.module_id != child_name:
            raise ValueError(
                f"模块包名 {child_name!r} 与 MODULE_SPEC.module_id={spec.module_id!r} 不一致"
            )
        if spec.module_id in seen_ids:
            continue
        seen_ids.add(spec.module_id)
        discovered.append(spec)

    discovered.sort(key=lambda s: (s.order, s.module_id))
    return discovered


def mount_module_spec(app: Any, ctx: Any, spec: ModuleSpec) -> None:
    """将单个插件 `ModuleSpec` 接入 PlatformContext 与 FastAPI 应用实例."""
    ctx.module_registry.register(spec)
    ctx.register_module_manifest(
        module_id=spec.module_id,
        title=spec.title,
        subtitle=spec.subtitle,
        api_prefix=spec.api_prefix,
        desktop_app_id=spec.desktop_app_id,
    )

    if spec.desktop_app_id and hasattr(ctx, "asset_store"):
        ctx.asset_store.register_app_id(spec.desktop_app_id)

    if hasattr(ctx, "store"):
        if spec.private_tables and hasattr(ctx.store, "register_private_tables"):
            ctx.store.register_private_tables(spec.private_tables)
        if spec.workspace_tables and hasattr(ctx.store, "register_workspace_tables"):
            ctx.store.register_workspace_tables(spec.workspace_tables)
        if spec.workspace_meta_keys and hasattr(ctx.store, "register_workspace_meta_keys"):
            ctx.store.register_workspace_meta_keys(spec.workspace_meta_keys)

    if spec.init_hook is not None:
        spec.init_hook(ctx)

    if spec.router is not None and app is not None:
        # 若应用已挂载根路径 "/" 静态文件托管（如运行期/单测动态挂载插件），将新路由插入其前方避免被 "/" 吞掉
        routes = getattr(getattr(app, "router", None), "routes", None)
        static_mount_idx = None
        if isinstance(routes, list):
            for idx, r in enumerate(routes):
                if getattr(r, "name", "") == "static" and getattr(r, "path", "") in ("", "/"):
                    static_mount_idx = idx
                    break
        if static_mount_idx is not None:
            before_len = len(routes)
            app.include_router(spec.router)
            added = routes[before_len:]
            del routes[before_len:]
            routes[static_mount_idx:static_mount_idx] = added
        else:
            app.include_router(spec.router)
        if hasattr(app, "openapi_schema"):
            app.openapi_schema = None


def mount_discovered_modules(
    app: Any,
    ctx: Any,
    *,
    specs: Optional[Iterable[ModuleSpec]] = None,
) -> List[ModuleSpec]:
    """自动发现并挂载全部插件模块（测试可通过 `specs` 注入额外/自定义插件）."""
    resolved = list(specs) if specs is not None else discover_module_specs()
    resolved.sort(key=lambda s: (s.order, s.module_id))
    for spec in resolved:
        mount_module_spec(app, ctx, spec)
    return resolved
