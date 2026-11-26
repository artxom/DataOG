"""Data.O.G 4.0 Init SDK 独立运行期与脚手架模板集 (tools/sdk_templates.py).

供 `tools/export_plugin_sdk.py` 装配产出零漂移、可离线自洽运行的 Antigravity 插件开发工作区。
注意：本文件中的所有生成模板严禁内嵌任何 Ed25519 私钥。
"""

from __future__ import annotations

DEV_HOST_PY_TEMPLATE = r'''"""Data.O.G 4.0 离线双轨开发宿主与 Mock 底座 (dev_host.py).

设计目标：
1. 独立运行时零依赖 Data.O.G 主仓库：向 `sys.modules["core.platform.*"]` 注入 API 兼容的离线微内核底座；
2. 提供真实双库 SQLite WAL 持久化（workspace / private 分流）、内置 `conn_demo_sqlite` 示例数据源与真只读查询；
3. 挂载 `.dataog_sdk/ui_shell/` 真实 macOS 桌面外壳与当前工作区插件，支持浏览器实时预览与 `DevTestClient` 离线单测。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime
import importlib
import importlib.util
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import uuid
import webbrowser
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlencode

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

_SDK_DIR = os.path.dirname(os.path.abspath(__file__))
_SAFE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_READONLY_FORBIDDEN_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|UPSERT|REPLACE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA)\b",
    re.IGNORECASE,
)


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _DevProgressHub:
    """线程安全的任务进度与取消追踪中枢（兼容 ImportProgressHub）."""

    _VALID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._cancelled: set[str] = set()

    @classmethod
    def is_valid_id(cls, tid: str) -> bool:
        return bool(tid and cls._VALID_RE.match(tid))

    def start(self, tid: str, title: str, *, detail: str = "") -> None:
        if not tid:
            return
        with self._lock:
            self._items[tid] = {
                "task_id": tid,
                "title": title,
                "status": "running",
                "percent": 0,
                "stage": "开始",
                "detail": detail or title,
                "processed": 0,
                "total": 0,
                "warning": "",
                "updated_at": _now_str(),
            }

    def make_reporter(self, tid: str) -> Callable[..., None]:
        def _report(percent: int, stage: str = "", detail: str = "", processed: int = 0, total: int = 0) -> None:
            if not tid:
                return
            with self._lock:
                cur = self._items.get(tid) or {"task_id": tid, "title": ""}
                cur.update(
                    {
                        "status": "running",
                        "percent": max(0, min(100, int(percent))),
                        "stage": str(stage or ""),
                        "detail": str(detail or ""),
                        "processed": int(processed or 0),
                        "total": int(total or 0),
                        "updated_at": _now_str(),
                    }
                )
                self._items[tid] = cur

        return _report

    def finish(
        self,
        tid: str,
        *,
        stage: str = "完成",
        detail: str = "",
        warning: str = "",
        processed: int = 0,
        total: int = 0,
    ) -> None:
        if not tid:
            return
        with self._lock:
            cur = self._items.get(tid) or {"task_id": tid}
            cur.update(
                {
                    "status": "completed",
                    "percent": 100,
                    "stage": stage,
                    "detail": detail,
                    "warning": warning,
                    "processed": int(processed),
                    "total": int(total),
                    "updated_at": _now_str(),
                }
            )
            self._items[tid] = cur

    def fail(self, tid: str, error: str) -> None:
        if not tid:
            return
        with self._lock:
            cur = self._items.get(tid) or {"task_id": tid}
            cur.update(
                {
                    "status": "failed",
                    "stage": "失败",
                    "detail": str(error),
                    "updated_at": _now_str(),
                }
            )
            self._items[tid] = cur

    def cancel(self, tid: str) -> bool:
        if not tid:
            return False
        with self._lock:
            self._cancelled.add(tid)
            if tid in self._items:
                self._items[tid]["status"] = "cancelled"
                self._items[tid]["updated_at"] = _now_str()
            return True

    def is_cancelled(self, tid: str) -> bool:
        with self._lock:
            return tid in self._cancelled

    def get(self, tid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(tid)
            return dict(item) if item else None


class _DevStateStore:
    """沙箱版三层分离双库 SQLite 引擎（兼容 PlatformStateStore 核心插件 API）."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.private_db_path = os.path.join(self.data_dir, "dev_private.db")
        self.workspace_db_path = os.path.join(self.data_dir, "dev_workspace.db")
        self.is_split_storage = True
        self._lock = threading.RLock()
        self._private_tables: set[str] = {"app_meta", "execution_logs"}
        self._workspace_tables: set[str] = {"workspace_meta"}
        self._workspace_meta_keys: set[str] = {"workspace_name", "workspace_format_version"}

        for path, sql in (
            (
                self.private_db_path,
                "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS execution_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT NOT NULL, target TEXT NOT NULL, message TEXT NOT NULL, sql_text TEXT DEFAULT '');",
            ),
            (
                self.workspace_db_path,
                "CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
            ),
        ):
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.executescript(sql)
                conn.commit()

    def _open_conn(self, scope: str) -> sqlite3.Connection:
        path = self.private_db_path if scope == "private" else self.workspace_db_path
        conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def register_table_schema(
        self,
        table_name: str,
        create_sql: str,
        expected_columns: List[Tuple[str, str]],
        *,
        scope: str = "workspace",
    ) -> None:
        clean = str(table_name or "").strip()
        if not _SAFE_IDENT_RE.match(clean):
            raise ValueError(f"非法的插件表名: {table_name!r}")
        norm_scope = "private" if str(scope or "").lower() == "private" else "workspace"
        with self._lock:
            if norm_scope == "private":
                self._private_tables.add(clean)
            else:
                self._workspace_tables.add(clean)
            with contextlib.closing(self._open_conn(norm_scope)) as conn:
                if create_sql.strip():
                    conn.executescript(create_sql)
                existing = {r["name"] for r in conn.execute(f'PRAGMA table_info("{clean}")').fetchall()}
                for col_name, col_def in expected_columns or []:
                    if col_name not in existing and _SAFE_IDENT_RE.match(col_name):
                        conn.execute(f'ALTER TABLE "{clean}" ADD COLUMN "{col_name}" {col_def}')
                conn.commit()

    @contextlib.contextmanager
    def module_db(self, scope: str = "workspace"):
        norm_scope = "private" if str(scope or "").lower() == "private" else "workspace"
        with self._lock:
            conn = self._open_conn(norm_scope)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def register_private_tables(self, table_names: Iterable[str]) -> None:
        for t in table_names or ():
            if _SAFE_IDENT_RE.match(str(t or "")):
                self._private_tables.add(str(t))

    def register_workspace_tables(self, table_names: Iterable[str]) -> None:
        for t in table_names or ():
            if _SAFE_IDENT_RE.match(str(t or "")):
                self._workspace_tables.add(str(t))

    def register_workspace_meta_keys(self, keys: Iterable[str]) -> None:
        for k in keys or ():
            if str(k or "").strip():
                self._workspace_meta_keys.add(str(k).strip())

    def save_meta(self, key: str, value: Any) -> None:
        val_str = json.dumps(value, ensure_ascii=False)
        scope = "workspace" if key in self._workspace_meta_keys else "private"
        tbl = "workspace_meta" if scope == "workspace" else "app_meta"
        with self.module_db(scope=scope) as conn:
            conn.execute(
                f"INSERT INTO {tbl} (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, val_str),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        scope = "workspace" if key in self._workspace_meta_keys else "private"
        tbl = "workspace_meta" if scope == "workspace" else "app_meta"
        with self.module_db(scope=scope) as conn:
            row = conn.execute(f"SELECT value FROM {tbl} WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def add_log(self, level: str, target: str, message: str, sql_text: str = "") -> None:
        with self.module_db(scope="private") as conn:
            conn.execute(
                "INSERT INTO execution_logs (created_at, level, target, message, sql_text) VALUES (?, ?, ?, ?, ?)",
                (_now_str(), str(level).upper(), str(target), str(message), str(sql_text)),
            )

    def list_logs(self, limit: int = 150) -> List[Dict[str, Any]]:
        with self.module_db(scope="private") as conn:
            rows = conn.execute("SELECT * FROM execution_logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def close(self) -> None:
        pass


class _DevConnectionHub:
    """预置真实 SQLite 示例库的连接中心 (`conn_demo_sqlite`)."""

    def __init__(self, data_dir: str) -> None:
        self.sample_db_path = os.path.join(data_dir, "demo_warehouse.sqlite3")
        self._init_sample_db()
        self._profiles = {
            "conn_demo_sqlite": types.SimpleNamespace(
                connection_id="conn_demo_sqlite",
                name="SDK 示例数仓 (SQLite)",
                db_type="SQLITE",
                host="",
                port=0,
                dbname="demo_warehouse",
                default_schema="main",
                sqlite_path=self.sample_db_path,
                standard_format="AUTO",
            )
        }

    def _init_sample_db(self) -> None:
        if os.path.isfile(self.sample_db_path):
            return
        with contextlib.closing(sqlite3.connect(self.sample_db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dim_customer (
                    cust_id TEXT PRIMARY KEY,
                    cust_name TEXT NOT NULL,
                    id_no TEXT NOT NULL,
                    risk_grade TEXT NOT NULL,
                    open_date TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fact_transactions (
                    tx_id TEXT PRIMARY KEY,
                    cust_id TEXT NOT NULL,
                    tx_amt REAL NOT NULL,
                    ccy_code TEXT NOT NULL,
                    tx_time TEXT NOT NULL
                );
                INSERT OR IGNORE INTO dim_customer VALUES
                    ('C001', '张明', '110101199001011234', 'LOW', '2024-03-15'),
                    ('C002', '李华', '310101198805124321', 'MEDIUM', '2025-01-20'),
                    ('C003', '启明科技', '91110000MA01234567', 'LOW', '2025-06-08');
                INSERT OR IGNORE INTO fact_transactions VALUES
                    ('TX1001', 'C001', 12800.50, 'CNY', '2026-11-20 10:15:00'),
                    ('TX1002', 'C002', 56000.00, 'CNY', '2026-11-21 14:22:00'),
                    ('TX1003', 'C003', 8920.00, 'USD', '2026-11-25 09:05:00');
                """
            )
            conn.commit()

    def list_profiles(self) -> List[Dict[str, Any]]:
        return [
            {
                "connection_id": "conn_demo_sqlite",
                "name": "SDK 示例数仓 (SQLite)",
                "driver_track": "NATIVE",
                "db_type": "SQLITE",
                "host": "",
                "port": 0,
                "dbname": "demo_warehouse",
                "default_schema": "main",
                "standard_format": "AUTO",
            }
        ]

    def get_profile(self, connection_id: str) -> Optional[Any]:
        return self._profiles.get(connection_id)

    def require_profile(self, connection_id: str) -> Any:
        prof = self._profiles.get(connection_id)
        if not prof:
            raise ValueError(f"沙箱连接不存在: {connection_id}")
        return prof

    @contextlib.contextmanager
    def readonly_session(self, connection_id: str, timeout_sec: int = 12):
        _ = timeout_sec
        self.require_profile(connection_id)
        conn = sqlite3.connect(f"file:{self.sample_db_path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()

    @contextlib.contextmanager
    def probe_session(self, connection_id: str, timeout_sec: int = 12):
        with self.readonly_session(connection_id, timeout_sec=timeout_sec) as conn:
            yield conn

    def close_warm_connections(self) -> None:
        pass


class _DevMetadataHub:
    def __init__(self, conn_hub: _DevConnectionHub) -> None:
        self.conn_hub = conn_hub

    def get_catalog_tree(self, connection_id: str, auto_refresh: bool = False) -> Dict[str, Any]:
        _ = auto_refresh
        with self.conn_hub.readonly_session(connection_id) as conn:
            rows = conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        tables = []
        for r in rows:
            tables.append(
                {
                    "connection_id": connection_id,
                    "schema_name": "main",
                    "table_name": r["name"],
                    "object_type": str(r["type"]).upper(),
                    "table_comment": "SDK 预置示例表",
                }
            )
        return {
            "schemas": [{"schema_name": "main", "table_count": len(tables), "view_count": 0}],
            "tables": tables,
        }

    def get_table_metadata(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        _ = (schema_name, force_refresh)
        if not _SAFE_IDENT_RE.match(table_name or ""):
            raise ValueError(f"非法表名: {table_name}")
        with self.conn_hub.readonly_session(connection_id) as conn:
            cols_raw = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        if not cols_raw:
            raise ValueError(f"表不存在: {table_name}")
        cols = [
            {
                "ordinal_position": int(c["cid"]) + 1,
                "column_name": c["name"],
                "data_type": c["type"] or "TEXT",
                "normalized_type": c["type"] or "TEXT",
                "is_not_null": bool(c["notnull"]),
                "is_primary_key": bool(c["pk"]),
                "column_comment": "",
            }
            for c in cols_raw
        ]
        return {
            "connection_id": connection_id,
            "schema_name": "main",
            "table_name": table_name,
            "object_type": "TABLE",
            "columns": cols,
        }


class _DevQueryEngine:
    def __init__(self, conn_hub: _DevConnectionHub) -> None:
        self.conn_hub = conn_hub

    def execute_readonly_sql(
        self,
        connection_id: str,
        sql: str,
        *,
        limit: int = 100,
        timeout_sec: int = 12,
    ) -> Any:
        _ = timeout_sec
        clean = str(sql or "").strip()
        if not clean:
            raise ValueError("SQL 语句不能为空")
        no_trail = clean.rstrip(";").strip()
        if ";" in no_trail:
            raise ValueError("只读沙箱仅允许执行单条 SQL 语句")
        if _READONLY_FORBIDDEN_RE.search(no_trail):
            raise ValueError("只读沙箱禁止执行写操作或 DDL 语句")
        if not re.match(r"^(?:SELECT|WITH|EXPLAIN)\b", no_trail, re.IGNORECASE):
            raise ValueError("只读沙箱仅放行 SELECT / WITH / EXPLAIN 查询")

        t0 = time.perf_counter()
        cap = max(1, min(200, int(limit or 100)))
        with self.conn_hub.readonly_session(connection_id) as conn:
            cur = conn.execute(no_trail)
            cols = [types.SimpleNamespace(name=d[0], data_type="TEXT") for d in (cur.description or [])]
            fetched = cur.fetchmany(cap + 1)
            truncated = len(fetched) > cap
            rows = [[row[i] for i in range(len(cols))] for row in fetched[:cap]]
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return types.SimpleNamespace(
            columns=[{"name": c.name, "data_type": c.data_type} for c in cols],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )


class _DevLLMHub:
    def list_providers(self) -> List[Dict[str, Any]]:
        return [
            {
                "provider_id": "sdk_mock_llm",
                "name": "SDK 离线模拟大模型",
                "provider_type": "OPENAI_COMPATIBLE",
                "enabled": True,
                "base_url": "http://127.0.0.1/mock",
                "has_api_key": True,
                "api_key_masked": "sk-sdk-****",
                "default_model": "sdk-mock-model",
                "models": ["sdk-mock-model"],
                "is_default": True,
            }
        ]

    def list_available_models(self) -> List[Dict[str, Any]]:
        return [{"provider_id": "sdk_mock_llm", "model": "sdk-mock-model", "label": "SDK Mock Model"}]

    def chat_completion(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        last = str((messages or [{}])[-1].get("content") or "")
        return {
            "ok": True,
            "model": "sdk-mock-model",
            "content": f"[SDK Mock LLM 响应] 已收到提示词（{len(last)} 字符）。生产环境将自动路由至用户配置的大模型。",
        }


class _DevStandardCatalog:
    _SAMPLES = [
        {"std_code": "STD-CUST-001", "cn_name": "客户编号", "en_name": "cust_id", "type_dws": "VARCHAR(32)", "data_category": "客户信息"},
        {"std_code": "STD-CUST-002", "cn_name": "客户名称", "en_name": "cust_name", "type_dws": "VARCHAR(120)", "data_category": "客户信息"},
        {"std_code": "STD-ID-001", "cn_name": "证件号码", "en_name": "id_no", "type_dws": "VARCHAR(32)", "data_category": "身份标识"},
        {"std_code": "STD-TX-001", "cn_name": "交易金额", "en_name": "tx_amt", "type_dws": "NUMERIC(18,2)", "data_category": "交易指标"},
        {"std_code": "STD-TX-002", "cn_name": "币种代码", "en_name": "ccy_code", "type_dws": "CHAR(3)", "data_category": "公共代码"},
    ]

    def get_summary(self) -> Dict[str, Any]:
        return {"total_count": len(self._SAMPLES), "filename": "SDK内置示例标准.xlsx", "imported_at": _now_str()}

    def list_items(self, keyword: str = "", page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        kw = str(keyword or "").strip().lower()
        items = [
            s
            for s in self._SAMPLES
            if not kw or kw in s["std_code"].lower() or kw in s["cn_name"].lower() or kw in s["en_name"].lower()
        ]
        return {"items": items, "total": len(items), "page": page, "page_size": page_size}


class _DevAssetStore:
    def __init__(self) -> None:
        self.app_ids: set[str] = {"settings"}

    def register_app_id(self, app_id: str) -> None:
        if app_id:
            self.app_ids.add(str(app_id))

    def get_public_appearance(self) -> Dict[str, Any]:
        return {
            "wallpaper_id": "default",
            "wallpaper_fit": "cover",
            "custom_wallpaper_url": "",
            "brand_icon_url": "",
            "app_icons": {},
        }


class DevModuleRegistry:
    def __init__(self) -> None:
        self._specs: Dict[str, Any] = {}

    def register(self, spec: Any) -> None:
        self._specs[spec.module_id] = spec

    def get(self, module_id: str) -> Optional[Any]:
        return self._specs.get(module_id)

    def list_manifests(self) -> List[Dict[str, Any]]:
        return [s.to_manifest_dict() for s in sorted(self._specs.values(), key=lambda x: (x.order, x.module_id))]


@dataclasses.dataclass(frozen=True)
class DevModuleSpec:
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


class DevPlatformContext:
    """供独立插件工作区使用的 API 兼容 PlatformContext."""

    def __init__(self, data_dir: str, platform_version: str = "__PLATFORM_VERSION__") -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.app_version = platform_version
        self.store = _DevStateStore(self.data_dir)
        self.connection_hub = _DevConnectionHub(self.data_dir)
        self.metadata_hub = _DevMetadataHub(self.connection_hub)
        self.query_engine = _DevQueryEngine(self.connection_hub)
        self.llm_hub = _DevLLMHub()
        self.knowledge_hub = types.SimpleNamespace(
            get_summary=lambda: {"total_docs": 0},
            search=lambda *a, **kw: [],
            list_documents=lambda *a, **kw: [],
        )
        self.standard_catalog = _DevStandardCatalog()
        self.asset_store = _DevAssetStore()
        self.import_progress = _DevProgressHub()
        self.task_progress = self.import_progress
        self.module_registry = DevModuleRegistry()
        self._registered_modules: List[Dict[str, str]] = []
        self._frontend_plugins: List[Dict[str, Any]] = []

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
        self._registered_modules.append(
            {
                "module_id": module_id,
                "title": title,
                "subtitle": subtitle,
                "api_prefix": api_prefix,
                "desktop_app_id": desktop_app_id,
            }
        )

    def build_platform_bootstrap(self) -> Dict[str, Any]:
        conns = self.connection_hub.list_profiles()
        trees = {}
        for c in conns:
            cid = c["connection_id"]
            cat = self.metadata_hub.get_catalog_tree(cid)
            trees[cid] = {
                "schema_count": len(cat["schemas"]),
                "table_count": len(cat["tables"]),
                "schemas": cat["schemas"],
                "tables": cat["tables"],
            }
        return {
            "app_name": "Data.O.G",
            "brand_name": "Data.O.G",
            "version": self.app_version,
            "platform_abi": "4.0",
            "storage_mode": "SEPARATED",
            "license": {
                "authorized": True,
                "status": "ACTIVE",
                "status_message": "SDK 离线开发沙箱",
                "expires_date": "2099-12-31",
                "login_modes": ["ACTIVATION_CODE"],
            },
            "active_workspace": {"workspace_id": "dev-ws", "name": "SDK 离线沙箱工作区", "is_default": True},
            "workspaces": [{"workspace_id": "dev-ws", "name": "SDK 离线沙箱工作区", "is_default": True}],
            "appearance": self.asset_store.get_public_appearance(),
            "connections": conns,
            "catalog_trees": trees,
            "drivers": [],
            "driver_templates": [],
            "java_runtime": {"available": False, "jdbc_extension_ready": False},
            "llm_providers": self.llm_hub.list_providers(),
            "llm_models": self.llm_hub.list_available_models(),
            "knowledge_summary": {"total_docs": 0},
            "standard_dict_summary": self.standard_catalog.get_summary(),
            "plugins": list(self._frontend_plugins),
            "plugin_summary": {"platform_abi": "4.0", "installed_count": len(self._frontend_plugins), "active_count": len(self._frontend_plugins), "dev_count": len(self._frontend_plugins)},
            "modules": list(self._registered_modules),
            "logs": self.store.list_logs(limit=150),
        }

    def close(self) -> None:
        self.store.close()


def get_platform_context(request: Request) -> DevPlatformContext:
    ctx = getattr(request.app.state, "platform_ctx", None)
    if ctx is None:
        raise RuntimeError("DevPlatformContext 未绑定")
    return ctx


def _json_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(message)}, status_code=status_code)


def _extract_task_id(request: Request, query_task_id: str = "") -> str:
    cand = (query_task_id or request.headers.get("x-task-id", "") or "").strip()
    return cand if _DevProgressHub.is_valid_id(cand) else ""


def _file_download_response(
    content: bytes,
    filename: str,
    *,
    media_type: str = "application/octet-stream",
    ascii_fallback: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Response:
    safe_name = (filename or "download.bin").replace("\r", "").replace("\n", "")
    disp = f"attachment; filename*=UTF-8''{quote(safe_name)}"
    if ascii_fallback:
        disp = f"attachment; filename={ascii_fallback}; filename*=UTF-8''{quote(safe_name)}"
    hdrs = {"Content-Disposition": disp, "X-Content-Type-Options": "nosniff"}
    if extra_headers:
        hdrs.update(extra_headers)
    return Response(content=content, media_type=media_type, headers=hdrs)


async def _run_tracked_task(
    *,
    request: Request,
    task_id: str,
    ctx: DevPlatformContext,
    title: str,
    start_detail: str,
    fn: Callable[..., Any],
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    default_detail: str = "处理完成",
    finish_stage: str = "处理完成",
    detail_fn: Optional[Callable[[Any], str]] = None,
    count_fn: Optional[Callable[[Any], Tuple[int, int]]] = None,
    warning_fn: Optional[Callable[[Any], str]] = None,
    response_fn: Optional[Callable[[Any], Any]] = None,
    inject_progress_cb: bool = True,
    inject_cancel_check: bool = False,
    error_status: int = 400,
) -> Any:
    tid = _extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(tid, title, detail=start_detail)
        call_kwargs = dict(kwargs or {})
        if inject_progress_cb:
            call_kwargs["progress_cb"] = ctx.task_progress.make_reporter(tid)
        if inject_cancel_check:
            call_kwargs["cancel_check"] = (lambda: ctx.task_progress.is_cancelled(tid)) if tid else None
        res = await run_in_threadpool(lambda: fn(*args, **call_kwargs))
        if tid and not ctx.task_progress.is_cancelled(tid):
            processed, total = count_fn(res) if count_fn else (0, 0)
            detail = detail_fn(res) if detail_fn else (str(res.get("message") or default_detail) if isinstance(res, dict) else default_detail)
            warning = warning_fn(res) if warning_fn else (str(res.get("warning") or "") if isinstance(res, dict) else "")
            ctx.task_progress.finish(tid, stage=finish_stage, detail=detail, warning=warning, processed=processed, total=total)
        return response_fn(res) if response_fn else res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _json_error(str(exc), error_status)


async def _run_tracked_upload(
    *,
    ctx: DevPlatformContext,
    progress_id: str,
    title: str,
    fn: Callable[..., Dict[str, Any]],
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    default_detail: str = "导入完成",
    detail_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    count_fn: Optional[Callable[[Dict[str, Any]], Tuple[int, int]]] = None,
    error_status: int = 400,
) -> Any:
    pid = (progress_id or "").strip()
    try:
        ctx.import_progress.start(pid, title)
        call_kwargs = dict(kwargs or {})
        call_kwargs["progress_cb"] = ctx.import_progress.make_reporter(pid)
        res = await run_in_threadpool(lambda: fn(*args, **call_kwargs))
        processed, total = count_fn(res) if count_fn else (0, 0)
        detail = detail_fn(res) if detail_fn else str(res.get("message") or default_detail)
        ctx.import_progress.finish(pid, stage="导入完成", detail=detail, processed=processed, total=total)
        return res
    except Exception as exc:
        ctx.import_progress.fail(pid, str(exc))
        return _json_error(str(exc), error_status)


def install_dev_platform() -> None:
    """在离线工作区将兼容的 `core.platform.*` 模块装配入 sys.modules（仅当真实 DataOG 底座不在路径中时）."""
    if "core.platform.context" in sys.modules:
        return

    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = []  # type: ignore[attr-defined]
    plat_pkg = types.ModuleType("core.platform")
    plat_pkg.__path__ = []  # type: ignore[attr-defined]

    ctx_mod = types.ModuleType("core.platform.context")
    ctx_mod.PlatformContext = DevPlatformContext  # type: ignore[attr-defined]
    ctx_mod.get_platform_context = get_platform_context  # type: ignore[attr-defined]

    reg_mod = types.ModuleType("core.platform.module_registry")
    reg_mod.ModuleSpec = DevModuleSpec  # type: ignore[attr-defined]

    http_mod = types.ModuleType("core.platform.http_utils")
    http_mod.MEDIA_TYPE_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"  # type: ignore[attr-defined]
    http_mod.MEDIA_TYPE_HTML = "text/html; charset=utf-8"  # type: ignore[attr-defined]
    http_mod.MEDIA_TYPE_JSON = "application/json; charset=utf-8"  # type: ignore[attr-defined]
    http_mod.json_error = _json_error  # type: ignore[attr-defined]
    http_mod.extract_task_id = _extract_task_id  # type: ignore[attr-defined]
    http_mod.file_download_response = _file_download_response  # type: ignore[attr-defined]
    http_mod.run_tracked_task = _run_tracked_task  # type: ignore[attr-defined]
    http_mod.run_tracked_upload = _run_tracked_upload  # type: ignore[attr-defined]

    excel_mod = types.ModuleType("core.platform.excel_io")
    excel_mod.sanitize_excel_cell = lambda v: ("'" + str(v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v)  # type: ignore[attr-defined]
    excel_mod.sanitize_excel_row = lambda row: [excel_mod.sanitize_excel_cell(c) for c in row]  # type: ignore[attr-defined]
    excel_mod.sanitize_excel_sheet_title = lambda t: re.sub(r"[\\/*?:\[\]]", "_", str(t or "Sheet1"))[:31] or "Sheet1"  # type: ignore[attr-defined]

    sys.modules["core"] = core_pkg
    sys.modules["core.platform"] = plat_pkg
    sys.modules["core.platform.context"] = ctx_mod
    sys.modules["core.platform.module_registry"] = reg_mod
    sys.modules["core.platform.http_utils"] = http_mod
    sys.modules["core.platform.excel_io"] = excel_mod


def create_dev_app(plugin_dir: str = ".", *, data_dir: Optional[str] = None) -> FastAPI:
    """构建轻量离线开发宿主 FastAPI 实例并挂载当前插件 + ui_shell."""
    install_dev_platform()
    abs_plugin = os.path.abspath(plugin_dir)
    manifest_path = os.path.join(abs_plugin, "plugin.json")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    plugin_id = str(manifest["plugin_id"]).strip()

    eff_data_dir = os.path.abspath(data_dir) if data_dir else os.path.join(abs_plugin, ".dataog_dev_data")
    ctx = DevPlatformContext(data_dir=eff_data_dir)

    app = FastAPI(title=f"Data.O.G SDK DevHost ({plugin_id})", version=ctx.app_version)
    app.state.platform_ctx = ctx
    app.state.store = ctx.store

    # 注册平台轻量接口以驱动真实 ui_shell/main.js
    @app.get("/api/platform/auth/status")
    def dev_auth_status():
        st = ctx.build_platform_bootstrap()["license"]
        return {"ok": True, "license": st, "appearance": ctx.asset_store.get_public_appearance(), **st}

    @app.get("/api/platform/bootstrap")
    def dev_bootstrap():
        return {"ok": True, **ctx.build_platform_bootstrap()}

    @app.get("/api/platform/connections")
    def dev_connections():
        return {"ok": True, "connections": ctx.connection_hub.list_profiles()}

    @app.get("/api/platform/metadata/{connection_id}/tree")
    def dev_meta_tree(connection_id: str, auto_refresh: bool = False):
        _ = auto_refresh
        return {"ok": True, **ctx.metadata_hub.get_catalog_tree(connection_id)}

    @app.get("/api/platform/metadata/{connection_id}/table")
    def dev_meta_table(connection_id: str, schema_name: str = "main", table_name: str = "", force_refresh: bool = False):
        _ = force_refresh
        return {"ok": True, "table": ctx.metadata_hub.get_table_metadata(connection_id, schema_name, table_name)}

    @app.get("/api/platform/metadata/search")
    def dev_meta_search(keyword: str = "", connection_id: str = "", limit: int = 50):
        _ = (connection_id, limit)
        kw = str(keyword or "").lower()
        cat = ctx.metadata_hub.get_catalog_tree("conn_demo_sqlite")
        hits = [t for t in cat["tables"] if not kw or kw in t["table_name"].lower()]
        return {"ok": True, "items": hits}

    @app.post("/api/platform/query/execute-readonly")
    def dev_exec_readonly(payload: Dict[str, Any]):
        res = ctx.query_engine.execute_readonly_sql(
            str(payload.get("connection_id") or "conn_demo_sqlite"),
            str(payload.get("sql") or ""),
            limit=int(payload.get("limit") or 100),
        )
        return {"ok": True, "result": vars(res)}

    @app.get("/api/platform/standards")
    def dev_standards(keyword: str = "", page: int = 1, page_size: int = 50):
        return {"ok": True, **ctx.standard_catalog.list_items(keyword=keyword, page=page, page_size=page_size)}

    @app.get("/api/platform/tasks/{task_id}")
    def dev_task_status(task_id: str):
        snap = ctx.task_progress.get(task_id) or {"task_id": task_id, "status": "unknown", "percent": 0}
        return {"ok": True, "progress": snap}

    @app.post("/api/platform/tasks/{task_id}/cancel")
    def dev_task_cancel(task_id: str):
        ctx.task_progress.cancel(task_id)
        return {"ok": True}

    @app.get("/api/platform/import-progress/{progress_id}")
    def dev_import_progress(progress_id: str):
        snap = ctx.import_progress.get(progress_id) or {"task_id": progress_id, "status": "unknown", "percent": 0}
        return {"ok": True, "progress": snap}

    @app.get("/api/platform/logs")
    def dev_logs(limit: int = 150):
        return {"ok": True, "logs": ctx.store.list_logs(limit=limit)}

    @app.post("/api/platform/logs/clear")
    def dev_logs_clear():
        return {"ok": True}

    @app.get("/api/platform/diagnostics/ping")
    def dev_diag_ping():
        return {"ok": True, "ts": time.time()}

    # 加载当前开发中的插件后端
    backend_dir = os.path.join(abs_plugin, "backend")
    vendor_dir = os.path.join(backend_dir, "vendor")
    if os.path.isdir(vendor_dir) and vendor_dir not in sys.path:
        sys.path.append(vendor_dir)

    if "dataog_plugins" not in sys.modules:
        ns = types.ModuleType("dataog_plugins")
        ns.__path__ = []  # type: ignore[attr-defined]
        ns.__package__ = "dataog_plugins"
        sys.modules["dataog_plugins"] = ns

    full_pkg = f"dataog_plugins.{plugin_id}"
    for k in list(sys.modules.keys()):
        if k == full_pkg or k.startswith(full_pkg + "."):
            sys.modules.pop(k, None)

    pkg_spec = importlib.util.spec_from_file_location(
        full_pkg,
        os.path.join(backend_dir, "__init__.py"),
        submodule_search_locations=[backend_dir],
    )
    if pkg_spec is None or pkg_spec.loader is None:
        raise RuntimeError("无法加载插件 backend/__init__.py")
    mod_obj = importlib.util.module_from_spec(pkg_spec)
    sys.modules[full_pkg] = mod_obj
    pkg_spec.loader.exec_module(mod_obj)

    spec = getattr(mod_obj, "MODULE_SPEC", None)
    if spec is None:
        raise RuntimeError("backend/__init__.py 未导出 MODULE_SPEC")

    storage_decl = manifest.get("storage") or {}
    ctx.store.register_workspace_tables(list(spec.workspace_tables) + list(storage_decl.get("workspace_tables") or []))
    ctx.store.register_private_tables(list(spec.private_tables) + list(storage_decl.get("private_tables") or []))
    ctx.store.register_workspace_meta_keys(list(spec.workspace_meta_keys) + list(storage_decl.get("workspace_meta_keys") or []))
    ctx.module_registry.register(spec)
    ctx.register_module_manifest(
        module_id=spec.module_id,
        title=spec.title,
        subtitle=spec.subtitle,
        api_prefix=spec.api_prefix,
        desktop_app_id=spec.desktop_app_id,
    )
    if spec.init_hook:
        spec.init_hook(ctx)
    if spec.router is not None:
        app.include_router(spec.router)

    d_app = manifest["desktop_app"]
    entry_js = str(d_app.get("entry_js") or "register.js").lstrip("/")
    ctx._frontend_plugins.append(
        {
            "plugin_id": plugin_id,
            "app_id": d_app["app_id"],
            "title": d_app["title"],
            "version": manifest["version"],
            "entry_url": f"/plugins/{plugin_id}/{entry_js}",
            "stylesheets": [f"/plugins/{plugin_id}/{str(s).lstrip('/')}" for s in (d_app.get("stylesheets") or [])],
        }
    )

    # 挂载插件前端静态目录
    frontend_dir = os.path.realpath(os.path.join(abs_plugin, "frontend"))
    mime_map = {
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".html": "text/html; charset=utf-8",
    }

    @app.api_route("/plugins/{req_pid}/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def dev_plugin_asset(req_pid: str, asset_path: str) -> Response:
        if req_pid != plugin_id:
            return JSONResponse(status_code=404, content={"ok": False, "error": "未知插件"})
        clean_rel = str(asset_path or "").strip().lstrip("/")
        parts = clean_rel.split("/")
        if not clean_rel or any(not p or p in (".", "..") for p in parts):
            return JSONResponse(status_code=404, content={"ok": False, "error": "非法路径"})
        target = os.path.realpath(os.path.join(frontend_dir, *parts))
        if not target.startswith(frontend_dir + os.sep) or not os.path.isfile(target):
            return JSONResponse(status_code=404, content={"ok": False, "error": "资源不存在"})
        ext = os.path.splitext(target)[1].lower()
        mt = mime_map.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            return Response(content=fh.read(), media_type=mt, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    ui_shell_dir = os.path.join(_SDK_DIR, "ui_shell")
    if os.path.isdir(ui_shell_dir):
        app.mount("/", StaticFiles(directory=ui_shell_dir, html=True), name="static")
    return app


class _DevAsgiResponse:
    def __init__(self, status_code: int, raw_headers: List[Tuple[bytes, bytes]], content: bytes) -> None:
        self.status_code = status_code
        self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class DevTestClient:
    """零 httpx 依赖的离线 ASGI 单元测试客户端."""

    __test__ = False

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(self, method: str, url: str, *, json_body: Any = None) -> _DevAsgiResponse:
        if "?" in url:
            path, qs = url.split("?", 1)
        else:
            path, qs = url, ""
        hdrs: List[Tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:18090"), (b"sec-fetch-site", b"same-origin")]
        body = b""
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.append((b"content-type", b"application/json"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": unquote(path),
            "raw_path": path.encode("utf-8"),
            "query_string": qs.encode("utf-8"),
            "headers": hdrs,
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 18090),
        }

        async def _call() -> _DevAsgiResponse:
            sent = False
            done = asyncio.Event()
            st = 500
            rh: List[Tuple[bytes, bytes]] = []
            chunks: List[bytes] = []

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(msg):
                nonlocal st, rh
                if msg["type"] == "http.response.start":
                    st = int(msg["status"])
                    rh = list(msg.get("headers") or [])
                elif msg["type"] == "http.response.body":
                    chunks.append(msg.get("body") or b"")
                    if not msg.get("more_body", False):
                        done.set()

            await self.app(scope, receive, send)
            return _DevAsgiResponse(st, rh, b"".join(chunks))

        return asyncio.run(_call())

    def get(self, url: str) -> _DevAsgiResponse:
        return self.request("GET", url)

    def post(self, url: str, *, json: Any = None) -> _DevAsgiResponse:
        return self.request("POST", url, json_body=json)

    def put(self, url: str, *, json: Any = None) -> _DevAsgiResponse:
        return self.request("PUT", url, json_body=json)

    def delete(self, url: str) -> _DevAsgiResponse:
        return self.request("DELETE", url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Data.O.G 4.0 插件离线双轨开发预览宿主")
    parser.add_argument("--plugin-dir", default=".", help="插件工作区目录（默认当前目录）")
    parser.add_argument("--port", type=int, default=18090, help="本地预览端口 (默认 18090)")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args()

    app = create_dev_app(args.plugin_dir)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[Data.O.G SDK DevHost] 离线开发宿主已启动: {url}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", ws="none")


if __name__ == "__main__":
    main()
'''

