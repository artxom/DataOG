"""Data.O.G 4.0 外置插件运行时中枢与故障隔离装载器 (core/platform/plugin_hub.py).

职责：
1. 管理 Layer 2 私有插件仓 (`<private_root>/rt_plg/`)：registry.json、已签名解包目录 `packages/<plugin_id>/<version>/`、双渠道吸纳 (`<install_dir>/plugins/` 与 UI 上传)；
2. 以 `dataog_plugins.<plugin_id>` 隔离命名空间加载已验签插件后端 `MODULE_SPEC`（支持纯 Python `vendor/` 路径，严禁二进制扩展）；
3. 启动期单插件故障隔离自愈 (quarantine)：任何插件初始化或导入异常自动回滚其脏路由/模块并隔离记录，绝不拖垮底座或内置应用；
4. 完整生命周期管理：版本回退、启停、开发态免签 `--dev-plugin` 目录热挂（冻结态禁止）、卸载默认保留表与可选安全清表；
5. 严格脱敏所有返回值中的物理路径。
"""

from __future__ import annotations

import datetime
import importlib
import importlib.abc
import importlib.util
import json
import logging
import os
import shutil
import sys
import types
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.platform.module_registry import ModuleSpec, mount_module_spec
from core.platform.plugin_package import (
    FORBIDDEN_BINARY_EXTS,
    MAX_PLUGIN_PACKAGE_BYTES,
    PLATFORM_ABI_VERSION,
    PluginVerificationError,
    RESERVED_PLUGIN_IDS,
    validate_plugin_manifest,
    verify_installed_plugin_dir,
    verify_plugin_package_bytes,
)
from core.platform.storage_paths import redact_private_storage_paths
import update_bootstrap

logger = logging.getLogger("dataog")

_ALLOWED_SCAN_EXTS = (".dogapp", ".zip", ".dat")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _PurePythonVendorFinder(importlib.abc.MetaPathFinder):
    """为外置插件提供纯 Python `backend/vendor/` 依赖查找，置于 sys.meta_path 末尾以免覆盖底座模块."""

    def __init__(self) -> None:
        self.vendor_dirs: List[str] = []

    def add_vendor_dir(self, path: str) -> None:
        abs_p = os.path.abspath(path)
        if os.path.isdir(abs_p) and abs_p not in self.vendor_dirs:
            # 预检严禁包含任何编译态扩展
            for cur_root, _, files in os.walk(abs_p, followlinks=False):
                for fname in files:
                    low = fname.lower()
                    if any(low.endswith(ext) for ext in FORBIDDEN_BINARY_EXTS):
                        raise PluginVerificationError(
                            "FORBIDDEN_CONTENT",
                            f"插件 vendor/ 目录严禁包含编译态或可执行文件: {fname}",
                        )
            self.vendor_dirs.append(abs_p)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None):
        if not self.vendor_dirs:
            return None
        if fullname in ("core", "modules", "dataog_plugins") or fullname.startswith(
            ("core.", "modules.", "dataog_plugins.")
        ):
            return None
        parts = fullname.split(".")
        search_roots = list(path) if path else self.vendor_dirs
        for root in search_roots:
            pkg_init = os.path.join(root, *parts, "__init__.py") if not path else os.path.join(root, parts[-1], "__init__.py")
            mod_file = os.path.join(root, *parts[:-1], f"{parts[-1]}.py") if not path else os.path.join(root, f"{parts[-1]}.py")
            if os.path.isfile(pkg_init):
                pkg_dir = os.path.dirname(pkg_init)
                return importlib.util.spec_from_file_location(
                    fullname,
                    pkg_init,
                    submodule_search_locations=[pkg_dir],
                )
            if os.path.isfile(mod_file):
                return importlib.util.spec_from_file_location(fullname, mod_file)
        return None


