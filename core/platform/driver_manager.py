"""DataOG 双轨驱动引擎中心 (DriverManager).

双轨架构：
1. 内置原生通道 (`NATIVE`，免 Java 开箱即用)：
   - `DWS` / `PGSQL` (`psycopg2`)
   - `INCEPTOR` (纯 Python `InceptorThriftClient`)
   - `MYSQL` (`PyMySQL`，覆盖 MySQL / OceanBase / Doris / StarRocks / TiDB)
   - `SQLITE` (标准库 `sqlite3`)
2. 自定义 JDBC 扩展通道 (`JDBC`，用户上传 `.jar` 驱动包)：
   - 安全隔离存放于 `data/drivers/`（非静态目录），严格校验 `.jar` 后缀、64MB 上限、`PK\\x03\\x04` 魔数头、防路径穿越与 SHA-256 指纹；
   - 自动扫描 `META-INF/services/java.sql.Driver` 识别驱动主类；
   - 按需懒加载 `JPype1` + 独立 `java.net.URLClassLoader` 直接实例化 `Driver.connect(url, props)`，
     在未安装 JRE 的机器上绝不影响应用启动与内置原生驱动运行。
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import sqlite3
import threading
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.platform.inceptor_client import InceptorThriftClient
from core.platform.models import ConnectionProfile, DriverPackage
from core.platform.storage import PlatformStateStore

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

try:
    import pymysql
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]

logger = logging.getLogger("dataog")

MAX_JAR_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MB
JAR_MAGIC_BYTES = b"PK\x03\x04"

DRIVER_TEMPLATES: List[Dict[str, Any]] = [
    {
        "db_type": "DWS",
        "label": "华为云 GaussDB(DWS)",
        "driver_track": "NATIVE",
        "default_port": 8000,
        "default_schema": "public",
        "driver_note": "内置 psycopg2 原生驱动（免 Java）",
    },
    {
        "db_type": "PGSQL",
        "label": "PostgreSQL / openGauss",
        "driver_track": "NATIVE",
        "default_port": 5432,
        "default_schema": "public",
        "driver_note": "内置 psycopg2 原生驱动（免 Java）",
    },
    {
        "db_type": "INCEPTOR",
        "label": "星环 Transwarp Inceptor / Hive",
        "driver_track": "NATIVE",
        "default_port": 10000,
        "default_schema": "default",
        "driver_note": "内置纯 Python HiveServer2 Thrift 引擎（免 Java）",
    },
    {
        "db_type": "MYSQL",
        "label": "MySQL / OceanBase / Doris / StarRocks / TiDB",
        "driver_track": "NATIVE",
        "default_port": 3306,
        "default_schema": "",
        "driver_note": "内置 PyMySQL 原生驱动（免 Java）",
    },
    {
        "db_type": "SQLITE",
        "label": "SQLite 3 本地数据库",
        "driver_track": "NATIVE",
        "default_port": 0,
        "default_schema": "main",
        "driver_note": "内置 Python sqlite3 标准库（免配置）",
    },
    {
        "db_type": "DM8",
        "label": "达梦数据库 DM8 (JDBC)",
        "driver_track": "JDBC",
        "default_port": 5236,
        "default_schema": "SYSDBA",
        "jdbc_driver_class": "dm.jdbc.driver.DmDriver",
        "jdbc_url_template": "jdbc:dm://{host}:{port}/{dbname}",
        "driver_note": "需上传 DmJdbcDriver18.jar",
    },
    {
        "db_type": "KINGBASE",
        "label": "人大金仓 KingbaseES V8 (JDBC)",
        "driver_track": "JDBC",
        "default_port": 54321,
        "default_schema": "public",
        "jdbc_driver_class": "com.kingbase8.Driver",
        "jdbc_url_template": "jdbc:kingbase8://{host}:{port}/{dbname}",
        "driver_note": "需上传 kingbase8-*.jar（亦可选用 PGSQL 原生通道直连）",
    },
    {
        "db_type": "ORACLE",
        "label": "Oracle Database (JDBC)",
        "driver_track": "JDBC",
        "default_port": 1521,
        "default_schema": "",
        "jdbc_driver_class": "oracle.jdbc.OracleDriver",
        "jdbc_url_template": "jdbc:oracle:thin:@//{host}:{port}/{dbname}",
        "driver_note": "需上传 ojdbc8.jar / ojdbc11.jar",
    },
    {
        "db_type": "DB2",
        "label": "IBM Db2 (JDBC)",
        "driver_track": "JDBC",
        "default_port": 50000,
        "default_schema": "",
        "jdbc_driver_class": "com.ibm.db2.jcc.DB2Driver",
        "jdbc_url_template": "jdbc:db2://{host}:{port}/{dbname}",
        "driver_note": "需上传 db2jcc4.jar",
    },
    {
        "db_type": "GBASE",
        "label": "南大通用 GBase 8a/8s (JDBC)",
        "driver_track": "JDBC",
        "default_port": 5258,
        "default_schema": "",
        "jdbc_driver_class": "com.gbase.jdbc.Driver",
        "jdbc_url_template": "jdbc:gbase://{host}:{port}/{dbname}",
        "driver_note": "需上传 gbase-connector-java-*.jar",
    },
    {
        "db_type": "CUSTOM_JDBC",
        "label": "任意自定义 JDBC 数据源",
        "driver_track": "JDBC",
        "default_port": 0,
        "default_schema": "",
        "jdbc_driver_class": "",
        "jdbc_url_template": "jdbc:vendor://{host}:{port}/{dbname}",
        "driver_note": "上传任意符合 JDBC 4.0+ 标准的 .jar 驱动包即可接入",
    },
]


class JdbcCursorAdapter:
    """将 java.sql.Statement / ResultSet 包装为类 Python DB-API 2.0 游标接口."""

    def __init__(self, jdbc_conn: Any) -> None:
        self._jdbc_conn = jdbc_conn
        self._stmt: Any = None
        self._rs: Any = None
        self.description: Optional[List[Tuple[Any, ...]]] = None
        self.rowcount: int = -1

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None, timeout_sec: int = 0) -> "JdbcCursorAdapter":
        self._close_current_rs()
        if params:
            self._stmt = self._jdbc_conn.prepareStatement(sql)
            if timeout_sec > 0:
                try:
                    self._stmt.setQueryTimeout(int(timeout_sec))
                except Exception:
                    pass
            for idx, val in enumerate(params, start=1):
                self._stmt.setObject(idx, val)
            has_rs = bool(self._stmt.execute())
        else:
            self._stmt = self._jdbc_conn.createStatement()
            if timeout_sec > 0:
                try:
                    self._stmt.setQueryTimeout(int(timeout_sec))
                except Exception:
                    pass
            has_rs = bool(self._stmt.execute(sql))

        if has_rs:
            self._rs = self._stmt.getResultSet()
            meta = self._rs.getMetaData()
            col_cnt = int(meta.getColumnCount())
            desc = []
            for i in range(1, col_cnt + 1):
                col_label = str(meta.getColumnLabel(i) or meta.getColumnName(i) or f"col_{i}")
                col_type = str(meta.getColumnTypeName(i) or "")
                desc.append((col_label, col_type, None, None, None, None, None))
            self.description = desc
            self.rowcount = -1
        else:
            self.description = None
            try:
                self.rowcount = int(self._stmt.getUpdateCount())
            except Exception:
                self.rowcount = 0
        return self

    def _read_current_row(self) -> Tuple[Any, ...]:
        assert self._rs is not None and self.description is not None
        col_cnt = len(self.description)
        vals: List[Any] = []
        for i in range(1, col_cnt + 1):
            obj = self._rs.getObject(i)
            if obj is None or self._rs.wasNull():
                vals.append(None)
            elif isinstance(obj, (bool, int, float, str)):
                vals.append(obj)
            else:
                vals.append(str(obj))
        return tuple(vals)

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._rs is None:
            return None
        if self._rs.next():
            return self._read_current_row()
        return None

    def fetchmany(self, size: int = 100) -> List[Tuple[Any, ...]]:
        if self._rs is None:
            return []
        rows: List[Tuple[Any, ...]] = []
        for _ in range(max(0, int(size))):
            if not self._rs.next():
                break
            rows.append(self._read_current_row())
        return rows

    def fetchall(self) -> List[Tuple[Any, ...]]:
        if self._rs is None:
            return []
        rows: List[Tuple[Any, ...]] = []
        while self._rs.next():
            rows.append(self._read_current_row())
        return rows

    def _close_current_rs(self) -> None:
        if self._rs is not None:
            try:
                self._rs.close()
            except Exception:
                pass
            self._rs = None
        if self._stmt is not None:
            try:
                self._stmt.close()
            except Exception:
                pass
            self._stmt = None

    def close(self) -> None:
        self._close_current_rs()

    def __enter__(self) -> "JdbcCursorAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class JdbcConnectionAdapter:
    """将 java.sql.Connection 封装为统一管理对象，暴露 raw_jdbc_conn 以支持 DatabaseMetaData 探查."""

    def __init__(self, jdbc_conn: Any) -> None:
        self.raw_jdbc_conn = jdbc_conn

    def cursor(self) -> JdbcCursorAdapter:
        return JdbcCursorAdapter(self.raw_jdbc_conn)

    def commit(self) -> None:
        try:
            if not self.raw_jdbc_conn.getAutoCommit():
                self.raw_jdbc_conn.commit()
        except Exception:
            pass

    def rollback(self) -> None:
        try:
            if not self.raw_jdbc_conn.getAutoCommit():
                self.raw_jdbc_conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.raw_jdbc_conn.close()
        except Exception:
            pass

    def __enter__(self) -> "JdbcConnectionAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class DriverManager:
    """双轨驱动管理器：统一管控内置 Python 原生驱动与用户上传的自定义 JDBC .jar 驱动."""

    def __init__(self, store: PlatformStateStore, drivers_dir: str) -> None:
        self.store = store
        self.drivers_dir = os.path.abspath(drivers_dir)
        os.makedirs(self.drivers_dir, exist_ok=True)
        self._jvm_lock = threading.Lock()
        self._classloader_cache: Dict[str, Any] = {}

    def list_templates(self) -> List[Dict[str, Any]]:
        return list(DRIVER_TEMPLATES)

    def get_template(self, db_type: str) -> Dict[str, Any]:
        target = (db_type or "").strip().upper()
        for tpl in DRIVER_TEMPLATES:
            if tpl["db_type"] == target:
                return dict(tpl)
        return dict(DRIVER_TEMPLATES[-1])

    def get_java_runtime_status(self) -> Dict[str, Any]:
        """无副作用探测本机 JPype1 与 Java JRE/JDK 可用状态（绝不在此阶段启动 JVM）."""
        jpype_installed = False
        jvm_path = ""
        try:
            import jpype  # type: ignore

            jpype_installed = True
            try:
                jvm_path = str(jpype.getDefaultJVMPath() or "")
            except Exception:
                jvm_path = ""
        except ImportError:
            jpype_installed = False

        java_bin = shutil.which("java") or ""
        java_home = os.environ.get("JAVA_HOME", "")
        available = bool(jpype_installed and (jvm_path or java_home or java_bin))
        return {
            "jdbc_extension_ready": available,
            "jpype_installed": jpype_installed,
            "jvm_detected": bool(jvm_path or java_home or java_bin),
            "java_home": java_home,
            "note": (
                "自定义 JDBC 扩展通道就绪"
                if available
                else "未检测到本地 Java 运行环境 (JRE/JDK 8+)；内置原生通道 (DWS/PGSQL/Inceptor/MySQL/SQLite) 不受影响可正常使用。"
            ),
        }

    # =========================================================================
    # 自定义 JDBC .jar 驱动包安全上传、指纹校验与隔离存储
    # =========================================================================

    @staticmethod
    def validate_and_inspect_jar_bytes(filename: str, content: bytes) -> Tuple[str, List[str]]:
        """严格校验 .jar 文件头魔数、大小上限、防目录穿越，并自动提取 META-INF 驱动类名列表."""
        clean_fn = os.path.basename((filename or "").strip())
        if not clean_fn or not clean_fn.lower().endswith(".jar"):
            raise ValueError("仅允许上传标准 .jar 格式的 JDBC 驱动包文件")
        if not content:
            raise ValueError("上传的 .jar 驱动文件内容为空")
        if len(content) > MAX_JAR_UPLOAD_BYTES:
            raise ValueError("上传的 .jar 驱动包超过 64MB 安全大小限制")
        if not content.startswith(JAR_MAGIC_BYTES):
            raise ValueError("非法的 .jar 驱动包（ZIP/JAR 魔数头 PK\\x03\\x04 校验失败）")

        discovered_drivers: List[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                has_class_or_manifest = False
                for info in zf.infolist():
                    norm = os.path.normpath(info.filename).replace("\\", "/")
                    if norm.startswith("/") or norm.startswith("..") or "/../" in norm:
                        raise ValueError("驱动包内检测到非法的路径穿越条目，已安全拦截")
                    if norm.endswith(".class") or norm.upper() == "META-INF/MANIFEST.MF":
                        has_class_or_manifest = True
                    if norm == "META-INF/services/java.sql.Driver":
                        raw_svc = zf.read(info.filename).decode("utf-8", errors="replace")
                        for ln in raw_svc.splitlines():
                            cls_name = ln.split("#", 1)[0].strip()
                            if cls_name and cls_name not in discovered_drivers:
                                discovered_drivers.append(cls_name)
                if not has_class_or_manifest:
                    raise ValueError("上传的文件不是有效的 Java JAR 归档（未包含 .class 或 MANIFEST.MF）")
        except zipfile.BadZipFile as exc:
            raise ValueError("损坏的 .jar 压缩归档文件") from exc

        sha256_hex = hashlib.sha256(content).hexdigest()
        return sha256_hex, discovered_drivers

    def register_uploaded_jar(
        self,
        *,
        filename: str,
        content: bytes,
        name: str = "",
        db_type_hint: str = "CUSTOM_JDBC",
        driver_class_hint: str = "",
    ) -> DriverPackage:
        sha256_hex, discovered_drivers = self.validate_and_inspect_jar_bytes(
            filename, content
        )
        clean_orig_name = os.path.basename(filename.strip())
        safe_stored_name = f"drv_{sha256_hex[:16]}.jar"
        abs_target = os.path.join(self.drivers_dir, safe_stored_name)

        # 确保隔离写入在 drivers_dir 内部
        if os.path.commonpath([self.drivers_dir, os.path.abspath(abs_target)]) != self.drivers_dir:
            raise ValueError("非法的驱动存储目标路径")

        with open(abs_target, "wb") as fh:
            fh.write(content)
        try:
            os.chmod(abs_target, 0o644)
        except Exception:
            pass

        # 若同一 SHA-256 已注册则复用其 driver_id 幂等更新
        existing = next(
            (d for d in self.store.list_jdbc_drivers() if d.sha256 == sha256_hex),
            None,
        )
        driver_id = existing.driver_id if existing else f"jar-{sha256_hex[:12]}"
        eff_driver_class = (
            driver_class_hint.strip()
            or (discovered_drivers[0] if discovered_drivers else "")
            or self.get_template(db_type_hint).get("jdbc_driver_class", "")
        )
        pkg = DriverPackage(
            driver_id=driver_id,
            name=(name or clean_orig_name).strip(),
            file_name=clean_orig_name,
            stored_rel_path=safe_stored_name,
            sha256=sha256_hex,
            file_size=len(content),
            driver_class_hint=eff_driver_class,
            db_type_hint=(db_type_hint or "CUSTOM_JDBC").strip().upper(),
        )
        saved = self.store.upsert_jdbc_driver(pkg)
        self.store.add_log(
            "INFO",
            "驱动中心",
            f"已注册 JDBC 驱动包 [{saved.name}] (SHA256={saved.sha256[:16]}..., 驱动类={saved.driver_class_hint or '待指定'})",
        )
        return saved

    def delete_driver(self, driver_id: str) -> bool:
        pkg = self.store.delete_jdbc_driver(driver_id)
        if not pkg:
            return False
        self._classloader_cache.pop(pkg.sha256, None)
        abs_file = os.path.join(self.drivers_dir, os.path.basename(pkg.stored_rel_path))
        if os.path.isfile(abs_file):
            try:
                os.remove(abs_file)
            except Exception:
                pass
        return True

    def resolve_driver_jar_path(self, driver_id: str) -> Tuple[DriverPackage, str]:
        pkg = self.store.get_jdbc_driver(driver_id)
        if not pkg:
            raise ValueError(f"未找到指定的 JDBC 驱动包档案 ({driver_id})，请先在驱动中心上传 .jar 驱动")
        abs_path = os.path.join(self.drivers_dir, os.path.basename(pkg.stored_rel_path))
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"JDBC 驱动文件不存在或已被移除: {pkg.file_name}")
        # 二次完整性校验：核验磁盘文件魔数与 SHA-256 指纹未被篡改
        h = hashlib.sha256()
        with open(abs_path, "rb") as fh:
            head = fh.read(4)
            if head != JAR_MAGIC_BYTES:
                raise ValueError("本地 JDBC 驱动文件头校验失败")
            h.update(head)
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        if h.hexdigest() != pkg.sha256:
            raise ValueError("本地 JDBC 驱动文件 SHA-256 指纹校验不匹配，拒绝加载")
        return pkg, abs_path

    # =========================================================================
    # 双轨数据库连接建立 (Native & Lazy JPype JDBC)
    # =========================================================================

    def build_jdbc_url(self, profile: ConnectionProfile) -> str:
        if profile.jdbc_url and profile.jdbc_url.strip():
            return profile.jdbc_url.strip()
        tpl = self.get_template(profile.db_type)
        url_tpl = tpl.get("jdbc_url_template") or ""
        if not url_tpl:
            raise ValueError("自定义 JDBC 数据源必须填写完整的 JDBC URL")
        return url_tpl.format(
            host=profile.host.strip(),
            port=int(profile.port or tpl.get("default_port") or 0),
            dbname=profile.dbname.strip(),
        )

    def _ensure_jvm_started(self) -> Any:
        try:
            import jpype  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "当前环境未安装 JPype1 依赖，无法启用自定义 JDBC .jar 驱动扩展通道。"
            ) from exc

        with self._jvm_lock:
            if not jpype.isJVMStarted():
                try:
                    jvm_path = jpype.getDefaultJVMPath()
                except Exception as exc:
                    raise RuntimeError(
                        "未检测到本地 Java 运行环境 (JRE/JDK 8+)。请安装 JRE 或配置 JAVA_HOME 后再使用自定义 JDBC 驱动连接；内置原生驱动 (DWS/PGSQL/Inceptor/MySQL/SQLite) 无需 Java 可直接使用。"
                    ) from exc
                try:
                    jpype.startJVM(jvm_path, "-Djava.awt.headless=true", convertStrings=True)
                except Exception as exc:
                    raise RuntimeError(f"启动本地 Java 虚拟机失败: {exc}") from exc
            return jpype

    def _open_jdbc_connection(self, profile: ConnectionProfile, timeout_sec: int = 10) -> JdbcConnectionAdapter:
        if not profile.jdbc_driver_id:
            raise ValueError("使用自定义 JDBC 通道时，请先选择已上传的 .jar 驱动包")
        pkg, jar_abs_path = self.resolve_driver_jar_path(profile.jdbc_driver_id)
        driver_cls_name = (
            profile.jdbc_driver_class.strip()
            or pkg.driver_class_hint.strip()
            or self.get_template(profile.db_type).get("jdbc_driver_class", "")
        )
        if not driver_cls_name:
            raise ValueError("未能识别 JDBC Driver 类名，请在连接配置中填写（如 dm.jdbc.driver.DmDriver）")

        jdbc_url = self.build_jdbc_url(profile)
        jpype = self._ensure_jvm_started()

        JFile = jpype.JClass("java.io.File")
        URLClassLoader = jpype.JClass("java.net.URLClassLoader")
        Properties = jpype.JClass("java.util.Properties")
        DriverManagerJava = jpype.JClass("java.sql.DriverManager")

        try:
            DriverManagerJava.setLoginTimeout(max(3, int(timeout_sec)))
        except Exception:
            pass

        with self._jvm_lock:
            loader = self._classloader_cache.get(pkg.sha256)
            if loader is None:
                jar_url = JFile(jar_abs_path).toURI().toURL()
                loader = URLClassLoader([jar_url])
                self._classloader_cache[pkg.sha256] = loader

        try:
            drv_class = loader.loadClass(driver_cls_name)
            drv_instance = drv_class.getDeclaredConstructor().newInstance()
        except Exception as exc:
            raise RuntimeError(
                f"在驱动包 [{pkg.file_name}] 中加载驱动类 [{driver_cls_name}] 失败: {exc}"
            ) from exc

        props = Properties()
        if profile.user:
            props.setProperty("user", profile.user)
        if profile.password:
            props.setProperty("password", profile.password)
        for k, v in (profile.extra_props or {}).items():
            if k and v is not None:
                props.setProperty(str(k), str(v))

        raw_conn = drv_instance.connect(jdbc_url, props)
        if raw_conn is None:
            raise RuntimeError(
                f"驱动 [{driver_cls_name}] 不接受当前 JDBC URL 格式: {jdbc_url}"
            )
        return JdbcConnectionAdapter(raw_conn)

    def open_connection(self, profile: ConnectionProfile, timeout_sec: int = 10) -> Any:
        """根据 ConnectionProfile 打开对应驱动连接（返回 DB-API 连接、InceptorThriftClient 或 JdbcConnectionAdapter）."""
        track = (profile.driver_track or "NATIVE").strip().upper()
        db_type = (profile.db_type or "DWS").strip().upper()

        if track == "JDBC" or db_type in ("DM8", "ORACLE", "DB2", "GBASE", "CUSTOM_JDBC") or (
            db_type == "KINGBASE" and profile.jdbc_driver_id
        ):
            return self._open_jdbc_connection(profile, timeout_sec=timeout_sec)

        if db_type in ("DWS", "PGSQL", "KINGBASE"):
            if psycopg2 is None:
                raise RuntimeError("当前环境未安装 psycopg2-binary 驱动")
            if not profile.host or not profile.dbname or not profile.user:
                raise ValueError(f"{db_type} 连接配置不完整（需填写主机、端口、数据库名与用户名）")
            default_port = 8000 if db_type == "DWS" else 5432
            return psycopg2.connect(
                host=profile.host.strip(),
                port=int(profile.port or default_port),
                dbname=profile.dbname.strip(),
                user=profile.user.strip(),
                password=profile.password or "",
                sslmode=(profile.sslmode or "prefer").strip(),
                connect_timeout=max(3, int(timeout_sec)),
            )

        if db_type == "INCEPTOR":
            if not profile.host:
                raise ValueError("星环 Inceptor 主机地址不能为空")
            client = InceptorThriftClient(
                host=profile.host.strip(),
                port=int(profile.port or 10000),
                dbname=(profile.dbname or profile.default_schema or "default").strip(),
                user=(profile.user or "hive").strip(),
                password=profile.password or "",
                auth_mode=(profile.auth_mode or "LDAP").strip().upper(),
                timeout=float(max(5, int(timeout_sec))),
            )
            client.connect()
            return client

        if db_type in ("MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB"):
            if pymysql is None:
                raise RuntimeError("当前环境未安装 PyMySQL 驱动")
            if not profile.host or not profile.user:
                raise ValueError(f"{db_type} 连接配置不完整（需填写主机地址与用户名）")
            return pymysql.connect(
                host=profile.host.strip(),
                port=int(profile.port or 3306),
                user=profile.user.strip(),
                password=profile.password or "",
                database=(profile.dbname or profile.default_schema or "").strip() or None,
                charset="utf8mb4",
                connect_timeout=max(3, int(timeout_sec)),
                read_timeout=max(10, int(timeout_sec) * 3),
                write_timeout=max(10, int(timeout_sec) * 3),
                autocommit=True,
            )

        if db_type == "SQLITE":
            raw_path = (profile.sqlite_path or profile.dbname or "").strip()
            if not raw_path:
                raise ValueError("SQLite 数据源需指定数据库文件路径 (sqlite_path 或 dbname)")
            if raw_path != ":memory:":
                abs_sq = os.path.abspath(raw_path)
                os.makedirs(os.path.dirname(abs_sq), exist_ok=True)
                raw_path = abs_sq
            conn = sqlite3.connect(raw_path, timeout=float(max(5, int(timeout_sec))), check_same_thread=False)
            return conn

        raise ValueError(f"暂不支持的数据源驱动类型: {db_type}")