VALIDATE_PLUGIN_PY_TEMPLATE = r'''"""Data.O.G 4.0 插件离线强合约与安全防线校验器 (.dataog_sdk/validate_plugin.py)."""

from __future__ import annotations

import ast
import glob
import importlib
import inspect
import json
import os
import re
import sys
import tempfile
import typing
from typing import Any, Dict, List

_SDK_DIR = os.path.dirname(os.path.abspath(__file__))
if _SDK_DIR not in sys.path:
    sys.path.insert(0, _SDK_DIR)

_SAFE_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_SAFE_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_SAFE_SQL_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

RESERVED_IDS = frozenset(
    {
        "standard_migrator",
        "cross_comparator",
        "ddl_translator",
        "data_explorer",
        "ai_assistant",
        "settings",
        "explorer",
        "standard",
        "polyglot",
        "platform",
        "core",
        "modules",
        "plugins",
    }
)
FORBIDDEN_BINARY_EXTS = (
    ".pyd", ".so", ".dylib", ".dll", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".jar", ".pyc", ".pyo"
)
FORBIDDEN_JS_PATTERNS = (
    (re.compile(r"\.innerHTML\b"), "禁止使用 innerHTML（请使用 /js/core/dom.js 的 el/clearEl）"),
    (re.compile(r"\.outerHTML\b"), "禁止使用 outerHTML"),
    (re.compile(r"\.insertAdjacentHTML\b"), "禁止使用 insertAdjacentHTML"),
    (re.compile(r"\bdocument\.write\b"), "禁止使用 document.write"),
    (re.compile(r"\beval\s*\("), "禁止使用 eval()"),
    (re.compile(r"\bnew\s+Function\b"), "禁止使用 new Function()"),
    (re.compile(r"(?<!\.)\b(?:alert|confirm|prompt)\s*\(|\bwindow\.(?:alert|confirm|prompt)\s*\("), "禁止阻塞式原生弹窗（请使用 /js/ui/modal.js 的 Modal）"),
)


def validate_plugin_dir(plugin_dir: str = ".") -> Dict[str, Any]:
    abs_dir = os.path.abspath(plugin_dir)
    errors: List[str] = []

    manifest_path = os.path.join(abs_dir, "plugin.json")
    if not os.path.isfile(manifest_path):
        raise ValueError(f"缺失 plugin.json: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    if manifest.get("schema_version") != 1:
        errors.append("plugin.json schema_version 必须为 1")
    if str(manifest.get("api_version") or "") != "4.0":
        errors.append("plugin.json api_version 必须为 '4.0'")

    pid = str(manifest.get("plugin_id") or "").strip()
    if not _SAFE_PLUGIN_ID_RE.match(pid) or pid in RESERVED_IDS:
        errors.append(f"非法的或保留的 plugin_id: {pid!r}")

    for vkey in ("version", "min_platform_version"):
        if not _SEMVER_RE.match(str(manifest.get(vkey) or "")):
            errors.append(f"plugin.json {vkey} 必须为三段式 SemVer")

    d_app = manifest.get("desktop_app") or {}
    app_id = str(d_app.get("app_id") or "").strip()
    if not _SAFE_APP_ID_RE.match(app_id) or app_id in RESERVED_IDS:
        errors.append(f"非法的或保留的 desktop_app.app_id: {app_id!r}")

    backend_cfg = manifest.get("backend") or {}
    if backend_cfg.get("api_prefix") != f"/api/modules/{pid}":
        errors.append(f"backend.api_prefix 必须为 '/api/modules/{pid}'")

    storage_cfg = manifest.get("storage") or {}
    for skey in ("workspace_tables", "private_tables"):
        for tbl in storage_cfg.get(skey) or []:
            if not _SAFE_SQL_IDENT_RE.match(str(tbl)):
                errors.append(f"storage.{skey} 存在非法表名: {tbl!r}")

    # 扫描 backend/
    backend_dir = os.path.join(abs_dir, "backend")
    if not os.path.isfile(os.path.join(backend_dir, "__init__.py")):
        errors.append("缺失 backend/__init__.py")
    else:
        for cur_root, dirs, files in os.walk(backend_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                full = os.path.join(cur_root, fname)
                rel = os.path.relpath(full, abs_dir)
                if fname.lower().endswith(FORBIDDEN_BINARY_EXTS):
                    errors.append(f"禁止包含编译态或可执行文件: {rel}")
                    continue
                if not fname.endswith(".py"):
                    continue
                with open(full, "r", encoding="utf-8") as fh:
                    src = fh.read()
                tree = ast.parse(src, filename=full)
                for node in ast.walk(tree):
                    mods = []
                    if isinstance(node, ast.Import):
                        mods = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        mods = [node.module]
                    for m in mods:
                        if m == "modules" or m.startswith("modules."):
                            errors.append(f"{rel} 严禁反向或跨模块导入 {m}")
                if fname == "router.py":
                    for m in re.finditer(
                        r"@router\.\w+\([^\)]+\)\s*(?:@router\.\w+\([^\)]+\)\s*)*(?:async\s+)?def\s+(\w+)\s*\((.*?)\)\s*(?:->.*?)?:",
                        src,
                        re.S,
                    ):
                        if "get_platform_context" not in m.group(2):
                            errors.append(f"{rel}:{m.group(1)} 缺少 Gate 2 依赖 get_platform_context")

    # 扫描 frontend/
    frontend_dir = os.path.join(abs_dir, "frontend")
    entry_rel = str(d_app.get("entry_js") or "register.js")
    entry_full = os.path.join(frontend_dir, entry_rel)
    if not os.path.isfile(entry_full):
        errors.append(f"缺失声明的前端入口: frontend/{entry_rel}")
    else:
        with open(entry_full, "r", encoding="utf-8") as fh:
            reg_src = fh.read()
        if "registerDesktopApp" not in reg_src:
            errors.append(f"frontend/{entry_rel} 必须调用 registerDesktopApp")

    for cur_root, _, files in os.walk(frontend_dir):
        for fname in files:
            if not fname.endswith((".js", ".mjs")):
                continue
            full = os.path.join(cur_root, fname)
            rel = os.path.relpath(full, abs_dir)
            with open(full, "r", encoding="utf-8") as fh:
                js_src = fh.read()
            for pat, msg in FORBIDDEN_JS_PATTERNS:
                if pat.search(js_src):
                    errors.append(f"{rel}: {msg}")
            if re.search(r"""from\s+["']\.\.\/.*\/(?:core|ui)\/""", js_src):
                errors.append(f"{rel}: 禁止使用相对路径导入平台 core/ui，请使用根绝对路径 '/js/core/...' 或 '/js/ui/...'")

    if errors:
        raise ValueError("插件合约校验失败:\n- " + "\n- ".join(errors))

    # 运行真实离线 ASGI 冒烟校验
    import dev_host

    with tempfile.TemporaryDirectory() as tmp:
        app = dev_host.create_dev_app(abs_dir, data_dir=tmp)
        client = dev_host.DevTestClient(app)
        boot = client.get("/api/platform/bootstrap")
        if boot.status_code != 200 or not boot.json().get("ok"):
            raise RuntimeError(f"离线宿主 bootstrap 失败: {boot.status_code}")
        reg_resp = client.get(f"/plugins/{pid}/{entry_rel}")
        if reg_resp.status_code != 200:
            raise RuntimeError(f"插件前端入口访问失败: {reg_resp.status_code}")
        openapi = app.openapi()
        plugin_paths = [p for p in openapi.get("paths", {}) if p.startswith(f"/api/modules/{pid}/")]
        if not plugin_paths:
            raise RuntimeError(f"OpenAPI 中未挂载任何 /api/modules/{pid}/ 路由")

        # 校验 router 类型注解完整可解析
        router_mod = sys.modules.get(f"dataog_plugins.{pid}.router")
        if router_mod is not None:
            for _, fn in inspect.getmembers(router_mod, inspect.isfunction):
                if getattr(fn, "__module__", "") == router_mod.__name__:
                    typing.get_type_hints(fn, include_extras=True)

    return {
        "ok": True,
        "plugin_id": pid,
        "version": manifest["version"],
        "api_prefix": f"/api/modules/{pid}",
    }


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    res = validate_plugin_dir(target)
    print(f"[VALIDATE] OK | plugin_id={res['plugin_id']} | version=v{res['version']} | prefix={res['api_prefix']}")


if __name__ == "__main__":
    main()
'''

