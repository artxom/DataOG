"""Data.O.G 三层分离存储路径解析与本地私有保险箱混淆定位器 (storage_paths).

设计原则：
1. 应用安装目录（`DataOG.exe` 所在目录）彻底保持无状态，绝不在程序目录创建或写入运行期 `data/` 目录，
   保证软件版本更新全量覆盖解压时绝不丢失用户任何配置与业务进度；
2. 本地私有存储默认落在操作系统当前用户私有数据目录（Windows `%LOCALAPPDATA%` / macOS
   `~/Library/Application Support` / Linux `$XDG_DATA_HOME` 或 `~/.local/share`），
   目录层级与核心数据库/时钟锚点文件名均采用确定性混淆派生与系统隐藏属性，
   且严禁向前端界面、API 响应或终端日志暴露具体物理路径；
3. 支持环境变量 `DATAOG_HOME` 覆盖根目录，供自动化单元测试、CI 隔离与受控便携部署使用。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import sys
from typing import Any, Dict, List, Optional, Sequence

# 混淆拆分的内部派生种子（用于生成无品牌字样的确定性目录与文件名，避免明文标识）
_S_A = (0x7B, 0x2E, 0x91, 0x4C, 0xD3, 0x18, 0xA5, 0x60)
_S_B = (0x19, 0x4F, 0xF2, 0x28, 0xB1, 0x77, 0xC4, 0x03)
_S_C = (0x55, 0xA1, 0x3C, 0xE8, 0x12, 0x9F, 0x44, 0x6B)


def _derive_tag(slot: str, length: int = 8) -> str:
    seed = bytes(a ^ b ^ c for a, b, c in zip(_S_A, _S_B, _S_C))
    digest = hashlib.sha256(seed + b":" + slot.encode("utf-8")).hexdigest()
    return digest[:length]


def _anchor_mask_key() -> bytes:
    seed = bytes(a ^ b ^ c for a, b, c in zip(_S_A, _S_B, _S_C))
    return hashlib.sha256(seed + b":clk_mask_v27").digest()


def encode_obfuscated_anchor(obj: Dict[str, Any]) -> str:
    """将单调时钟水印序列化并做轻量混淆编码，避免纯文本 JSON 直观暴露结构."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    key = _anchor_mask_key()
    masked = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return "V2:" + base64.urlsafe_b64encode(masked).decode("ascii")


