"""Data.O.G 平台增量更新中枢 (UpdateHub).

职责：
1. 封装 Layer 2 私有增量补丁仓 (`<private_root>/rt_upd/`) 的查询、上传校验、原子安装、回退、恢复基础版与缓存清理；
2. 对接只读 `<install_dir>/updates/` 扫描吸纳机制；
3. 严格禁止向任何返回值泄露 `private_root`、`updates_dir` 或 `install_dir` 等本地物理路径。
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional

from core.platform.storage_paths import redact_private_storage_paths
import update_bootstrap


class UpdateHub:
    """增量补丁状态管理与双渠道安装中枢."""

    def __init__(
        self,
        *,
        updates_dir: str,
        running_version: str,
        base_version: Optional[str] = None,
        install_dir: Optional[str] = None,
        verifier_pubkey: Optional[bytes] = None,
    ) -> None:
        self.updates_dir = os.path.abspath(updates_dir)
        self.running_version = update_bootstrap.normalize_semver(running_version)
        eff_base = (
            base_version
            or update_bootstrap.get_runtime_base_version(fallback=self.running_version)
            or self.running_version
        )
        self.base_version = update_bootstrap.normalize_semver(eff_base)
        self.install_dir = update_bootstrap.resolve_install_dir(install_dir)
        self.verifier_pubkey = verifier_pubkey

    def _running_patch_version(self) -> Optional[str]:
        boot_state = update_bootstrap.get_active_patch_state()
        if boot_state.get("active") and boot_state.get("active_version"):
            return str(boot_state["active_version"])
        return None

    def _redact(self, text: str) -> str:
        return redact_private_storage_paths(
            str(text or ""),
            extra_paths=(self.updates_dir, self.install_dir),
        )

    def _list_installed_releases(
        self,
        state: Dict[str, Any],
        running_patch: Optional[str],
    ) -> List[Dict[str, Any]]:
        releases_root = os.path.join(self.updates_dir, "releases")
        if not os.path.isdir(releases_root):
            return []

        history_by_ver: Dict[str, Dict[str, Any]] = {
            str(item.get("version")): item
            for item in (state.get("history") or [])
            if isinstance(item, dict) and item.get("version")
        }
        active_ver = state.get("active_version")
        prev_ver = state.get("previous_version")

        rows: List[Dict[str, Any]] = []
        for entry in os.listdir(releases_root):
            rel_dir = os.path.join(releases_root, entry)
            if not os.path.isdir(rel_dir) or os.path.islink(rel_dir):
                continue
            try:
                ver_norm = update_bootstrap.normalize_semver(entry)
                ver_tuple = update_bootstrap.parse_semver(ver_norm)
            except ValueError:
                continue

            manifest_path = os.path.join(rel_dir, "manifest.json")
            manifest: Dict[str, Any] = {}
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        manifest = loaded
                except Exception:
                    manifest = {}

            is_verified = False
            verify_error = ""
            try:
                update_bootstrap.verify_installed_release_dir(
                    rel_dir,
                    base_version=None,
                    verifier_pubkey=self.verifier_pubkey,
                )
                is_verified = True
            except Exception as exc:
                verify_error = self._redact(str(exc))

            hist = history_by_ver.get(ver_norm, {})
            code_meta = manifest.get("code_zip") if isinstance(manifest.get("code_zip"), dict) else {}
            static_meta = manifest.get("static_files") if isinstance(manifest.get("static_files"), dict) else {}

            rows.append(
                {
                    "_sort_key": ver_tuple,
                    "version": ver_norm,
                    "series": str(manifest.get("series") or update_bootstrap.format_series(ver_norm)),
                    "min_base_version": str(
                        manifest.get("min_base_version") or update_bootstrap.compute_min_base_version(ver_norm)
                    ),
                    "built_at": str(manifest.get("built_at") or hist.get("built_at") or ""),
                    "installed_at": str(hist.get("installed_at") or ""),
                    "source": str(hist.get("source") or ""),
                    "summary": str(manifest.get("summary") or hist.get("summary") or ""),
                    "notes": str(manifest.get("notes") or hist.get("notes") or ""),
                    "code_file_count": int(code_meta.get("file_count") or hist.get("code_file_count") or 0),
                    "static_file_count": int(len(static_meta) or hist.get("static_file_count") or 0),
                    "is_active": active_ver == ver_norm,
                    "is_running": running_patch == ver_norm,
                    "is_previous": prev_ver == ver_norm,
                    "verified": is_verified,
                    "verify_error": verify_error,
                }
            )

        rows.sort(key=lambda r: r["_sort_key"], reverse=True)
        for r in rows:
            r.pop("_sort_key", None)
        return rows

    def get_status(self) -> Dict[str, Any]:
        """获取当前运行版本、底座兼容性与增量补丁仓库状态（零物理路径泄露）."""
        state = update_bootstrap.load_update_state(self.updates_dir)
        boot_state = update_bootstrap.get_active_patch_state()
        running_patch = self._running_patch_version()
        active_patch = state.get("active_version")
        prev_patch = state.get("previous_version")
        installed_releases = self._list_installed_releases(state, running_patch)

        prev_exists = bool(
            prev_patch
            and prev_patch != active_patch
            and os.path.isdir(os.path.join(self.updates_dir, "releases", str(prev_patch)))
        )

        pending_count = 0
        try:
            pending_count = update_bootstrap.count_pending_install_dir_updates(
                updates_dir=self.updates_dir,
                install_dir=self.install_dir,
                base_version=self.base_version,
                verifier_pubkey=self.verifier_pubkey,
            )
        except Exception:
            pending_count = 0

        restart_required = active_patch != running_patch
        return {
            "running_version": self.running_version,
            "base_version": self.base_version,
            "series": update_bootstrap.format_series(self.base_version),
            "min_compatible_base": update_bootstrap.compute_min_base_version(self.base_version),
            "patch_active": running_patch is not None,
            "running_patch_version": running_patch,
            "active_patch_version": active_patch,
            "previous_version": prev_patch if prev_exists else None,
            "restart_required": restart_required,
            "pending_target_version": (active_patch or self.base_version) if restart_required else None,
            "can_rollback": prev_exists,
            "can_reset_to_base": active_patch is not None,
            "retired_reason": self._redact(str(state.get("retired_reason") or boot_state.get("retired_reason") or "")) or None,
            "last_rollback_reason": self._redact(str(state.get("last_rollback_reason") or "")) or None,
            "install_dir_pending_count": pending_count,
            "installed_releases": installed_releases,
        }

    def get_bootstrap_summary(self) -> Dict[str, Any]:
        """返回供 /api/platform/bootstrap 使用的轻量版本升级摘要."""
        state = update_bootstrap.load_update_state(self.updates_dir)
        running_patch = self._running_patch_version()
        active_patch = state.get("active_version")
        return {
            "running_version": self.running_version,
            "base_version": self.base_version,
            "series": update_bootstrap.format_series(self.base_version),
            "patch_active": running_patch is not None,
            "running_patch_version": running_patch,
            "active_patch_version": active_patch,
            "restart_required": active_patch != running_patch,
        }

    def inspect_update_bytes(self, raw_bytes: bytes, filename: str = "") -> Dict[str, Any]:
        """校验证书签名与文件完整性并返回兼容性诊断（不实际落盘安装）."""
        _ = filename
        verified = update_bootstrap.verify_update_package_bytes(
            raw_bytes,
            base_version=None,
            verifier_pubkey=self.verifier_pubkey,
        )
        manifest = verified["manifest"]
        compatible, compat_msg = update_bootstrap.check_version_compatibility(
            manifest["version"],
            self.base_version,
            manifest["min_base_version"],
        )
        return {
            "ok": True,
            "version": manifest["version"],
            "series": manifest["series"],
            "min_base_version": manifest["min_base_version"],
            "built_at": manifest.get("built_at") or "",
            "summary": manifest.get("summary") or "",
            "notes": manifest.get("notes") or "",
            "code_file_count": verified["code_file_count"],
            "static_file_count": verified["static_file_count"],
            "container_type": verified["container_type"],
            "package_size": verified["package_size"],
            "dgp_size": verified["dgp_size"],
            "dgp_sha256": verified["dgp_sha256"],
            "compatible": compatible,
            "compatibility_message": compat_msg,
            "current_base_version": self.base_version,
            "current_running_version": self.running_version,
        }

    def install_update_bytes(self, raw_bytes: bytes, filename: str = "") -> Dict[str, Any]:
        """校验签名与兼容性并安装到 Layer 2，返回更新后状态."""
        verified = update_bootstrap.verify_update_package_bytes(
            raw_bytes,
            base_version=self.base_version,
            verifier_pubkey=self.verifier_pubkey,
        )
        ver = verified["manifest"]["version"]
        dgp_sha = verified["dgp_sha256"]
        running_patch = self._running_patch_version()
        state = update_bootstrap.load_update_state(self.updates_dir)
        ingested = state.get("ingested_hashes") or {}
        if (
            running_patch == ver
            and state.get("active_version") == ver
            and isinstance(ingested.get(dgp_sha), dict)
            and ingested[dgp_sha].get("version") == ver
        ):
            status = self.get_status()
            return {
                "ok": True,
                "installed_version": ver,
                "already_running": True,
                "restart_required": bool(status["restart_required"]),
                "message": f"增量更新包 v{ver} 已处于运行激活状态，无需重复安装。",
                "update": status,
            }

        res = update_bootstrap.install_verified_update_bytes(
            raw_bytes,
            updates_dir=self.updates_dir,
            base_version=self.base_version,
            verifier_pubkey=self.verifier_pubkey,
            source="upload",
            source_filename=os.path.basename(filename or ""),
        )
        ver = res["version"]
        status = self.get_status()
        restart_req = bool(status["restart_required"])
        msg = (
            f"增量更新包 v{ver} 签名校验通过并安装就绪，请关闭并重启 DataOG 后生效。"
            if restart_req
            else f"增量更新包 v{ver} 校验完成，当前已运行该版本。"
        )
        return {
            "ok": True,
            "installed_version": ver,
            "restart_required": restart_req,
            "message": msg,
            "update": status,
        }

    def scan_install_dir_updates(self) -> Dict[str, Any]:
        """扫描安装目录 updates/ 子目录中的更新包并吸纳最新合法版本."""
        scan_res = update_bootstrap.scan_and_ingest_install_dir_updates(
            updates_dir=self.updates_dir,
            install_dir=self.install_dir,
            base_version=self.base_version,
            verifier_pubkey=self.verifier_pubkey,
        )
        safe_skipped = [
            {
                "filename": os.path.basename(str(item.get("filename") or "")),
                "reason": self._redact(str(item.get("reason") or "")),
            }
            for item in (scan_res.get("skipped") or [])
        ]
        status = self.get_status()
        inst_ver = scan_res.get("installed_version")
        if inst_ver:
            msg = f"已从 updates 目录自动吸纳增量更新包 v{inst_ver}，重启 DataOG 后生效。"
        elif int(scan_res.get("already_ingested_count") or 0) > 0:
            msg = "updates 目录中的增量包此前已安装就绪，无需重复导入。"
        elif int(scan_res.get("found_count") or 0) == 0:
            msg = "安装目录 updates/ 下暂未发现待导入的 .zip 或 .dgp 增量更新包。"
        else:
            msg = "扫描完成，updates/ 目录中的候选包未通过兼容性或签名校验。"

        return {
            "ok": True,
            "found_count": int(scan_res.get("found_count") or 0),
            "installed_version": inst_ver,
            "already_ingested_count": int(scan_res.get("already_ingested_count") or 0),
            "skipped": safe_skipped,
            "restart_required": bool(status["restart_required"]),
            "message": msg,
            "update": status,
        }

    def rollback_to_previous(self) -> Dict[str, Any]:
        """一键切换回上一个已缓存补丁版本（重启后生效）."""
        state = update_bootstrap.load_update_state(self.updates_dir)
        prev = state.get("previous_version")
        if not prev:
            raise ValueError("当前没有可回退的上一版本增量补丁")
        prev_dir = os.path.join(self.updates_dir, "releases", str(prev))
        if not os.path.isdir(prev_dir):
            raise ValueError(f"上一版本 v{prev} 缓存已不存在，请选择恢复内置基础版本")

        update_bootstrap.verify_installed_release_dir(
            prev_dir,
            base_version=self.base_version,
            verifier_pubkey=self.verifier_pubkey,
        )
        old_active = state.get("active_version")
        state["active_version"] = prev
        if old_active and old_active != prev and os.path.isdir(
            os.path.join(self.updates_dir, "releases", str(old_active))
        ):
            state["previous_version"] = old_active
        else:
            state["previous_version"] = None
        state["last_rollback_reason"] = None
        state["retired_reason"] = None
        update_bootstrap.save_update_state(self.updates_dir, state)
        update_bootstrap.save_boot_guard(
            self.updates_dir,
            {"version": prev, "consecutive_failures": 0, "last_attempt_at": None, "healthy": False},
        )
        status = self.get_status()
        return {
            "ok": True,
            "target_version": prev,
            "restart_required": bool(status["restart_required"]),
            "message": f"已将启动目标切换为上一版本 v{prev}，重启 DataOG 后生效。",
            "update": status,
        }

    def reset_to_base_version(self) -> Dict[str, Any]:
        """取消增量补丁激活，下次启动恢复内置底座版本."""
        state = update_bootstrap.load_update_state(self.updates_dir)
        old_active = state.get("active_version")
        if not old_active:
            raise ValueError("当前已处于内置基础版本模式，无需重复恢复")
        state["previous_version"] = old_active
        state["active_version"] = None
        state["last_rollback_reason"] = None
        state["retired_reason"] = None
        update_bootstrap.save_update_state(self.updates_dir, state)
        update_bootstrap.save_boot_guard(
            self.updates_dir,
            {"version": None, "consecutive_failures": 0, "last_attempt_at": None, "healthy": True},
        )
        status = self.get_status()
        return {
            "ok": True,
            "target_version": self.base_version,
            "restart_required": bool(status["restart_required"]),
            "message": f"已恢复下次启动使用内置基础版本 v{self.base_version}，重启 DataOG 后生效。",
            "update": status,
        }

    def delete_cached_patch(self, version: str) -> Dict[str, Any]:
        """清理非活动、非运行中的本地缓存补丁目录."""
        ver = update_bootstrap.normalize_semver(version)
        state = update_bootstrap.load_update_state(self.updates_dir)
        running_patch = self._running_patch_version()
        if ver == state.get("active_version") or ver == running_patch:
            raise ValueError("不能删除当前正在运行或下次即将启动的补丁版本")

        target_dir = os.path.join(self.updates_dir, "releases", ver)
        if not os.path.isdir(target_dir):
            raise ValueError(f"未找到缓存补丁版本 v{ver}")

        shutil.rmtree(target_dir)
        if state.get("previous_version") == ver:
            state["previous_version"] = None
        state["ingested_hashes"] = {
            h: info
            for h, info in (state.get("ingested_hashes") or {}).items()
            if not (isinstance(info, dict) and info.get("version") == ver)
        }
        state["history"] = [
            h
            for h in (state.get("history") or [])
            if not (isinstance(h, dict) and h.get("version") == ver)
        ]
        update_bootstrap.save_update_state(self.updates_dir, state)
        return {
            "ok": True,
            "deleted_version": ver,
            "message": f"已清理缓存补丁 v{ver}",
            "update": self.get_status(),
        }