PACK_PLUGIN_PY_TEMPLATE = r'''"""Data.O.G 4.0 插件加密混淆签名打包器 (.dataog_sdk/pack_plugin.py).

纯 Python 标准库实现：
- 先强制执行 `validate_plugin.validate_plugin_dir()` 全量合约自检；
- 使用 `DGAP` (BLAKE2b-512-CTR + HMAC-SHA256) + Ed25519 双重签名生成零明文代码暴露的
  `.dogapp` 与银行邮件网关安全外层 `.zip`；
- 本脚本严禁内置任何签名私钥，私钥须通过 `--sign-key` 或环境变量 `DATAOG_UPDATE_SIGNING_KEY` 提供。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import struct
import sys
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SDK_DIR = os.path.dirname(os.path.abspath(__file__))
if _SDK_DIR not in sys.path:
    sys.path.insert(0, _SDK_DIR)

import validate_plugin

DGP_PLUGIN_MAGIC = b"DGAP"
DGP2_BUNDLE_MAGIC = b"DGPB"
DGP2_BUNDLE_VERSION = 1
PLUGIN_FORMAT_VERSION = 1
PLUGIN_HEADER_CORE_STRUCT = struct.Struct("!4sHHB3s24sII")
PLUGIN_SIG_DOMAIN = b"DataOG-Plugin-v1:"
PLUGIN_MANIFEST_SIG_DOMAIN = b"DataOG-Plugin-v1-Manifest:"
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FIXED_ZIP_DT = (2025, 1, 1, 0, 0, 0)

# 与主平台严格一致的混淆派生信封盐值（非私钥）
_ENV_PERM = (
    9, 10, 23, 1, 7, 17, 11, 21, 13, 15, 0, 12, 26, 14, 27, 16,
    3, 20, 8, 2, 5, 24, 6, 18, 28, 19, 30, 29, 22, 4, 31, 25,
)
_ENV_T0 = (0x1FDDD60E4897AEF4, 0x943281DA7EFB203E, 0x0BC334FDCF6C98F5, 0x6841D7FA306DFE7B)
_ENV_T1 = (0x9290F2A3323CFE47, 0x72AFA8295D683C05, 0x4C70ED8AF6F844C6, 0x011FB8904300BEA3)
_ENV_T2 = (0x0D56DC49FD664536, 0x55CDEFC91FA67E04, 0x62CA6159DDCA6864, 0x379390CBAFF9B2C5)
_ENV_T3 = (0x2193F52D9A8FC0DC, 0x79EF9711A4709A72, 0xB005CA05E54BC5EB, 0x8EEAF8887FD5DDB9)

_ED_P = (1 << 255) - 19
_ED_L = (1 << 252) + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P)
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if x % 2 != 0:
        x = _ED_P - x
    return x


_ED_BY = (4 * pow(5, _ED_P - 2, _ED_P)) % _ED_P
_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = (_ED_BX % _ED_P, _ED_BY % _ED_P, 1, (_ED_BX * _ED_BY) % _ED_P)
_ED_IDENT = (0, 1, 1, 0)


def _ed_add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = ((y1 - x1) * (y2 - x2)) % _ED_P
    b = ((y1 + x1) * (y2 + x2)) % _ED_P
    c = (2 * _ED_D * t1 * t2) % _ED_P
    d = (2 * z1 * z2) % _ED_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return ((e * f) % _ED_P, (g * h) % _ED_P, (f * g) % _ED_P, (e * h) % _ED_P)


def _ed_scalarmult(pt, e: int):
    res = _ED_IDENT
    cur = pt
    while e > 0:
        if e & 1:
            res = _ed_add(res, cur)
        cur = _ed_add(cur, cur)
        e >>= 1
    return res


def _ed_encodepoint(pt) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, _ED_P - 2, _ED_P)
    return (((y * zi) % _ED_P) | ((((x * zi) % _ED_P) & 1) << 255)).to_bytes(32, "little")


def _ed25519_derive_pubkey(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return _ed_encodepoint(_ed_scalarmult(_ED_BASE, a))


def _ed25519_sign(msg: bytes, seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    prefix = h[32:64]
    pk = _ed_encodepoint(_ed_scalarmult(_ED_BASE, a))
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _ED_L
    r_bytes = _ed_encodepoint(_ed_scalarmult(_ED_BASE, r))
    k = int.from_bytes(hashlib.sha512(r_bytes + pk + msg).digest(), "little") % _ED_L
    s = (r + k * a) % _ED_L
    return r_bytes + s.to_bytes(32, "little")


def _reconstruct_pepper() -> bytes:
    buf = bytearray(32)
    for i in range(32):
        q, sh = i // 8, (i % 8) * 8
        buf[_ENV_PERM[i]] = (
            ((_ENV_T0[q] >> sh) & 0xFF)
            ^ ((_ENV_T1[q] >> sh) & 0xFF)
            ^ ((_ENV_T2[q] >> sh) & 0xFF)
            ^ ((_ENV_T3[q] >> sh) & 0xFF)
        )
    return bytes(buf)


def _ctr_xor(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return b""
    base_h = hashlib.blake2b(key=enc_key, person=b"DG2-CTR-STR-V02\x00", digest_size=64)
    base_h.update(nonce)
    out = bytearray(n)
    pos, blk_idx, chunk = 0, 0, 65536
    while pos < n:
        end = min(pos + chunk, n)
        need = end - pos
        num_blks = (need + 63) // 64
        ks = b"".join(
            (lambda h: (h.update((blk_idx + i).to_bytes(8, "big")), h.digest())[1])(base_h.copy())
            for i in range(num_blks)
        )[:need]
        out[pos:end] = (int.from_bytes(data[pos:end], "little") ^ int.from_bytes(ks, "little")).to_bytes(need, "little")
        pos = end
        blk_idx += num_blks
    return bytes(out)


def _pack_dgpb(entries: Sequence[Tuple[str, bytes]]) -> bytes:
    items = sorted(((n, n.encode("utf-8"), bytes(d)) for n, d in entries), key=lambda x: x[0])
    buf = io.BytesIO()
    buf.write(struct.pack("!4sHI", DGP2_BUNDLE_MAGIC, DGP2_BUNDLE_VERSION, len(items)))
    for _, nb, db in items:
        buf.write(struct.pack("!HI", len(nb), len(db)))
        buf.write(nb)
        buf.write(db)
    return buf.getvalue()


def _collect_entries(plugin_dir: str) -> Tuple[Dict[str, Any], List[Tuple[str, bytes]]]:
    abs_dir = os.path.abspath(plugin_dir)
    with open(os.path.join(abs_dir, "plugin.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    entries: List[Tuple[str, bytes]] = []
    with open(os.path.join(abs_dir, "plugin.json"), "rb") as fh:
        entries.append(("plugin.json", fh.read()))
    for sub in ("backend", "frontend"):
        sub_dir = os.path.join(abs_dir, sub)
        for cur_root, dirs, files in os.walk(sub_dir, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            for fname in sorted(files):
                if fname.startswith(".") or fname in (".DS_Store", "Thumbs.db", "Desktop.ini"):
                    continue
                full = os.path.join(cur_root, fname)
                rel = os.path.relpath(full, abs_dir).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    entries.append((rel, fh.read()))
    entries.sort(key=lambda x: x[0])
    return manifest, entries


def pack_plugin(
    plugin_dir: str = ".",
    *,
    output_dir: str = "dist",
    sign_key_file: str = "",
    check_only: bool = False,
) -> Dict[str, Any]:
    val_res = validate_plugin.validate_plugin_dir(plugin_dir)
    manifest, payload_entries = _collect_entries(plugin_dir)
    if check_only:
        return {"ok": True, "check_only": True, **val_res, "file_count": len(payload_entries)}

    raw_hex = ""
    if sign_key_file:
        with open(sign_key_file, "r", encoding="utf-8") as fh:
            raw_hex = fh.read().strip()
    elif (os.environ.get("DATAOG_UPDATE_SIGNING_KEY") or "").strip():
        raw_hex = os.environ["DATAOG_UPDATE_SIGNING_KEY"].strip()
    elif (os.environ.get("DATAOG_MASTER_SEED") or "").strip():
        raw_hex = os.environ["DATAOG_MASTER_SEED"].strip()
    if not raw_hex:
        raise ValueError("打包签名需要私钥种子：请通过 --sign-key 或环境变量 DATAOG_UPDATE_SIGNING_KEY 提供 64 位十六进制 Ed25519 私钥种子（或使用 --check-only 执行无私钥校验）")
    if not _HEX64_RE.match(raw_hex):
        raise ValueError("签名私钥格式无效：必须为 64 位十六进制字符串")
    seed = bytes.fromhex(raw_hex)
    pubkey = _ed25519_derive_pubkey(seed)

    pid = manifest["plugin_id"]
    ver = manifest["version"]
    pkg_meta = {
        "format_version": PLUGIN_FORMAT_VERSION,
        "plugin_id": pid,
        "version": ver,
        "abi_version": "4.0",
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in payload_entries},
    }
    pkg_meta_raw = json.dumps(pkg_meta, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    sig_raw = _ed25519_sign(PLUGIN_MANIFEST_SIG_DOMAIN + pkg_meta_raw, seed)
    raw_bundle = _pack_dgpb(list(payload_entries) + [("package_manifest.json", pkg_meta_raw), ("package_manifest.sig", sig_raw)])

    nonce = secrets.token_bytes(24)
    compressed = zlib.compress(raw_bundle, level=9)
    ikm = _reconstruct_pepper() + b"DataOG-Plugin-v1:" + pubkey + nonce
    enc_key = hashlib.blake2b(ikm, person=b"DGAP-ENC-V01\x00\x00\x00\x00", digest_size=32).digest()
    mac_key = hashlib.blake2b(ikm, person=b"DGAP-MAC-V01\x00\x00\x00\x00", digest_size=32).digest()
    ciphertext = _ctr_xor(compressed, enc_key, nonce)
    header_core = PLUGIN_HEADER_CORE_STRUCT.pack(
        DGP_PLUGIN_MAGIC, PLUGIN_FORMAT_VERSION, 0x0003, 1, b"\x00\x00\x00", nonce, len(raw_bundle), len(ciphertext)
    )
    env_mac = hmac.new(mac_key, header_core + ciphertext, hashlib.sha256).digest()
    env_sig = _ed25519_sign(PLUGIN_SIG_DOMAIN + header_core + hashlib.sha256(ciphertext).digest(), seed)
    dogapp_bytes = header_core + env_mac + env_sig + ciphertext

    for marker in (b"plugin.json", b"backend/__init__.py", b"registerDesktopApp", b"MODULE_SPEC"):
        if marker in dogapp_bytes:
            raise RuntimeError(f"加密安全自检失败：密文包含明文特征 {marker!r}")

    readme = (
        f"Data.O.G 4.0 外置应用插件包：{manifest['name']} ({pid} v{ver})\n"
        f"请通过 Data.O.G → 系统配置 → 应用插件 上传本压缩包或内部 .dogapp 文件安装。\n"
    ).encode("utf-8")
    out_buf = io.BytesIO()
    inner_name = f"plugins/DataOG-Plugin-{pid}-v{ver}.dogapp"
    with zipfile.ZipFile(out_buf, "w") as zf:
        for arc, data in ((inner_name, dogapp_bytes), ("PLUGIN_README.txt", readme)):
            zi = zipfile.ZipInfo(filename=arc, date_time=_FIXED_ZIP_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o644 & 0xFFFF) << 16
            zf.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    abs_out = os.path.abspath(output_dir)
    os.makedirs(abs_out, exist_ok=True)
    zip_path = os.path.join(abs_out, f"DataOG-Plugin-{pid}-v{ver}.zip")
    dogapp_path = os.path.join(abs_out, f"DataOG-Plugin-{pid}-v{ver}.dogapp")
    with open(zip_path, "wb") as fh:
        fh.write(out_buf.getvalue())
    with open(dogapp_path, "wb") as fh:
        fh.write(dogapp_bytes)

    return {"ok": True, "plugin_id": pid, "version": ver, "zip_path": zip_path, "dogapp_path": dogapp_path, "file_count": len(payload_entries)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Data.O.G 4.0 外置插件校验与加密签名打包器")
    parser.add_argument("--plugin-dir", default=".", help="插件目录 (默认当前目录)")
    parser.add_argument("--output-dir", default="dist", help="输出目录 (默认 dist/)")
    parser.add_argument("--sign-key", default="", help="Ed25519 私钥种子文件路径 (64 位 hex)")
    parser.add_argument("--check-only", action="store_true", help="仅执行契约与文件收集校验，不签名打包")
    args = parser.parse_args()
    res = pack_plugin(args.plugin_dir, output_dir=args.output_dir, sign_key_file=args.sign_key, check_only=args.check_only)
    if res.get("check_only"):
        print(f"[PACK-CHECK] OK | plugin_id={res['plugin_id']} | files={res['file_count']}")
    else:
        print(f"[PACK] OK | zip={res['zip_path']} | dogapp={res['dogapp_path']}")


if __name__ == "__main__":
    main()
'''

