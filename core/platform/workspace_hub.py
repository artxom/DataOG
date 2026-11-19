"""Data.O.G Layer 3 团队共享工作区生命周期管理与引导式增量合并引擎 (WorkspaceManager).

核心职责：
1. 管理本地托管的团队共享工作区 (`*.dataog.db`)：创建（空白/克隆）、重命名、热切换、删除与业务统计汇总；
2. 零密钥纯净导出 (`export_workspace_bytes`)：按白名单业务表重构独立 `DELETE` 日志模式 + `VACUUM` 后的 `.dataog.db`，
   结构上彻底不含任何激活码、AI API Key、数据库账号密码、个人外观或个人 AI 探查会话；
3. 安全导入与引导式增量合并 (`import_workspace_as_new` / `inspect_merge_source` / `execute_merge`)：
   - 同时兼容新版 `.dataog.db` 与旧版 `dataog_state.db` / `dws_migrator_state.db`；
   - 严格校验 SQLite 魔数头并以 `mode=ro` + `PRAGMA query_only=ON` 只读打开，仅查询白名单业务表；
   - 智能按 `connection_id` 与 `(db_type, host, port, dbname)` / `jdbc_url` 指纹映射数据源并自动匹配本机个人凭据；
   - 支持三大冲突策略（`supplement` 仅补充缺失/未绑定、`newer` 按 `updated_at` 保留较新、`overwrite` 覆盖冲突项但保留本地独有项），
     并在合并前输出跨环境表对差异预览 (`table_diffs`)，全程单事务原子落盘；
4. 零路径泄露红线：所有对外返回字典均不暴露私有存储目录及底层物理文件路径。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core.platform.connection_hub import ConnectionHub
from core.platform.metadata_hub import MetadataHub
from core.platform.models import (
    compute_connection_fingerprint,
    split_extra_props_secrets,
)
from core.platform.storage import (
    WORKSPACE_META_KEYS,
    PlatformStateStore,
    init_workspace_schema_on_conn,
)
from core.platform.storage_paths import default_workspace_filename

WORKSPACE_FORMAT_VERSION = "2.7.0"
DEFAULT_WORKSPACE_ID = "ws-default"
DEFAULT_WORKSPACE_FILE = default_workspace_filename()
DEFAULT_WORKSPACE_NAME = "默认工作区"

SQLITE_MAGIC_HEADER = b"SQLite format 3\x00"
MAX_WORKSPACE_UPLOAD_BYTES = 256 * 1024 * 1024  # 256 MB
MERGE_STAGE_TTL_SEC = 1800.0  # 30 分钟
MAX_STAGED_MERGE_FILES = 6

SAFE_WS_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:dataog\.db|dat)$")
SAFE_TOKEN_RE = re.compile(r"^[a-f0-9]{24,64}$")

ALL_MERGE_MODULES: Tuple[str, ...] = (
    "standard_dictionary",
    "field_standard_bindings",
    "cross_mappings",
    "migration_items",
    "connection_topology",
    "metadata_cache",
)

VALID_MERGE_STRATEGIES = frozenset({"supplement", "newer", "overwrite"})

# 仅读取旧版 connection_profiles 中的非敏感拓扑字段（绝不 SELECT user / password）
_TOPOLOGY_SAFE_COLS: Tuple[str, ...] = (
    "connection_id",
    "name",
    "driver_track",
    "db_type",
    "host",
    "port",
    "dbname",
    "default_schema",
    "sslmode",
    "auth_mode",
    "sqlite_path",
    "jdbc_driver_id",
    "jdbc_url",
    "jdbc_driver_class",
    "extra_props_json",
    "standard_format",
    "created_at",
    "updated_at",
)

_STD_DICT_COLS: Tuple[str, ...] = (
    "std_code",
    "reg_category",
    "cat_l1",
    "cat_l2",
    "cat_l3",
    "cn_name",
    "cn_alias",
    "en_name",
    "en_abbr",
    "biz_definition",
    "scope_desc",
    "biz_rule",
    "max_length",
    "min_scale",
    "ref_code_no",
    "ref_code_name",
    "ref_code_detail",
    "security_level",
    "ref_std_no",
    "ref_std_name",
    "domain_name",
    "domain_code",
    "data_category",
    "data_format",
    "value_range",
    "source_system",
    "ddm_catalog",
    "is_standard_dict",
    "version_no",
    "remark",
    "type_dws",
    "type_dw_mart",
    "type_mysql",
    "type_oracle",
    "type_dameng",
    "type_goldendb",
    "type_ob_oracle",
    "type_ob_mysql",
    "type_argodb",
    "type_oceanbase",
    "extra_attrs_json",
    "updated_at",
)


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_export_basename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return (cleaned or "DataOG_Workspace")[:64]


def _open_readonly_sqlite(file_path: str, *, immutable: bool = False) -> sqlite3.Connection:
    abs_p = os.path.abspath(file_path)
    qs = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"{Path(abs_p).as_uri()}?{qs}"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _checkpoint_sqlite_quietly(db_path: str) -> None:
    """若来源库处于 WAL 模式，先执行轻量 checkpoint 确保导出/克隆读取到最新提交."""
    if not db_path or not os.path.isfile(db_path):
        return
    try:
        with contextlib.closing(sqlite3.connect(db_path, timeout=10.0)) as ck_conn:
            ck_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except Exception:
        pass


def _get_table_names(conn: sqlite3.Connection) -> Set[str]:
    return {
        str(r["name"])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> Set[str]:
    return {
        str(r["name"])
        for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _sanitize_extra_props_json(raw_json: Any) -> str:
    try:
        parsed = json.loads(str(raw_json or "{}"))
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception:
        parsed = {}
    pub_props, _ = split_extra_props_secrets(parsed)
    return json.dumps(pub_props, ensure_ascii=False)


def _row_fingerprint(row_dict: Dict[str, Any]) -> str:
    return compute_connection_fingerprint(
        driver_track=str(row_dict.get("driver_track") or "NATIVE"),
        db_type=str(row_dict.get("db_type") or ""),
        host=str(row_dict.get("host") or ""),
        port=int(row_dict.get("port") or 0),
        dbname=str(row_dict.get("dbname") or ""),
        jdbc_url=str(row_dict.get("jdbc_url") or ""),
        sqlite_path=str(row_dict.get("sqlite_path") or ""),
    )


def _format_endpoint_label(row_dict: Dict[str, Any]) -> str:
    dt = str(row_dict.get("db_type") or "").upper()
    track = str(row_dict.get("driver_track") or "NATIVE").upper()
    if dt == "SQLITE":
        return str(row_dict.get("sqlite_path") or row_dict.get("dbname") or "SQLite")
    if track == "JDBC" and row_dict.get("jdbc_url"):
        return str(row_dict.get("jdbc_url"))
    host = str(row_dict.get("host") or "")
    port = int(row_dict.get("port") or 0)
    dbn = str(row_dict.get("dbname") or "")
    base = f"{host}:{port}" if port else host
    return f"{base}/{dbn}" if dbn else (base or dt)


def _read_whitelisted_meta(conn: sqlite3.Connection, tables: Set[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "workspace_meta" in tables:
        for r in conn.execute("SELECT key, value FROM workspace_meta").fetchall():
            k = str(r["key"] or "")
            if k in WORKSPACE_META_KEYS:
                try:
                    out[k] = json.loads(r["value"])
                except Exception:
                    out[k] = r["value"]
    if "app_meta" in tables:
        for r in conn.execute("SELECT key, value FROM app_meta").fetchall():
            k = str(r["key"] or "")
            if k in WORKSPACE_META_KEYS and k not in out:
                try:
                    out[k] = json.loads(r["value"])
                except Exception:
                    out[k] = r["value"]
    return out


def _read_safe_topology_rows(conn: sqlite3.Connection, tables: Set[str]) -> List[Dict[str, Any]]:
    """从来源库读取纯无密码连接拓扑（严禁查询 user / password 列；兼容旧版 connection_profiles 回退）."""
    candidates: List[str] = []
    if "connection_topology" in tables:
        candidates.append("connection_topology")
    if "connection_profiles" in tables:
        candidates.append("connection_profiles")
    if not candidates:
        return []

    for src_tbl in candidates:
        avail_cols = _get_table_columns(conn, src_tbl)
        select_cols = [c for c in _TOPOLOGY_SAFE_COLS if c in avail_cols]
        if "connection_id" not in select_cols or "name" not in select_cols:
            continue
        col_sql = ", ".join(select_cols)
        rows = conn.execute(f"SELECT {col_sql} FROM {src_tbl}").fetchall()
        if not rows:
            continue
        result: List[Dict[str, Any]] = []
        for r in rows:
            item: Dict[str, Any] = {
                "connection_id": str(r["connection_id"] or "").strip(),
                "name": str(r["name"] or "").strip(),
                "driver_track": str(r["driver_track"] if "driver_track" in select_cols else "NATIVE") or "NATIVE",
                "db_type": str(r["db_type"] if "db_type" in select_cols else "DWS") or "DWS",
                "host": str(r["host"] if "host" in select_cols else "") or "",
                "port": int((r["port"] if "port" in select_cols else 0) or 0),
                "dbname": str(r["dbname"] if "dbname" in select_cols else "") or "",
                "default_schema": str(r["default_schema"] if "default_schema" in select_cols else "public") or "public",
                "sslmode": str(r["sslmode"] if "sslmode" in select_cols else "prefer") or "prefer",
                "auth_mode": str(r["auth_mode"] if "auth_mode" in select_cols else "LDAP") or "LDAP",
                "sqlite_path": str(r["sqlite_path"] if "sqlite_path" in select_cols else "") or "",
                "jdbc_driver_id": str(r["jdbc_driver_id"] if "jdbc_driver_id" in select_cols else "") or "",
                "jdbc_url": str(r["jdbc_url"] if "jdbc_url" in select_cols else "") or "",
                "jdbc_driver_class": str(r["jdbc_driver_class"] if "jdbc_driver_class" in select_cols else "") or "",
                "extra_props_json": _sanitize_extra_props_json(
                    r["extra_props_json"] if "extra_props_json" in select_cols else "{}"
                ),
                "standard_format": str((r["standard_format"] if "standard_format" in select_cols else "AUTO") or "AUTO"),
                "created_at": str(r["created_at"] if "created_at" in select_cols else "") or "",
                "updated_at": str(r["updated_at"] if "updated_at" in select_cols else "") or "",
            }
            if item["connection_id"]:
                result.append(item)
        if result:
            return result
    return []


def _validate_sqlite_upload(filename: str, raw_bytes: bytes) -> None:
    if not raw_bytes:
        raise ValueError("上传的工作区文件为空")
    if len(raw_bytes) > MAX_WORKSPACE_UPLOAD_BYTES:
        raise ValueError("工作区文件体积超过 256 MB 安全上限")
    if len(raw_bytes) < 100 or not raw_bytes.startswith(SQLITE_MAGIC_HEADER):
        raise ValueError("文件格式无效：请上传有效的 Data.O.G 工作区文件 (.dataog.db) 或旧版状态库 (.db)")
    base = os.path.basename((filename or "").replace("\\", "/")).strip().lower()
    if base and not (base.endswith(".dataog.db") or base.endswith(".db") or base.endswith(".sqlite") or base.endswith(".sqlite3")):
        raise ValueError("不支持的文件后缀，请上传 .dataog.db 或 .db 格式文件")


def _extract_sanitized_workspace_to_file(
    src_conn: sqlite3.Connection,
    dst_file_path: str,
    *,
    include_metadata_cache: bool = True,
    workspace_name: Optional[str] = None,
    workspace_description: Optional[str] = None,
) -> Dict[str, int]:
    """将来源 SQLite 库中的白名单业务表净化写入全新的自包含 `.dataog.db` 文件（零私有表、零凭据）."""
    src_tables = _get_table_names(src_conn)
    recognized = {
        "workspace_meta",
        "connection_topology",
        "connection_profiles",
        "standard_dictionary",
        "field_standard_bindings",
        "cross_table_bindings",
        "cross_column_bindings",
        "migration_items",
        "meta_schemas",
        "meta_tables",
        "meta_columns",
    }
    if not (src_tables & recognized):
        raise ValueError("该 SQLite 文件不包含任何 Data.O.G 业务工作区表结构")

    os.makedirs(os.path.dirname(os.path.abspath(dst_file_path)), exist_ok=True)
    if os.path.exists(dst_file_path):
        os.remove(dst_file_path)

    dst_conn = sqlite3.connect(dst_file_path, timeout=15.0)
    dst_conn.row_factory = sqlite3.Row
    counts: Dict[str, int] = {}
    try:
        dst_conn.execute("PRAGMA journal_mode = DELETE;")
        dst_conn.execute("PRAGMA synchronous = NORMAL;")
        init_workspace_schema_on_conn(dst_conn)

        # 1. workspace_meta（严格仅保留业务元数据键）
        meta_map = _read_whitelisted_meta(src_conn, src_tables)
        meta_map["workspace_format_version"] = WORKSPACE_FORMAT_VERSION
        if workspace_name is not None:
            meta_map["workspace_name"] = workspace_name
        if workspace_description is not None:
            meta_map["workspace_description"] = workspace_description
        for k, v in meta_map.items():
            if k in WORKSPACE_META_KEYS:
                dst_conn.execute(
                    "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                    (k, json.dumps(v, ensure_ascii=False)),
                )

        # 2. connection_topology（严格无密码）
        topo_rows = _read_safe_topology_rows(src_conn, src_tables)
        if topo_rows:
            dst_conn.executemany(
                """
                INSERT OR REPLACE INTO connection_topology (
                    connection_id, name, driver_track, db_type,
                    host, port, dbname, default_schema,
                    sslmode, auth_mode, sqlite_path,
                    jdbc_driver_id, jdbc_url, jdbc_driver_class,
                    extra_props_json, standard_format, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    tuple(t[c] for c in _TOPOLOGY_SAFE_COLS)
                    for t in topo_rows
                ],
            )
        counts["connections_count"] = len(topo_rows)

        # 3. 其余白名单业务表
        copy_specs: List[Tuple[str, bool]] = [
            ("standard_dictionary", True),
            ("field_standard_bindings", True),
            ("cross_table_bindings", True),
            ("cross_column_bindings", True),
            ("migration_items", True),
            ("meta_schemas", include_metadata_cache),
            ("meta_tables", include_metadata_cache),
            ("meta_columns", include_metadata_cache),
        ]
        for tbl, should_copy in copy_specs:
            if not should_copy or tbl not in src_tables:
                counts[tbl] = 0
                continue
            dst_cols = _get_table_columns(dst_conn, tbl)
            rows = src_conn.execute(f"SELECT * FROM {tbl}").fetchall()
            if not rows:
                counts[tbl] = 0
                continue
            common = [c for c in rows[0].keys() if c in dst_cols and c != "id"]
            if not common:
                counts[tbl] = 0
                continue
            ph = ", ".join("?" for _ in common)
            col_sql = ", ".join(common)
            dst_conn.executemany(
                f"INSERT OR REPLACE INTO {tbl} ({col_sql}) VALUES ({ph})",
                [[r[c] for c in common] for r in rows],
            )
            counts[tbl] = len(rows)

        dst_conn.commit()
        dst_conn.execute("VACUUM;")
    finally:
        dst_conn.close()
    return counts


