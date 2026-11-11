"""纯 Python 星环 Transwarp Inceptor / HiveServer2 Thrift 协议客户端."""

from __future__ import annotations

import logging
import socket
import struct
from typing import Any, Dict, List, Optional, Tuple

from core.platform.dialects.identifiers import validate_identifier
from core.platform.models import ColumnMeta

logger = logging.getLogger("dataog")


class InceptorThriftClient:
    """高兼容纯 Python HiveServer2 Thrift 客户端（全面适配星环 Inceptor 各版本默认 10000 端口）.

    核心特性：
    1. 严格遵循 TCLIService.thrift 规范提取 TOpenSessionResp.sessionHandle (field 3)；
    2. 支持协议版本自动协商 (V5 -> V6 -> V4 -> V3 -> V1)；
    3. 支持传输层自动协商回退 (SASL PLAIN / NOSASL UNFRAMED / NOSASL FRAMED)；
    4. 支持 GetResultSetMetadata (probe_query_schema) 与带列头及行数上限的查询执行。
    """

    T_STOP = 0
    T_BOOL = 2
    T_BYTE = 3
    T_DOUBLE = 4
    T_I16 = 6
    T_I32 = 8
    T_I64 = 10
    T_STRING = 11
    T_STRUCT = 12
    T_MAP = 13
    T_SET = 14
    T_LIST = 15

    SASL_START = 1
    SASL_OK = 2
    SASL_BAD = 3
    SASL_ERROR = 4
    SASL_COMPLETE = 5

    HIVE_TYPE_MAP = {
        0: "BOOLEAN",
        1: "TINYINT",
        2: "SMALLINT",
        3: "INT",
        4: "BIGINT",
        5: "FLOAT",
        6: "DOUBLE",
        7: "STRING",
        8: "TIMESTAMP",
        9: "BINARY",
        10: "ARRAY",
        11: "MAP",
        12: "STRUCT",
        13: "UNIONTYPE",
        15: "DECIMAL",
        16: "NULL",
        17: "DATE",
        18: "VARCHAR",
        19: "CHAR",
        20: "INTERVAL_YEAR_MONTH",
        21: "INTERVAL_DAY_TIME",
    }

    def __init__(
        self,
        host: str,
        port: int = 10000,
        dbname: str = "default",
        user: str = "hive",
        password: str = "",
        auth_mode: str = "LDAP",
        timeout: float = 12.0,
    ) -> None:
        self.host = host.strip()
        self.port = int(port or 10000)
        self.dbname = (dbname or "default").strip() or "default"
        self.user = (user or "anonymous").strip() or "anonymous"
        self.raw_password = password or ""
        self.auth_mode = (auth_mode or "LDAP").strip().upper()
        if self.raw_password:
            self.password = self.raw_password
        elif self.auth_mode == "LDAP":
            self.password = ""
        else:
            self.password = "anonymous"
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._seq_id = 0
        self._session_handle: Optional[Tuple[bytes, bytes]] = None
        self._protocol_version = 4
        self._transport_framing = "SASL"

    def __enter__(self) -> "InceptorThriftClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("星环 Inceptor HiveServer2 连接已断开")
            buf.extend(chunk)
        return bytes(buf)

    def _send_sasl_msg(self, status: int, payload: bytes) -> None:
        assert self._sock is not None
        header = struct.pack(">BI", status, len(payload))
        self._sock.sendall(header + payload)

    def _recv_sasl_msg(self) -> Tuple[int, bytes]:
        header = self._recv_exact(5)
        status, length = struct.unpack(">BI", header)
        payload = self._recv_exact(length) if length > 0 else b""
        return status, payload

    def _perform_sasl_plain_handshake(self, pwd_to_use: str) -> None:
        self._send_sasl_msg(self.SASL_START, b"PLAIN")
        auth_bytes = (
            b"\x00"
            + self.user.encode("utf-8")
            + b"\x00"
            + pwd_to_use.encode("utf-8")
        )
        self._send_sasl_msg(self.SASL_OK, auth_bytes)
        while True:
            status, payload = self._recv_sasl_msg()
            if status == self.SASL_COMPLETE:
                break
            if status in (self.SASL_BAD, self.SASL_ERROR):
                err_txt = payload.decode("utf-8", errors="replace")
                raise PermissionError(f"星环 Inceptor 账号或密码认证失败 (SASL): {err_txt}")
            self._send_sasl_msg(self.SASL_OK, b"")

    def connect(self) -> None:
        if self.auth_mode == "NOSASL":
            framing_candidates = ["UNFRAMED", "FRAMED", "SASL"]
        else:
            framing_candidates = ["SASL", "UNFRAMED", "FRAMED"]

        proto_candidates = [4, 5, 3, 2, 0]
        last_err: Optional[Exception] = None

        for framing in framing_candidates:
            for proto_ver in proto_candidates:
                self._close_socket_only()
                try:
                    self._sock = socket.create_connection(
                        (self.host, self.port), timeout=self.timeout
                    )
                    self._transport_framing = framing
                    self._protocol_version = proto_ver
                    self._seq_id = 0

                    if framing == "SASL":
                        pwd_try = self.password if self.password != "" else "anonymous"
                        self._perform_sasl_plain_handshake(pwd_try)

                    self._open_session(proto_ver)

                    if self.dbname and self.dbname.lower() != "default":
                        try:
                            safe_db = validate_identifier(self.dbname, "Inceptor数据库名")
                            self.execute_query(f"USE {safe_db}")
                        except Exception as use_exc:
                            logger.warning(
                                "星环 Inceptor 连接已建立，但切换默认库 USE %s 未生效 (%s)，将使用服务端默认库。",
                                self.dbname,
                                use_exc,
                            )
                    return
                except PermissionError:
                    self._close_socket_only()
                    raise
                except Exception as exc:
                    last_err = exc
                    self._close_socket_only()
                    if (
                        self._session_handle is None
                        and "OpenSession" not in str(exc)
                        and framing == "SASL"
                        and proto_ver == proto_candidates[0]
                    ):
                        continue

        raise RuntimeError(
            f"无法建立星环 Inceptor ({self.host}:{self.port}) 会话: {last_err}"
        )

    def _send_framed_rpc(self, method_name: str, body_bytes: bytes) -> bytes:
        assert self._sock is not None
        self._seq_id += 1
        name_b = method_name.encode("utf-8")
        msg = (
            struct.pack(">II", 0x80010001, len(name_b))
            + name_b
            + struct.pack(">i", self._seq_id)
            + body_bytes
            + b"\x00"
        )
        if self._transport_framing in ("SASL", "FRAMED"):
            self._sock.sendall(struct.pack(">I", len(msg)) + msg)
            frame_len = struct.unpack(">I", self._recv_exact(4))[0]
            return self._recv_exact(frame_len)
        else:
            self._sock.sendall(msg)
            buf = bytearray()
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    if not buf:
                        raise ConnectionError(f"星环 Inceptor 在执行 {method_name} 时断开连接")
                    break
                buf.extend(chunk)
                try:
                    self._probe_complete_thrift_message(bytes(buf))
                    return bytes(buf)
                except (struct.error, IndexError):
                    continue

    @classmethod
    def _probe_complete_thrift_message(cls, data: bytes) -> None:
        if len(data) < 12:
            raise IndexError("incomplete header")
        offset = 4
        name_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4 + name_len + 4
        if offset >= len(data):
            raise IndexError("incomplete body")
        cls._read_struct(data, offset)

    @staticmethod
    def _pack_string(val: str) -> bytes:
        b = val.encode("utf-8")
        return struct.pack(">I", len(b)) + b

    @staticmethod
    def _pack_binary(val: bytes) -> bytes:
        return struct.pack(">I", len(val)) + val

    def _pack_handle_id(self, guid: bytes, secret: bytes) -> bytes:
        return (
            struct.pack(">Bh", self.T_STRING, 1)
            + self._pack_binary(guid)
            + struct.pack(">Bh", self.T_STRING, 2)
            + self._pack_binary(secret)
            + b"\x00"
        )

    def _open_session(self, proto_ver: Optional[int] = None) -> None:
        eff_proto = self._protocol_version if proto_ver is None else int(proto_ver)
        req_body = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + struct.pack(">Bh", self.T_I32, 1)
            + struct.pack(">i", eff_proto)
            + struct.pack(">Bh", self.T_STRING, 2)
            + self._pack_string(self.user)
            + struct.pack(">Bh", self.T_STRING, 3)
            + self._pack_string(self.password)
            + b"\x00"
        )
        resp = self._send_framed_rpc("OpenSession", req_body)
        parsed = self._parse_thrift_message(resp)
        res_struct = parsed.get(0, {})
        self._check_status(res_struct.get(1, {}))
        if 2 in res_struct and isinstance(res_struct[2], int):
            self._protocol_version = res_struct[2]

        sess_struct = res_struct.get(3)
        if not isinstance(sess_struct, dict) or 1 not in sess_struct:
            sess_struct = res_struct.get(4, {}) if isinstance(res_struct.get(4), dict) else {}
        handle_id = sess_struct.get(1, {}) if isinstance(sess_struct, dict) else {}
        guid = handle_id.get(1, b"") if isinstance(handle_id, dict) else b""
        secret = handle_id.get(2, b"") if isinstance(handle_id, dict) else b""
        if isinstance(guid, str):
            guid = guid.encode("latin-1")
        if isinstance(secret, str):
            secret = secret.encode("latin-1")
        if not guid:
            raise RuntimeError("OpenSession 未返回有效的 SessionHandle GUID")
        self._session_handle = (guid, secret)

    @staticmethod
    def _check_status(status_struct: Dict[int, Any]) -> None:
        if not isinstance(status_struct, dict):
            return
        code = status_struct.get(1, 0)
        if code in (3, 4):
            err_msg = status_struct.get(3, b"Inceptor Thrift RPC Error")
            if isinstance(err_msg, bytes):
                err_msg = err_msg.decode("utf-8", errors="replace")
            raise RuntimeError(f"星环 Inceptor 执行异常: {err_msg}")

    def _encode_op_handle(
        self, op_guid: bytes, op_secret: bytes, op_type: int, has_result_set: bool
    ) -> bytes:
        return (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + self._pack_handle_id(op_guid, op_secret)
            + struct.pack(">Bh", self.T_I32, 2)
            + struct.pack(">i", op_type)
            + struct.pack(">Bh", self.T_BOOL, 3)
            + struct.pack(">B", 1 if has_result_set else 0)
            + b"\x00"
        )

    def _close_operation(self, op_handle_bytes: bytes) -> None:
        try:
            req = (
                struct.pack(">Bh", self.T_STRUCT, 1)
                + struct.pack(">Bh", self.T_STRUCT, 1)
                + op_handle_bytes
                + b"\x00"
            )
            self._send_framed_rpc("CloseOperation", req)
        except Exception:
            pass

    def _execute_statement_raw(self, sql: str) -> Tuple[bytes, bool]:
        if not self._session_handle:
            raise RuntimeError("星环 Inceptor 会话尚未初始化")
        guid, secret = self._session_handle
        sess_bytes = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + self._pack_handle_id(guid, secret)
            + b"\x00"
        )
        req_body = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + struct.pack(">Bh", self.T_STRUCT, 1)
            + sess_bytes
            + struct.pack(">Bh", self.T_STRING, 2)
            + self._pack_string(sql)
            + struct.pack(">BhB", self.T_BOOL, 4, 0)
            + b"\x00"
        )
        resp = self._send_framed_rpc("ExecuteStatement", req_body)
        parsed = self._parse_thrift_message(resp)
        res_struct = parsed.get(0, {})
        self._check_status(res_struct.get(1, {}))
        op_handle = res_struct.get(2, {})
        has_result_set = bool(op_handle.get(3, False))
        op_id = op_handle.get(1, {})
        op_guid = op_id.get(1, b"")
        op_secret = op_id.get(2, b"")
        if isinstance(op_guid, str):
            op_guid = op_guid.encode("latin-1")
        if isinstance(op_secret, str):
            op_secret = op_secret.encode("latin-1")
        op_type = int(op_handle.get(2, 0))
        op_handle_bytes = self._encode_op_handle(
            op_guid, op_secret, op_type, has_result_set
        )
        return op_handle_bytes, has_result_set

    def _fetch_metadata_for_op(self, op_handle_bytes: bytes) -> List[ColumnMeta]:
        meta_req = (
            struct.pack(">Bh", self.T_STRUCT, 1)
            + struct.pack(">Bh", self.T_STRUCT, 1)
            + op_handle_bytes
            + b"\x00"
        )
        meta_resp = self._send_framed_rpc("GetResultSetMetadata", meta_req)
        meta_parsed = self._parse_thrift_message(meta_resp)
        meta_res = meta_parsed.get(0, {})
        self._check_status(meta_res.get(1, {}))
        schema_struct = meta_res.get(2, {})
        col_descs = schema_struct.get(1, []) if isinstance(schema_struct, dict) else []
        cols: List[ColumnMeta] = []

        for cd in col_descs:
            if not isinstance(cd, dict):
                continue
            raw_name = cd.get(1, b"")
            if isinstance(raw_name, bytes):
                raw_name = raw_name.decode("utf-8", errors="replace")
            col_name = str(raw_name).strip()
            if "." in col_name:
                col_name = col_name.split(".")[-1].strip()

            raw_comm = cd.get(4, b"")
            if isinstance(raw_comm, bytes):
                raw_comm = raw_comm.decode("utf-8", errors="replace")
            col_comm = str(raw_comm or "").strip()

            col_type = "STRING"
            type_desc = cd.get(2, {})
            type_entries = type_desc.get(1, []) if isinstance(type_desc, dict) else []
            if type_entries and isinstance(type_entries[0], dict):
                prim = type_entries[0].get(1, {})
                if isinstance(prim, dict):
                    tid = int(prim.get(1, 7))
                    base_t = self.HIVE_TYPE_MAP.get(tid, "STRING")
                    quals_struct = prim.get(2, {})
                    q_map_raw = quals_struct.get(1, {}) if isinstance(quals_struct, dict) else {}
                    q_map: Dict[str, int] = {}
                    if isinstance(q_map_raw, dict):
                        for qk, qv in q_map_raw.items():
                            k_str = qk.decode("utf-8", errors="replace") if isinstance(qk, bytes) else str(qk)
                            if isinstance(qv, dict) and 1 in qv and isinstance(qv[1], int):
                                q_map[k_str] = qv[1]
                    if base_t in ("VARCHAR", "CHAR") and "characterMaximumLength" in q_map:
                        col_type = f"{base_t}({q_map['characterMaximumLength']})"
                    elif base_t == "DECIMAL" and "precision" in q_map:
                        sc = q_map.get("scale", 0)
                        col_type = f"DECIMAL({q_map['precision']},{sc})"
                    else:
                        col_type = base_t

            if col_name:
                cols.append(
                    ColumnMeta(
                        ordinal_position=len(cols) + 1,
                        column_name=col_name,
                        data_type=col_type,
                        is_not_null=False,
                        column_default=None,
                        column_comment=col_comm,
                    )
                )
        return cols

    def execute_query(self, sql: str, max_rows: int = 5000) -> List[List[Any]]:
        op_handle_bytes, has_result_set = self._execute_statement_raw(sql)
        if not has_result_set:
            self._close_operation(op_handle_bytes)
            return []

        try:
            fetch_req = (
                struct.pack(">Bh", self.T_STRUCT, 1)
                + struct.pack(">Bh", self.T_STRUCT, 1)
                + op_handle_bytes
                + struct.pack(">Bh", self.T_I32, 2)
                + struct.pack(">i", 0)
                + struct.pack(">Bh", self.T_I64, 3)
                + struct.pack(">q", max(1, int(max_rows)))
                + b"\x00"
            )
            fetch_resp = self._send_framed_rpc("FetchResults", fetch_req)
            fetch_parsed = self._parse_thrift_message(fetch_resp)
            fetch_res = fetch_parsed.get(0, {})
            self._check_status(fetch_res.get(1, {}))
            rowset = fetch_res.get(3, {})
            return self._extract_rows_from_rowset(rowset)
        finally:
            self._close_operation(op_handle_bytes)

    def execute_query_with_meta(
        self, sql: str, max_rows: int = 500
    ) -> Tuple[List[ColumnMeta], List[List[Any]], bool]:
        """执行 SQL 查询并同时返回列头类型描述、结果行与截断标记."""
        op_handle_bytes, has_result_set = self._execute_statement_raw(sql)
        if not has_result_set:
            self._close_operation(op_handle_bytes)
            return [], [], False

        try:
            cols: List[ColumnMeta] = []
            try:
                cols = self._fetch_metadata_for_op(op_handle_bytes)
            except Exception:
                cols = []

            fetch_limit = max(1, int(max_rows)) + 1
            fetch_req = (
                struct.pack(">Bh", self.T_STRUCT, 1)
                + struct.pack(">Bh", self.T_STRUCT, 1)
                + op_handle_bytes
                + struct.pack(">Bh", self.T_I32, 2)
                + struct.pack(">i", 0)
                + struct.pack(">Bh", self.T_I64, 3)
                + struct.pack(">q", fetch_limit)
                + b"\x00"
            )
            fetch_resp = self._send_framed_rpc("FetchResults", fetch_req)
            fetch_parsed = self._parse_thrift_message(fetch_resp)
            fetch_res = fetch_parsed.get(0, {})
            self._check_status(fetch_res.get(1, {}))
            rowset = fetch_res.get(3, {})
            rows = self._extract_rows_from_rowset(rowset)
            truncated = len(rows) > max_rows
            if truncated:
                rows = rows[:max_rows]
            if not cols and rows:
                cols = [
                    ColumnMeta(
                        ordinal_position=i + 1,
                        column_name=f"col_{i + 1}",
                        data_type="STRING",
                    )
                    for i in range(len(rows[0]))
                ]
            return cols, rows, truncated
        finally:
            self._close_operation(op_handle_bytes)

    def probe_query_schema(self, sql: str) -> List[ColumnMeta]:
        """利用 HiveServer2 GetResultSetMetadata 对视图或 SELECT 语句执行零行结果集元数据探测."""
        op_handle_bytes, _ = self._execute_statement_raw(sql)
        try:
            return self._fetch_metadata_for_op(op_handle_bytes)
        finally:
            self._close_operation(op_handle_bytes)

    @classmethod
    def _extract_rows_from_rowset(cls, rowset: Dict[int, Any]) -> List[List[Any]]:
        cols = rowset.get(3)
        if isinstance(cols, list) and cols:
            col_arrays: List[List[Any]] = []
            for c_struct in cols:
                if not isinstance(c_struct, dict):
                    continue
                vals: List[Any] = []
                for field_id in (7, 4, 5, 3, 6, 1, 2):
                    if field_id in c_struct and isinstance(c_struct[field_id], dict):
                        raw_list = c_struct[field_id].get(1, [])
                        vals = [
                            v.decode("utf-8", errors="replace")
                            if isinstance(v, bytes)
                            else v
                            for v in raw_list
                        ]
                        break
                col_arrays.append(vals)
            if not col_arrays:
                return []
            num_rows = max(len(arr) for arr in col_arrays)
            rows: List[List[Any]] = []
            for r_idx in range(num_rows):
                rows.append(
                    [
                        arr[r_idx] if r_idx < len(arr) else None
                        for arr in col_arrays
                    ]
                )
            return rows

        raw_rows = rowset.get(2)
        if isinstance(raw_rows, list) and raw_rows:
            out: List[List[Any]] = []
            for r_struct in raw_rows:
                col_vals = r_struct.get(1, []) if isinstance(r_struct, dict) else []
                row_items: List[Any] = []
                for cv in col_vals:
                    val = None
                    if isinstance(cv, dict):
                        for fid in (7, 4, 5, 3, 6, 1, 2):
                            if fid in cv and isinstance(cv[fid], dict):
                                v = cv[fid].get(1)
                                if isinstance(v, bytes):
                                    v = v.decode("utf-8", errors="replace")
                                val = v
                                break
                    row_items.append(val)
                out.append(row_items)
            return out
        return []

    @classmethod
    def _parse_thrift_message(cls, data: bytes) -> Dict[int, Any]:
        offset = 0
        ver_type = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        name_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4 + name_len
        _seq_id = struct.unpack_from(">i", data, offset)[0]
        offset += 4
        msg_type = ver_type & 0xFF
        struct_val, _ = cls._read_struct(data, offset)
        if msg_type == 3:
            err_msg = struct_val.get(1, b"Thrift Application Exception")
            if isinstance(err_msg, bytes):
                err_msg = err_msg.decode("utf-8", errors="replace")
            raise RuntimeError(f"Thrift RPC 异常: {err_msg}")
        return struct_val

    @classmethod
    def _read_struct(cls, data: bytes, offset: int) -> Tuple[Dict[int, Any], int]:
        fields: Dict[int, Any] = {}
        while offset < len(data):
            ftype = data[offset]
            offset += 1
            if ftype == cls.T_STOP:
                break
            fid = struct.unpack_from(">h", data, offset)[0]
            offset += 2
            val, offset = cls._read_value(data, offset, ftype)
            fields[fid] = val
        return fields, offset

    @classmethod
    def _read_value(cls, data: bytes, offset: int, ftype: int) -> Tuple[Any, int]:
        if ftype == cls.T_BOOL:
            return bool(data[offset]), offset + 1
        if ftype == cls.T_BYTE:
            return struct.unpack_from(">b", data, offset)[0], offset + 1
        if ftype == cls.T_I16:
            return struct.unpack_from(">h", data, offset)[0], offset + 2
        if ftype == cls.T_I32:
            return struct.unpack_from(">i", data, offset)[0], offset + 4
        if ftype == cls.T_I64:
            return struct.unpack_from(">q", data, offset)[0], offset + 8
        if ftype == cls.T_DOUBLE:
            return struct.unpack_from(">d", data, offset)[0], offset + 8
        if ftype == cls.T_STRING:
            length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            return data[offset : offset + length], offset + length
        if ftype == cls.T_STRUCT:
            return cls._read_struct(data, offset)
        if ftype == cls.T_LIST or ftype == cls.T_SET:
            elem_type = data[offset]
            count = struct.unpack_from(">I", data, offset + 1)[0]
            offset += 5
            items = []
            for _ in range(count):
                v, offset = cls._read_value(data, offset, elem_type)
                items.append(v)
            return items, offset
        if ftype == cls.T_MAP:
            ktype = data[offset]
            vtype = data[offset + 1]
            count = struct.unpack_from(">I", data, offset + 2)[0]
            offset += 6
            mp = {}
            for _ in range(count):
                k, offset = cls._read_value(data, offset, ktype)
                v, offset = cls._read_value(data, offset, vtype)
                mp[k] = v
            return mp, offset
        raise ValueError(f"未知的 Thrift 字段类型: {ftype}")

    def _close_socket_only(self) -> None:
        self._session_handle = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def close(self) -> None:
        if self._sock and self._session_handle:
            try:
                guid, secret = self._session_handle
                sess_bytes = (
                    struct.pack(">Bh", self.T_STRUCT, 1)
                    + self._pack_handle_id(guid, secret)
                    + b"\x00"
                )
                req = (
                    struct.pack(">Bh", self.T_STRUCT, 1)
                    + struct.pack(">Bh", self.T_STRUCT, 1)
                    + sess_bytes
                    + b"\x00"
                )
                self._send_framed_rpc("CloseSession", req)
            except Exception:
                pass
        self._close_socket_only()