UI_SHELL_SETTINGS_APP_INDEX_JS = r'''/**
 * Data.O.G 4.0 SDK 离线预览沙箱内置占位应用入口
 * 仅提供轻量沙箱说明配置窗口，真实插件由 dev_host 通过 /plugins/<id>/register.js 动态注入。
 */

import { registerDesktopApp } from "../core/app_registry.js";
import { clearEl, el, badge } from "../core/dom.js";
import { store } from "../core/state.js";

class SandboxSettingsView {
  constructor() {
    this.root = el("div", { className: "mac-pane mac-pane-padded" });
  }
  destroy() {}
  getHeaderParts() {
    return {
      center: el("span", { style: { fontSize: "12.5px", fontWeight: "600", color: "var(--os-text-secondary)" }, text: "SDK 离线沙箱" }),
      right: null,
    };
  }
  mount(container) {
    const conns = store.state.connections || [];
    const plugs = store.state.plugins || [];
    clearEl(
      this.root,
      el(
        "div",
        { className: "mac-section" },
        el("div", { className: "mac-section-header" }, el("h3", { className: "mac-section-title", text: "Data.O.G 4.0 插件开发离线沙箱" })),
        el(
          "div",
          { className: "ws-arch-card", style: { display: "grid", gap: "8px" } },
          el("div", { style: { display: "flex", gap: "8px", alignItems: "center" } }, badge("ABI v4.0", "info"), badge(`已挂载开发插件 ${plugs.length} 个`, "success"), badge(`预置数据源 ${conns.length} 个`, "neutral")),
          el("p", { style: { margin: "0", fontSize: "12.5px" }, text: "当前运行在独立工作区 .dataog_sdk/dev_host.py 离线宿主中：SQLite 双库持久化、只读查询沙箱 (conn_demo_sqlite) 与 Mock AI/标准已就绪。" }),
          el("p", { className: "muted", style: { margin: "0", fontSize: "12px" }, text: "需要联调真实数据库或大模型时，请在 Data.O.G 主仓库目录执行：python3 app.py --dev-plugin <当前插件工作区路径>" })
        )
      )
    );
    clearEl(container, this.root);
  }
}

registerDesktopApp({
  id: "settings",
  title: "沙箱环境",
  defaultSummary: "SDK 离线宿主",
  iconName: "settings",
  order: 10,
  resetOnWorkspaceSwitch: false,
  loader: async () => new SandboxSettingsView(),
});
'''

