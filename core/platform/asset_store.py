"""Data.O.G 本地桌面壁纸与软件/模块图标个性化存储中心 (AssetStore).

安全机制（严格遵循本地隔离与文件上传安全基线）：
1. 仅允许安全光栅图像格式（PNG / JPEG / WebP / GIF），双重校验文件扩展名白名单与二进制魔数头，彻底阻断 SVG / HTML 脚本注入；
2. 强制体积上限（壁纸 <= 12MB，软件与模块图标 <= 4MB）；
3. 所有上传文件统一重命名为不可预测的随机 UUID 文件名，隔离存放于 Web 静态目录之外的 `data/assets/`，并强制校验 `realpath` 目录边界防路径穿越；
4. 自动清理被替换或重置的旧资源文件，映射状态持久化于本地 SQLite `data/dataog_state.db`。
"""

# TODO(security): 本地单机离线工具未外接云端杀毒沙箱 (AV API) 与 EXIF/CDR 元数据剥离服务；已通过严格光栅魔数白名单、nosniff 与独立 CSP 沙箱头阻断脚本执行。

from __future__ import annotations

import os
import re
import stat
import threading
import uuid
from typing import Any, Dict, Optional, Tuple

from core.platform.storage import PlatformStateStore

APPEARANCE_META_KEY = "desktop_appearance_v1"

VALID_WALLPAPER_PRESETS = {"default", "sonoma", "sequoia", "aurora", "custom"}
VALID_WALLPAPER_FITS = {"cover", "contain"}
VALID_APP_IDS = ("settings", "explorer", "standard", "polyglot")
VALID_UPLOAD_SLOTS = {
    "wallpaper",
    "brand",
    *(f"app:{aid}" for aid in VALID_APP_IDS),
}

MAX_WALLPAPER_BYTES = 12 * 1024 * 1024  # 12 MB
MAX_ICON_BYTES = 4 * 1024 * 1024  # 4 MB

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SAFE_ASSET_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(png|jpg|webp|gif)$")


def detect_safe_raster_image(content: bytes, filename: str = "") -> Tuple[str, str]:
    """双重校验扩展名（若提供）与文件二进制魔数头，返回标准化扩展名与真实 MIME 类型."""
    if not content or len(content) < 12:
        raise ValueError("上传的图片文件为空或已损坏")

    if filename:
        base = os.path.basename(filename.replace("\\", "/")).strip()
        _, ext_raw = os.path.splitext(base)
        if ext_raw and ext_raw.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"不支持的图片扩展名 [{ext_raw}]，仅支持 PNG / JPG / WebP / GIF 光栅图片"
            )

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"

    raise ValueError("文件内容未通过图像魔数头安全校验，仅允许标准 PNG / JPEG / WebP / GIF 图片")