class WorkspaceManager:
    """管理本地托管工作区、零密钥导出与引导式增量合并."""

    def __init__(
        self,
        store: PlatformStateStore,
        connection_hub: ConnectionHub,
        metadata_hub: MetadataHub,
        *,
        workspaces_dir: str,
        tmp_dir: str,
    ) -> None:
        self.store = store
        self.connection_hub = connection_hub
        self.metadata_hub = metadata_hub
        self.workspaces_dir = os.path.realpath(os.path.abspath(workspaces_dir))
        self.tmp_dir = os.path.realpath(os.path.abspath(tmp_dir))
        os.makedirs(self.workspaces_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._staged_meta: Dict[str, Dict[str, Any]] = {}
        self.ensure_default_workspace()

    def _resolve_safe_workspace_path(self, file_name: str) -> str:
        clean = (file_name or "").strip()
        if not SAFE_WS_FILENAME_RE.match(clean):
            raise ValueError("非法的工作区文件标识")
        candidate = os.path.realpath(os.path.join(self.workspaces_dir, clean))
        if not candidate.startswith(self.workspaces_dir + os.sep):
            raise ValueError("工作区文件越界访问被拦截")
        return candidate

    def ensure_default_workspace(self) -> None:
        """确保默认工作区已注册并激活；若已存在用户切换的活动工作区则自动恢复."""
        with self._lock:
            records = self.store.list_workspace_records()
            if not records:
                ws_name = str(self.store.get_meta("workspace_name", "") or "").strip() or DEFAULT_WORKSPACE_NAME
                ws_desc = str(self.store.get_meta("workspace_description", "") or "").strip()
                self.store.upsert_workspace_record(
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    name=ws_name,
                    description=ws_desc,
                    file_name=DEFAULT_WORKSPACE_FILE,
                    is_default=True,
                )
                if not self.store.get_meta("active_workspace_id", ""):
                    self.store.save_meta("active_workspace_id", DEFAULT_WORKSPACE_ID)
                self.store.save_meta("workspace_format_version", WORKSPACE_FORMAT_VERSION)
                if not self.store.get_meta("workspace_name", ""):
                    self.store.save_meta("workspace_name", ws_name)

            active_id = str(self.store.get_meta("active_workspace_id", "") or "").strip()
            active_rec = self.store.get_workspace_record(active_id) if active_id else None
            if active_rec is None:
                all_recs = self.store.list_workspace_records()
                active_rec = all_recs[0] if all_recs else None
                if active_rec:
                    self.store.save_meta("active_workspace_id", active_rec["workspace_id"])

            # 确保 store 指向活动工作区文件（兼容分离存储模式，以及单库兼容模式下已切至非默认托管工作区后的重启恢复）
            if active_rec and (
                self.store.is_split_storage or active_rec["workspace_id"] != DEFAULT_WORKSPACE_ID
            ):
                target_path = self._resolve_safe_workspace_path(active_rec["file_name"])
                if (
                    self.store.is_split_storage or os.path.isfile(target_path)
                ) and os.path.abspath(self.store.workspace_db_path) != os.path.abspath(target_path):
                    self.store.switch_workspace_db(target_path)

    def _get_workspace_file_path_for_record(self, rec: Dict[str, Any]) -> str:
        """解析记录对应的工作区物理文件（兼容单库兼容模式下的默认工作区）."""
        active_id = str(self.store.get_meta("active_workspace_id", "") or "").strip()
        if not self.store.is_split_storage and rec["workspace_id"] == active_id:
            return self.store.workspace_db_path
        return self._resolve_safe_workspace_path(rec["file_name"])

    @staticmethod
    def _collect_db_stats(db_file: str) -> Dict[str, Any]:
        stats = {
            "standards_count": 0,
            "field_bindings_count": 0,
            "cross_tables_count": 0,
            "cross_columns_count": 0,
            "migration_items_count": 0,
            "connections_count": 0,
            "meta_tables_count": 0,
        }
        size_bytes = 0
        if not db_file or not os.path.isfile(db_file):
            return {"stats": stats, "size_bytes": 0}
        try:
            size_bytes = os.path.getsize(db_file)
            for suffix in ("-wal", "-shm"):
                side = db_file + suffix
                if os.path.isfile(side):
                    size_bytes += os.path.getsize(side)
        except OSError:
            pass
        try:
            with contextlib.closing(_open_readonly_sqlite(db_file)) as conn:
                tbls = _get_table_names(conn)
                mapping = (
                    ("standard_dictionary", "standards_count"),
                    ("field_standard_bindings", "field_bindings_count"),
                    ("cross_table_bindings", "cross_tables_count"),
                    ("cross_column_bindings", "cross_columns_count"),
                    ("migration_items", "migration_items_count"),
                    ("connection_topology", "connections_count"),
                    ("meta_tables", "meta_tables_count"),
                )
                for tbl, key in mapping:
                    if tbl in tbls:
                        row = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                        stats[key] = int(row[0]) if row else 0
        except Exception:
            pass
        return {"stats": stats, "size_bytes": size_bytes}

    def _format_public_workspace(self, rec: Dict[str, Any], active_id: str) -> Dict[str, Any]:
        """构建前端安全的工作区概要对象（严格不含任何物理路径或内部文件名）."""
        fpath = self._get_workspace_file_path_for_record(rec)
        metric = self._collect_db_stats(fpath)
        return {
            "workspace_id": rec["workspace_id"],
            "name": rec["name"],
            "description": rec.get("description") or "",
            "is_default": bool(rec.get("is_default")),
            "is_active": rec["workspace_id"] == active_id,
            "created_at": rec.get("created_at") or "",
            "updated_at": rec.get("updated_at") or "",
            "size_bytes": metric["size_bytes"],
            "stats": metric["stats"],
        }

    def list_workspaces(self) -> List[Dict[str, Any]]:
        self.ensure_default_workspace()
        active_id = str(self.store.get_meta("active_workspace_id", DEFAULT_WORKSPACE_ID) or DEFAULT_WORKSPACE_ID)
        return [
            self._format_public_workspace(rec, active_id)
            for rec in self.store.list_workspace_records()
        ]

    def get_active_workspace_summary(self) -> Dict[str, Any]:
        items = self.list_workspaces()
        for it in items:
            if it["is_active"]:
                return it
        return items[0] if items else {
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "name": DEFAULT_WORKSPACE_NAME,
            "description": "",
            "is_default": True,
            "is_active": True,
            "created_at": "",
            "updated_at": "",
            "size_bytes": 0,
            "stats": {
                "standards_count": 0,
                "field_bindings_count": 0,
                "cross_tables_count": 0,
                "cross_columns_count": 0,
                "migration_items_count": 0,
                "connections_count": 0,
                "meta_tables_count": 0,
            },
        }

    def create_workspace(
        self,
        *,
        name: str,
        description: str = "",
        clone_from_workspace_id: Optional[str] = None,
        switch_immediately: bool = True,
    ) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("工作区名称不能为空")
        if len(clean_name) > 80:
            raise ValueError("工作区名称不能超过 80 个字符")
        clean_desc = (description or "").strip()[:240]

        with self._lock:
            ws_id = f"ws-{uuid.uuid4().hex[:10]}"
            file_name = f"s_{uuid.uuid4().hex[:10]}.dat"
            dst_path = self._resolve_safe_workspace_path(file_name)

            if clone_from_workspace_id:
                src_rec = self.store.get_workspace_record(clone_from_workspace_id)
                if not src_rec:
                    raise LookupError("未找到要复制的来源工作区")
                src_path = self._get_workspace_file_path_for_record(src_rec)
                _checkpoint_sqlite_quietly(src_path)
                with contextlib.closing(_open_readonly_sqlite(src_path)) as sconn:
                    _extract_sanitized_workspace_to_file(
                        sconn,
                        dst_path,
                        include_metadata_cache=True,
                        workspace_name=clean_name,
                        workspace_description=clean_desc,
                    )
            else:
                conn = sqlite3.connect(dst_path, timeout=15.0)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA journal_mode = WAL;")
                    init_workspace_schema_on_conn(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                        ("workspace_format_version", json.dumps(WORKSPACE_FORMAT_VERSION)),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                        ("workspace_name", json.dumps(clean_name, ensure_ascii=False)),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                        ("workspace_description", json.dumps(clean_desc, ensure_ascii=False)),
                    )
                    conn.commit()
                finally:
                    conn.close()

            self.store.upsert_workspace_record(
                workspace_id=ws_id,
                name=clean_name,
                description=clean_desc,
                file_name=file_name,
                is_default=False,
            )
            self.store.add_log(
                "INFO",
                "工作区协作",
                f"已新建工作区 [{clean_name}]" + ("（基于现有工作区克隆）" if clone_from_workspace_id else "（空白工作区）"),
            )
            if switch_immediately:
                self.switch_workspace(ws_id)

        active_id = str(self.store.get_meta("active_workspace_id", DEFAULT_WORKSPACE_ID))
        rec = self.store.get_workspace_record(ws_id)
        return self._format_public_workspace(rec, active_id) if rec else {}

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            rec = self.store.get_workspace_record(workspace_id)
            if not rec:
                raise LookupError("未找到指定的工作区")
            new_name = rec["name"] if name is None else (name or "").strip()
            if not new_name:
                raise ValueError("工作区名称不能为空")
            if len(new_name) > 80:
                raise ValueError("工作区名称不能超过 80 个字符")
            new_desc = rec.get("description", "") if description is None else (description or "").strip()[:240]

            updated = self.store.upsert_workspace_record(
                workspace_id=workspace_id,
                name=new_name,
                description=new_desc,
                file_name=rec["file_name"],
                is_default=bool(rec.get("is_default")),
                created_at=rec.get("created_at") or "",
            )
            active_id = str(self.store.get_meta("active_workspace_id", DEFAULT_WORKSPACE_ID))
            if workspace_id == active_id:
                self.store.save_meta("workspace_name", new_name)
                self.store.save_meta("workspace_description", new_desc)
            else:
                fpath = self._get_workspace_file_path_for_record(updated)
                if os.path.isfile(fpath):
                    with self.store._lock, contextlib.closing(sqlite3.connect(fpath, timeout=15.0)) as wconn:
                        wconn.row_factory = sqlite3.Row
                        init_workspace_schema_on_conn(wconn)
                        wconn.execute(
                            "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                            ("workspace_name", json.dumps(new_name, ensure_ascii=False)),
                        )
                        wconn.execute(
                            "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                            ("workspace_description", json.dumps(new_desc, ensure_ascii=False)),
                        )
                        wconn.commit()
        return self._format_public_workspace(updated, active_id)

    def switch_workspace(self, workspace_id: str) -> Dict[str, Any]:
        """热切换当前活动工作区，立即清空温连接池与元数据负缓存，私有库（激活码/AI 密钥/DB 密码/壁纸）原样保留."""
        with self._lock:
            rec = self.store.get_workspace_record(workspace_id)
            if not rec:
                raise LookupError("未找到指定的工作区")
            # 若当前处于单库兼容模式且切回默认工作区，保留兼容路径；否则切至托管文件
            if workspace_id == DEFAULT_WORKSPACE_ID and not self.store.is_split_storage:
                target_path = self.store.workspace_db_path
            else:
                target_path = self._resolve_safe_workspace_path(rec["file_name"])
                # 若是单库兼容模式下首次切出默认工作区，先把默认工作区业务数据安全快照到托管默认工作区文件
                if not self.store.is_split_storage and workspace_id != DEFAULT_WORKSPACE_ID:
                    def_path = self._resolve_safe_workspace_path(DEFAULT_WORKSPACE_FILE)
                    if not os.path.isfile(def_path):
                        _checkpoint_sqlite_quietly(self.store.workspace_db_path)
                        with contextlib.closing(_open_readonly_sqlite(self.store.workspace_db_path)) as sconn:
                            _extract_sanitized_workspace_to_file(
                                sconn,
                                def_path,
                                include_metadata_cache=True,
                            )

            self.store.switch_workspace_db(target_path)
            self.store.save_meta("active_workspace_id", workspace_id)
            self.connection_hub.close_warm_connections()
            self.metadata_hub.invalidate_negative_cache()
            self.store.add_log("INFO", "工作区协作", f"已切换至工作区 [{rec['name']}]")

        return self.get_active_workspace_summary()

    def delete_workspace(self, workspace_id: str) -> bool:
        with self._lock:
            active_id = str(self.store.get_meta("active_workspace_id", DEFAULT_WORKSPACE_ID))
            if workspace_id == active_id:
                raise ValueError("不能删除当前正在使用的工作区，请先切换至其他工作区")
            records = self.store.list_workspace_records()
            if len(records) <= 1:
                raise ValueError("至少需要保留一个本地工作区")
            rec = self.store.get_workspace_record(workspace_id)
            if not rec:
                raise LookupError("未找到要删除的工作区")
            fpath = self._resolve_safe_workspace_path(rec["file_name"])
            self.store.delete_workspace_record(workspace_id)
            for p in (fpath, fpath + "-wal", fpath + "-shm"):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self.store.add_log("INFO", "工作区协作", f"已删除工作区 [{rec['name']}]")
            return True

    def export_workspace_bytes(
        self,
        workspace_id: Optional[str] = None,
        *,
        include_metadata_cache: bool = True,
    ) -> Tuple[bytes, str]:
        """导出零密钥、自包含的 `.dataog.db` 团队共享工作区二进制流."""
        with self._lock:
            self.ensure_default_workspace()
            active_id = str(self.store.get_meta("active_workspace_id", DEFAULT_WORKSPACE_ID))
            target_id = (workspace_id or "").strip() or active_id
            rec = self.store.get_workspace_record(target_id)
            if not rec:
                raise LookupError("未找到要导出的工作区")
            src_path = self._get_workspace_file_path_for_record(rec)

            _checkpoint_sqlite_quietly(src_path)

            fd, tmp_export_path = tempfile.mkstemp(prefix="exp_", suffix=".bin", dir=self.tmp_dir)
            os.close(fd)
            try:
                with contextlib.closing(_open_readonly_sqlite(src_path)) as sconn:
                    _extract_sanitized_workspace_to_file(
                        sconn,
                        tmp_export_path,
                        include_metadata_cache=bool(include_metadata_cache),
                        workspace_name=rec["name"],
                        workspace_description=rec.get("description") or "",
                    )
                with open(tmp_export_path, "rb") as fh:
                    data = fh.read()
            finally:
                for p in (tmp_export_path, tmp_export_path + "-wal", tmp_export_path + "-shm"):
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

            date_tag = datetime.datetime.now().strftime("%Y%m%d")
            dl_name = f"{_safe_export_basename(rec['name'])}_{date_tag}.dataog.db"
            self.store.add_log(
                "INFO",
                "工作区协作",
                f"已导出共享工作区 [{rec['name']}] ({len(data)} bytes，零密钥安全过滤"
                + ("，含元数据缓存" if include_metadata_cache else "，不含元数据缓存")
                + ")",
            )
            return data, dl_name

    def import_workspace_as_new(
        self,
        *,
        filename: str,
        raw_bytes: bytes,
        name: str = "",
        description: str = "",
        switch_immediately: bool = True,
    ) -> Dict[str, Any]:
        """导入同事分享的 `.dataog.db` 或旧版 `dataog_state.db` 为独立新工作区（严格仅提取白名单业务表）."""
        _validate_sqlite_upload(filename, raw_bytes)

        fd, tmp_upload_path = tempfile.mkstemp(prefix="ws_imp_", suffix=".bin", dir=self.tmp_dir)
        os.close(fd)
        try:
            with open(tmp_upload_path, "wb") as fh:
                fh.write(raw_bytes)

            with self._lock:
                with contextlib.closing(_open_readonly_sqlite(tmp_upload_path, immutable=True)) as sconn:
                    src_tables = _get_table_names(sconn)
                    src_meta = _read_whitelisted_meta(sconn, src_tables)
                    default_base = os.path.basename((filename or "").replace("\\", "/"))
                    for ext in (".dataog.db", ".sqlite3", ".sqlite", ".db"):
                        if default_base.lower().endswith(ext):
                            default_base = default_base[: -len(ext)]
                            break
                    eff_name = (
                        (name or "").strip()
                        or str(src_meta.get("workspace_name") or "").strip()
                        or default_base.strip()
                        or f"导入工作区_{datetime.datetime.now().strftime('%m%d_%H%M')}"
                    )[:80]
                    eff_desc = (
                        (description or "").strip()
                        or str(src_meta.get("workspace_description") or "").strip()
                    )[:240]

                    ws_id = f"ws-{uuid.uuid4().hex[:10]}"
                    file_name = f"s_{uuid.uuid4().hex[:10]}.dat"
                    dst_path = self._resolve_safe_workspace_path(file_name)
                    counts = _extract_sanitized_workspace_to_file(
                        sconn,
                        dst_path,
                        include_metadata_cache=True,
                        workspace_name=eff_name,
                        workspace_description=eff_desc,
                    )
                    topo_rows = _read_safe_topology_rows(sconn, src_tables)

                self.store.upsert_workspace_record(
                    workspace_id=ws_id,
                    name=eff_name,
                    description=eff_desc,
                    file_name=file_name,
                    is_default=False,
                )

                # 计算导入连接与本机个人凭据库的自动匹配统计
                cred_by_cid, cred_by_fp, _ = self.store._load_credential_maps()
                auto_matched = 0
                needs_creds = 0
                for t in topo_rows:
                    if str(t.get("db_type") or "").upper() == "SQLITE":
                        continue
                    cid = t["connection_id"]
                    fp = _row_fingerprint(t)
                    if (cid in cred_by_cid and (cred_by_cid[cid].get("user") or cred_by_cid[cid].get("password"))) or (
                        fp and fp in cred_by_fp
                    ):
                        auto_matched += 1
                    else:
                        needs_creds += 1

                if switch_immediately:
                    self.switch_workspace(ws_id)

                self.store.add_log(
                    "INFO",
                    "工作区协作",
                    f"已安全导入工作区 [{eff_name}]（标准 {counts.get('standard_dictionary', 0)} 条，"
                    f"绑标 {counts.get('field_standard_bindings', 0)} 条，跨环境表对 {counts.get('cross_table_bindings', 0)} 个）",
                )

                active_id = str(self.store.get_meta("active_workspace_id", DEFAULT_WORKSPACE_ID))
                rec = self.store.get_workspace_record(ws_id)
                return {
                    "workspace": self._format_public_workspace(rec, active_id) if rec else {},
                    "credential_summary": {
                        "total_connections": len(topo_rows),
                        "auto_matched_count": auto_matched,
                        "needs_credentials_count": needs_creds,
                    },
                }
        finally:
            if os.path.isfile(tmp_upload_path):
                try:
                    os.remove(tmp_upload_path)
                except OSError:
                    pass

    # =========================================================================
    # 引导式增量合并同事工作区进度 (Inspect & Atomic Transactional Execute)
    # =========================================================================

    def _cleanup_expired_staged_files(self) -> None:
        now = time.time()
        expired_tokens: List[str] = []
        for tok, meta in list(self._staged_meta.items()):
            if (now - float(meta.get("created_ts", 0))) > MERGE_STAGE_TTL_SEC:
                expired_tokens.append(tok)
        for tok in expired_tokens:
            self._remove_staged_token(tok)
        # 限制同时暂存数量，淘汰最旧
        while len(self._staged_meta) > MAX_STAGED_MERGE_FILES:
            oldest = min(self._staged_meta.items(), key=lambda kv: float(kv[1].get("created_ts", 0)))[0]
            self._remove_staged_token(oldest)

    def _staged_file_path(self, token: str) -> str:
        clean = (token or "").strip().lower()
        if not SAFE_TOKEN_RE.match(clean):
            raise ValueError("无效或已过期的合并预览凭证，请重新上传文件")
        cand = os.path.realpath(os.path.join(self.tmp_dir, f"merge_{clean}.bin"))
        if not cand.startswith(self.tmp_dir + os.sep):
            raise ValueError("合并暂存路径校验失败")
        return cand

    def _remove_staged_token(self, token: str) -> None:
        self._staged_meta.pop(token, None)
        try:
            p = self._staged_file_path(token)
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

    def _build_connection_id_map(
        self,
        src_topos: List[Dict[str, Any]],
        local_topos: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, str], List[Dict[str, Any]], int, int]:
        """构建来源 connection_id -> 本地 connection_id 映射表（优先精确 ID，其次端点指纹自动对齐）."""
        local_by_cid = {t["connection_id"]: t for t in local_topos}
        local_by_fp: Dict[str, Dict[str, Any]] = {}
        for t in local_topos:
            fp = _row_fingerprint(t)
            if fp and fp not in local_by_fp:
                local_by_fp[fp] = t

        cred_by_cid, cred_by_fp, _ = self.store._load_credential_maps()
        conn_id_map: Dict[str, str] = {}
        preview: List[Dict[str, Any]] = []
        auto_cred_matched = 0
        needs_cred = 0

        for src in src_topos:
            scid = src["connection_id"]
            sfp = _row_fingerprint(src)
            is_sqlite = str(src.get("db_type") or "").upper() == "SQLITE"

            if scid in local_by_cid:
                mapped_cid = scid
                action = "MATCH_ID"
                cred_match = "EXISTING"
            elif sfp and sfp in local_by_fp:
                mapped_cid = local_by_fp[sfp]["connection_id"]
                action = "MAP_EXISTING"
                cred_match = "EXISTING"
            else:
                mapped_cid = scid
                action = "ADD"
                if is_sqlite:
                    cred_match = "SQLITE_LOCAL"
                elif (scid in cred_by_cid and (cred_by_cid[scid].get("user") or cred_by_cid[scid].get("password"))) or (
                    sfp and sfp in cred_by_fp
                ):
                    cred_match = "AUTO_MATCHED"
                    auto_cred_matched += 1
                else:
                    cred_match = "NEEDS_INPUT"
                    needs_cred += 1

            conn_id_map[scid] = mapped_cid
            preview.append(
                {
                    "source_connection_id": scid,
                    "mapped_connection_id": mapped_cid,
                    "name": src["name"],
                    "mapped_local_name": local_by_cid[mapped_cid]["name"] if mapped_cid in local_by_cid else "",
                    "db_type": src["db_type"],
                    "endpoint": _format_endpoint_label(src),
                    "standard_format": src.get("standard_format") or "AUTO",
                    "action": action,
                    "credential_match": cred_match,
                }
            )
        return conn_id_map, preview, auto_cred_matched, needs_cred

    @staticmethod
    def _col_sig(row: Dict[str, Any]) -> Tuple[str, str, str]:
        bs = str(row.get("bound_source_col") or "").strip().lower()
        bt = str(row.get("bound_target_col") or "").strip().lower()
        sc = str(row.get("std_col_name") or "").strip().lower()
        if sc.startswith("__unpaired_"):
            return (bs, bt, "")
        return (bs, bt, sc)

    def inspect_merge_source(self, *, filename: str, raw_bytes: bytes) -> Dict[str, Any]:
        """预检同事分享的工作区文件，生成各模块冲突统计、跨环境表对差异预览及连接映射预览."""
        _validate_sqlite_upload(filename, raw_bytes)

        with self._lock:
            self._cleanup_expired_staged_files()
            token = uuid.uuid4().hex
            staged_path = self._staged_file_path(token)
            with open(staged_path, "wb") as fh:
                fh.write(raw_bytes)

            try:
                with contextlib.closing(_open_readonly_sqlite(staged_path, immutable=True)) as sconn:
                    src_tables = _get_table_names(sconn)
                    recognized = {
                        "workspace_meta",
                        "connection_topology",
                        "connection_profiles",
                        "standard_dictionary",
                        "field_standard_bindings",
                        "cross_table_bindings",
                        "cross_column_bindings",
                        "migration_items",
                        "meta_schemas",
                        "meta_tables",
                        "meta_columns",
                    }
                    if not (src_tables & recognized):
                        raise ValueError("上传的 SQLite 文件不包含有效的 Data.O.G 业务数据表")

                    is_legacy = "connection_profiles" in src_tables or (
                        "app_meta" in src_tables and "workspace_meta" not in src_tables
                    )
                    src_meta = _read_whitelisted_meta(sconn, src_tables)
                    src_topos = _read_safe_topology_rows(sconn, src_tables)

                    with self.store.module_db("workspace") as lconn:
                        local_tables = _get_table_names(lconn)
                        local_topos = _read_safe_topology_rows(lconn, local_tables)
                        conn_id_map, conn_preview, auto_cred_matched, needs_cred = (
                            self._build_connection_id_map(src_topos, local_topos)
                        )

                        # 1. standard_dictionary diff
                        sd_info = self._inspect_standard_dict(sconn, src_tables, lconn)

                        # 2. field_standard_bindings diff
                        fsb_info = self._inspect_field_bindings(sconn, src_tables, lconn, conn_id_map)

                        # 3. cross_mappings diff + table-pair preview
                        cross_info, table_diffs = self._inspect_cross_mappings(sconn, src_tables, lconn)

                        # 4. migration_items diff
                        mig_info = self._inspect_migration_items(sconn, src_tables, lconn)

                        # 5. connection_topology summary
                        topo_new = sum(1 for c in conn_preview if c["action"] == "ADD")
                        topo_matched = len(conn_preview) - topo_new
                        topo_info = {
                            "source_count": len(src_topos),
                            "local_count": len(local_topos),
                            "new_count": topo_new,
                            "conflict_count": topo_matched,
                            "same_count": topo_matched,
                            "matched_existing_count": topo_matched,
                            "auto_cred_matched_count": auto_cred_matched,
                            "needs_cred_count": needs_cred,
                        }

                        # 6. metadata_cache diff
                        meta_info = self._inspect_metadata_cache(sconn, src_tables, lconn, conn_id_map)

                self._staged_meta[token] = {
                    "filename": os.path.basename((filename or "workspace.dataog.db").replace("\\", "/")),
                    "created_ts": time.time(),
                    "file_size": len(raw_bytes),
                }

                return {
                    "merge_token": token,
                    "source_info": {
                        "filename": self._staged_meta[token]["filename"],
                        "source_kind": "LEGACY_STATE_DB" if is_legacy else "WORKSPACE",
                        "source_kind_label": "旧版状态库 (.db，已自动隔离私密表)" if is_legacy else "团队共享工作区 (.dataog.db)",
                        "workspace_name": str(src_meta.get("workspace_name") or "").strip(),
                        "format_version": str(src_meta.get("workspace_format_version") or ("<2.7" if is_legacy else WORKSPACE_FORMAT_VERSION)),
                        "file_size": len(raw_bytes),
                    },
                    "security_notice": "安全隔离模式：仅读取白名单业务表，已彻底跳过对方激活码、AI 密钥、数据库账号密码与个人日志。",
                    "modules": {
                        "standard_dictionary": sd_info,
                        "field_standard_bindings": fsb_info,
                        "cross_mappings": cross_info,
                        "migration_items": mig_info,
                        "connection_topology": topo_info,
                        "metadata_cache": meta_info,
                    },
                    "table_diffs": table_diffs[:300],
                    "table_diffs_total": len(table_diffs),
                    "connections_preview": conn_preview,
                }
            except Exception:
                self._remove_staged_token(token)
                raise

    @staticmethod
    def _inspect_standard_dict(
        sconn: sqlite3.Connection, src_tables: Set[str], lconn: sqlite3.Connection
    ) -> Dict[str, int]:
        local_map: Dict[str, Dict[str, Any]] = {}
        for r in lconn.execute("SELECT std_code, cn_name, en_abbr, type_dws, data_format, updated_at FROM standard_dictionary").fetchall():
            local_map[str(r["std_code"])] = dict(r)
        if "standard_dictionary" not in src_tables:
            return {"source_count": 0, "local_count": len(local_map), "new_count": 0, "conflict_count": 0, "same_count": 0, "newer_in_source_count": 0}
        scols = _get_table_columns(sconn, "standard_dictionary")
        if "std_code" not in scols:
            return {"source_count": 0, "local_count": len(local_map), "new_count": 0, "conflict_count": 0, "same_count": 0, "newer_in_source_count": 0}
        srows = sconn.execute("SELECT * FROM standard_dictionary").fetchall()
        new_cnt = conf_cnt = same_cnt = newer_cnt = 0
        for r in srows:
            code = str(r["std_code"] or "").strip()
            if not code:
                continue
            loc = local_map.get(code)
            if loc is None:
                new_cnt += 1
                continue
            rk = set(r.keys())
            diff = (
                str(r["cn_name"] if "cn_name" in rk else "") != str(loc.get("cn_name") or "")
                or str(r["en_abbr"] if "en_abbr" in rk else "") != str(loc.get("en_abbr") or "")
                or str(r["type_dws"] if "type_dws" in rk else "") != str(loc.get("type_dws") or "")
                or str(r["data_format"] if "data_format" in rk else "") != str(loc.get("data_format") or "")
            )
            if diff:
                conf_cnt += 1
                s_upd = str(r["updated_at"] if "updated_at" in rk else "")
                if s_upd and s_upd > str(loc.get("updated_at") or ""):
                    newer_cnt += 1
            else:
                same_cnt += 1
        return {
            "source_count": len(srows),
            "local_count": len(local_map),
            "new_count": new_cnt,
            "conflict_count": conf_cnt,
            "same_count": same_cnt,
            "newer_in_source_count": newer_cnt,
        }

    @staticmethod
    def _inspect_field_bindings(
        sconn: sqlite3.Connection,
        src_tables: Set[str],
        lconn: sqlite3.Connection,
        conn_id_map: Dict[str, str],
    ) -> Dict[str, int]:
        local_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in lconn.execute(
            "SELECT connection_id, col_key, std_code, updated_at FROM field_standard_bindings"
        ).fetchall():
            k = (str(r["connection_id"]), str(r["col_key"] or "").lower())
            local_map[k] = dict(r)
        if "field_standard_bindings" not in src_tables:
            return {"source_count": 0, "local_count": len(local_map), "new_count": 0, "conflict_count": 0, "same_count": 0, "newer_in_source_count": 0}
        srows = sconn.execute("SELECT * FROM field_standard_bindings").fetchall()
        new_cnt = conf_cnt = same_cnt = newer_cnt = 0
        for r in srows:
            cid = conn_id_map.get(str(r["connection_id"] or ""), str(r["connection_id"] or ""))
            ckey = str(r["col_key"] or "").lower()
            if not cid or not ckey:
                continue
            s_code = str(r["std_code"] or "").strip()
            loc = local_map.get((cid, ckey))
            if loc is None:
                new_cnt += 1
                continue
            l_code = str(loc.get("std_code") or "").strip()
            if not l_code and s_code:
                new_cnt += 1
            elif s_code.lower() == l_code.lower():
                same_cnt += 1
            else:
                conf_cnt += 1
                s_upd = str(r["updated_at"] or "")
                if s_code and s_upd and s_upd > str(loc.get("updated_at") or ""):
                    newer_cnt += 1
        return {
            "source_count": len(srows),
            "local_count": len(local_map),
            "new_count": new_cnt,
            "conflict_count": conf_cnt,
            "same_count": same_cnt,
            "newer_in_source_count": newer_cnt,
        }

    def _inspect_cross_mappings(
        self,
        sconn: sqlite3.Connection,
        src_tables: Set[str],
        lconn: sqlite3.Connection,
    ) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
        local_tbls = {
            str(r["std_table_key"]).lower(): dict(r)
            for r in lconn.execute("SELECT * FROM cross_table_bindings").fetchall()
        }
        local_cols_by_tbl: Dict[str, List[Dict[str, Any]]] = {}
        local_col_total = 0
        for r in lconn.execute("SELECT * FROM cross_column_bindings ORDER BY row_index ASC, id ASC").fetchall():
            local_col_total += 1
            tk = str(r["std_table_key"] or "").lower()
            local_cols_by_tbl.setdefault(tk, []).append(dict(r))

        if "cross_table_bindings" not in src_tables:
            return (
                {
                    "source_tables_count": 0,
                    "local_tables_count": len(local_tbls),
                    "source_columns_count": 0,
                    "local_columns_count": local_col_total,
                    "new_count": 0,
                    "conflict_count": 0,
                    "same_count": 0,
                    "newer_in_source_count": 0,
                },
                [],
            )

        src_tbls = [dict(r) for r in sconn.execute("SELECT * FROM cross_table_bindings ORDER BY id ASC").fetchall()]
        src_cols_by_tbl: Dict[str, List[Dict[str, Any]]] = {}
        src_col_total = 0
        if "cross_column_bindings" in src_tables:
            for r in sconn.execute("SELECT * FROM cross_column_bindings ORDER BY row_index ASC, id ASC").fetchall():
                src_col_total += 1
                tk = str(r["std_table_key"] or "").lower()
                src_cols_by_tbl.setdefault(tk, []).append(dict(r))

        new_cnt = conf_cnt = same_cnt = newer_cnt = 0
        diffs: List[Dict[str, Any]] = []
        for st in src_tbls:
            tk = str(st["std_table_key"] or "").lower()
            if not tk:
                continue
            scols = src_cols_by_tbl.get(tk, [])
            lt = local_tbls.get(tk)
            lcols = local_cols_by_tbl.get(tk, [])
            src_ref = f"{st.get('source_schema') or ''}.{st.get('source_table') or ''}".strip(".")
            tgt_ref = f"{st.get('target_schema') or ''}.{st.get('target_table') or ''}".strip(".")
            s_max_upd = max([str(st.get("updated_at") or ""), *(str(c.get("updated_at") or "") for c in scols)])

            if lt is None:
                new_cnt += 1
                diffs.append(
                    {
                        "std_table_key": tk,
                        "source_table_ref": src_ref,
                        "target_table_ref": tgt_ref,
                        "local_source_table_ref": "",
                        "local_target_table_ref": "",
                        "status": "NEW",
                        "source_col_count": len(scols),
                        "local_col_count": 0,
                        "added_cols_count": len(scols),
                        "changed_cols_count": 0,
                        "source_updated_at": s_max_upd,
                        "local_updated_at": "",
                        "source_is_newer": True,
                    }
                )
                continue

            l_src_ref = f"{lt.get('source_schema') or ''}.{lt.get('source_table') or ''}".strip(".")
            l_tgt_ref = f"{lt.get('target_schema') or ''}.{lt.get('target_table') or ''}".strip(".")
            l_max_upd = max([str(lt.get("updated_at") or ""), *(str(c.get("updated_at") or "") for c in lcols)])
            source_is_newer = bool(s_max_upd and s_max_upd > l_max_upd)

            # 比较列级映射差异
            local_by_sig = {self._col_sig(c): c for c in lcols}
            added_cols = 0
            changed_cols = 0
            for sc in scols:
                sig = self._col_sig(sc)
                lc = local_by_sig.get(sig)
                if lc is None:
                    added_cols += 1
                else:
                    if (
                        str(sc.get("source_std_code") or "").strip().lower()
                        != str(lc.get("source_std_code") or "").strip().lower()
                        or str(sc.get("target_std_code") or "").strip().lower()
                        != str(lc.get("target_std_code") or "").strip().lower()
                    ):
                        changed_cols += 1

            tbl_pair_diff = (
                src_ref.lower() != l_src_ref.lower() or tgt_ref.lower() != l_tgt_ref.lower()
            )
            if tbl_pair_diff or added_cols > 0 or changed_cols > 0 or len(scols) != len(lcols):
                status = "CONFLICT"
                conf_cnt += 1
                if source_is_newer:
                    newer_cnt += 1
            else:
                status = "SAME"
                same_cnt += 1

            diffs.append(
                {
                    "std_table_key": tk,
                    "source_table_ref": src_ref,
                    "target_table_ref": tgt_ref,
                    "local_source_table_ref": l_src_ref,
                    "local_target_table_ref": l_tgt_ref,
                    "status": status,
                    "source_col_count": len(scols),
                    "local_col_count": len(lcols),
                    "added_cols_count": added_cols,
                    "changed_cols_count": changed_cols,
                    "source_updated_at": s_max_upd,
                    "local_updated_at": l_max_upd,
                    "source_is_newer": source_is_newer,
                }
            )

        # 优先展示 CONFLICT 与 NEW 便于审核
        order = {"CONFLICT": 0, "NEW": 1, "SAME": 2}
        diffs.sort(key=lambda d: (order.get(d["status"], 9), d["std_table_key"]))

        return (
            {
                "source_tables_count": len(src_tbls),
                "local_tables_count": len(local_tbls),
                "source_columns_count": src_col_total,
                "local_columns_count": local_col_total,
                "new_count": new_cnt,
                "conflict_count": conf_cnt,
                "same_count": same_cnt,
                "newer_in_source_count": newer_cnt,
            },
            diffs,
        )

    @staticmethod
    def _inspect_migration_items(
        sconn: sqlite3.Connection, src_tables: Set[str], lconn: sqlite3.Connection
    ) -> Dict[str, int]:
        def _mkey(r: Any) -> Tuple[str, str, str, str]:
            return (
                str(r["schema_name"] or "").lower(),
                str(r["table_name"] or "").lower(),
                str(r["old_col_name"] or "").lower(),
                str(r["new_col_name"] or "").lower(),
            )

        local_map = {
            _mkey(r): dict(r)
            for r in lconn.execute("SELECT * FROM migration_items").fetchall()
        }
        if "migration_items" not in src_tables:
            return {"source_count": 0, "local_count": len(local_map), "new_count": 0, "conflict_count": 0, "same_count": 0, "newer_in_source_count": 0}
        srows = sconn.execute("SELECT * FROM migration_items").fetchall()
        new_cnt = conf_cnt = same_cnt = newer_cnt = 0
        for r in srows:
            k = _mkey(r)
            loc = local_map.get(k)
            if loc is None:
                new_cnt += 1
                continue
            diff = (
                str(r["new_col_type"] or "") != str(loc.get("new_col_type") or "")
                or str(r["new_col_comment"] or "") != str(loc.get("new_col_comment") or "")
                or str(r["status"] or "") != str(loc.get("status") or "")
            )
            if diff:
                conf_cnt += 1
                if str(r["updated_at"] or "") > str(loc.get("updated_at") or ""):
                    newer_cnt += 1
            else:
                same_cnt += 1
        return {
            "source_count": len(srows),
            "local_count": len(local_map),
            "new_count": new_cnt,
            "conflict_count": conf_cnt,
            "same_count": same_cnt,
            "newer_in_source_count": newer_cnt,
        }

    @staticmethod
    def _inspect_metadata_cache(
        sconn: sqlite3.Connection,
        src_tables: Set[str],
        lconn: sqlite3.Connection,
        conn_id_map: Dict[str, str],
    ) -> Dict[str, int]:
        local_map = {
            (str(r["connection_id"]), str(r["schema_name"]).lower(), str(r["table_name"]).lower()): dict(r)
            for r in lconn.execute(
                "SELECT connection_id, schema_name, table_name, columns_cached, refreshed_at FROM meta_tables"
            ).fetchall()
        }
        if "meta_tables" not in src_tables:
            return {"source_count": 0, "local_count": len(local_map), "new_count": 0, "conflict_count": 0, "same_count": 0, "newer_in_source_count": 0}
        srows = sconn.execute("SELECT connection_id, schema_name, table_name, columns_cached, refreshed_at FROM meta_tables").fetchall()
        new_cnt = conf_cnt = same_cnt = newer_cnt = 0
        for r in srows:
            cid = conn_id_map.get(str(r["connection_id"]), str(r["connection_id"]))
            k = (cid, str(r["schema_name"]).lower(), str(r["table_name"]).lower())
            loc = local_map.get(k)
            if loc is None:
                new_cnt += 1
            elif int(loc.get("columns_cached") or 0) == 0 and int(r["columns_cached"] or 0) != 0:
                new_cnt += 1
            elif str(r["refreshed_at"] or "") != str(loc.get("refreshed_at") or ""):
                conf_cnt += 1
                if str(r["refreshed_at"] or "") > str(loc.get("refreshed_at") or ""):
                    newer_cnt += 1
            else:
                same_cnt += 1
        return {
            "source_count": len(srows),
            "local_count": len(local_map),
            "new_count": new_cnt,
            "conflict_count": conf_cnt,
            "same_count": same_cnt,
            "newer_in_source_count": newer_cnt,
        }

    def execute_merge(
        self,
        *,
        merge_token: str,
        selected_modules: Sequence[str],
        strategy: str = "supplement",
    ) -> Dict[str, Any]:
        """按勾选模块与冲突策略，在单事务内将同事工作区增量合并至当前活动工作区（绝不触碰私有库）."""
        eff_strategy = (strategy or "supplement").strip().lower()
        if eff_strategy not in VALID_MERGE_STRATEGIES:
            raise ValueError(f"不支持的合并冲突策略: {strategy}")

        chosen_set = {str(m).strip() for m in (selected_modules or []) if str(m).strip() in ALL_MERGE_MODULES}
        if not chosen_set:
            raise ValueError("请至少勾选一个要合并的业务模块")

        with self._lock:
            staged_path = self._staged_file_path(merge_token)
            if not os.path.isfile(staged_path):
                raise ValueError("合并预览凭证已失效或已使用，请重新上传同事的工作区文件")
            meta_entry = self._staged_meta.get(merge_token, {})
            src_filename = str(meta_entry.get("filename") or "workspace.dataog.db")

            details: Dict[str, Dict[str, int]] = {
                m: {"added": 0, "updated": 0, "skipped": 0} for m in ALL_MERGE_MODULES
            }

            try:
                with contextlib.closing(_open_readonly_sqlite(staged_path, immutable=True)) as sconn:
                    src_tables = _get_table_names(sconn)
                    src_meta = _read_whitelisted_meta(sconn, src_tables)
                    src_topos = _read_safe_topology_rows(sconn, src_tables)

                    with self.store.module_db("workspace") as lconn:
                        local_tables = _get_table_names(lconn)
                        local_topos = _read_safe_topology_rows(lconn, local_tables)
                        conn_id_map, _, _, _ = self._build_connection_id_map(src_topos, local_topos)

                        if "connection_topology" in chosen_set:
                            details["connection_topology"] = self._merge_connection_topology(
                                src_topos, local_topos, conn_id_map, lconn, eff_strategy
                            )

                        if "standard_dictionary" in chosen_set and "standard_dictionary" in src_tables:
                            details["standard_dictionary"] = self._merge_standard_dict(
                                sconn, lconn, eff_strategy
                            )

                        if "field_standard_bindings" in chosen_set and "field_standard_bindings" in src_tables:
                            details["field_standard_bindings"] = self._merge_field_bindings(
                                sconn, lconn, conn_id_map, eff_strategy
                            )

                        if "cross_mappings" in chosen_set and "cross_table_bindings" in src_tables:
                            details["cross_mappings"] = self._merge_cross_mappings(
                                sconn, src_tables, lconn, eff_strategy
                            )

                        if "migration_items" in chosen_set and "migration_items" in src_tables:
                            details["migration_items"] = self._merge_migration_items(
                                sconn, lconn, eff_strategy
                            )

                        if "metadata_cache" in chosen_set:
                            details["metadata_cache"] = self._merge_metadata_cache(
                                sconn, src_tables, lconn, conn_id_map, eff_strategy
                            )

                        # 仅当本地工作区元信息为空时按已勾选模块顺带补充来源业务元信息（按连接映射修正 connection_id 且校验拓扑存在性）
                        self._supplement_workspace_meta(lconn, src_meta, conn_id_map, chosen_set)

                # 合并成功后再清理暂存文件与缓存
                self._remove_staged_token(merge_token)
                self.metadata_hub.invalidate_negative_cache()
            except Exception:
                raise

            total_added = sum(d["added"] for d in details.values())
            total_updated = sum(d["updated"] for d in details.values())
            total_skipped = sum(d["skipped"] for d in details.values())

            strategy_labels = {
                "supplement": "仅补充缺失/未绑定",
                "newer": "按更新时间保留较新",
                "overwrite": "覆盖冲突项",
            }
            self.store.add_log(
                "INFO",
                "工作区协作",
                f"已合并同事工作区 [{src_filename}]（策略：{strategy_labels[eff_strategy]}）："
                f"新增 {total_added} 项、更新 {total_updated} 项、跳过 {total_skipped} 项",
            )

            return {
                "strategy": eff_strategy,
                "selected_modules": sorted(chosen_set),
                "summary": {
                    "added": total_added,
                    "updated": total_updated,
                    "skipped": total_skipped,
                },
                "details": details,
                "active_workspace": self.get_active_workspace_summary(),
                "workspaces": self.list_workspaces(),
            }

    @staticmethod
    def _merge_connection_topology(
        src_topos: List[Dict[str, Any]],
        local_topos: List[Dict[str, Any]],
        conn_id_map: Dict[str, str],
        lconn: sqlite3.Connection,
        strategy: str,
    ) -> Dict[str, int]:
        added = updated = skipped = 0
        local_by_cid = {t["connection_id"]: t for t in local_topos}
        now = _now_str()
        for src in src_topos:
            mapped_cid = conn_id_map.get(src["connection_id"], src["connection_id"])
            loc = local_by_cid.get(mapped_cid)
            if loc is None:
                lconn.execute(
                    """
                    INSERT OR REPLACE INTO connection_topology (
                        connection_id, name, driver_track, db_type,
                        host, port, dbname, default_schema,
                        sslmode, auth_mode, sqlite_path,
                        jdbc_driver_id, jdbc_url, jdbc_driver_class,
                        extra_props_json, standard_format, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mapped_cid,
                        src["name"],
                        src["driver_track"],
                        src["db_type"],
                        src["host"],
                        int(src["port"] or 0),
                        src["dbname"],
                        src["default_schema"],
                        src["sslmode"],
                        src["auth_mode"],
                        src["sqlite_path"],
                        src["jdbc_driver_id"],
                        src["jdbc_url"],
                        src["jdbc_driver_class"],
                        src["extra_props_json"],
                        src["standard_format"],
                        src["created_at"] or now,
                        src["updated_at"] or now,
                    ),
                )
                local_by_cid[mapped_cid] = src
                added += 1
                continue

            should_update = False
            if strategy == "overwrite":
                should_update = True
            elif strategy == "newer":
                if str(src.get("updated_at") or "") > str(loc.get("updated_at") or ""):
                    should_update = True
            elif strategy == "supplement":
                # 仅在本地为 AUTO 且对方显式配置了落标标准格式时温和补全
                if (loc.get("standard_format") or "AUTO") == "AUTO" and (src.get("standard_format") or "AUTO") != "AUTO":
                    lconn.execute(
                        "UPDATE connection_topology SET standard_format = ?, updated_at = ? WHERE connection_id = ?",
                        (src["standard_format"], now, mapped_cid),
                    )
                    updated += 1
                    continue

            if should_update:
                lconn.execute(
                    """
                    UPDATE connection_topology
                    SET name = ?, default_schema = ?, standard_format = ?,
                        sslmode = ?, auth_mode = ?, jdbc_driver_class = ?,
                        extra_props_json = ?, updated_at = ?
                    WHERE connection_id = ?
                    """,
                    (
                        src["name"],
                        src["default_schema"],
                        src["standard_format"],
                        src["sslmode"],
                        src["auth_mode"],
                        src["jdbc_driver_class"],
                        src["extra_props_json"],
                        src["updated_at"] or now,
                        mapped_cid,
                    ),
                )
                updated += 1
            else:
                skipped += 1
        return {"added": added, "updated": updated, "skipped": skipped}

    @staticmethod
    def _merge_standard_dict(
        sconn: sqlite3.Connection, lconn: sqlite3.Connection, strategy: str
    ) -> Dict[str, int]:
        added = updated = skipped = 0
        scols = _get_table_columns(sconn, "standard_dictionary")
        if "std_code" not in scols:
            return {"added": 0, "updated": 0, "skipped": 0}
        common_cols = [c for c in _STD_DICT_COLS if c in scols]
        local_map = {
            str(r["std_code"]): dict(r)
            for r in lconn.execute("SELECT * FROM standard_dictionary").fetchall()
        }
        col_sql = ", ".join(common_cols)
        ph = ", ".join("?" for _ in common_cols)
        update_sql = ", ".join(f"{c} = excluded.{c}" for c in common_cols if c != "std_code")

        to_insert: List[List[Any]] = []
        to_upsert: List[List[Any]] = []
        for r in sconn.execute(f"SELECT {col_sql} FROM standard_dictionary").fetchall():
            code = str(r["std_code"] or "").strip()
            if not code:
                continue
            vals = [code if c == "std_code" else r[c] for c in common_cols]
            loc = local_map.get(code)
            if loc is None:
                to_insert.append(vals)
                local_map[code] = {c: v for c, v in zip(common_cols, vals)}
                added += 1
                continue
            is_same = all(
                str(code if c == "std_code" else (r[c] or "")) == str(loc.get(c) or "")
                for c in common_cols
                if c != "updated_at"
            )
            if is_same:
                skipped += 1
                continue
            if strategy == "supplement":
                skipped += 1
            elif strategy == "newer":
                s_upd = str(r["updated_at"] or "") if "updated_at" in common_cols else ""
                if s_upd and s_upd > str(loc.get("updated_at") or ""):
                    to_upsert.append(vals)
                    local_map[code] = {c: v for c, v in zip(common_cols, vals)}
                    updated += 1
                else:
                    skipped += 1
            else:
                to_upsert.append(vals)
                local_map[code] = {c: v for c, v in zip(common_cols, vals)}
                updated += 1

        if to_insert:
            lconn.executemany(
                f"INSERT OR IGNORE INTO standard_dictionary ({col_sql}) VALUES ({ph})",
                to_insert,
            )
        if to_upsert and update_sql:
            lconn.executemany(
                f"INSERT INTO standard_dictionary ({col_sql}) VALUES ({ph}) "
                f"ON CONFLICT(std_code) DO UPDATE SET {update_sql}",
                to_upsert,
            )
        return {"added": added, "updated": updated, "skipped": skipped}

    @staticmethod
    def _merge_field_bindings(
        sconn: sqlite3.Connection,
        lconn: sqlite3.Connection,
        conn_id_map: Dict[str, str],
        strategy: str,
    ) -> Dict[str, int]:
        added = updated = skipped = 0
        local_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in lconn.execute("SELECT * FROM field_standard_bindings").fetchall():
            local_map[(str(r["connection_id"]), str(r["col_key"] or "").lower())] = dict(r)

        now = _now_str()
        for r in sconn.execute("SELECT * FROM field_standard_bindings").fetchall():
            cid = conn_id_map.get(str(r["connection_id"] or ""), str(r["connection_id"] or ""))
            ckey_raw = str(r["col_key"] or "").strip()
            if not cid or not ckey_raw:
                continue
            ckey_low = ckey_raw.lower()
            s_code = str(r["std_code"] or "").strip()
            s_upd = str(r["updated_at"] or now)
            loc = local_map.get((cid, ckey_low))

            if loc is None:
                lconn.execute(
                    """
                    INSERT INTO field_standard_bindings (
                        connection_id, schema_name, table_name, column_name, col_key,
                        std_code, bind_source, compliance_status, compliance_detail, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        str(r["schema_name"] or "public"),
                        str(r["table_name"] or ""),
                        str(r["column_name"] or ""),
                        ckey_raw,
                        s_code,
                        str(r["bind_source"] or "MERGE"),
                        str(r["compliance_status"] or "UNCHECKED"),
                        str(r["compliance_detail"] or ""),
                        s_upd,
                    ),
                )
                local_map[(cid, ckey_low)] = {"std_code": s_code, "updated_at": s_upd, "col_key": ckey_raw}
                added += 1
                continue

            l_code = str(loc.get("std_code") or "").strip()
            if not l_code and s_code:
                lconn.execute(
                    """
                    UPDATE field_standard_bindings
                    SET std_code = ?, bind_source = ?, compliance_status = ?, compliance_detail = ?, updated_at = ?
                    WHERE connection_id = ? AND col_key = ?
                    """,
                    (
                        s_code,
                        str(r["bind_source"] or "MERGE"),
                        str(r["compliance_status"] or "UNCHECKED"),
                        str(r["compliance_detail"] or ""),
                        s_upd,
                        cid,
                        loc["col_key"],
                    ),
                )
                local_map[(cid, ckey_low)] = {
                    "std_code": s_code,
                    "updated_at": s_upd,
                    "col_key": loc["col_key"],
                }
                updated += 1
                continue

            if s_code.lower() == l_code.lower():
                skipped += 1
                continue

            should_replace = strategy == "overwrite" or (
                strategy == "newer" and bool(s_code) and s_upd > str(loc.get("updated_at") or "")
            )
            if should_replace:
                lconn.execute(
                    """
                    UPDATE field_standard_bindings
                    SET std_code = ?, bind_source = ?, compliance_status = ?, compliance_detail = ?, updated_at = ?
                    WHERE connection_id = ? AND col_key = ?
                    """,
                    (
                        s_code,
                        str(r["bind_source"] or "MERGE"),
                        str(r["compliance_status"] or "UNCHECKED"),
                        str(r["compliance_detail"] or ""),
                        s_upd,
                        cid,
                        loc["col_key"],
                    ),
                )
                local_map[(cid, ckey_low)] = {
                    "std_code": s_code,
                    "updated_at": s_upd,
                    "col_key": loc["col_key"],
                }
                updated += 1
            else:
                skipped += 1
        return {"added": added, "updated": updated, "skipped": skipped}

    def _merge_cross_mappings(
        self,
        sconn: sqlite3.Connection,
        src_tables: Set[str],
        lconn: sqlite3.Connection,
        strategy: str,
    ) -> Dict[str, int]:
        added = updated = skipped = 0
        local_tbls = {
            str(r["std_table_key"]).lower(): dict(r)
            for r in lconn.execute("SELECT * FROM cross_table_bindings").fetchall()
        }
        local_cols_by_tbl: Dict[str, List[Dict[str, Any]]] = {}
        for r in lconn.execute("SELECT * FROM cross_column_bindings ORDER BY row_index ASC, id ASC").fetchall():
            local_cols_by_tbl.setdefault(str(r["std_table_key"] or "").lower(), []).append(dict(r))

        src_cols_by_tbl: Dict[str, List[Dict[str, Any]]] = {}
        if "cross_column_bindings" in src_tables:
            for r in sconn.execute("SELECT * FROM cross_column_bindings ORDER BY row_index ASC, id ASC").fetchall():
                src_cols_by_tbl.setdefault(str(r["std_table_key"] or "").lower(), []).append(dict(r))

        now = _now_str()
        for st_row in sconn.execute("SELECT * FROM cross_table_bindings ORDER BY id ASC").fetchall():
            st = dict(st_row)
            tk = str(st.get("std_table_key") or "").lower()
            if not tk:
                continue
            scols = src_cols_by_tbl.get(tk, [])
            lt = local_tbls.get(tk)
            s_max_upd = max([str(st.get("updated_at") or now), *(str(c.get("updated_at") or "") for c in scols)])

            if lt is None:
                lconn.execute(
                    """
                    INSERT INTO cross_table_bindings (
                        std_table_key, std_schema_name, std_table_name, std_table_comment,
                        source_schema, source_table, target_schema, target_table, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tk,
                        str(st.get("std_schema_name") or "public"),
                        str(st.get("std_table_name") or tk),
                        str(st.get("std_table_comment") or ""),
                        str(st.get("source_schema") or ""),
                        str(st.get("source_table") or ""),
                        str(st.get("target_schema") or ""),
                        str(st.get("target_table") or ""),
                        str(st.get("updated_at") or now),
                    ),
                )
                self._insert_cross_col_rows(lconn, tk, scols, now)
                added += 1
                continue

            local_tk = str(lt["std_table_key"])
            lcols = local_cols_by_tbl.get(tk, [])
            l_max_upd = max([str(lt.get("updated_at") or ""), *(str(c.get("updated_at") or "") for c in lcols)])
            replace_whole_table = strategy == "overwrite" or (
                strategy == "newer" and s_max_upd > l_max_upd
            )

            if replace_whole_table:
                lconn.execute(
                    """
                    UPDATE cross_table_bindings
                    SET std_schema_name = ?, std_table_name = ?, std_table_comment = ?,
                        source_schema = ?, source_table = ?, target_schema = ?, target_table = ?, updated_at = ?
                    WHERE std_table_key = ?
                    """,
                    (
                        str(st.get("std_schema_name") or lt["std_schema_name"]),
                        str(st.get("std_table_name") or lt["std_table_name"]),
                        str(st.get("std_table_comment") or lt["std_table_comment"]),
                        str(st.get("source_schema") or ""),
                        str(st.get("source_table") or ""),
                        str(st.get("target_schema") or ""),
                        str(st.get("target_table") or ""),
                        s_max_upd,
                        local_tk,
                    ),
                )
                lconn.execute(
                    "DELETE FROM cross_column_bindings WHERE std_table_key = ? OR LOWER(std_table_key) = ?",
                    (local_tk, tk),
                )
                self._insert_cross_col_rows(lconn, local_tk, scols, now)
                updated += 1
                continue

            # newer 策略下本地表已较新或一致：完整保留本地版本，避免将本地已删除的旧字段重新塞回
            if strategy == "newer":
                skipped += 1
                continue

            # supplement 模式：补充本地空缺的表级单侧绑定及缺失列/空数标绑定
            tbl_changed = False
            new_src_schema = str(lt.get("source_schema") or "").strip()
            new_src_table = str(lt.get("source_table") or "").strip()
            new_tgt_schema = str(lt.get("target_schema") or "").strip()
            new_tgt_table = str(lt.get("target_table") or "").strip()
            tbl_pair_filled = False
            if not new_src_table and str(st.get("source_table") or "").strip():
                new_src_schema = str(st.get("source_schema") or "").strip()
                new_src_table = str(st.get("source_table") or "").strip()
                tbl_pair_filled = True
            if not new_tgt_table and str(st.get("target_table") or "").strip():
                new_tgt_schema = str(st.get("target_schema") or "").strip()
                new_tgt_table = str(st.get("target_table") or "").strip()
                tbl_pair_filled = True
            if tbl_pair_filled:
                lconn.execute(
                    """
                    UPDATE cross_table_bindings
                    SET source_schema = ?, source_table = ?, target_schema = ?, target_table = ?, updated_at = ?
                    WHERE std_table_key = ?
                    """,
                    (new_src_schema, new_src_table, new_tgt_schema, new_tgt_table, now, local_tk),
                )
                tbl_changed = True

            local_by_sig = {self._col_sig(c): c for c in lcols}
            next_idx = max((int(c.get("row_index") or 0) for c in lcols), default=0) + 1
            for sc in scols:
                sig = self._col_sig(sc)
                lc = local_by_sig.get(sig)
                if lc is None:
                    cur = lconn.execute(
                        """
                        INSERT INTO cross_column_bindings (
                            std_table_key, row_index,
                            orig_col_name, orig_col_type, orig_col_comment,
                            std_col_name, std_col_type, std_col_comment,
                            bound_source_col, bound_target_col,
                            source_std_code, target_std_code, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            local_tk,
                            next_idx,
                            str(sc.get("orig_col_name") or ""),
                            str(sc.get("orig_col_type") or ""),
                            str(sc.get("orig_col_comment") or ""),
                            str(sc.get("std_col_name") or ""),
                            str(sc.get("std_col_type") or ""),
                            str(sc.get("std_col_comment") or ""),
                            str(sc.get("bound_source_col") or ""),
                            str(sc.get("bound_target_col") or ""),
                            str(sc.get("source_std_code") or ""),
                            str(sc.get("target_std_code") or ""),
                            str(sc.get("updated_at") or now),
                        ),
                    )
                    inserted_row = dict(sc)
                    inserted_row["id"] = cur.lastrowid
                    local_by_sig[sig] = inserted_row
                    next_idx += 1
                    tbl_changed = True
                else:
                    new_src_std = str(lc.get("source_std_code") or "").strip() or str(sc.get("source_std_code") or "").strip()
                    new_tgt_std = str(lc.get("target_std_code") or "").strip() or str(sc.get("target_std_code") or "").strip()
                    new_src_col = str(lc.get("bound_source_col") or "").strip() or str(sc.get("bound_source_col") or "").strip()
                    new_tgt_col = str(lc.get("bound_target_col") or "").strip() or str(sc.get("bound_target_col") or "").strip()
                    if (
                        new_src_std != str(lc.get("source_std_code") or "").strip()
                        or new_tgt_std != str(lc.get("target_std_code") or "").strip()
                        or new_src_col != str(lc.get("bound_source_col") or "").strip()
                        or new_tgt_col != str(lc.get("bound_target_col") or "").strip()
                    ):
                        lconn.execute(
                            """
                            UPDATE cross_column_bindings
                            SET bound_source_col = ?, bound_target_col = ?,
                                source_std_code = ?, target_std_code = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (new_src_col, new_tgt_col, new_src_std, new_tgt_std, now, lc["id"]),
                        )
                        lc["bound_source_col"] = new_src_col
                        lc["bound_target_col"] = new_tgt_col
                        lc["source_std_code"] = new_src_std
                        lc["target_std_code"] = new_tgt_std
                        tbl_changed = True
            if tbl_changed:
                updated += 1
            else:
                skipped += 1

        return {"added": added, "updated": updated, "skipped": skipped}

    @staticmethod
    def _insert_cross_col_rows(
        lconn: sqlite3.Connection, tk: str, scols: List[Dict[str, Any]], fallback_now: str
    ) -> None:
        if not scols:
            return
        lconn.executemany(
            """
            INSERT INTO cross_column_bindings (
                std_table_key, row_index,
                orig_col_name, orig_col_type, orig_col_comment,
                std_col_name, std_col_type, std_col_comment,
                bound_source_col, bound_target_col,
                source_std_code, target_std_code, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    tk,
                    int(c.get("row_index") or idx),
                    str(c.get("orig_col_name") or ""),
                    str(c.get("orig_col_type") or ""),
                    str(c.get("orig_col_comment") or ""),
                    str(c.get("std_col_name") or ""),
                    str(c.get("std_col_type") or ""),
                    str(c.get("std_col_comment") or ""),
                    str(c.get("bound_source_col") or ""),
                    str(c.get("bound_target_col") or ""),
                    str(c.get("source_std_code") or ""),
                    str(c.get("target_std_code") or ""),
                    str(c.get("updated_at") or fallback_now),
                )
                for idx, c in enumerate(scols, start=1)
            ],
        )

    @staticmethod
    def _merge_migration_items(
        sconn: sqlite3.Connection, lconn: sqlite3.Connection, strategy: str
    ) -> Dict[str, int]:
        added = updated = skipped = 0

        def _mkey(r: Any) -> Tuple[str, str, str, str]:
            return (
                str(r["schema_name"] or "").lower(),
                str(r["table_name"] or "").lower(),
                str(r["old_col_name"] or "").lower(),
                str(r["new_col_name"] or "").lower(),
            )

        local_rows = [dict(r) for r in lconn.execute("SELECT * FROM migration_items").fetchall()]
        local_map = {_mkey(r): r for r in local_rows}
        next_row_idx = max((int(r.get("row_index") or 0) for r in local_rows), default=0) + 1
        now = _now_str()

        for srow in sconn.execute("SELECT * FROM migration_items ORDER BY row_index ASC, id ASC").fetchall():
            sr = dict(srow)
            k = _mkey(sr)
            loc = local_map.get(k)
            if loc is None:
                lconn.execute(
                    """
                    INSERT INTO migration_items (
                        row_index, schema_name, table_name,
                        old_col_name, old_col_type, old_col_comment,
                        new_col_name, new_col_type, new_col_comment,
                        status, strategy, ordinal_position, total_columns,
                        db_current_col_name, db_current_type, db_current_comment,
                        is_distribute_key, table_orientation,
                        status_detail, error_message, generated_sql, executed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_row_idx,
                        str(sr.get("schema_name") or "public"),
                        str(sr.get("table_name") or ""),
                        str(sr.get("old_col_name") or ""),
                        str(sr.get("old_col_type") or ""),
                        str(sr.get("old_col_comment") or ""),
                        str(sr.get("new_col_name") or ""),
                        str(sr.get("new_col_type") or ""),
                        str(sr.get("new_col_comment") or ""),
                        str(sr.get("status") or "UNCHECKED"),
                        str(sr.get("strategy") or "DIRECT_ALTER"),
                        sr.get("ordinal_position"),
                        sr.get("total_columns"),
                        str(sr.get("db_current_col_name") or ""),
                        str(sr.get("db_current_type") or ""),
                        str(sr.get("db_current_comment") or ""),
                        int(sr.get("is_distribute_key") or 0),
                        str(sr.get("table_orientation") or ""),
                        str(sr.get("status_detail") or ""),
                        str(sr.get("error_message") or ""),
                        str(sr.get("generated_sql") or ""),
                        str(sr.get("executed_at") or ""),
                        str(sr.get("updated_at") or now),
                    ),
                )
                next_row_idx += 1
                added += 1
                continue

            is_same = (
                str(sr.get("new_col_type") or "") == str(loc.get("new_col_type") or "")
                and str(sr.get("new_col_comment") or "") == str(loc.get("new_col_comment") or "")
                and str(sr.get("status") or "") == str(loc.get("status") or "")
            )
            if is_same:
                skipped += 1
                continue

            should_replace = strategy == "overwrite" or (
                strategy == "newer" and str(sr.get("updated_at") or "") > str(loc.get("updated_at") or "")
            )
            if should_replace:
                lconn.execute(
                    """
                    UPDATE migration_items
                    SET old_col_type = ?, old_col_comment = ?, new_col_type = ?, new_col_comment = ?,
                        status = ?, strategy = ?, status_detail = ?, generated_sql = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(sr.get("old_col_type") or ""),
                        str(sr.get("old_col_comment") or ""),
                        str(sr.get("new_col_type") or ""),
                        str(sr.get("new_col_comment") or ""),
                        str(sr.get("status") or "UNCHECKED"),
                        str(sr.get("strategy") or "DIRECT_ALTER"),
                        str(sr.get("status_detail") or ""),
                        str(sr.get("generated_sql") or ""),
                        str(sr.get("updated_at") or now),
                        loc["id"],
                    ),
                )
                updated += 1
            else:
                skipped += 1

        return {"added": added, "updated": updated, "skipped": skipped}

    @staticmethod
    def _merge_metadata_cache(
        sconn: sqlite3.Connection,
        src_tables: Set[str],
        lconn: sqlite3.Connection,
        conn_id_map: Dict[str, str],
        strategy: str,
    ) -> Dict[str, int]:
        added = updated = skipped = 0
        if "meta_schemas" in src_tables:
            local_schemas = {
                (str(r["connection_id"]), str(r["schema_name"]).lower()): dict(r)
                for r in lconn.execute("SELECT * FROM meta_schemas").fetchall()
            }
            for r in sconn.execute("SELECT * FROM meta_schemas").fetchall():
                cid = conn_id_map.get(str(r["connection_id"]), str(r["connection_id"]))
                skey = (cid, str(r["schema_name"]).lower())
                l_sch = local_schemas.get(skey)
                if l_sch is None:
                    lconn.execute(
                        """
                        INSERT OR IGNORE INTO meta_schemas (
                            connection_id, schema_name, table_count, view_count, refreshed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            str(r["schema_name"]),
                            int(r["table_count"] or 0),
                            int(r["view_count"] or 0),
                            str(r["refreshed_at"] or ""),
                        ),
                    )
                    local_schemas[skey] = dict(r)
                elif strategy == "overwrite" or (
                    strategy == "newer"
                    and str(r["refreshed_at"] or "") > str(l_sch.get("refreshed_at") or "")
                ):
                    lconn.execute(
                        """
                        UPDATE meta_schemas
                        SET table_count = ?, view_count = ?, refreshed_at = ?
                        WHERE connection_id = ? AND schema_name = ?
                        """,
                        (
                            int(r["table_count"] or 0),
                            int(r["view_count"] or 0),
                            str(r["refreshed_at"] or ""),
                            cid,
                            str(l_sch["schema_name"]),
                        ),
                    )

        if "meta_tables" not in src_tables:
            return {"added": 0, "updated": 0, "skipped": 0}

        local_tbls = {
            (str(r["connection_id"]), str(r["schema_name"]).lower(), str(r["table_name"]).lower()): dict(r)
            for r in lconn.execute("SELECT * FROM meta_tables").fetchall()
        }
        src_cols_by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        if "meta_columns" in src_tables:
            for cr in sconn.execute("SELECT * FROM meta_columns ORDER BY ordinal_position ASC").fetchall():
                k = (str(cr["connection_id"]), str(cr["schema_name"]).lower(), str(cr["table_name"]).lower())
                src_cols_by_key.setdefault(k, []).append(dict(cr))

        for tr in sconn.execute("SELECT * FROM meta_tables").fetchall():
            t = dict(tr)
            orig_cid = str(t["connection_id"])
            cid = conn_id_map.get(orig_cid, orig_cid)
            key = (cid, str(t["schema_name"]).lower(), str(t["table_name"]).lower())
            loc = local_tbls.get(key)

            def _write_table_and_cols() -> None:
                if loc is not None and (
                    str(loc["schema_name"]) != str(t["schema_name"])
                    or str(loc["table_name"]) != str(t["table_name"])
                ):
                    lconn.execute(
                        "DELETE FROM meta_tables WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                        (cid, str(loc["schema_name"]), str(loc["table_name"])),
                    )
                    lconn.execute(
                        "DELETE FROM meta_columns WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                        (cid, str(loc["schema_name"]), str(loc["table_name"])),
                    )
                lconn.execute(
                    """
                    INSERT OR REPLACE INTO meta_tables (
                        connection_id, schema_name, table_name, object_type, table_comment,
                        orientation, with_clause, distribute_clause, distribute_keys_json,
                        partition_clause, partition_keys_json, primary_keys_json,
                        penetrated_base_tables_json, row_count, raw_ddl, view_definition,
                        columns_cached, refreshed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        str(t["schema_name"]),
                        str(t["table_name"]),
                        str(t.get("object_type") or "TABLE"),
                        str(t.get("table_comment") or ""),
                        str(t.get("orientation") or "ROW"),
                        str(t.get("with_clause") or ""),
                        str(t.get("distribute_clause") or ""),
                        str(t.get("distribute_keys_json") or "[]"),
                        str(t.get("partition_clause") or ""),
                        str(t.get("partition_keys_json") or "[]"),
                        str(t.get("primary_keys_json") or "[]"),
                        str(t.get("penetrated_base_tables_json") or "[]"),
                        int(t.get("row_count") or 0),
                        str(t.get("raw_ddl") or ""),
                        str(t.get("view_definition") or ""),
                        int(t.get("columns_cached") or 0),
                        str(t.get("refreshed_at") or ""),
                    ),
                )
                cols = src_cols_by_key.get(
                    (orig_cid, str(t["schema_name"]).lower(), str(t["table_name"]).lower()), []
                )
                if cols:
                    lconn.execute(
                        "DELETE FROM meta_columns WHERE connection_id = ? AND schema_name = ? AND table_name = ?",
                        (cid, str(t["schema_name"]), str(t["table_name"])),
                    )
                    lconn.executemany(
                        """
                        INSERT OR REPLACE INTO meta_columns (
                            connection_id, schema_name, table_name, ordinal_position,
                            column_name, data_type, normalized_type, is_not_null,
                            column_default, column_comment, is_primary_key,
                            index_names_json, lineage_source, refreshed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                cid,
                                str(t["schema_name"]),
                                str(t["table_name"]),
                                int(c.get("ordinal_position") or 0),
                                str(c["column_name"]),
                                str(c.get("data_type") or ""),
                                str(c.get("normalized_type") or ""),
                                int(c.get("is_not_null") or 0),
                                c.get("column_default"),
                                str(c.get("column_comment") or ""),
                                int(c.get("is_primary_key") or 0),
                                str(c.get("index_names_json") or "[]"),
                                str(c.get("lineage_source") or ""),
                                str(c.get("refreshed_at") or ""),
                            )
                            for c in cols
                        ],
                    )
                local_tbls[key] = dict(t)

            if loc is None:
                _write_table_and_cols()
                added += 1
            elif int(loc.get("columns_cached") or 0) == 0 and int(t.get("columns_cached") or 0) != 0:
                _write_table_and_cols()
                updated += 1
            elif strategy == "overwrite" or (
                strategy == "newer" and str(t.get("refreshed_at") or "") > str(loc.get("refreshed_at") or "")
            ):
                _write_table_and_cols()
                updated += 1
            else:
                skipped += 1

        return {"added": added, "updated": updated, "skipped": skipped}

    @staticmethod
    def _supplement_workspace_meta(
        lconn: sqlite3.Connection,
        src_meta: Dict[str, Any],
        conn_id_map: Dict[str, str],
        chosen_modules: Set[str],
    ) -> None:
        existing = {
            str(r["key"]): str(r["value"])
            for r in lconn.execute("SELECT key, value FROM workspace_meta").fetchall()
        }
        existing_conn_ids = {
            str(r["connection_id"])
            for r in lconn.execute("SELECT connection_id FROM connection_topology").fetchall()
        }
        conn_ref_keys = {
            "migrator_connection_id",
            "cross_source_connection_id",
            "cross_target_connection_id",
        }
        copy_keys: Set[str] = set()
        if "standard_dictionary" in chosen_modules:
            copy_keys.update({"std_dict_filename", "std_dict_imported_at"})
        if "migration_items" in chosen_modules:
            copy_keys.update({"current_excel_name", "imported_at"})
        if "cross_mappings" in chosen_modules:
            copy_keys.update({"cross_standard_filename", "cross_standard_imported_at"})
        if "connection_topology" in chosen_modules:
            copy_keys.update(conn_ref_keys)
        if "field_standard_bindings" in chosen_modules or "migration_items" in chosen_modules:
            copy_keys.add("migrator_connection_id")
        if "cross_mappings" in chosen_modules:
            copy_keys.update({"cross_source_connection_id", "cross_target_connection_id"})

        for k in copy_keys:
            if k not in src_meta:
                continue
            val = src_meta[k]
            if val in ("", None, "未导入"):
                continue
            cur_raw = existing.get(k)
            cur_val = None
            if cur_raw is not None:
                try:
                    cur_val = json.loads(cur_raw)
                except Exception:
                    cur_val = cur_raw
            if cur_val not in ("", None, "未导入"):
                continue
            if k in conn_ref_keys and isinstance(val, str):
                val = conn_id_map.get(val, val)
                if val not in existing_conn_ids:
                    continue
            lconn.execute(
                "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)",
                (k, json.dumps(val, ensure_ascii=False)),
            )