STARTER_AGENTS_MD_TEMPLATE = r'''# Data.O.G 4.0 独立插件开发工作区规范 (AGENTS.md)

本工作区由 **Data.O.G 4.0 Plugin SDK (`v__PLATFORM_VERSION__` / ABI `v4.0`)** 自动生成，用于在不掺杂 Data.O.G 主仓海量代码与上下文的前提下，独立开发、测试、预览并打包可无缝拼插进 Data.O.G 的应用模块。

## 1. 工作区结构与严苛红线
- `plugin.json`：插件元信息契约清单（`api_version = "4.0"`、`plugin_id`、`desktop_app`、`backend`、`storage`）。
- `backend/`：纯 Python 插件后端（`__init__.py` 导出 `MODULE_SPEC`，`router.py` / `service.py` / `schemas.py`）。
  - **相对导入铁律**：模块内部互引一律使用**相对导入**（`from .service import ...`），确保在离线沙箱与真实 Data.O.G (`dataog_plugins.<plugin_id>`) 中零修改通用。
  - **底座调用铁律**：通过 `core.platform.*` 与 `ctx: PlatformContext = Depends(get_platform_context)` 调用底座能力，详细 API 见 [`.dataog_sdk/CONTRACT.md`](.dataog_sdk/CONTRACT.md) 与 [`.dataog_sdk/stubs/core/platform/`](.dataog_sdk/stubs/core/platform/)。
  - **依赖限制**：严禁在 `backend/` 或 `backend/vendor/` 放入 `.pyd`/`.so`/`.dylib`/`.dll`/`.exe`/`.jar` 等编译态二进制，严禁导入 `modules.*`。
  - **路由安全**：`router.py` 所有 `@router.*` 端点必须注入 `ctx: PlatformContext = Depends(get_platform_context)` 并声明完整类型注解。
- `frontend/`：零构建原生 ES Modules 前端（`register.js`、`api.js`、`view.js`、`style.css`）。
  - **根绝对导入**：导入平台底座前端模块必须使用 `/js/core/*.js` 与 `/js/ui/*.js`（以及 `/plugins/<plugin_id>/style.css`），详见 [`.dataog_sdk/frontend_api.d.ts`](.dataog_sdk/frontend_api.d.ts)。
  - **零 XSS 与非阻塞交互**：严禁出现 `innerHTML`、`outerHTML`、`insertAdjacentHTML`、`document.write`、`eval`、`new Function` 及 `window.alert`/`confirm`/`prompt`；一律使用 `/js/core/dom.js` (`el` / `clearEl`) 与 `/js/ui/modal.js` (`Modal`)。

## 2. 双轨开发、自检与打包命令
1. **运行单元测试（离线零主仓依赖）**：
   ```bash
   python3 -m unittest discover -s tests -p "test_*.py" -v
   ```
2. **运行强合约与 ASGI 冒烟校验**：
   ```bash
   python3 .dataog_sdk/validate_plugin.py
   ```
3. **启动离线 macOS 桌面沙箱浏览器预览**（预置 `conn_demo_sqlite` 真实只读库与双库 SQLite 持久化）：
   ```bash
   python3 .dataog_sdk/dev_host.py --port 18090
   ```
4. **热挂载到本机真实 Data.O.G 联调真实数据库 / AI 大模型**（在 DataOG 主仓库目录执行）：
   ```bash
   python3 app.py --dev-plugin /path/to/this_plugin_workspace
   ```
5. **发布打包（生成可过银行网关的 `.zip` 与 `.dogapp`）**：
   ```bash
   python3 .dataog_sdk/pack_plugin.py --check-only
   # 生产签名打包（由发布者传入私钥种子路径或环境变量 DATAOG_UPDATE_SIGNING_KEY）：
   python3 .dataog_sdk/pack_plugin.py --sign-key /path/to/master_ed25519.key --output-dir dist
   ```
'''

