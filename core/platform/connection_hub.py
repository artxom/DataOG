"""DataOG 全局命名数据源连接中心 (ConnectionHub).

所有功能模块一律通过 `connection_id` 引用数据源，严禁业务模块私自维护账号密码。
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from typing import Any, Dict, Generator, List, Optional, Tuple

from core.platform.driver_manager import DriverManager, JdbcConnectionAdapter
from core.platform.inceptor_client import InceptorThriftClient
from core.platform.models import ConnectionProfile
from core.platform.standard_catalog import VALID_FORMAT_PROFILE_VALUES
from core.platform.storage import PlatformStateStore


class ConnectionHub:
    """统一管理命名数据源档案、密码安全留存、连通性探测与连接会话分发."""

    def __init__(
        self,
        store: PlatformStateStore,
        driver_manager: DriverManager,
        *,
        warm_ttl_sec: float = 60.0,
    ) -> None:
        self.store = store
        self.driver_manager = driver_manager
        self.warm_ttl_sec = max(0.0, float(warm_ttl_sec))
        self._warm_lock = threading.Lock()
        self._warm_pool: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _profile_fingerprint(profile: ConnectionProfile) -> Tuple[Any, ...]:
        return (
            profile.driver_track,
            profile.db_type,
            profile.host,
            int(profile.port or 0),
            profile.dbname,
            profile.default_schema,
            profile.user,
            profile.password,
            profile.sslmode,
            profile.auth_mode,
            profile.sqlite_path,
            profile.jdbc_driver_id,
            profile.jdbc_url,
            profile.jdbc_driver_class,
            json.dumps(profile.extra_props or {}, sort_keys=True, ensure_ascii=False),
        )

    @staticmethod
    def _safe_close_conn(conn: Any) -> None:
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _is_conn_alive(conn: Any) -> bool:
        if conn is None:
            return False
        try:
            if getattr(conn, "closed", 0) != 0:
                return False
            if not getattr(conn, "open", True):
                return False
            if (
                isinstance(conn, InceptorThriftClient)
                and hasattr(conn, "_sock")
                and getattr(conn, "_sock") is None
            ):
                return False
            if isinstance(conn, JdbcConnectionAdapter):
                raw = getattr(conn, "raw_jdbc_conn", None)
                if raw is not None and hasattr(raw, "isClosed") and raw.isClosed():
                    return False
        except Exception:
            return False
        return True

    def close_warm_connections(self, connection_id: Optional[str] = None) -> None:
        to_close: List[Any] = []
        with self._warm_lock:
            if connection_id is None:
                to_close = [
                    entry["conn"]
                    for entry in self._warm_pool.values()
                    if entry.get("conn") is not None
                ]
                self._warm_pool.clear()
            else:
                entry = self._warm_pool.pop(connection_id, None)
                if entry and entry.get("conn") is not None:
                    to_close.append(entry["conn"])
        for conn in to_close:
            self._safe_close_conn(conn)

    def list_profiles(self) -> List[Dict[str, Any]]:
        return [p.to_public_dict() for p in self.store.list_connection_profiles()]

    def get_profile(self, connection_id: str) -> Optional[ConnectionProfile]:
        return self.store.get_connection_profile(connection_id)

    def require_profile(self, connection_id: str) -> ConnectionProfile:
        profile = self.get_profile(connection_id)
        if not profile:
            raise ValueError(f"未找到数据源连接档案 ({connection_id})，请先在数据源中心创建或选择有效连接")
        return profile

    def build_profile_from_payload(
        self, payload: Dict[str, Any], connection_id: Optional[str] = None
    ) -> ConnectionProfile:
        cid = (connection_id or payload.get("connection_id") or "").strip() or f"conn-{uuid.uuid4().hex[:10]}"
        existing = self.store.get_connection_profile(cid)

        db_type = str(payload.get("db_type") or (existing.db_type if existing else "DWS")).strip().upper()
        tpl = self.driver_manager.get_template(db_type)
        driver_track = str(
            payload.get("driver_track")
            or (existing.driver_track if existing else "")
            or tpl.get("driver_track", "NATIVE")
        ).strip().upper()

        host = str(payload.get("host") if "host" in payload else (existing.host if existing else "")).strip()
        raw_port = payload.get("port")
        if raw_port is None or raw_port == "":
            port = existing.port if existing else int(tpl.get("default_port") or 0)
        else:
            port = int(raw_port)

        dbname = str(
            payload.get("dbname") if "dbname" in payload else (existing.dbname if existing else "")
        ).strip()
        fallback_schema = (
            (dbname or "default")
            if db_type == "INCEPTOR"
            else (
                "main"
                if db_type == "SQLITE"
                else (
                    dbname
                    if db_type in ("MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB")
                    else "public"
                )
            )
        )
        default_schema = str(
            payload.get("default_schema")
            or payload.get("schema_name")
            or (existing.default_schema if existing else "")
            or tpl.get("default_schema")
            or fallback_schema
        ).strip()
        user = str(
            payload.get("user") if "user" in payload else (existing.user if existing else "")
        ).strip()

        incoming_pwd = payload.get("password")
        clear_pwd = bool(payload.get("clear_password", False))
        if clear_pwd:
            password = ""
        elif incoming_pwd is not None and str(incoming_pwd) != "":
            password = str(incoming_pwd)
        elif existing is not None:
            password = existing.password
        else:
            password = ""

        sqlite_path = str(
            payload.get("sqlite_path")
            if "sqlite_path" in payload
            else (existing.sqlite_path if existing else "")
        ).strip()
        if db_type == "SQLITE" and not sqlite_path and dbname:
            sqlite_path = dbname
        if db_type == "SQLITE" and sqlite_path and not dbname:
            dbname = sqlite_path

        name = str(payload.get("name") or "").strip()
        if not name:
            if existing and existing.name:
                name = existing.name
            elif db_type == "SQLITE":
                name = f"SQLite [{sqlite_path or ':memory:'}]"
            else:
                name = f"{db_type} · {host}:{port}/{dbname}" if dbname else f"{db_type} · {host}:{port}"

        return ConnectionProfile(
            connection_id=cid,
            name=name,
            driver_track=driver_track,
            db_type=db_type,
            host=host,
            port=port,
            dbname=dbname,
            default_schema=default_schema,
            user=user,
            password=password,
            sslmode=str(payload.get("sslmode") or (existing.sslmode if existing else "prefer")).strip(),
            auth_mode=str(payload.get("auth_mode") or (existing.auth_mode if existing else "LDAP")).strip().upper(),
            sqlite_path=sqlite_path,
            jdbc_driver_id=str(
                payload.get("jdbc_driver_id")
                if "jdbc_driver_id" in payload
                else (existing.jdbc_driver_id if existing else "")
            ).strip(),
            jdbc_url=str(
                payload.get("jdbc_url")
                if "jdbc_url" in payload
                else (existing.jdbc_url if existing else "")
            ).strip(),
            jdbc_driver_class=str(
                payload.get("jdbc_driver_class")
                if "jdbc_driver_class" in payload
                else (existing.jdbc_driver_class if existing else "")
            ).strip(),
            extra_props=dict(payload.get("extra_props") or (existing.extra_props if existing else {})),
            standard_format=self._resolve_valid_standard_format(
                payload.get("standard_format") if "standard_format" in payload else None,
                fallback=existing.standard_format if existing else "AUTO",
            ),
            created_at=existing.created_at if existing else "",
        )

    @staticmethod
    def _resolve_valid_standard_format(
        raw_value: Optional[str], fallback: str = "AUTO"
    ) -> str:
        if raw_value is None:
            candidate = (fallback or "AUTO").strip() or "AUTO"
        else:
            candidate = str(raw_value).strip()
            if not candidate:
                raise ValueError(f"不支持的数据标准格式选项: {raw_value}")
        if candidate not in VALID_FORMAT_PROFILE_VALUES:
            raise ValueError(f"不支持的数据标准格式选项: {candidate}")
        return candidate

    def update_standard_format(
        self, connection_id: str, standard_format: str
    ) -> ConnectionProfile:
        fmt = self._resolve_valid_standard_format(standard_format, fallback="AUTO")
        existing = self.get_profile(connection_id)
        if not existing:
            raise LookupError(f"未找到数据源连接档案 ({connection_id})")
        existing.standard_format = fmt
        saved = self.store.upsert_connection_profile(existing)
        self.store.add_log(
            "INFO",
            "连接中心",
            f"已更新数据源 [{saved.name}] 默认数据标准格式为 [{saved.standard_format}]",
        )
        return saved

    def save_profile(
        self, payload: Dict[str, Any], connection_id: Optional[str] = None
    ) -> ConnectionProfile:
        profile = self.build_profile_from_payload(payload, connection_id=connection_id)
        saved = self.store.upsert_connection_profile(profile)
        self.close_warm_connections(saved.connection_id)
        self.store.add_log(
            "INFO",
            "连接中心",
            f"已保存数据源连接档案 [{saved.name}] ({saved.db_type} / {saved.driver_track})",
        )
        return saved

    def delete_profile(self, connection_id: str) -> bool:
        self.close_warm_connections(connection_id)
        ok = self.store.delete_connection_profile(connection_id)
        if ok:
            self.store.add_log("INFO", "连接中心", f"已移除数据源连接档案 [{connection_id}] 及其本地元数据缓存")
        return ok

    @contextlib.contextmanager
    def session(self, connection_id: str, timeout_sec: int = 12) -> Generator[Any, None, None]:
        """按 connection_id 获取安全托管的数据库连接上下文，退出时自动关闭."""
        profile = self.require_profile(connection_id)
        conn = self.driver_manager.open_connection(profile, timeout_sec=timeout_sec)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @contextlib.contextmanager
    def probe_session(self, connection_id: str, timeout_sec: int = 15) -> Generator[Any, None, None]:
        """按 connection_id 获取用于只读元数据探查的温连接复用会话（异常销毁，正常回滚后归还）."""
        profile = self.require_profile(connection_id)
        if profile.db_type == "SQLITE" and not getattr(self, "_pool_sqlite_for_test", False):
            with self.session(connection_id, timeout_sec=timeout_sec) as conn:
                yield conn
            return

        fp = self._profile_fingerprint(profile)
        cur_open_fn = self.driver_manager.open_connection
        now = time.monotonic()
        stale_conns: List[Any] = []
        pooled_conn: Any = None

        with self._warm_lock:
            expired_ids = [
                cid
                for cid, entry in self._warm_pool.items()
                if (now - float(entry.get("last_used", 0.0))) > self.warm_ttl_sec
            ]
            for cid in expired_ids:
                exp_entry = self._warm_pool.pop(cid, None)
                if exp_entry and exp_entry.get("conn") is not None:
                    stale_conns.append(exp_entry["conn"])

            entry = self._warm_pool.pop(connection_id, None)
            if entry is not None:
                if entry.get("fingerprint") == fp and entry.get("open_fn") == cur_open_fn:
                    pooled_conn = entry.get("conn")
                elif entry.get("conn") is not None:
                    stale_conns.append(entry["conn"])

        for stale in stale_conns:
            self._safe_close_conn(stale)

        conn = pooled_conn
        if conn is not None and not self._is_conn_alive(conn):
            self._safe_close_conn(conn)
            conn = None

        if conn is None:
            conn = cur_open_fn(profile, timeout_sec=timeout_sec)

        try:
            yield conn
        except BaseException:
            self._safe_close_conn(conn)
            raise

        try:
            if hasattr(conn, "rollback") and not getattr(conn, "autocommit", False):
                conn.rollback()
        except Exception:
            self._safe_close_conn(conn)
            return

        if not self._is_conn_alive(conn):
            self._safe_close_conn(conn)
            return

        put_back = False
        with self._warm_lock:
            if connection_id not in self._warm_pool:
                self._warm_pool[connection_id] = {
                    "conn": conn,
                    "fingerprint": fp,
                    "open_fn": cur_open_fn,
                    "last_used": time.monotonic(),
                }
                put_back = True
        if not put_back:
            self._safe_close_conn(conn)

    @contextlib.contextmanager
    def readonly_session(
        self, connection_id: str, timeout_sec: int = 12
    ) -> Generator[Any, None, None]:
        """按 connection_id 获取硬件级只读隔离会话上下文，退出时无条件执行 rollback() 并关闭连接."""
        profile = self.require_profile(connection_id)
        conn = self.driver_manager.open_connection(
            profile, timeout_sec=timeout_sec, readonly=True
        )
        try:
            yield conn
        finally:
            if hasattr(conn, "rollback"):
                try:
                    conn.rollback()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    def test_profile(self, profile: ConnectionProfile) -> Dict[str, Any]:
        """执行真实数据库握手与版本/探针查询（绝不记录或返回明文密码）."""
        t0 = time.perf_counter()
        conn = self.driver_manager.open_connection(profile, timeout_sec=10)
        server_version = ""
        try:
            if isinstance(conn, InceptorThriftClient):
                rows = conn.execute_query("SHOW DATABASES", max_rows=20)
                server_version = f"Inceptor/HiveServer2 (可见库 {len(rows)} 个)"
            elif isinstance(conn, JdbcConnectionAdapter):
                dbmd = conn.raw_jdbc_conn.getMetaData()
                prod = str(dbmd.getDatabaseProductName() or profile.db_type)
                ver = str(dbmd.getDatabaseProductVersion() or "")
                server_version = f"{prod} {ver}".strip()
            elif profile.db_type == "SQLITE":
                cur = conn.execute("SELECT sqlite_version()")
                row = cur.fetchone()
                server_version = f"SQLite {row[0] if row else ''}".strip()
            else:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT VERSION()")
                    row = cur.fetchone()
                    server_version = str(row[0] if row else "").splitlines()[0][:120]
                finally:
                    cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "ok": True,
            "connection_id": profile.connection_id,
            "name": profile.name,
            "db_type": profile.db_type,
            "driver_track": profile.driver_track,
            "server_version": server_version,
            "elapsed_ms": elapsed_ms,
            "message": f"连接成功 ({elapsed_ms} ms)" + (f" · {server_version}" if server_version else ""),
        }

    def test_connection_by_id(self, connection_id: str) -> Dict[str, Any]:
        profile = self.require_profile(connection_id)
        return self.test_profile(profile)