class PluginHub:
    """Data.O.G 4.0 外置插件生命周期与安全运行时中枢."""

    def __init__(
        self,
        *,
        store: Any,
        plugins_dir: str,
        platform_version: str,
        install_dir: Optional[str] = None,
        verifier_pubkey: Optional[bytes] = None,
    ) -> None:
        self.store = store
        self.plugins_dir = os.path.abspath(plugins_dir)
        self.platform_version = update_bootstrap.normalize_semver(platform_version)
        self.install_dir = update_bootstrap.resolve_install_dir(install_dir)
        self.verifier_pubkey = verifier_pubkey

        self._running_records: Dict[str, Dict[str, Any]] = {}
        self._dev_records: Dict[str, Dict[str, Any]] = {}
        self._active_frontend_dirs: Dict[str, str] = {}
        self._active_frontend_plugins: List[Dict[str, Any]] = []
        self._vendor_finder = _PurePythonVendorFinder()

    def _registry_path(self) -> str:
        return os.path.join(self.plugins_dir, "registry.json")

    def _packages_root(self) -> str:
        return os.path.join(self.plugins_dir, "packages")

    def _redact(self, text: str) -> str:
        return redact_private_storage_paths(
            str(text or ""),
            store=self.store,
            extra_paths=(self.plugins_dir, self.install_dir),
        )

    def _load_registry(self) -> Dict[str, Any]:
        reg_path = self._registry_path()
        if not os.path.isfile(reg_path):
            return {"plugins": {}}
        try:
            with open(reg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
                return data
        except Exception:
            pass
        return {"plugins": {}}

    def _save_registry(self, registry: Dict[str, Any]) -> None:
        os.makedirs(self.plugins_dir, exist_ok=True)
        reg_path = self._registry_path()
        tmp_path = f"{reg_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, reg_path)

    def _ensure_vendor_finder_registered(self) -> None:
        if self._vendor_finder not in sys.meta_path:
            sys.meta_path.append(self._vendor_finder)

    @staticmethod
    def _purge_plugin_sys_modules(plugin_id: str) -> None:
        prefix = f"dataog_plugins.{plugin_id}"
        for mod_name in list(sys.modules.keys()):
            if mod_name == prefix or mod_name.startswith(prefix + "."):
                sys.modules.pop(mod_name, None)

    @staticmethod
    def _ensure_namespace_pkg() -> None:
        if "dataog_plugins" not in sys.modules:
            ns = types.ModuleType("dataog_plugins")
            ns.__path__ = []  # type: ignore[attr-defined]
            ns.__package__ = "dataog_plugins"
            sys.modules["dataog_plugins"] = ns

    def _import_and_mount_plugin(
        self,
        app: Any,
        ctx: Any,
        *,
        plugin_id: str,
        manifest: Dict[str, Any],
        backend_dir: str,
        frontend_dir: str,
    ) -> ModuleSpec:
        if not os.path.isdir(backend_dir):
            raise PluginVerificationError("INVALID_ARCHIVE", f"插件 {plugin_id} 缺失 backend/ 目录")
        init_py = os.path.join(backend_dir, "__init__.py")
        if not os.path.isfile(init_py):
            raise PluginVerificationError("INVALID_ARCHIVE", f"插件 {plugin_id} 缺失 backend/__init__.py")

        vendor_dir = os.path.join(backend_dir, "vendor")
        if os.path.isdir(vendor_dir):
            self._ensure_vendor_finder_registered()
            self._vendor_finder.add_vendor_dir(vendor_dir)

        self._ensure_namespace_pkg()
        self._purge_plugin_sys_modules(plugin_id)

        full_pkg_name = f"dataog_plugins.{plugin_id}"
        pkg_spec = importlib.util.spec_from_file_location(
            full_pkg_name,
            init_py,
            submodule_search_locations=[backend_dir],
        )
        if pkg_spec is None or pkg_spec.loader is None:
            raise ImportError(f"无法构造插件 {plugin_id} 模块加载规范")

        mod_obj = importlib.util.module_from_spec(pkg_spec)
        sys.modules[full_pkg_name] = mod_obj
        pkg_spec.loader.exec_module(mod_obj)

        spec = getattr(mod_obj, "MODULE_SPEC", None)
        if not isinstance(spec, ModuleSpec):
            raise TypeError(f"插件 {plugin_id} backend/__init__.py 未导出合法 MODULE_SPEC 实例")
        if spec.module_id != plugin_id:
            raise ValueError(f"插件 MODULE_SPEC.module_id ({spec.module_id}) 与 plugin_id ({plugin_id}) 不一致")
        if spec.module_id in RESERVED_PLUGIN_IDS:
            raise ValueError(f"插件 module_id={spec.module_id!r} 为平台保留标识")
        expected_prefix = f"/api/modules/{plugin_id}"
        if spec.api_prefix != expected_prefix:
            raise ValueError(f"插件 api_prefix 必须为 {expected_prefix!r}，当前为 {spec.api_prefix!r}")
        if ctx.module_registry.get(plugin_id) is not None:
            raise ValueError(f"插件 module_id={plugin_id!r} 与已加载模块冲突")

        storage_decl = manifest.get("storage") or {}
        ws_tables = tuple(dict.fromkeys(list(spec.workspace_tables) + list(storage_decl.get("workspace_tables") or [])))
        priv_tables = tuple(dict.fromkeys(list(spec.private_tables) + list(storage_decl.get("private_tables") or [])))
        ws_meta = tuple(dict.fromkeys(list(spec.workspace_meta_keys) + list(storage_decl.get("workspace_meta_keys") or [])))
        app_id = str(manifest["desktop_app"]["app_id"])

        eff_spec = ModuleSpec(
            module_id=spec.module_id,
            title=spec.title or manifest["name"],
            subtitle=spec.subtitle or manifest.get("summary") or "",
            api_prefix=expected_prefix,
            router=spec.router,
            desktop_app_id=spec.desktop_app_id or app_id,
            private_tables=priv_tables,
            workspace_tables=ws_tables,
            workspace_meta_keys=ws_meta,
            init_hook=spec.init_hook,
            order=spec.order,
        )

        mount_module_spec(app, ctx, eff_spec)

        if os.path.isdir(frontend_dir):
            self._active_frontend_dirs[plugin_id] = os.path.realpath(frontend_dir)
            d_app = manifest["desktop_app"]
            entry_js = str(d_app.get("entry_js") or "register.js").lstrip("/")
            self._active_frontend_plugins.append(
                {
                    "plugin_id": plugin_id,
                    "app_id": app_id,
                    "title": d_app.get("title") or manifest["name"],
                    "version": manifest["version"],
                    "entry_url": f"/plugins/{plugin_id}/{entry_js}",
                    "stylesheets": [
                        f"/plugins/{plugin_id}/{str(s).lstrip('/')}" for s in (d_app.get("stylesheets") or [])
                    ],
                }
            )
        return eff_spec

    def _safe_mount_one(
        self,
        app: Any,
        ctx: Any,
        *,
        plugin_id: str,
        manifest: Dict[str, Any],
        backend_dir: str,
        frontend_dir: str,
    ) -> Tuple[bool, str, Optional[ModuleSpec]]:
        routes = getattr(getattr(app, "router", None), "routes", None) if app is not None else None
        routes_backup = list(routes) if isinstance(routes, list) else None
        manifest_len = len(getattr(ctx, "_registered_modules", []))
        had_spec = plugin_id in getattr(getattr(ctx, "module_registry", None), "_specs", {})
        try:
            eff_spec = self._import_and_mount_plugin(
                app,
                ctx,
                plugin_id=plugin_id,
                manifest=manifest,
                backend_dir=backend_dir,
                frontend_dir=frontend_dir,
            )
            return True, "", eff_spec
        except BaseException as exc:
            if isinstance(routes, list) and routes_backup is not None:
                routes[:] = routes_backup
                if hasattr(app, "openapi_schema"):
                    app.openapi_schema = None
            if hasattr(ctx, "_registered_modules"):
                del ctx._registered_modules[manifest_len:]
            if not had_spec and hasattr(ctx, "module_registry"):
                ctx.module_registry._specs.pop(plugin_id, None)
            self._active_frontend_dirs.pop(plugin_id, None)
            self._active_frontend_plugins = [
                p for p in self._active_frontend_plugins if p.get("plugin_id") != plugin_id
            ]
            self._purge_plugin_sys_modules(plugin_id)
            err_msg = self._redact(f"{type(exc).__name__}: {exc}")
            return False, err_msg, None

    def _cleanup_old_plugin_versions(self, plugin_id: str, keep_versions: Sequence[str]) -> None:
        pkg_root = os.path.join(self._packages_root(), plugin_id)
        if not os.path.isdir(pkg_root):
            return
        keep_set = {str(v) for v in keep_versions if v}
        for entry in os.listdir(pkg_root):
            if entry in keep_set:
                continue
            full = os.path.join(pkg_root, entry)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)

    def apply_pending_and_load_all(
        self,
        app: Any,
        ctx: Any,
        *,
        dev_plugin_dirs: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """启动阶段统一处理待卸载清理、目录吸纳、故障隔离装载与开发目录热挂载."""
        self._running_records.clear()
        self._dev_records.clear()
        self._active_frontend_dirs.clear()
        self._active_frontend_plugins.clear()

        reg = self._load_registry()
        plugins_map: Dict[str, Dict[str, Any]] = reg.get("plugins") or {}
        reg_changed = False

        # 1. 处理挂起卸载 (pending_action == "uninstall")
        for pid in list(plugins_map.keys()):
            rec = plugins_map[pid]
            if rec.get("pending_action") != "uninstall":
                continue
            pkg_base = os.path.join(self._packages_root(), pid)
            if os.path.isdir(pkg_base):
                shutil.rmtree(pkg_base, ignore_errors=True)
            if rec.get("purge_data_on_uninstall"):
                st = rec.get("storage") or {}
                try:
                    self.store.drop_plugin_tables(
                        workspace_tables=st.get("workspace_tables") or (),
                        private_tables=st.get("private_tables") or (),
                        workspace_meta_keys=st.get("workspace_meta_keys") or (),
                    )
                except Exception as exc:
                    logger.warning("[PluginHub] 卸载插件 %s 清表失败: %s", pid, self._redact(str(exc)))
            plugins_map.pop(pid, None)
            reg_changed = True

        if reg_changed:
            reg["plugins"] = plugins_map
            self._save_registry(reg)

        # 2. 只读扫描 <install_dir>/plugins/ 静默吸纳新签名插件（不影响无插件时的零写入）
        try:
            install_plugins_dir = os.path.join(self.install_dir, "plugins")
            if os.path.isdir(install_plugins_dir):
                self.scan_install_dir_plugins()
                reg = self._load_registry()
                plugins_map = reg.get("plugins") or {}
        except Exception as exc:
            logger.warning("[PluginHub] 启动扫描 plugins/ 目录异常: %s", self._redact(str(exc)))

        # 3. 装载开发期免签热挂载插件（严格限制非冻结态）
        dev_list = [os.path.abspath(p) for p in (dev_plugin_dirs or ()) if str(p or "").strip()]
        if dev_list and getattr(sys, "frozen", False):
            logger.warning("[PluginHub] 生产冻结态客户端严禁加载未签名开发插件目录，已拒绝 --dev-plugin 参数")
            self.store.add_log("WARN", "plugin:dev", "冻结态客户端拒绝加载未签名 --dev-plugin 开发目录")
            dev_list = []

        for dev_dir in dev_list:
            manifest_path = os.path.join(dev_dir, "plugin.json")
            if not os.path.isfile(manifest_path):
                logger.warning("[PluginHub] 跳过无效开发插件目录 (缺少 plugin.json): %s", self._redact(dev_dir))
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    manifest = validate_plugin_manifest(json.load(fh), platform_version=self.platform_version)
                pid = manifest["plugin_id"]
                ok, err_msg, _ = self._safe_mount_one(
                    app,
                    ctx,
                    plugin_id=pid,
                    manifest=manifest,
                    backend_dir=os.path.join(dev_dir, "backend"),
                    frontend_dir=os.path.join(dev_dir, "frontend"),
                )
                if ok:
                    self._dev_records[pid] = {
                        "plugin_id": pid,
                        "name": manifest["name"],
                        "version": manifest["version"],
                        "summary": manifest.get("summary") or "",
                        "author": manifest.get("author") or "",
                        "desktop_app": manifest["desktop_app"],
                        "storage": manifest["storage"],
                        "dev_mode": True,
                    }
                    self._running_records[pid] = {
                        "plugin_id": pid,
                        "version": manifest["version"],
                        "dev_mode": True,
                    }
                else:
                    self.store.add_log("ERROR", f"plugin:{pid}", f"开发插件挂载失败: {err_msg}")
            except Exception as exc:
                self.store.add_log("ERROR", "plugin:dev", f"开发插件解析失败: {self._redact(str(exc))}")

        # 4. 装载 Layer 2 已安装签名插件（单插件故障隔离自愈）
        loaded_ids: List[str] = list(self._dev_records.keys())
        quarantined_ids: List[str] = []
        reg_dirty = False
        for pid in sorted(plugins_map.keys()):
            if pid in self._dev_records:
                continue
            rec = plugins_map[pid]
            if not rec.get("enabled", True) or rec.get("quarantined", False):
                continue
            active_ver = str(rec.get("active_version") or "").strip()
            if not active_ver:
                continue
            pkg_dir = os.path.join(self._packages_root(), pid, active_ver)
            try:
                verified = verify_installed_plugin_dir(
                    pkg_dir,
                    platform_version=self.platform_version,
                    verifier_pubkey=self.verifier_pubkey,
                )
                manifest = verified["manifest"]
                ok, err_msg, _ = self._safe_mount_one(
                    app,
                    ctx,
                    plugin_id=pid,
                    manifest=manifest,
                    backend_dir=verified["backend_dir"],
                    frontend_dir=verified["frontend_dir"],
                )
                if not ok:
                    raise RuntimeError(err_msg)
                self._running_records[pid] = {
                    "plugin_id": pid,
                    "version": manifest["version"],
                    "dev_mode": False,
                }
                loaded_ids.append(pid)
            except BaseException as exc:
                reason = self._redact(f"{type(exc).__name__}: {exc}")
                rec["quarantined"] = True
                rec["quarantine_reason"] = reason
                rec["quarantined_at"] = _utc_now_iso()
                reg_dirty = True
                quarantined_ids.append(pid)
                self.store.add_log("ERROR", f"plugin:{pid}", f"外置插件启动异常已自动隔离: {reason}")
                logger.error("[PluginHub] 插件 %s 启动失败并已隔离: %s", pid, reason)

        if reg_dirty:
            reg["plugins"] = plugins_map
            self._save_registry(reg)

        return {"loaded": loaded_ids, "quarantined": quarantined_ids}

    def get_active_frontend_plugins(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._active_frontend_plugins]

    def get_active_frontend_dir(self, plugin_id: str) -> Optional[str]:
        return self._active_frontend_dirs.get(str(plugin_id or "").strip())

    def inspect_plugin_bytes(self, raw_bytes: bytes, filename: str = "") -> Dict[str, Any]:
        _ = filename
        compat_ok = True
        compat_msg = ""
        try:
            verified = verify_plugin_package_bytes(
                raw_bytes,
                platform_version=self.platform_version,
                verifier_pubkey=self.verifier_pubkey,
            )
        except PluginVerificationError as exc:
            if exc.code in ("INCOMPATIBLE_VERSION", "INCOMPATIBLE_ABI"):
                verified = verify_plugin_package_bytes(
                    raw_bytes,
                    platform_version=None,
                    verifier_pubkey=self.verifier_pubkey,
                )
                compat_ok = False
                compat_msg = exc.message
            else:
                raise
        manifest = verified["manifest"]
        return {
            "ok": True,
            "plugin_id": manifest["plugin_id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "summary": manifest.get("summary") or "",
            "author": manifest.get("author") or "",
            "api_version": manifest["api_version"],
            "min_platform_version": manifest["min_platform_version"],
            "max_platform_version": manifest.get("max_platform_version") or "",
            "desktop_app": manifest["desktop_app"],
            "storage": manifest["storage"],
            "format_version": verified["format_version"],
            "encrypted": verified["encrypted"],
            "cipher": verified["cipher"],
            "file_count": verified["file_count"],
            "package_size": verified["package_size"],
            "dogapp_size": verified["dogapp_size"],
            "dogapp_sha256": verified["dogapp_sha256"],
            "compatible": compat_ok,
            "compatibility_message": compat_msg,
        }

    def install_plugin_bytes(
        self,
        raw_bytes: bytes,
        filename: str = "",
        *,
        source: str = "upload",
    ) -> Dict[str, Any]:
        verified = verify_plugin_package_bytes(
            raw_bytes,
            platform_version=self.platform_version,
            verifier_pubkey=self.verifier_pubkey,
            include_payload=True,
        )
        manifest = verified["manifest"]
        plugin_id = manifest["plugin_id"]
        version = manifest["version"]
        dogapp_sha = verified["dogapp_sha256"]
        payload_files: Dict[str, bytes] = verified["payload_files"]

        reg = self._load_registry()
        plugins_map: Dict[str, Dict[str, Any]] = reg.get("plugins") or {}
        prev_rec = plugins_map.get(plugin_id) or {}
        running = self._running_records.get(plugin_id)

        target_dir = os.path.join(self._packages_root(), plugin_id, version)
        if (
            prev_rec.get("active_version") == version
            and prev_rec.get("dogapp_sha256") == dogapp_sha
            and os.path.isdir(target_dir)
            and prev_rec.get("pending_action") != "uninstall"
        ):
            status = self.get_status()
            return {
                "ok": True,
                "plugin_id": plugin_id,
                "version": version,
                "already_installed": True,
                "restart_required": bool(status["restart_required"]),
                "message": f"插件 {manifest['name']} (v{version}) 已安装就绪，无需重复导入。",
                "plugin_hub": status,
            }

        stage_dir = os.path.join(self._packages_root(), plugin_id, f".staging_{version}_{os.getpid()}")
        if os.path.exists(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)
        try:
            os.makedirs(stage_dir, exist_ok=True)
            for rel, content in payload_files.items():
                dest = os.path.join(stage_dir, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(content)
            verify_installed_plugin_dir(
                stage_dir,
                platform_version=self.platform_version,
                verifier_pubkey=self.verifier_pubkey,
            )
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            os.replace(stage_dir, target_dir)
        finally:
            if os.path.exists(stage_dir):
                shutil.rmtree(stage_dir, ignore_errors=True)

        old_active = str(prev_rec.get("active_version") or "").strip()
        previous_version = (
            old_active
            if (old_active and old_active != version and os.path.isdir(os.path.join(self._packages_root(), plugin_id, old_active)))
            else prev_rec.get("previous_version")
        )
        if previous_version == version:
            previous_version = None

        ingested = dict(prev_rec.get("ingested_hashes") or {})
        ingested[dogapp_sha] = {"version": version, "installed_at": _utc_now_iso()}

        plugins_map[plugin_id] = {
            "plugin_id": plugin_id,
            "name": manifest["name"],
            "summary": manifest.get("summary") or "",
            "author": manifest.get("author") or "",
            "active_version": version,
            "previous_version": previous_version,
            "enabled": True,
            "quarantined": False,
            "quarantine_reason": "",
            "pending_action": "",
            "purge_data_on_uninstall": False,
            "dogapp_sha256": dogapp_sha,
            "source": source,
            "source_filename": os.path.basename(filename or ""),
            "installed_at": _utc_now_iso(),
            "desktop_app": manifest["desktop_app"],
            "storage": manifest["storage"],
            "min_platform_version": manifest["min_platform_version"],
            "max_platform_version": manifest.get("max_platform_version") or "",
            "ingested_hashes": ingested,
        }
        reg["plugins"] = plugins_map
        self._save_registry(reg)
        self._cleanup_old_plugin_versions(plugin_id, [version, previous_version or ""])

        status = self.get_status()
        restart_req = running is None or running.get("version") != version or bool(status["restart_required"])
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "version": version,
            "restart_required": restart_req,
            "message": f"插件 {manifest['name']} (v{version}) 签名校验通过并安装就绪，重启 Data.O.G 后生效。",
            "plugin_hub": status,
        }

    def scan_install_dir_plugins(self) -> Dict[str, Any]:
        scan_dir = os.path.join(self.install_dir, "plugins")
        if not os.path.isdir(scan_dir):
            status = self.get_status()
            return {
                "ok": True,
                "found_count": 0,
                "installed": [],
                "already_ingested_count": 0,
                "skipped": [],
                "restart_required": bool(status["restart_required"]),
                "message": "安装目录 plugins/ 下暂未发现待导入的 .dogapp、.zip 或 .dat 插件包。",
                "plugin_hub": status,
            }

        reg = self._load_registry()
        plugins_map = reg.get("plugins") or {}
        found_count = 0
        already_ingested_count = 0
        installed: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []

        best_by_id: Dict[str, Tuple[Tuple[int, int, int], str, bytes]] = {}
        for fname in sorted(os.listdir(scan_dir)):
            if not fname.lower().endswith(_ALLOWED_SCAN_EXTS):
                continue
            full = os.path.join(scan_dir, fname)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            found_count += 1
            try:
                if os.path.getsize(full) > MAX_PLUGIN_PACKAGE_BYTES:
                    skipped.append({"filename": fname, "reason": "插件包体积超出 32 MB 安全上限"})
                    continue
                with open(full, "rb") as fh:
                    raw = fh.read(MAX_PLUGIN_PACKAGE_BYTES + 1)
                verified = verify_plugin_package_bytes(
                    raw,
                    platform_version=self.platform_version,
                    verifier_pubkey=self.verifier_pubkey,
                )
                pid = verified["manifest"]["plugin_id"]
                ver = verified["manifest"]["version"]
                sha = verified["dogapp_sha256"]
                existing = plugins_map.get(pid) or {}
                if (
                    sha in (existing.get("ingested_hashes") or {})
                    and os.path.isdir(os.path.join(self._packages_root(), pid, ver))
                ):
                    already_ingested_count += 1
                    continue
                ver_t = update_bootstrap.parse_semver(ver)
                cur_best = best_by_id.get(pid)
                if cur_best is None or ver_t > cur_best[0]:
                    best_by_id[pid] = (ver_t, fname, raw)
            except Exception as exc:
                skipped.append({"filename": fname, "reason": self._redact(str(exc))})

        for pid, (_, fname, raw) in best_by_id.items():
            res = self.install_plugin_bytes(raw, fname, source="install_dir")
            installed.append({"plugin_id": pid, "version": res["version"]})

        status = self.get_status()
        if installed:
            msg = f"已从 plugins/ 目录吸纳 {len(installed)} 个签名插件，重启 Data.O.G 后生效。"
        elif already_ingested_count > 0:
            msg = "plugins/ 目录中的插件包均已安装就绪，无需重复导入。"
        elif found_count == 0:
            msg = "安装目录 plugins/ 下暂未发现待导入的 .dogapp、.zip 或 .dat 插件包。"
        else:
            msg = "扫描完成，plugins/ 目录中的候选包未通过签名或兼容性校验。"

        return {
            "ok": True,
            "found_count": found_count,
            "installed": installed,
            "already_ingested_count": already_ingested_count,
            "skipped": skipped,
            "restart_required": bool(status["restart_required"]),
            "message": msg,
            "plugin_hub": status,
        }

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> Dict[str, Any]:
        reg = self._load_registry()
        plugins_map = reg.get("plugins") or {}
        rec = plugins_map.get(plugin_id)
        if not rec:
            raise ValueError(f"未找到已安装的插件: {plugin_id}")
        rec["enabled"] = bool(enabled)
        if enabled:
            rec["quarantined"] = False
            rec["quarantine_reason"] = ""
            rec["pending_action"] = ""
            rec["purge_data_on_uninstall"] = False
        reg["plugins"] = plugins_map
        self._save_registry(reg)
        status = self.get_status()
        act = "启用" if enabled else "停用"
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "enabled": bool(enabled),
            "restart_required": bool(status["restart_required"]),
            "message": f"已将插件 {rec.get('name') or plugin_id} 设为{act}状态，重启 Data.O.G 后生效。",
            "plugin_hub": status,
        }

    def rollback_plugin(self, plugin_id: str) -> Dict[str, Any]:
        reg = self._load_registry()
        plugins_map = reg.get("plugins") or {}
        rec = plugins_map.get(plugin_id)
        if not rec:
            raise ValueError(f"未找到已安装的插件: {plugin_id}")
        prev_ver = str(rec.get("previous_version") or "").strip()
        if not prev_ver:
            raise ValueError("该插件没有可回退的上一版本")
        prev_dir = os.path.join(self._packages_root(), plugin_id, prev_ver)
        verify_installed_plugin_dir(
            prev_dir,
            platform_version=self.platform_version,
            verifier_pubkey=self.verifier_pubkey,
        )
        cur_active = rec.get("active_version")
        rec["active_version"] = prev_ver
        rec["previous_version"] = (
            cur_active
            if cur_active and cur_active != prev_ver and os.path.isdir(os.path.join(self._packages_root(), plugin_id, str(cur_active)))
            else None
        )
        rec["enabled"] = True
        rec["quarantined"] = False
        rec["quarantine_reason"] = ""
        rec["pending_action"] = ""
        reg["plugins"] = plugins_map
        self._save_registry(reg)
        status = self.get_status()
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "target_version": prev_ver,
            "restart_required": bool(status["restart_required"]),
            "message": f"已将插件 {rec.get('name') or plugin_id} 切换回 v{prev_ver}，重启 Data.O.G 后生效。",
            "plugin_hub": status,
        }

    def uninstall_plugin(self, plugin_id: str, *, purge_data: bool = False) -> Dict[str, Any]:
        reg = self._load_registry()
        plugins_map = reg.get("plugins") or {}
        rec = plugins_map.get(plugin_id)
        if not rec:
            raise ValueError(f"未找到已安装的插件: {plugin_id}")

        name = str(rec.get("name") or plugin_id)
        is_running = plugin_id in self._running_records and not self._running_records[plugin_id].get("dev_mode")
        if is_running:
            rec["enabled"] = False
            rec["pending_action"] = "uninstall"
            rec["purge_data_on_uninstall"] = bool(purge_data)
            reg["plugins"] = plugins_map
            self._save_registry(reg)
            status = self.get_status()
            return {
                "ok": True,
                "plugin_id": plugin_id,
                "pending_uninstall": True,
                "purge_data": bool(purge_data),
                "restart_required": True,
                "message": f"已标记卸载插件 {name}，重启 Data.O.G 后将完成文件{'与业务表' if purge_data else '（保留业务数据表）'}清理。",
                "plugin_hub": status,
            }

        pkg_base = os.path.join(self._packages_root(), plugin_id)
        if os.path.isdir(pkg_base):
            shutil.rmtree(pkg_base, ignore_errors=True)
        drop_res = {"dropped_workspace_tables": 0, "dropped_private_tables": 0, "deleted_workspace_meta": 0}
        if purge_data:
            st = rec.get("storage") or {}
            drop_res = self.store.drop_plugin_tables(
                workspace_tables=st.get("workspace_tables") or (),
                private_tables=st.get("private_tables") or (),
                workspace_meta_keys=st.get("workspace_meta_keys") or (),
            )
        plugins_map.pop(plugin_id, None)
        reg["plugins"] = plugins_map
        self._save_registry(reg)
        status = self.get_status()
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "pending_uninstall": False,
            "purge_data": bool(purge_data),
            "dropped": drop_res,
            "restart_required": bool(status["restart_required"]),
            "message": f"插件 {name} 已卸载{'并清理关联业务数据表' if purge_data else '（已保留关联业务数据表）'}。",
            "plugin_hub": status,
        }

    def get_status(self) -> Dict[str, Any]:
        reg = self._load_registry()
        plugins_map: Dict[str, Dict[str, Any]] = reg.get("plugins") or {}
        rows: List[Dict[str, Any]] = []
        any_restart_required = False
        quarantined_count = 0
        active_count = 0

        for pid in sorted(plugins_map.keys()):
            rec = plugins_map[pid]
            running = self._running_records.get(pid)
            running_ver = running.get("version") if (running and not running.get("dev_mode")) else None
            is_running = running_ver is not None
            active_ver = str(rec.get("active_version") or "")
            enabled = bool(rec.get("enabled", True))
            quarantined = bool(rec.get("quarantined", False))
            pending_action = str(rec.get("pending_action") or "")

            if pending_action == "uninstall":
                restart_needed = True
            elif enabled and not quarantined:
                restart_needed = running_ver != active_ver
            else:
                restart_needed = is_running

            if restart_needed:
                any_restart_required = True
            if is_running:
                active_count += 1
            if quarantined:
                quarantined_count += 1

            prev_ver = rec.get("previous_version")
            can_rollback = bool(
                prev_ver
                and prev_ver != active_ver
                and os.path.isdir(os.path.join(self._packages_root(), pid, str(prev_ver)))
            )

            rows.append(
                {
                    "plugin_id": pid,
                    "name": rec.get("name") or pid,
                    "summary": rec.get("summary") or "",
                    "author": rec.get("author") or "",
                    "active_version": active_ver,
                    "running_version": running_ver,
                    "previous_version": prev_ver if can_rollback else None,
                    "can_rollback": can_rollback,
                    "enabled": enabled,
                    "is_running": is_running,
                    "quarantined": quarantined,
                    "quarantine_reason": self._redact(str(rec.get("quarantine_reason") or "")),
                    "pending_action": pending_action,
                    "purge_data_on_uninstall": bool(rec.get("purge_data_on_uninstall", False)),
                    "restart_required": restart_needed,
                    "dev_mode": False,
                    "installed_at": rec.get("installed_at") or "",
                    "source": rec.get("source") or "",
                    "desktop_app": rec.get("desktop_app") or {},
                    "storage": rec.get("storage") or {},
                }
            )

        for pid, dev_rec in sorted(self._dev_records.items()):
            active_count += 1
            rows.append(
                {
                    "plugin_id": pid,
                    "name": dev_rec["name"],
                    "summary": dev_rec.get("summary") or "",
                    "author": dev_rec.get("author") or "",
                    "active_version": dev_rec["version"],
                    "running_version": dev_rec["version"],
                    "previous_version": None,
                    "can_rollback": False,
                    "enabled": True,
                    "is_running": True,
                    "quarantined": False,
                    "quarantine_reason": "",
                    "pending_action": "",
                    "purge_data_on_uninstall": False,
                    "restart_required": False,
                    "dev_mode": True,
                    "installed_at": "",
                    "source": "dev_plugin",
                    "desktop_app": dev_rec.get("desktop_app") or {},
                    "storage": dev_rec.get("storage") or {},
                }
            )

        return {
            "platform_abi": PLATFORM_ABI_VERSION,
            "platform_version": self.platform_version,
            "installed_count": len(plugins_map),
            "active_count": active_count,
            "quarantined_count": quarantined_count,
            "dev_count": len(self._dev_records),
            "restart_required": any_restart_required,
            "plugins": rows,
        }

    def get_bootstrap_summary(self) -> Dict[str, Any]:
        st = self.get_status()
        return {
            "platform_abi": PLATFORM_ABI_VERSION,
            "installed_count": st["installed_count"],
            "active_count": st["active_count"],
            "quarantined_count": st["quarantined_count"],
            "dev_count": st["dev_count"],
            "restart_required": st["restart_required"],
        }