STARTER_SKILL_MD_TEMPLATE = r'''---
name: dataog-plugin-dev
description: >-
  Guide for developing, testing, validating, previewing, and packaging a Data.O.G 4.0
  external plugin module in this standalone workspace.
---

# Data.O.G 4.0 独立插件开发标准工作流

1. 先阅读 `.dataog_sdk/CONTRACT.md` 与 `plugin.json`，确认 `plugin_id`、`api_prefix` (`/api/modules/<plugin_id>`) 与所需存储表 (`storage.workspace_tables` / `storage.private_tables`)。
2. 编辑 `backend/`：
   - 在 `backend/service.py` 通过 `ctx.store.register_table_schema(..., scope="workspace" | "private")` 建表，用 `ctx.store.module_db(scope=...)` 读写。
   - 查询数据库只用 `ctx.query_engine.execute_readonly_sql(...)` 或 `ctx.connection_hub.readonly_session(...)`。
   - 长耗时任务使用 `core.platform.http_utils.run_tracked_task`。
   - 内部文件引用一律使用相对导入（`from .service import ...`）。
3. 编辑 `frontend/`：
   - `register.js` 调用 `registerDesktopApp({ id, title, iconPath, iconGradient, stylesheets: ["/plugins/<plugin_id>/style.css"], loader: ... })`。
   - 仅从 `/js/core/*` 与 `/js/ui/*` 导入，100% 使用 `el()` / `clearEl()` 构造 DOM，弹窗使用 `Modal`，提示使用 `Toast`。
4. 验证与预览：
   - 运行 `python3 .dataog_sdk/validate_plugin.py`
   - 运行 `python3 -m unittest discover -s tests -p "test_*.py" -v`
   - 需要视觉验证时运行 `python3 .dataog_sdk/dev_host.py --port 18090 --no-browser` 并使用浏览器访问预览。
'''