def decode_obfuscated_anchor(text: str) -> Optional[Dict[str, Any]]:
    """解码混淆时钟锚点，同时向下兼容读取历史明文 JSON 格式."""
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("V2:"):
        try:
            masked = base64.urlsafe_b64decode(s[3:].encode("ascii"))
            key = _anchor_mask_key()
            raw = bytes(b ^ key[i % len(key)] for i, b in enumerate(masked))
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def get_app_install_dir() -> str:
    """获取应用程序安装目录（只读探测用，绝不向此目录写入运行期数据）."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # core/platform/storage_paths.py -> 项目根目录
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _apply_hidden_dir_attrs(path: str) -> None:
    """尽力为私有存储目录应用当前用户专属权限及 Windows 隐藏属性."""
    if not path or not os.path.exists(path):
        return
    if os.name == "nt":
        try:
            import ctypes

            FILE_ATTRIBUTE_HIDDEN = 0x02
            INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            get_attrs = kernel32.GetFileAttributesW
            get_attrs.argtypes = [ctypes.c_wchar_p]
            get_attrs.restype = ctypes.c_uint32
            set_attrs = kernel32.SetFileAttributesW
            set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            set_attrs.restype = ctypes.c_int

            attrs = int(get_attrs(str(path)))
            if attrs != INVALID_FILE_ATTRIBUTES and not (attrs & FILE_ATTRIBUTE_HIDDEN):
                set_attrs(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass
    else:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass


def default_workspace_filename() -> str:
    """生成默认共享工作区在本地私有目录内的混淆文件名（绝不含品牌或 .db 明文后缀）."""
    return f"s_{_derive_tag('default_ws', 8)}.dat"


def _resolve_os_obfuscated_private_root(
    *,
    os_name: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> str:
    """在操作系统当前用户私有目录中派生无品牌明文的混淆嵌套目录."""
    seg_outer = _derive_tag("outer_ns", 6)
    seg_mid = _derive_tag("mid_bucket", 8)
    seg_inner = _derive_tag("inner_cell", 10)
    eff_os = os.name if os_name is None else os_name
    eff_plat = sys.platform if platform_name is None else platform_name

    if eff_os == "nt":
        base = (
            os.environ.get("LOCALAPPDATA", "").strip()
            or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
        return os.path.join(base, f".rtcache_{seg_outer}", f"c{seg_mid}", f"_{seg_inner}")

    if eff_plat == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
        return os.path.join(base, f".com.sys.rt.{seg_outer}", f".{seg_mid}", f"_{seg_inner}")

    base = (
        os.environ.get("XDG_DATA_HOME", "").strip()
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    )
    return os.path.join(base, f".sys-rt-{seg_outer}", f".{seg_mid}", f"_{seg_inner}")


@dataclass(frozen=True)
class StorageLayout:
    """三层分离运行期物理目录布局（仅供后端底座内部使用，严禁对外暴露）."""

    private_root: str
    private_db_path: str
    clock_anchor_path: str
    workspaces_dir: str
    default_workspace_path: str
    drivers_dir: str
    assets_dir: str
    logs_dir: str
    service_log_path: str
    tmp_dir: str
    legacy_app_data_dir: str
    is_env_overridden: bool = False

    def ensure_directories(self) -> None:
        """按需创建私有存储内部子目录并应用最小权限/隐藏属性（绝不创建程序目录 data/）."""
        # 对自创建的混淆外层与中层目录同步应用隐藏属性（Windows）或 0o700 仅当前用户可读权限（POSIX）
        if not self.is_env_overridden:
            outer = os.path.dirname(os.path.dirname(self.private_root))
            mid = os.path.dirname(self.private_root)
            for p in (outer, mid, self.private_root):
                os.makedirs(p, exist_ok=True)
                _apply_hidden_dir_attrs(p)
        else:
            os.makedirs(self.private_root, exist_ok=True)
            _apply_hidden_dir_attrs(self.private_root)

        for sub in (
            os.path.dirname(self.clock_anchor_path),
            self.workspaces_dir,
            self.drivers_dir,
            self.assets_dir,
            self.logs_dir,
            self.tmp_dir,
        ):
            os.makedirs(sub, exist_ok=True)
            _apply_hidden_dir_attrs(sub)


def resolve_private_storage_layout(
    private_root_override: Optional[str] = None,
    *,
    create_dirs: bool = False,
    os_name: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> StorageLayout:
    """解析 Data.O.G 私有存储与共享工作区布局."""
    env_home = (os.environ.get("DATAOG_HOME") or "").strip()
    is_overridden = False
    if private_root_override:
        root = os.path.abspath(private_root_override)
        is_overridden = True
    elif env_home:
        root = os.path.abspath(env_home)
        is_overridden = True
    else:
        root = os.path.abspath(
            _resolve_os_obfuscated_private_root(
                os_name=os_name,
                platform_name=platform_name,
            )
        )

    vault_file = f"idx_{_derive_tag('vault_db', 8)}.dat"
    anchor_file = f"m_{_derive_tag('clk_anchor', 10)}.bin"
    workspaces_dir = os.path.join(root, "ws_store")

    layout = StorageLayout(
        private_root=root,
        private_db_path=os.path.join(root, vault_file),
        clock_anchor_path=os.path.join(root, ".chk", anchor_file),
        workspaces_dir=workspaces_dir,
        default_workspace_path=os.path.join(workspaces_dir, default_workspace_filename()),
        drivers_dir=os.path.join(root, "ext_drv"),
        assets_dir=os.path.join(root, "res_obj"),
        logs_dir=os.path.join(root, "rt_log"),
        service_log_path=os.path.join(root, "rt_log", "rt_svc.log"),
        tmp_dir=os.path.join(root, "rt_tmp"),
        legacy_app_data_dir=os.path.join(get_app_install_dir(), "data"),
        is_env_overridden=is_overridden,
    )
    if create_dirs:
        layout.ensure_directories()
    return layout


_INTERNAL_STORAGE_SEGMENT_RE = re.compile(
    r"(?:(?:\\{1,2}|/)(?:ws_store|ext_drv|res_obj|rt_log|rt_tmp|\.chk))(?:(?:\\{1,2}|/)[^'\"\s,;:)\]}]+)?",
)
_OBFUSCATED_FILE_RE = re.compile(
    r"\b(?:idx_[0-9a-f]{8}\.dat|s_[0-9a-f]{8,10}\.dat|m_[0-9a-f]{10}\.bin|rt_svc\.log)\b"
)


def _path_variants(raw_path: str) -> List[str]:
    p = (raw_path or "").strip()
    if not p:
        return []
    out: List[str] = []
    for cand in (p, os.path.abspath(p), os.path.realpath(p)):
        if cand and cand not in out:
            out.append(cand)
        if "\\" in cand:
            fwd = cand.replace("\\", "/")
            if fwd not in out:
                out.append(fwd)
            esc = cand.replace("\\", "\\\\")
            if esc not in out:
                out.append(esc)
    return out


def redact_private_storage_paths(
    text: str,
    *,
    ctx: Any = None,
    store: Any = None,
    extra_paths: Sequence[str] = (),
) -> str:
    """严格脱敏文本中任何本地私有保险箱物理路径、symlink 真实路径及内部混淆文件名."""
    msg = str(text or "")
    if not msg:
        return msg

    eff_store = store or (getattr(ctx, "store", None) if ctx is not None else None)
    is_split = bool(getattr(eff_store, "is_split_storage", False)) if eff_store is not None else False

    markers: List[str] = []
    # 始终收集私有库文件、工作区库文件、时钟锚点及各隔离资源子目录（同时覆盖 abspath 与 realpath，避免 macOS /private/var 绕过）
    path_candidates: List[str] = list(extra_paths or ())
    if eff_store is not None:
        priv_db = str(getattr(eff_store, "private_db_path", "") or "")
        ws_db = str(getattr(eff_store, "workspace_db_path", "") or "")
        anchor = str(getattr(eff_store, "clock_anchor_path", "") or "")
        path_candidates.extend([priv_db, ws_db, anchor])
        if anchor:
            path_candidates.append(os.path.dirname(anchor))
        if is_split:
            if priv_db:
                path_candidates.append(os.path.dirname(priv_db))
            if ws_db:
                path_candidates.append(os.path.dirname(ws_db))
        for fpath in (priv_db, ws_db, anchor):
            bname = os.path.basename(fpath) if fpath else ""
            if bname and bname not in ("dataog_state.db", "dws_migrator_state.db"):
                markers.append(bname)

    if ctx is not None:
        if is_split:
            data_dir = str(getattr(ctx, "data_dir", "") or "")
            if data_dir:
                path_candidates.append(data_dir)
        for attr_holder, attr_name in (
            ("driver_manager", "drivers_dir"),
            ("asset_store", "assets_dir"),
            ("workspace_hub", "workspaces_dir"),
            ("workspace_hub", "tmp_dir"),
        ):
            holder = getattr(ctx, attr_holder, None)
            val = str(getattr(holder, attr_name, "") or "") if holder is not None else ""
            if val:
                path_candidates.append(val)

    for p in path_candidates:
        markers.extend(_path_variants(p))

    for marker in sorted({m for m in markers if m}, key=len, reverse=True):
        if marker in msg:
            msg = msg.replace(marker, "<private>")

    msg = _INTERNAL_STORAGE_SEGMENT_RE.sub("/<private>", msg)
    msg = _OBFUSCATED_FILE_RE.sub("<private>", msg)
    return msg