class AssetStore:
    """管理桌面壁纸、Data.O.G 主图标及四大模块自定义图标的本地持久化."""

    def __init__(self, store: PlatformStateStore, assets_dir: str) -> None:
        self.store = store
        self.assets_dir = os.path.realpath(os.path.abspath(assets_dir))
        os.makedirs(self.assets_dir, exist_ok=True)
        try:
            os.chmod(self.assets_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
        self._lock = threading.Lock()

    def _resolve_safe_asset_path(self, asset_name: str) -> Optional[str]:
        clean = (asset_name or "").strip()
        if not SAFE_ASSET_NAME_RE.match(clean):
            return None
        candidate = os.path.realpath(os.path.join(self.assets_dir, clean))
        if not candidate.startswith(self.assets_dir + os.sep):
            return None
        return candidate

    def _remove_asset_quietly(self, asset_name: str) -> None:
        path = self._resolve_safe_asset_path(asset_name)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _load_raw_state(self) -> Dict[str, Any]:
        saved = self.store.get_meta(APPEARANCE_META_KEY, None)
        if not isinstance(saved, dict):
            saved = {}
        wp_id = str(saved.get("wallpaper_id") or "default").strip().lower()
        if wp_id not in VALID_WALLPAPER_PRESETS:
            wp_id = "default"
        wp_fit = str(saved.get("wallpaper_fit") or "cover").strip().lower()
        if wp_fit not in VALID_WALLPAPER_FITS:
            wp_fit = "cover"

        custom_wp = str(saved.get("custom_wallpaper_asset") or "").strip()
        if custom_wp and not self._asset_exists(custom_wp):
            custom_wp = ""
            if wp_id == "custom":
                wp_id = "default"

        brand_asset = str(saved.get("brand_icon_asset") or "").strip()
        if brand_asset and not self._asset_exists(brand_asset):
            brand_asset = ""

        raw_apps = saved.get("app_icons")
        app_icons: Dict[str, str] = {}
        if isinstance(raw_apps, dict):
            for aid in VALID_APP_IDS:
                aname = str(raw_apps.get(aid) or "").strip()
                if aname and self._asset_exists(aname):
                    app_icons[aid] = aname

        return {
            "wallpaper_id": wp_id,
            "wallpaper_fit": wp_fit,
            "custom_wallpaper_asset": custom_wp,
            "brand_icon_asset": brand_asset,
            "app_icons": app_icons,
        }

    def _asset_exists(self, asset_name: str) -> bool:
        path = self._resolve_safe_asset_path(asset_name)
        return bool(path and os.path.isfile(path))

    @staticmethod
    def _asset_url(asset_name: str) -> str:
        return f"/api/platform/assets/{asset_name}" if asset_name else ""

    def get_public_appearance(self) -> Dict[str, Any]:
        state = self._load_raw_state()
        return {
            "wallpaper_id": state["wallpaper_id"],
            "wallpaper_fit": state["wallpaper_fit"],
            "custom_wallpaper_url": self._asset_url(state["custom_wallpaper_asset"]),
            "brand_icon_url": self._asset_url(state["brand_icon_asset"]),
            "app_icons": {
                aid: self._asset_url(aname)
                for aid, aname in state["app_icons"].items()
                if aname
            },
        }

    def update_preferences(
        self,
        *,
        wallpaper_id: Optional[str] = None,
        wallpaper_fit: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load_raw_state()
            if wallpaper_id is not None:
                wid = wallpaper_id.strip().lower()
                if wid not in VALID_WALLPAPER_PRESETS:
                    raise ValueError(f"无效的壁纸预设标识: {wallpaper_id}")
                if wid == "custom" and not state["custom_wallpaper_asset"]:
                    wid = "default"
                state["wallpaper_id"] = wid
            if wallpaper_fit is not None:
                wfit = wallpaper_fit.strip().lower()
                if wfit not in VALID_WALLPAPER_FITS:
                    raise ValueError(f"无效的壁纸填充方式: {wallpaper_fit}")
                state["wallpaper_fit"] = wfit
            self.store.save_meta(APPEARANCE_META_KEY, state)
        return self.get_public_appearance()

    def save_uploaded_asset(
        self,
        *,
        slot: str,
        filename: str,
        content: bytes,
    ) -> Dict[str, Any]:
        norm_slot = (slot or "").strip().lower()
        if norm_slot not in VALID_UPLOAD_SLOTS:
            raise ValueError(f"不支持的图标/壁纸槽位: {slot}")

        max_bytes = MAX_WALLPAPER_BYTES if norm_slot == "wallpaper" else MAX_ICON_BYTES
        if len(content) > max_bytes:
            limit_mb = max_bytes // (1024 * 1024)
            raise ValueError(f"图片大小超出上限（最大允许 {limit_mb} MB）")

        ext, _mime = detect_safe_raster_image(content, filename=filename)
        asset_name = f"{uuid.uuid4().hex}.{ext}"
        dst_path = self._resolve_safe_asset_path(asset_name)
        if not dst_path:
            raise RuntimeError("生成本地资源存储路径失败")

        with self._lock:
            with open(dst_path, "wb") as fh:
                fh.write(content)
            try:
                os.chmod(dst_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

            state = self._load_raw_state()
            old_asset = ""
            if norm_slot == "wallpaper":
                old_asset = state.get("custom_wallpaper_asset", "")
                state["custom_wallpaper_asset"] = asset_name
                state["wallpaper_id"] = "custom"
            elif norm_slot == "brand":
                old_asset = state.get("brand_icon_asset", "")
                state["brand_icon_asset"] = asset_name
            elif norm_slot.startswith("app:"):
                app_id = norm_slot.split(":", 1)[1]
                old_asset = state["app_icons"].get(app_id, "")
                state["app_icons"][app_id] = asset_name

            self.store.save_meta(APPEARANCE_META_KEY, state)
            if old_asset and old_asset != asset_name:
                self._remove_asset_quietly(old_asset)

        return self.get_public_appearance()

    def reset_slot(self, slot: str) -> Dict[str, Any]:
        norm_slot = (slot or "").strip().lower()
        valid_resets = VALID_UPLOAD_SLOTS | {"all_icons", "all"}
        if norm_slot not in valid_resets:
            raise ValueError(f"不支持的重置槽位: {slot}")

        with self._lock:
            state = self._load_raw_state()
            to_delete = []

            if norm_slot in ("wallpaper", "all"):
                if state["custom_wallpaper_asset"]:
                    to_delete.append(state["custom_wallpaper_asset"])
                state["custom_wallpaper_asset"] = ""
                state["wallpaper_id"] = "default"

            if norm_slot in ("brand", "all_icons", "all"):
                if state["brand_icon_asset"]:
                    to_delete.append(state["brand_icon_asset"])
                state["brand_icon_asset"] = ""

            if norm_slot in ("all_icons", "all"):
                for aid, aname in list(state["app_icons"].items()):
                    if aname:
                        to_delete.append(aname)
                state["app_icons"] = {}
            elif norm_slot.startswith("app:"):
                aid = norm_slot.split(":", 1)[1]
                old = state["app_icons"].pop(aid, "")
                if old:
                    to_delete.append(old)

            self.store.save_meta(APPEARANCE_META_KEY, state)
            for aname in set(to_delete):
                self._remove_asset_quietly(aname)

        return self.get_public_appearance()

    def read_asset_bytes(self, asset_name: str) -> Optional[Tuple[bytes, str]]:
        """安全读取已落盘的光栅图片字节与真实 MIME 类型，供路由层安全响应."""
        path = self._resolve_safe_asset_path(asset_name)
        if not path or not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            data = fh.read(MAX_WALLPAPER_BYTES + 1)
        if len(data) > MAX_WALLPAPER_BYTES:
            return None
        try:
            _ext, mime = detect_safe_raster_image(data)
        except ValueError:
            return None
        return data, mime

    def read_brand_favicon(self) -> Optional[Tuple[bytes, str]]:
        """若用户自定义了 Data.O.G 软件主图标，则返回其字节与 MIME 供 /favicon.ico 使用."""
        state = self._load_raw_state()
        brand_asset = state.get("brand_icon_asset") or ""
        if not brand_asset:
            return None
        return self.read_asset_bytes(brand_asset)