STARTER_README_MD_TEMPLATE = r'''# __PLUGIN_NAME__ (`__PLUGIN_ID__`)

基于 **Data.O.G 4.0 Plugin SDK (`v__PLATFORM_VERSION__` / ABI `v4.0`)** 创建的独立可拼插应用插件工作区。

## 快速开始

```bash
# 1. 运行插件离线强合约校验
python3 .dataog_sdk/validate_plugin.py

# 2. 运行自动化单元测试
python3 -m unittest discover -s tests -p "test_*.py" -v

# 3. 启动离线 macOS 桌面沙箱预览界面 (http://127.0.0.1:18090)
python3 .dataog_sdk/dev_host.py --port 18090

# 4. 挂载到本机真实 Data.O.G 联调（在 Data.O.G 主仓库目录执行）
python3 app.py --dev-plugin "$(pwd)"
```

## 打包安装至 Data.O.G

```bash
python3 .dataog_sdk/pack_plugin.py --sign-key /path/to/master_ed25519.key --output-dir dist
```
将生成的 `dist/DataOG-Plugin-__PLUGIN_ID__-v1.0.0.zip`（或 `.dogapp`）在 **Data.O.G → 系统配置 → 应用插件** 上传安装，重启后即可作为原生 Dock 应用使用。
'''

STARTER_BACKEND_INIT = r'''"""Data.O.G 4.0 插件后端入口 (__init__.py)."""

from __future__ import annotations

from core.platform.module_registry import ModuleSpec

from .router import router
from .service import PluginService


def _init_plugin(ctx) -> None:
    PluginService.ensure_schema(ctx.store)


MODULE_SPEC = ModuleSpec(
    module_id="__PLUGIN_ID__",
    title="__PLUGIN_NAME__",
    subtitle="__PLUGIN_SUMMARY__",
    api_prefix="/api/modules/__PLUGIN_ID__",
    router=router,
    desktop_app_id="__APP_ID__",
    workspace_tables=("__WS_TABLE__",),
    private_tables=(),
    workspace_meta_keys=("__META_KEY__",),
    init_hook=_init_plugin,
    order=70,
)
'''

STARTER_BACKEND_SCHEMAS = r'''"""插件请求体 Pydantic 模型 (schemas.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NoteCreatePayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    content: str = Field(default="", max_length=4000)


class SampleQueryPayload(BaseModel):
    connection_id: str = Field(..., min_length=1)
    sql: str = Field(default="SELECT 1 AS ok")
    limit: int = Field(default=50, ge=1, le=200)
'''

STARTER_BACKEND_SERVICE = r'''"""插件核心业务服务 (service.py)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List

from core.platform.context import PlatformContext

WS_TABLE = "__WS_TABLE__"
META_KEY = "__META_KEY__"


class PluginService:
    """示例插件业务服务：演示工作区 SQLite 持久化与平台只读查询联动."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx
        self.ensure_schema(ctx.store)

    @staticmethod
    def ensure_schema(store: Any) -> None:
        store.register_table_schema(
            WS_TABLE,
            f"""
            CREATE TABLE IF NOT EXISTS {WS_TABLE} (
                note_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """,
            [
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("content", "TEXT NOT NULL DEFAULT ''"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="workspace",
        )

    def list_notes(self) -> List[Dict[str, Any]]:
        with self.ctx.store.module_db(scope="workspace") as conn:
            rows = conn.execute(
                f"SELECT note_id, title, content, created_at FROM {WS_TABLE} ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_note(self, title: str, content: str) -> Dict[str, Any]:
        note_id = f"N-{uuid.uuid4().hex[:8]}"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.ctx.store.module_db(scope="workspace") as conn:
            conn.execute(
                f"INSERT INTO {WS_TABLE} (note_id, title, content, created_at) VALUES (?, ?, ?, ?)",
                (note_id, title.strip(), content.strip(), now),
            )
        self.ctx.store.save_meta(META_KEY, note_id)
        return {"note_id": note_id, "title": title.strip(), "content": content.strip(), "created_at": now}

    def delete_note(self, note_id: str) -> bool:
        with self.ctx.store.module_db(scope="workspace") as conn:
            cur = conn.execute(f"DELETE FROM {WS_TABLE} WHERE note_id = ?", (note_id,))
            return bool(cur.rowcount)

    def preview_readonly_sql(self, connection_id: str, sql: str, limit: int = 50, *, progress_cb=None) -> Dict[str, Any]:
        if progress_cb:
            progress_cb(30, "只读探查", "正在通过底座安全沙箱执行只读查询...", 0, 1)
        res = self.ctx.query_engine.execute_readonly_sql(connection_id, sql, limit=limit)
        if progress_cb:
            progress_cb(90, "完成", f"返回 {res.row_count} 行", res.row_count, res.row_count)
        return {
            "ok": True,
            "columns": res.columns,
            "rows": res.rows,
            "row_count": res.row_count,
            "truncated": res.truncated,
            "elapsed_ms": res.elapsed_ms,
        }
'''

STARTER_BACKEND_ROUTER = r'''"""插件 FastAPI 路由层 (router.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error, run_tracked_task

from .schemas import NoteCreatePayload, SampleQueryPayload
from .service import META_KEY, PluginService

router = APIRouter(prefix="/api/modules/__PLUGIN_ID__", tags=["__PLUGIN_ID__"])


@router.get("/overview")
def api_overview(ctx: PlatformContext = Depends(get_platform_context)):
    svc = PluginService(ctx)
    return {
        "ok": True,
        "plugin_id": "__PLUGIN_ID__",
        "last_note_id": ctx.store.get_meta(META_KEY, ""),
        "notes": svc.list_notes(),
        "connections": ctx.connection_hub.list_profiles(),
    }


@router.post("/notes")
def api_create_note(
    payload: NoteCreatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        item = PluginService(ctx).create_note(payload.title, payload.content)
        return {"ok": True, "item": item}
    except Exception as exc:
        return json_error(str(exc), 400)


@router.delete("/notes/{note_id}")
def api_delete_note(
    note_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    deleted = PluginService(ctx).delete_note(note_id)
    return {"ok": True, "deleted": deleted}


@router.post("/query-preview")
async def api_query_preview(
    request: Request,
    payload: SampleQueryPayload,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = PluginService(ctx)
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="插件只读查询探查",
        start_detail="准备执行只读查询",
        fn=svc.preview_readonly_sql,
        args=(payload.connection_id, payload.sql, payload.limit),
        count_fn=lambda r: (int(r.get("row_count") or 0), int(r.get("row_count") or 0)),
    )
'''

STARTER_FRONTEND_REGISTER = r'''/**
 * Data.O.G 4.0 插件前端入口清单 (register.js)
 */

import { registerDesktopApp } from "/js/core/app_registry.js";

registerDesktopApp({
  id: "__APP_ID__",
  title: "__PLUGIN_NAME__",
  defaultSummary: "__PLUGIN_SUMMARY__",
  iconName: "__APP_ID__",
  iconPath:
    "M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zm0 2.3L6.2 8.5 12 11.7l5.8-3.2L12 5.3zm-6 5.2v5l5 2.8v-5l-5-2.8zm12 0l-5 2.8v5l5-2.8v-5z",
  iconGradient: "linear-gradient(145deg, #0ea5e9, #6366f1)",
  stylesheets: ["/plugins/__PLUGIN_ID__/style.css"],
  resetOnWorkspaceSwitch: true,
  order: 70,
  loader: async () => {
    const mod = await import("./view.js");
    return new mod.PluginView();
  },
});
'''

STARTER_FRONTEND_API = r'''/**
 * 插件专属前端 API 客户端 (api.js)
 * 严格仅从平台 /js/core/http.js 导入传输原语。
 */

import { requestJson } from "/js/core/http.js";

const BASE = "/api/modules/__PLUGIN_ID__";

export const pluginApi = {
  overview: () => requestJson(`${BASE}/overview`, { cache: "no-store" }),
  createNote: (payload) => requestJson(`${BASE}/notes`, { method: "POST", body: payload }),
  deleteNote: (noteId) => requestJson(`${BASE}/notes/${encodeURIComponent(noteId)}`, { method: "DELETE" }),
  previewQuery: (payload) => requestJson(`${BASE}/query-preview`, { method: "POST", body: payload }),
};
'''

STARTER_FRONTEND_VIEW = r'''/**
 * 插件主视图 (view.js) — 严格遵循零 innerHTML 与 macOS HIG 风格
 */

import { badge, clearEl, el } from "/js/core/dom.js";
import { store } from "/js/core/state.js";
import { Modal } from "/js/ui/modal.js";
import { Toast } from "/js/ui/toast.js";
import { pluginApi } from "./api.js";

export class PluginView {
  constructor() {
    this.root = el("div", { className: "plg-shell" });
    this.notes = [];
    this.queryResult = null;
    this.selectedConnId = "";
    this.sqlText = "SELECT cust_id, cust_name, risk_grade FROM dim_customer LIMIT 10";
    this.refreshHeader = null;
  }

  destroy() {}

  getHeaderParts() {
    store.setAppSummary("__APP_ID__", `笔记 ${this.notes.length} 条`);
    return {
      center: el("span", { style: { fontSize: "12.5px", fontWeight: "600", color: "var(--os-text-secondary)" }, text: "__PLUGIN_NAME__" }),
      right: badge("ABI v4.0", "info"),
    };
  }

  async mount(container, _context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    clearEl(container, this.root);
    await this.loadOverview();
  }

  async loadOverview() {
    try {
      const res = await pluginApi.overview();
      this.notes = res.notes || [];
      const conns = res.connections || store.state.connections || [];
      if (!this.selectedConnId && conns.length) {
        this.selectedConnId = conns[0].connection_id;
      }
      this.render();
      this.refreshHeader?.();
    } catch (err) {
      Toast.error(err.message || "加载插件数据失败");
    }
  }

  async handleAddNote(titleInput, contentInput) {
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!title) {
      Toast.warn("请输入标题");
      return;
    }
    try {
      await pluginApi.createNote({ title, content });
      titleInput.value = "";
      contentInput.value = "";
      Toast.success("已保存至当前工作区");
      await this.loadOverview();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async handleDeleteNote(noteId) {
    const ok = await Modal.confirm({
      title: "删除记录",
      message: "确定删除该条工作区记录吗？",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await pluginApi.deleteNote(noteId);
      Toast.success("已删除");
      await this.loadOverview();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async handleRunQuery() {
    if (!this.selectedConnId) {
      Toast.warn("请先配置或选择一个数据源连接");
      return;
    }
    try {
      this.queryResult = await pluginApi.previewQuery({
        connection_id: this.selectedConnId,
        sql: this.sqlText,
        limit: 50,
      });
      this.render();
    } catch (err) {
      Toast.error(err.message || "只读查询失败");
    }
  }

  render() {
    const conns = store.state.connections || [];
    const titleInput = el("input", { className: "mac-input", placeholder: "输入工作区记录标题..." });
    const contentInput = el("input", { className: "mac-input", placeholder: "备注内容（可选）..." });

    const noteCard = el(
      "section",
      { className: "mac-group-box plg-card" },
      el("h3", { className: "mac-section-title", text: "1. 工作区持久化记录 (scope=workspace)" }),
      el(
        "div",
        { className: "plg-row" },
        titleInput,
        contentInput,
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-primary",
          text: "保存",
          onClick: () => this.handleAddNote(titleInput, contentInput),
        })
      ),
      el(
        "div",
        { className: "plg-list" },
        this.notes.length
          ? this.notes.map((n) =>
              el(
                "div",
                { className: "plg-note-item" },
                el(
                  "div",
                  {},
                  el("strong", { text: n.title }),
                  n.content ? el("span", { className: "muted", style: { marginLeft: "8px" }, text: n.content }) : null
                ),
                el("button", {
                  type: "button",
                  className: "btn btn-xs btn-danger",
                  text: "删除",
                  onClick: () => this.handleDeleteNote(n.note_id),
                })
              )
            )
          : el("div", { className: "muted", text: "暂无工作区记录，尝试添加一条以验证三层存储持久化。" })
      )
    );

    const connSelect = el(
      "select",
      {
        className: "mac-input",
        onChange: (e) => {
          this.selectedConnId = e.target.value;
        },
      },
      conns.length
        ? conns.map((c) =>
            el("option", {
              value: c.connection_id,
              selected: c.connection_id === this.selectedConnId,
              text: `${c.name} (${c.db_type})`,
            })
          )
        : el("option", { value: "", text: "暂无可用数据源" })
    );

    const sqlInput = el("textarea", {
      className: "mac-input mono",
      rows: "3",
      value: this.sqlText,
      onInput: (e) => {
        this.sqlText = e.target.value;
      },
    });

    const queryCard = el(
      "section",
      { className: "mac-group-box plg-card" },
      el("h3", { className: "mac-section-title", text: "2. 底座硬件级只读 SQL 探查联动" }),
      el("div", { className: "plg-row" }, connSelect, el("button", { type: "button", className: "mac-btn mac-btn-primary", text: "执行只读查询", onClick: () => this.handleRunQuery() })),
      sqlInput,
      this.queryResult
        ? el(
            "div",
            { className: "plg-result" },
            el("div", { className: "muted", text: `返回 ${this.queryResult.row_count} 行 · 耗时 ${this.queryResult.elapsed_ms} ms` }),
            el("pre", { className: "mono plg-pre", text: JSON.stringify(this.queryResult.rows || [], null, 2) })
          )
        : null
    );

    clearEl(this.root, noteCard, queryCard);
  }
}
'''

STARTER_FRONTEND_CSS = r'''/* __PLUGIN_NAME__ 独立应用样式表 */
.plg-shell {
  padding: 16px;
  display: grid;
  gap: 14px;
  max-width: 980px;
  margin: 0 auto;
}
.plg-card {
  display: grid;
  gap: 10px;
  padding: 14px;
}
.plg-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.plg-list {
  display: grid;
  gap: 6px;
}
.plg-note-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--os-border);
  background: var(--os-subtle-bg);
}
.plg-pre {
  margin: 6px 0 0;
  padding: 8px;
  border-radius: 8px;
  background: var(--os-subtle-bg);
  border: 1px solid var(--os-border);
  font-size: 11.5px;
  overflow: auto;
  max-height: 220px;
}
'''

STARTER_TEST_PY = r'''"""Data.O.G 4.0 独立插件离线自动化单元测试 (tests/test_plugin.py)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SDK_DIR = os.path.join(_ROOT, ".dataog_sdk")
if _SDK_DIR not in sys.path:
    sys.path.insert(0, _SDK_DIR)

import dev_host
import validate_plugin


class TestPluginStandalone(unittest.TestCase):
    def test_contract_validation(self) -> None:
        res = validate_plugin.validate_plugin_dir(_ROOT)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plugin_id"], "__PLUGIN_ID__")

    def test_crud_and_readonly_query_in_dev_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = dev_host.create_dev_app(_ROOT, data_dir=tmp)
            client = dev_host.DevTestClient(app)

            ov = client.get("/api/modules/__PLUGIN_ID__/overview")
            self.assertEqual(ov.status_code, 200)
            self.assertTrue(ov.json()["ok"])

            created = client.post(
                "/api/modules/__PLUGIN_ID__/notes",
                json={"title": "SDK 测试笔记", "content": "来自离线工作区"},
            )
            self.assertEqual(created.status_code, 200)
            note_id = created.json()["item"]["note_id"]

            ov2 = client.get("/api/modules/__PLUGIN_ID__/overview").json()
            self.assertEqual(len(ov2["notes"]), 1)
            self.assertEqual(ov2["last_note_id"], note_id)

            qres = client.post(
                "/api/modules/__PLUGIN_ID__/query-preview",
                json={"connection_id": "conn_demo_sqlite", "sql": "SELECT cust_id, cust_name FROM dim_customer", "limit": 10},
            )
            self.assertEqual(qres.status_code, 200)
            self.assertEqual(qres.json()["row_count"], 3)

            deleted = client.delete(f"/api/modules/__PLUGIN_ID__/notes/{note_id}")
            self.assertEqual(deleted.status_code, 200)
            self.assertTrue(deleted.json()["deleted"])


if __name__ == "__main__":
    unittest.main()
'''

STARTER_GITIGNORE = r'''__pycache__/
*.py[cod]
.dataog_dev_data/
dist/
*.dogapp
.DS_Store
'''
