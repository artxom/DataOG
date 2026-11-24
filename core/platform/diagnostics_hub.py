"""Data.O.G 平台级零扰动全链路性能追踪、全栈基准压测与脱敏诊断导出中枢 (DiagnosticsHub).

设计原则：
1. 零观测扰动：所有请求 Trace 与子阶段 Span 均保存在内存环形缓冲 (`collections.deque`)，
   绝不在采集路径写入 SQLite 或触发磁盘 I/O，避免在内网慢磁盘 / EDR 扫描环境下放大开销；
2. 全链路覆盖：贯通浏览器前端 (`perf_monitor.js`) -> 本地环回/代理 -> ASGI Gate 1 鉴权
   -> 事件循环/线程池 -> SQLite 锁竞争与连接开闭 -> 远端 DB / JDBC / 元数据 / LLM；
3. 零泄密红线：对私有存储物理路径、混淆文件名、数据库密码、LLM API Key、Bearer Token
   及完整激活码执行全量递归脱敏，绝不向外暴露敏感凭据或私有目录结构。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ctypes
import datetime
import json
import math
import os
import platform
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from typing import Any, Deque, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from starlette.concurrency import run_in_threadpool

from core.platform.storage_paths import (
    redact_private_storage_paths,
    resolve_private_storage_layout,
)

MAX_SPANS_PER_TRACE = 120
MAX_RECENT_TRACES = 500
MAX_SLOW_TRACES = 200
DEFAULT_SLOW_THRESHOLD_MS = 300.0

_CURRENT_TRACE: contextvars.ContextVar[Optional["_ActiveRequestTrace"]] = (
    contextvars.ContextVar("dataog_current_trace", default=None)
)
_CURRENT_SPAN: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("dataog_current_span", default=None)
)

_DUMMY_ED25519_PK = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
_DUMMY_ED25519_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)

_RE_ACTIVATION_CODE = re.compile(r"\bDOG1-[A-Z0-9-]{16,}\b", re.IGNORECASE)
_RE_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
_RE_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_RE_GOOGLE_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{10,}\b")
_RE_KV_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|api_key|apikey|secret|token|authorization)\b(\s*[:=]\s*)([^\s,;\"'&\]]+)"
)


def _round_ms(val: float) -> float:
    return round(float(val or 0.0), 2)


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return _round_ms(ordered[0])
    idx = (max(0.0, min(100.0, pct)) / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return _round_ms(ordered[lo])
    frac = idx - lo
    return _round_ms(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def _sanitize_proxy_url(raw_url: str) -> str:
    txt = str(raw_url or "").strip()
    if not txt:
        return ""
    try:
        parsed = urlparse(txt if "://" in txt else f"http://{txt}")
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        if host:
            return f"{scheme}://{host}{port}"
    except Exception:
        pass
    return "<configured_proxy>"


class _ActiveRequestTrace:
    """单次 HTTP 请求的内存级链路采样容器."""

    __slots__ = (
        "trace_id",
        "started_at",
        "start_mono",
        "method",
        "path",
        "query_keys",
        "is_api",
        "concurrent_at_start",
        "active_threads",
        "spans",
        "spans_truncated",
        "category_totals",
        "_lock",
    )

    def __init__(
        self,
        *,
        trace_id: str,
        method: str,
        path: str,
        query_keys: List[str],
        is_api: bool,
        concurrent_at_start: int,
        active_threads: int,
    ) -> None:
        self.trace_id = trace_id
        self.started_at = datetime.datetime.now().isoformat(timespec="milliseconds")
        self.start_mono = time.perf_counter()
        self.method = method
        self.path = path
        self.query_keys = list(query_keys)
        self.is_api = is_api
        self.concurrent_at_start = concurrent_at_start
        self.active_threads = active_threads
        self.spans: List[Dict[str, Any]] = []
        self.spans_truncated = 0
        self.category_totals: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add_span(
        self,
        *,
        category: str,
        name: str,
        start_offset_ms: float,
        duration_ms: float,
        status: str = "ok",
        error_type: str = "",
        meta: Optional[Dict[str, Any]] = None,
        record_detail: bool = True,
    ) -> None:
        dur = round(max(0.0, float(duration_ms)), 3)
        off = round(max(0.0, float(start_offset_ms)), 3)
        thr = threading.current_thread()
        is_main = thr is threading.main_thread()
        tname = thr.name or "Thread"
        with self._lock:
            cat = self.category_totals.get(category)
            if cat is None:
                self.category_totals[category] = {
                    "count": 1,
                    "total_ms": dur,
                    "max_ms": dur,
                }
            else:
                cat["count"] += 1
                cat["total_ms"] = round(cat["total_ms"] + dur, 3)
                if dur > cat["max_ms"]:
                    cat["max_ms"] = dur

            if not record_detail:
                return
            if len(self.spans) >= MAX_SPANS_PER_TRACE:
                self.spans_truncated += 1
                return
            entry: Dict[str, Any] = {
                "category": category,
                "name": name,
                "start_offset_ms": off,
                "duration_ms": dur,
                "status": status,
                "is_main_thread": is_main,
                "thread_name": tname,
            }
            if error_type:
                entry["error_type"] = error_type
            if meta:
                entry["meta"] = dict(meta)
            self.spans.append(entry)


def get_current_trace() -> Optional[_ActiveRequestTrace]:
    return _CURRENT_TRACE.get()


def annotate_current_span(**kwargs: Any) -> None:
    """在当前活动 Span 上追加结构化诊断属性（无活动 Span 时为空操作）."""
    span_ctx = _CURRENT_SPAN.get()
    if span_ctx is None:
        return
    meta = span_ctx.get("meta")
    if isinstance(meta, dict):
        for k, v in kwargs.items():
            if v is not None:
                meta[str(k)] = v


set_current_span_meta = annotate_current_span


def record_instant_metric(
    category: str,
    name: str,
    duration_ms: float,
    meta: Optional[Dict[str, Any]] = None,
    *,
    min_span_ms: float = 0.0,
) -> None:
    """上报瞬时事件耗时至当前请求 Trace；始终计入分类汇总，仅超阈值写入明细 Span."""
    trace = _CURRENT_TRACE.get()
    if trace is None:
        return
    dur = max(0.0, float(duration_ms))
    off = max(0.0, (time.perf_counter() - trace.start_mono) * 1000.0 - dur)
    trace.add_span(
        category=category,
        name=name,
        start_offset_ms=off,
        duration_ms=dur,
        status="ok",
        meta=meta,
        record_detail=(dur >= float(min_span_ms)),
    )


@contextlib.contextmanager
def trace_span(
    category: str,
    name: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    """零开销链路 Span 上下文：无活动请求 Trace 时直接放行（< 1µs）."""
    trace = _CURRENT_TRACE.get()
    if trace is None:
        yield
        return

    t0 = time.perf_counter()
    start_offset_ms = (t0 - trace.start_mono) * 1000.0
    span_ctx: Dict[str, Any] = {"meta": dict(meta) if meta else {}}
    tok = _CURRENT_SPAN.set(span_ctx)
    status = "ok"
    err_type = ""
    try:
        yield
    except BaseException as exc:
        status = "error"
        err_type = type(exc).__name__
        raise
    finally:
        _CURRENT_SPAN.reset(tok)
        dur_ms = (time.perf_counter() - t0) * 1000.0
        trace.add_span(
            category=category,
            name=name,
            start_offset_ms=start_offset_ms,
            duration_ms=dur_ms,
            status=status,
            error_type=err_type,
            meta=span_ctx["meta"] or None,
            record_detail=True,
        )


def _detect_storage_drive_info(path_str: str) -> Tuple[str, bool]:
    """探测存储路径所属磁盘类型与是否为 UNC 网络共享路径（严禁返回具体盘符或路径）."""
    raw = str(path_str or "").strip()
    is_unc = raw.startswith("\\\\") or raw.startswith("//")
    if os.name == "nt":
        if is_unc:
            return "REMOTE", True
        try:
            drive, _ = os.path.splitdrive(os.path.abspath(raw))
            if drive:
                root_path = drive.rstrip("\\/") + "\\"
                get_drive_type = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
                get_drive_type.argtypes = [ctypes.c_wchar_p]
                get_drive_type.restype = ctypes.c_uint32
                dtype = int(get_drive_type(root_path))
                mapping = {
                    0: "UNKNOWN",
                    1: "NO_ROOT_DIR",
                    2: "REMOVABLE",
                    3: "FIXED",
                    4: "REMOTE",
                    5: "CDROM",
                    6: "RAMDISK",
                }
                return mapping.get(dtype, "UNKNOWN"), False
        except Exception:
            return "UNKNOWN", is_unc
        return "UNKNOWN", is_unc
    return ("REMOTE" if is_unc else "POSIX_LOCAL"), is_unc


class DiagnosticsHub:
    """平台全链路性能采样、基准压测、自动研判与脱敏诊断日志包导出中心."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._traces: Deque[Dict[str, Any]] = deque(maxlen=MAX_RECENT_TRACES)
        self._slow_traces: Deque[Dict[str, Any]] = deque(maxlen=MAX_SLOW_TRACES)
        self.slow_threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS

        self.started_at_iso = datetime.datetime.now().isoformat(timespec="seconds")
        self.started_monotonic = time.monotonic()
        self.total_requests = 0
        self.error_requests = 0
        self.suppressed_fast_polls = 0
        self.in_flight_current = 0
        self.in_flight_peak = 0
        self._last_benchmark: Optional[Dict[str, Any]] = None

        self._sqlite_stats: Dict[str, Any] = {
            "lock_acquires": 0,
            "lock_wait_total_ms": 0.0,
            "lock_wait_max_ms": 0.0,
            "lock_contention_count": 0,
            "lock_hold_max_ms": 0.0,
            "private_opens": 0,
            "private_open_total_ms": 0.0,
            "private_open_max_ms": 0.0,
            "private_reuses": 0,
            "workspace_opens": 0,
            "workspace_open_total_ms": 0.0,
            "workspace_open_max_ms": 0.0,
            "workspace_reuses": 0,
        }

        store = getattr(ctx, "store", None)
        if store is not None:
            init_stats = getattr(store, "_init_open_stats", None)
            if isinstance(init_stats, dict):
                p_init = init_stats.get("private") or {}
                w_init = init_stats.get("workspace") or {}
                self._sqlite_stats["private_opens"] = int(p_init.get("count") or 0)
                self._sqlite_stats["private_open_total_ms"] = float(p_init.get("total_ms") or 0.0)
                self._sqlite_stats["private_open_max_ms"] = float(p_init.get("max_ms") or 0.0)
                self._sqlite_stats["private_reuses"] = int(p_init.get("reuses") or 0)
                self._sqlite_stats["workspace_opens"] = int(w_init.get("count") or 0)
                self._sqlite_stats["workspace_open_total_ms"] = float(w_init.get("total_ms") or 0.0)
                self._sqlite_stats["workspace_open_max_ms"] = float(w_init.get("max_ms") or 0.0)
                self._sqlite_stats["workspace_reuses"] = int(w_init.get("reuses") or 0)
            store._diag_hub = self

    def record_sqlite_stat(
        self,
        kind: str,
        *,
        wait_ms: Optional[float] = None,
        hold_ms: Optional[float] = None,
        open_ms: Optional[float] = None,
    ) -> None:
        with self._lock:
            st = self._sqlite_stats
            if kind == "lock":
                if wait_ms is not None:
                    w = max(0.0, float(wait_ms))
                    st["lock_acquires"] += 1
                    st["lock_wait_total_ms"] += w
                    if w > st["lock_wait_max_ms"]:
                        st["lock_wait_max_ms"] = w
                    if w >= 1.0:
                        st["lock_contention_count"] += 1
                if hold_ms is not None:
                    h = max(0.0, float(hold_ms))
                    if h > st["lock_hold_max_ms"]:
                        st["lock_hold_max_ms"] = h
            elif kind == "private_open" and open_ms is not None:
                o = max(0.0, float(open_ms))
                st["private_opens"] += 1
                st["private_open_total_ms"] += o
                if o > st["private_open_max_ms"]:
                    st["private_open_max_ms"] = o
            elif kind == "private_reuse":
                st["private_reuses"] += 1
            elif kind == "workspace_open" and open_ms is not None:
                o = max(0.0, float(open_ms))
                st["workspace_opens"] += 1
                st["workspace_open_total_ms"] += o
                if o > st["workspace_open_max_ms"]:
                    st["workspace_open_max_ms"] = o
            elif kind == "workspace_reuse":
                if st["workspace_opens"] == 0 and st["private_opens"] > 0:
                    st["workspace_opens"] = 1
                st["workspace_reuses"] += 1

    def get_sqlite_stats(self) -> Dict[str, Any]:
        with self._lock:
            st = dict(self._sqlite_stats)
        acquires = max(1, int(st["lock_acquires"]))
        priv_opens = max(1, int(st["private_opens"]))
        ws_opens = max(1, int(st["workspace_opens"]))
        return {
            "lock_acquires": int(st["lock_acquires"]),
            "lock_wait_avg_ms": _round_ms(st["lock_wait_total_ms"] / acquires) if st["lock_acquires"] else 0.0,
            "lock_wait_max_ms": _round_ms(st["lock_wait_max_ms"]),
            "lock_contention_count": int(st["lock_contention_count"]),
            "lock_hold_max_ms": _round_ms(st["lock_hold_max_ms"]),
            "private_opens": int(st["private_opens"]),
            "private_open_avg_ms": _round_ms(st["private_open_total_ms"] / priv_opens) if st["private_opens"] else 0.0,
            "private_open_max_ms": _round_ms(st["private_open_max_ms"]),
            "private_reuses": int(st["private_reuses"]),
            "workspace_opens": int(st["workspace_opens"]),
            "workspace_open_avg_ms": _round_ms(st["workspace_open_total_ms"] / ws_opens) if st["workspace_opens"] else 0.0,
            "workspace_open_max_ms": _round_ms(st["workspace_open_max_ms"]),
            "workspace_reuses": int(st["workspace_reuses"]),
        }

    # =========================================================================
    # 请求生命周期采集
    # =========================================================================

    def begin_request(
        self,
        method: str,
        path: str,
        query_keys: Sequence[str] = (),
        *,
        is_api: bool = True,
    ) -> Tuple[_ActiveRequestTrace, contextvars.Token]:
        with self._lock:
            self.in_flight_current += 1
            if self.in_flight_current > self.in_flight_peak:
                self.in_flight_peak = self.in_flight_current
            conc = self.in_flight_current

        trace = _ActiveRequestTrace(
            trace_id=uuid.uuid4().hex[:12],
            method=str(method or "GET").upper(),
            path=str(path or "/"),
            query_keys=[str(k) for k in query_keys if str(k)],
            is_api=bool(is_api),
            concurrent_at_start=conc,
            active_threads=threading.active_count(),
        )
        tok = _CURRENT_TRACE.set(trace)
        return trace, tok

    def finish_request(
        self,
        trace: _ActiveRequestTrace,
        ctx_token: contextvars.Token,
        *,
        status_code: int,
        auth_gate1_ms: float = 0.0,
        handler_ms: float = 0.0,
        error_redact_ms: float = 0.0,
        response_bytes: Optional[int] = None,
        error_text: str = "",
    ) -> Dict[str, Any]:
        total_ms = (time.perf_counter() - trace.start_mono) * 1000.0
        try:
            _CURRENT_TRACE.reset(ctx_token)
        except Exception:
            pass

        with trace._lock:
            spans_copy = list(trace.spans)
            cat_totals = {k: dict(v) for k, v in trace.category_totals.items()}
            spans_trunc = trace.spans_truncated

        is_fast_poll = (
            status_code == 200
            and total_ms < 200.0
            and (
                trace.path.startswith("/api/platform/import-progress/")
                or trace.path.startswith("/api/platform/tasks/")
                or trace.path == "/api/platform/diagnostics/ping"
            )
            and not trace.path.endswith("/stream")
        )

        record: Dict[str, Any] = {
            "trace_id": trace.trace_id,
            "started_at": trace.started_at,
            "method": trace.method,
            "path": trace.path,
            "query_keys": trace.query_keys,
            "is_api": trace.is_api,
            "status_code": int(status_code),
            "total_ms": _round_ms(total_ms),
            "auth_gate1_ms": _round_ms(auth_gate1_ms),
            "handler_ms": _round_ms(handler_ms),
            "error_redact_ms": _round_ms(error_redact_ms),
            "concurrent_at_start": trace.concurrent_at_start,
            "active_threads": trace.active_threads,
            "response_bytes": int(response_bytes) if response_bytes is not None else None,
            "category_totals": cat_totals,
            "spans": spans_copy,
            "spans_truncated": spans_trunc,
        }
        if error_text:
            record["error_text"] = error_text[:500]

        with self._lock:
            if self.in_flight_current > 0:
                self.in_flight_current -= 1
            self.total_requests += 1
            if status_code >= 400:
                self.error_requests += 1
            if is_fast_poll:
                self.suppressed_fast_polls += 1
            else:
                self._traces.append(record)
                if total_ms >= self.slow_threshold_ms or status_code >= 400:
                    self._slow_traces.append(record)
        return record

    def update_config(self, *, slow_threshold_ms: float) -> float:
        val = max(50.0, min(10000.0, float(slow_threshold_ms)))
        with self._lock:
            self.slow_threshold_ms = val
        return val

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self._slow_traces.clear()
            self.total_requests = 0
            self.error_requests = 0
            self.suppressed_fast_polls = 0
            self.in_flight_peak = self.in_flight_current
            self._last_benchmark = None
            for k in list(self._sqlite_stats.keys()):
                self._sqlite_stats[k] = 0.0 if k.endswith("_ms") else 0

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        tid = str(trace_id or "").strip()
        if not tid:
            return None
        found: Optional[Dict[str, Any]] = None
        with self._lock:
            for item in reversed(self._traces):
                if item.get("trace_id") == tid:
                    found = dict(item)
                    break
            if found is None:
                for item in reversed(self._slow_traces):
                    if item.get("trace_id") == tid:
                        found = dict(item)
                        break
        if found is None:
            return None
        return self.sanitize_payload(found)

    # =========================================================================
    # 脱敏过滤引擎 (Zero Path & Zero Secret Exposure)
    # =========================================================================

    def _collect_secret_literals(self) -> List[str]:
        secrets: List[str] = []
        store = getattr(self.ctx, "store", None)
        if store is None:
            return secrets
        try:
            for p in store.list_llm_providers():
                key = str(getattr(p, "api_key", "") or "").strip()
                if len(key) >= 4:
                    secrets.append(key)
        except Exception:
            pass
        try:
            if hasattr(store, "list_connection_profiles"):
                for cp in store.list_connection_profiles():
                    pwd = str(getattr(cp, "password", "") or "").strip()
                    if len(pwd) >= 4:
                        secrets.append(pwd)
                    for v in (getattr(cp, "extra_props", None) or {}).values():
                        sv = str(v or "").strip()
                        if len(sv) >= 6:
                            secrets.append(sv)
        except Exception:
            pass
        try:
            act_code = str(store.get_meta("license_activation_code", "") or "").strip()
            if len(act_code) >= 8:
                secrets.append(act_code)
        except Exception:
            pass
        return sorted(set(secrets), key=len, reverse=True)

    def _sanitize_str(self, text: str, secret_literals: Sequence[str]) -> str:
        msg = str(text)
        if not msg:
            return msg
        msg = redact_private_storage_paths(msg, ctx=self.ctx)
        msg = _RE_ACTIVATION_CODE.sub("DOG1-<redacted>", msg)
        msg = _RE_BEARER.sub("Bearer <redacted>", msg)
        msg = _RE_SK_KEY.sub("<redacted_key>", msg)
        msg = _RE_GOOGLE_KEY.sub("<redacted_key>", msg)
        msg = _RE_KV_SECRET.sub(r"\1\2<redacted>", msg)
        for sec in secret_literals:
            if sec and sec in msg:
                msg = msg.replace(sec, "<redacted_secret>")
        return msg

    def sanitize_payload(self, obj: Any) -> Any:
        secret_literals = self._collect_secret_literals()

        def _walk(node: Any) -> Any:
            if isinstance(node, str):
                return self._sanitize_str(node, secret_literals)
            if isinstance(node, dict):
                return {
                    (self._sanitize_str(k, secret_literals) if isinstance(k, str) else k): _walk(v)
                    for k, v in node.items()
                }
            if isinstance(node, (list, tuple)):
                return [_walk(x) for x in node]
            return node

        return _walk(obj)

    # =========================================================================
    # 脱敏运行环境快照
    # =========================================================================

    def collect_environment_snapshot(self) -> Dict[str, Any]:
        store = getattr(self.ctx, "store", None)
        data_dir = str(getattr(self.ctx, "data_dir", "") or "")
        drive_type, is_unc = _detect_storage_drive_info(data_dir)
        env_home_set = bool((os.environ.get("DATAOG_HOME") or "").strip())

        def _file_size(p: Optional[str]) -> int:
            if not p:
                return 0
            try:
                return int(os.path.getsize(p)) if os.path.isfile(p) else 0
            except OSError:
                return 0

        def _journal_mode(p: Optional[str]) -> str:
            if not p or not os.path.isfile(p):
                return "NONE"
            try:
                with contextlib.closing(sqlite3.connect(p, timeout=2.0)) as conn:
                    row = conn.execute("PRAGMA journal_mode;").fetchone()
                    return str(row[0]).upper() if row else "UNKNOWN"
            except Exception:
                return "UNKNOWN"

        priv_db = getattr(store, "private_db_path", "") if store else ""
        ws_db = getattr(store, "workspace_db_path", "") if store else ""

        table_counts: Dict[str, int] = {}
        if store is not None:
            try:
                with store._lock:
                    with store._get_workspace_conn() as wconn:
                        for tbl in (
                            "connection_topology",
                            "meta_tables",
                            "meta_columns",
                            "standard_dictionary",
                            "field_standard_bindings",
                            "migration_items",
                        ):
                            try:
                                r = wconn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                                table_counts[tbl] = int(r[0]) if r else 0
                            except Exception:
                                table_counts[tbl] = 0
                    with store._get_private_conn() as pconn:
                        for tbl in ("execution_logs", "kb_documents", "llm_providers"):
                            try:
                                r = pconn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                                table_counts[tbl] = int(r[0]) if r else 0
                            except Exception:
                                table_counts[tbl] = 0
            except Exception:
                pass

        # 代理配置检查（仅保留脱敏 scheme://host:port 与是否豁免环回）
        env_proxies: Dict[str, str] = {}
        for var_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            val = (os.environ.get(var_name) or "").strip()
            if val:
                env_proxies[var_name.lower()] = _sanitize_proxy_url(val)
        no_proxy_raw = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
        no_proxy_tokens = [
            t.strip().lower() for t in re.split(r"[,;\s]+", no_proxy_raw) if t.strip()
        ]
        try:
            raw_sys_proxies = urllib.request.getproxies() or {}
        except Exception:
            raw_sys_proxies = {}
        sys_proxies = {
            str(k).lower(): _sanitize_proxy_url(v)
            for k, v in raw_sys_proxies.items()
            if str(k).lower() != "no" and v
        }
        sys_no_raw = str(raw_sys_proxies.get("no") or "")
        all_no_tokens = no_proxy_tokens + [
            t.strip().lower() for t in re.split(r"[,;\s]+", sys_no_raw) if t.strip()
        ]
        has_any_proxy = bool(env_proxies or sys_proxies)
        covers_loopback = any(
            tok in ("127.0.0.1", "localhost", "*", "127.0.0.0/8", "::1", "<local>")
            or tok.startswith("127.0.0.1:")
            or tok.startswith("localhost:")
            for tok in all_no_tokens
        )

        lic_hub = getattr(self.ctx, "license_hub", None)
        drv_mgr = getattr(self.ctx, "driver_manager", None)
        conn_hub = getattr(self.ctx, "connection_hub", None)
        meta_hub = getattr(self.ctx, "metadata_hub", None)

        java_status = {}
        if drv_mgr is not None:
            try:
                raw_jre = drv_mgr.get_java_runtime_status()
                java_status = {
                    "available": bool(raw_jre.get("available")),
                    "jpype_installed": bool(raw_jre.get("jpype_installed")),
                    "jvm_detected": bool(raw_jre.get("jvm_detected")),
                }
            except Exception:
                pass

        warm_pool_stats = conn_hub.get_pool_stats() if (conn_hub and hasattr(conn_hub, "get_pool_stats")) else {}
        neg_cache_size = meta_hub.get_cache_stats() if (meta_hub and hasattr(meta_hub, "get_cache_stats")) else {}

        return self.sanitize_payload(
            {
                "os": {
                    "os_name": os.name,
                    "platform": sys.platform,
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                    "python_version": platform.python_version(),
                    "is_frozen": bool(getattr(sys, "frozen", False)),
                    "cpu_count": os.cpu_count() or 1,
                    "pid": os.getpid(),
                    "uptime_sec": round(time.monotonic() - self.started_monotonic, 1),
                    "active_threads": threading.active_count(),
                },
                "storage": {
                    "storage_mode": "SEPARATED" if getattr(store, "is_split_storage", False) else "COMPAT",
                    "dataog_home_overridden": env_home_set,
                    "storage_drive_type": drive_type,
                    "is_unc_path": is_unc,
                    "private_db_size_bytes": _file_size(priv_db),
                    "private_wal_size_bytes": _file_size(f"{priv_db}-wal" if priv_db else ""),
                    "workspace_db_size_bytes": _file_size(ws_db),
                    "workspace_wal_size_bytes": _file_size(f"{ws_db}-wal" if ws_db else ""),
                    "private_journal_mode": _journal_mode(priv_db),
                    "workspace_journal_mode": _journal_mode(ws_db),
                    "table_counts": table_counts,
                },
                "network_proxy": {
                    "has_proxy_configured": has_any_proxy,
                    "env_proxies": env_proxies,
                    "system_proxies": sys_proxies,
                    "no_proxy_configured": bool(all_no_tokens),
                    "no_proxy_covers_loopback": bool(covers_loopback) if has_any_proxy else True,
                },
                "components": {
                    "native_license_shield_loaded": bool(
                        getattr(getattr(lic_hub, "_shield", None), "is_loaded", False)
                    ),
                    "java_runtime": java_status,
                    "warm_pool": warm_pool_stats,
                    "metadata_cache": neg_cache_size,
                },
            }
        )

    # =========================================================================
    # 一键全栈基准压测 (Benchmark)
    # =========================================================================

    def _bench_tmp_dir(self) -> str:
        ws_hub = getattr(self.ctx, "workspace_hub", None)
        cand = str(getattr(ws_hub, "tmp_dir", "") or "").strip()
        if not cand:
            data_dir = str(getattr(self.ctx, "data_dir", "") or "")
            if data_dir:
                cand = os.path.join(data_dir, "rt_tmp")
        if not cand:
            cand = resolve_private_storage_layout(create_dirs=True).tmp_dir
        os.makedirs(cand, exist_ok=True)
        return cand

    def _run_sync_benchmarks(
        self,
        *,
        include_connections: bool = False,
        include_llm: bool = False,
    ) -> Dict[str, Any]:
        # 1. CPU Ed25519 算力基准
        from core.platform.license_hub import ed25519_verify

        cpu_samples: List[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            ed25519_verify(b"", _DUMMY_ED25519_SIG, _DUMMY_ED25519_PK)
            cpu_samples.append((time.perf_counter() - t0) * 1000.0)
        cpu_ed25519 = {
            "iterations": len(cpu_samples),
            "avg_ms": _round_ms(sum(cpu_samples) / len(cpu_samples)),
            "max_ms": _round_ms(max(cpu_samples)),
        }

        # 2. License 授权与本机设备/网卡探针基准（重点检测企业内网 GetAdaptersInfo/DNS 卡顿）
        lic_hub = getattr(self.ctx, "license_hub", None)
        license_probe: Dict[str, Any] = {}
        if lic_hub is not None:
            t0 = time.perf_counter()
            dev_cached = lic_hub.get_device_info(force_refresh=False)
            cached_ms = (time.perf_counter() - t0) * 1000.0

            t1 = time.perf_counter()
            dev_fresh = lic_hub.get_device_info(force_refresh=True)
            fresh_ms = (time.perf_counter() - t1) * 1000.0

            t2 = time.perf_counter()
            auth_st = lic_hub.get_auth_status(force_refresh_ip=False)
            auth_ms = (time.perf_counter() - t2) * 1000.0

            license_probe = {
                "device_probe_cached_ms": _round_ms(cached_ms),
                "device_probe_fresh_ms": _round_ms(fresh_ms),
                "auth_status_ms": _round_ms(auth_ms),
                "native_shield_loaded": bool(auth_st.get("native_shield")),
                "ip_count": len(dev_fresh.get("all_ips") or dev_cached.get("all_ips") or []),
            }

        # 3. 主机名与本地 DNS/addrinfo 解析延迟
        t_hn = time.perf_counter()
        hostname = ""
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = ""
        hn_ms = (time.perf_counter() - t_hn) * 1000.0

        t_gb = time.perf_counter()
        hn_resolved_count = 0
        hn_err = ""
        if hostname:
            try:
                _, _, ip_list = socket.gethostbyname_ex(hostname)
                hn_resolved_count = len(ip_list or [])
            except Exception as exc:
                hn_err = str(exc)[:120]
        gb_ms = (time.perf_counter() - t_gb) * 1000.0

        t_lh = time.perf_counter()
        lh_addrs = 0
        try:
            lh_addrs = len(socket.getaddrinfo("localhost", 18080))
        except Exception:
            pass
        lh_ms = (time.perf_counter() - t_lh) * 1000.0

        t_lb = time.perf_counter()
        try:
            socket.getaddrinfo("127.0.0.1", 18080)
        except Exception:
            pass
        lb_ms = (time.perf_counter() - t_lb) * 1000.0

        dns_and_hostname = {
            "hostname_ms": _round_ms(hn_ms),
            "gethostbyname_ms": _round_ms(gb_ms),
            "gethostbyname_ips": hn_resolved_count,
            "gethostbyname_error": hn_err,
            "getaddrinfo_localhost_ms": _round_ms(lh_ms),
            "localhost_addr_count": lh_addrs,
            "getaddrinfo_loopback_ms": _round_ms(lb_ms),
        }

        # 4. 磁盘 I/O 与杀毒/EDR fsync 实时扫描耗时基准（全部临时文件于 finally 清除）
        def _remove_file_retry(target_file: str) -> None:
            for attempt in range(3):
                try:
                    if os.path.exists(target_file):
                        os.remove(target_file)
                    return
                except OSError:
                    if attempt < 2:
                        time.sleep(0.02)

        tmp_dir = self._bench_tmp_dir()
        small_fsync_samples: List[float] = []
        small_read_samples: List[float] = []
        block_4k = b"DOG_BENCH_4K_" + (b"X" * (4096 - 13))
        for idx in range(5):
            fpath = os.path.join(tmp_dir, f".diag_io_{uuid.uuid4().hex[:10]}_{idx}.tmp")
            try:
                t_w = time.perf_counter()
                with open(fpath, "wb") as fh:
                    fh.write(block_4k)
                    fh.flush()
                    os.fsync(fh.fileno())
                small_fsync_samples.append((time.perf_counter() - t_w) * 1000.0)

                t_r = time.perf_counter()
                with open(fpath, "rb") as fh:
                    _ = fh.read()
                small_read_samples.append((time.perf_counter() - t_r) * 1000.0)
            finally:
                _remove_file_retry(fpath)

        big_path = os.path.join(tmp_dir, f".diag_io_1mb_{uuid.uuid4().hex[:10]}.tmp")
        big_payload = (b"DATAOG_1MB_BENCHMARK_BLOCK_" * 40000)[: 1024 * 1024]
        big_write_ms = 0.0
        big_read_ms = 0.0
        try:
            t_bw = time.perf_counter()
            with open(big_path, "wb") as fh:
                fh.write(big_payload)
                fh.flush()
                os.fsync(fh.fileno())
            big_write_ms = (time.perf_counter() - t_bw) * 1000.0

            t_br = time.perf_counter()
            with open(big_path, "rb") as fh:
                _ = fh.read()
            big_read_ms = (time.perf_counter() - t_br) * 1000.0
        finally:
            _remove_file_retry(big_path)

        disk_io = {
            "fsync_4k_iterations": len(small_fsync_samples),
            "fsync_4k_avg_ms": _round_ms(sum(small_fsync_samples) / max(1, len(small_fsync_samples))),
            "fsync_4k_p95_ms": _percentile(small_fsync_samples, 95.0),
            "fsync_4k_max_ms": _round_ms(max(small_fsync_samples, default=0.0)),
            "read_4k_avg_ms": _round_ms(sum(small_read_samples) / max(1, len(small_read_samples))),
            "write_1mb_ms": _round_ms(big_write_ms),
            "read_1mb_ms": _round_ms(big_read_ms),
            "write_1mb_mb_s": round(1000.0 / max(0.05, big_write_ms), 2),
            "read_1mb_mb_s": round(1000.0 / max(0.05, big_read_ms), 2),
        }

        # 5. SQLite 连接开闭与 WAL 事务提交延迟基准
        store = getattr(self.ctx, "store", None)
        sqlite_io: Dict[str, Any] = {}
        if store is not None:
            priv_samples: List[float] = []
            ws_samples: List[float] = []
            meta_samples: List[float] = []
            for _ in range(10):
                t0 = time.perf_counter()
                with store._get_private_conn() as pconn:
                    pconn.execute("SELECT 1").fetchone()
                priv_samples.append((time.perf_counter() - t0) * 1000.0)

                t1 = time.perf_counter()
                with store._get_workspace_conn() as wconn:
                    wconn.execute("SELECT 1").fetchone()
                ws_samples.append((time.perf_counter() - t1) * 1000.0)

                t2 = time.perf_counter()
                store.get_meta_batch(("license_activation_code", "license_locked"))
                meta_samples.append((time.perf_counter() - t2) * 1000.0)

            wal_samples: List[float] = []
            wal_db = os.path.join(tmp_dir, f".diag_wal_{uuid.uuid4().hex[:10]}.db")
            try:
                with contextlib.closing(sqlite3.connect(wal_db, timeout=10.0)) as wconn:
                    wconn.execute("PRAGMA journal_mode = WAL;")
                    wconn.execute("PRAGMA synchronous = NORMAL;")
                    wconn.execute("CREATE TABLE IF NOT EXISTS bench_kv (k INTEGER PRIMARY KEY, v TEXT)")
                    wconn.commit()
                    for i in range(20):
                        tw = time.perf_counter()
                        wconn.execute(
                            "INSERT OR REPLACE INTO bench_kv (k, v) VALUES (?, ?)",
                            (i, f"bench_{i}"),
                        )
                        wconn.commit()
                        wal_samples.append((time.perf_counter() - tw) * 1000.0)
                    try:
                        wconn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                        wconn.execute("PRAGMA journal_mode = DELETE;")
                    except Exception:
                        pass
            finally:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    _remove_file_retry(wal_db + suffix)

            sqlite_io = {
                "private_open_close_avg_ms": _round_ms(sum(priv_samples) / len(priv_samples)),
                "private_open_close_max_ms": _round_ms(max(priv_samples)),
                "workspace_open_close_avg_ms": _round_ms(sum(ws_samples) / len(ws_samples)),
                "workspace_open_close_max_ms": _round_ms(max(ws_samples)),
                "meta_batch_avg_ms": _round_ms(sum(meta_samples) / len(meta_samples)),
                "meta_batch_max_ms": _round_ms(max(meta_samples)),
                "temp_wal_write_commit_avg_ms": _round_ms(sum(wal_samples) / max(1, len(wal_samples))),
                "temp_wal_write_commit_max_ms": _round_ms(max(wal_samples, default=0.0)),
            }

        # 6. Bootstrap 全流程子阶段独立剖面测试
        bootstrap_profile: Dict[str, Any] = {}
        try:
            boot_trace = _ActiveRequestTrace(
                trace_id="bench_boot",
                method="GET",
                path="/api/platform/bootstrap",
                query_keys=[],
                is_api=True,
                concurrent_at_start=1,
                active_threads=threading.active_count(),
            )
            tok = _CURRENT_TRACE.set(boot_trace)
            t_boot0 = time.perf_counter()
            try:
                boot_payload = self.ctx.build_platform_bootstrap()
            finally:
                _CURRENT_TRACE.reset(tok)
            boot_total_ms = (time.perf_counter() - t_boot0) * 1000.0
            try:
                boot_json_bytes = len(
                    json.dumps(boot_payload, ensure_ascii=False).encode("utf-8")
                )
            except Exception:
                boot_json_bytes = 0
            subphases = [
                {
                    "name": s["name"],
                    "duration_ms": _round_ms(s["duration_ms"]),
                }
                for s in boot_trace.spans
                if s.get("category") == "bootstrap"
            ]
            bootstrap_profile = {
                "total_ms": _round_ms(boot_total_ms),
                "json_bytes": boot_json_bytes,
                "category_totals": boot_trace.category_totals,
                "subphases": sorted(subphases, key=lambda x: x["duration_ms"], reverse=True),
            }
        except Exception as exc:
            bootstrap_profile = {"error": str(exc)}

        result: Dict[str, Any] = {
            "cpu_ed25519": cpu_ed25519,
            "license_probe": license_probe,
            "dns_and_hostname": dns_and_hostname,
            "disk_io": disk_io,
            "sqlite_io": sqlite_io,
            "bootstrap_profile": bootstrap_profile,
        }

        if include_connections:
            result["connections_probe"] = self._probe_configured_connections()
        if include_llm:
            result["llm_probe"] = self._probe_configured_llms()
        return result

    def _probe_configured_connections(self) -> List[Dict[str, Any]]:
        conn_hub = getattr(self.ctx, "connection_hub", None)
        if conn_hub is None:
            return []
        store = getattr(self.ctx, "store", None)
        profiles = store.list_connection_profiles() if store is not None else []
        out: List[Dict[str, Any]] = []
        for prof in profiles[:10]:
            item: Dict[str, Any] = {
                "connection_id": prof.connection_id,
                "name": prof.name,
                "db_type": prof.db_type,
                "driver_track": prof.driver_track,
                "host": prof.host if prof.db_type != "SQLITE" else "<local_sqlite>",
                "port": int(prof.port or 0),
                "dns_ms": None,
                "tcp_connect_ms": None,
                "handshake_ms": None,
                "ok": False,
                "error": "",
            }
            if prof.db_type != "SQLITE" and prof.host and int(prof.port or 0) > 0:
                t_dns = time.perf_counter()
                try:
                    socket.getaddrinfo(prof.host.strip(), int(prof.port), type=socket.SOCK_STREAM)
                    item["dns_ms"] = _round_ms((time.perf_counter() - t_dns) * 1000.0)
                except Exception as exc:
                    item["dns_ms"] = _round_ms((time.perf_counter() - t_dns) * 1000.0)
                    item["error"] = str(exc)[:200]
                    out.append(item)
                    continue

                t_tcp = time.perf_counter()
                try:
                    with socket.create_connection((prof.host.strip(), int(prof.port)), timeout=5.0):
                        pass
                    item["tcp_connect_ms"] = _round_ms((time.perf_counter() - t_tcp) * 1000.0)
                except Exception as exc:
                    item["tcp_connect_ms"] = _round_ms((time.perf_counter() - t_tcp) * 1000.0)
                    item["error"] = str(exc)[:200]
                    out.append(item)
                    continue

            t_hs = time.perf_counter()
            try:
                res = conn_hub.test_profile(prof)
                item["handshake_ms"] = _round_ms(
                    res.get("elapsed_ms", (time.perf_counter() - t_hs) * 1000.0)
                )
                item["ok"] = bool(res.get("ok", True))
                item["server_version"] = str(res.get("server_version") or "")[:80]
            except Exception as exc:
                item["handshake_ms"] = _round_ms((time.perf_counter() - t_hs) * 1000.0)
                item["error"] = str(exc)[:220]
            out.append(item)
        return out

    def _probe_configured_llms(self) -> List[Dict[str, Any]]:
        llm_hub = getattr(self.ctx, "llm_hub", None)
        store = getattr(self.ctx, "store", None)
        if llm_hub is None or store is None:
            return []
        from core.platform.llm_hub import build_llm_ssl_context

        out: List[Dict[str, Any]] = []
        for prof in store.list_llm_providers():
            is_local = any(h in (prof.base_url or "") for h in ("127.0.0.1", "localhost"))
            if not prof.enabled or (not prof.api_key and not is_local):
                continue
            parsed = urlparse(prof.base_url or "")
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            entry: Dict[str, Any] = {
                "provider_id": prof.provider_id,
                "name": prof.name,
                "host": host,
                "port": int(port),
                "scheme": parsed.scheme,
                "dns_ms": None,
                "tcp_connect_ms": None,
                "ssl_ctx_ms": None,
                "models_http_ms": None,
                "ok": False,
                "error": "",
            }
            if host:
                t_dns = time.perf_counter()
                try:
                    socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
                    entry["dns_ms"] = _round_ms((time.perf_counter() - t_dns) * 1000.0)
                except Exception as exc:
                    entry["dns_ms"] = _round_ms((time.perf_counter() - t_dns) * 1000.0)
                    entry["error"] = str(exc)[:200]
                    out.append(entry)
                    continue

                t_tcp = time.perf_counter()
                try:
                    with socket.create_connection((host, int(port)), timeout=5.0):
                        pass
                    entry["tcp_connect_ms"] = _round_ms((time.perf_counter() - t_tcp) * 1000.0)
                except Exception as exc:
                    entry["tcp_connect_ms"] = _round_ms((time.perf_counter() - t_tcp) * 1000.0)
                    entry["error"] = str(exc)[:200]
                    out.append(entry)
                    continue

            if parsed.scheme == "https":
                t_ssl = time.perf_counter()
                try:
                    build_llm_ssl_context()
                except Exception:
                    pass
                entry["ssl_ctx_ms"] = _round_ms((time.perf_counter() - t_ssl) * 1000.0)

            t_http = time.perf_counter()
            try:
                res = llm_hub.fetch_models({"provider_id": prof.provider_id})
                entry["models_http_ms"] = _round_ms(
                    res.get("elapsed_ms", (time.perf_counter() - t_http) * 1000.0)
                )
                entry["model_count"] = len(res.get("models") or [])
                entry["ok"] = True
            except Exception as exc:
                entry["models_http_ms"] = _round_ms((time.perf_counter() - t_http) * 1000.0)
                entry["error"] = str(exc)[:220]
            out.append(entry)
        return out

    async def run_benchmark_async(
        self,
        *,
        include_connections: bool = False,
        include_llm: bool = False,
    ) -> Dict[str, Any]:
        # 1. 事件循环唤醒抖动测试 (10 x 10ms)
        loop_lags: List[float] = []
        for _ in range(10):
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lag_ms = max(0.0, (time.perf_counter() - t0 - 0.01) * 1000.0)
            loop_lags.append(lag_ms)

        # 2. AnyIO 线程池调度排队测试 (5 次)
        tp_delays: List[float] = []
        for _ in range(5):
            t0 = time.perf_counter()
            worker_t = await run_in_threadpool(time.perf_counter)
            tp_delays.append(max(0.0, (worker_t - t0) * 1000.0))

        sync_part = await run_in_threadpool(
            lambda: self._run_sync_benchmarks(
                include_connections=include_connections,
                include_llm=include_llm,
            )
        )

        bench: Dict[str, Any] = {
            "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "include_connections": bool(include_connections),
            "include_llm": bool(include_llm),
            "event_loop_lag": {
                "iterations": len(loop_lags),
                "avg_lag_ms": _round_ms(sum(loop_lags) / len(loop_lags)),
                "p95_lag_ms": _percentile(loop_lags, 95.0),
                "max_lag_ms": _round_ms(max(loop_lags)),
            },
            "threadpool_dispatch": {
                "iterations": len(tp_delays),
                "avg_ms": _round_ms(sum(tp_delays) / len(tp_delays)),
                "max_ms": _round_ms(max(tp_delays)),
            },
            **sync_part,
        }
        sanitized = self.sanitize_payload(bench)
        with self._lock:
            self._last_benchmark = sanitized
        return sanitized

    def run_benchmark(
        self,
        *,
        include_connections: bool = False,
        include_llm: bool = False,
    ) -> Dict[str, Any]:
        """同步便捷入口（主要供单测或无事件循环上下文直接调用）."""
        sync_part = self._run_sync_benchmarks(
            include_connections=include_connections,
            include_llm=include_llm,
        )
        bench = {
            "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "include_connections": bool(include_connections),
            "include_llm": bool(include_llm),
            "event_loop_lag": {"iterations": 0, "avg_lag_ms": 0.0, "p95_lag_ms": 0.0, "max_lag_ms": 0.0},
            "threadpool_dispatch": {"iterations": 0, "avg_ms": 0.0, "max_ms": 0.0},
            **sync_part,
        }
        sanitized = self.sanitize_payload(bench)
        with self._lock:
            self._last_benchmark = sanitized
        return sanitized

    # =========================================================================
    # 自动瓶颈研判引擎
    # =========================================================================

    def compute_auto_findings(
        self,
        *,
        traces: Sequence[Dict[str, Any]],
        environment: Dict[str, Any],
        sqlite_stats: Dict[str, Any],
        benchmark: Optional[Dict[str, Any]] = None,
        traces_merged: Optional[Sequence[Dict[str, Any]]] = None,
        client_snapshot: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        findings: List[Dict[str, str]] = []

        # 1. 客户端 <-> 本地环回代理/网络差值
        gap_values: List[float] = []
        for tm in traces_merged or ():
            g = tm.get("network_gap_ms")
            if isinstance(g, (int, float)):
                gap_values.append(float(g))
        if not gap_values and isinstance(client_snapshot, dict):
            for cr in client_snapshot.get("requests") or ():
                g = cr.get("network_gap_ms")
                if isinstance(g, (int, float)):
                    gap_values.append(float(g))
        loopback_bench = (
            client_snapshot.get("last_loopback_benchmark")
            if isinstance(client_snapshot, dict)
            else None
        )
        lb_gap_p95 = float((loopback_bench or {}).get("seq_network_gap_p95_ms") or 0.0)
        gap_p95 = max(_percentile(gap_values, 95.0) if gap_values else 0.0, lb_gap_p95)
        if gap_p95 > 100.0:
            findings.append(
                {
                    "code": "CLIENT_LOOPBACK_OVERHEAD",
                    "severity": "critical" if gap_p95 > 500.0 else "warn",
                    "title": "浏览器到本地 127.0.0.1 环回请求存在显著额外耗时",
                    "evidence": f"客户端与服务端耗时差值 (network_gap_ms) P95 达 {gap_p95:.1f} ms（服务端处理很快，但浏览器等待时间极长）。",
                    "suggestion": "重点排查企业代理/PAC 脚本未豁免 127.0.0.1/localhost、安全软件本地 HTTP 流量扫描，或浏览器并发连接数受限。",
                }
            )

        # 2. 代理未配置环回豁免
        proxy_info = environment.get("network_proxy") or {}
        if proxy_info.get("has_proxy_configured") and not proxy_info.get("no_proxy_covers_loopback", True):
            findings.append(
                {
                    "code": "PROXY_NO_BYPASS_LOOPBACK",
                    "severity": "warn",
                    "title": "系统/环境变量配置了代理且未显式豁免 127.0.0.1 / localhost",
                    "evidence": "检测到 HTTP/HTTPS/系统代理配置，但 NO_PROXY 列表中未包含 127.0.0.1 或 localhost。",
                    "suggestion": "在系统代理例外列表及 NO_PROXY 中添加 127.0.0.1,localhost，防止本地流量绕行企业网关或等待 WPAD/PAC 超时。",
                }
            )

        # 3. Gate 1 鉴权 / 设备探针卡顿
        auth_vals = [float(t.get("auth_gate1_ms") or 0.0) for t in traces if t.get("auth_gate1_ms") is not None]
        auth_p95 = _percentile(auth_vals, 95.0) if auth_vals else 0.0
        dev_fresh_ms = float(((benchmark or {}).get("license_probe") or {}).get("device_probe_fresh_ms") or 0.0)
        dns_gb_ms = float(((benchmark or {}).get("dns_and_hostname") or {}).get("gethostbyname_ms") or 0.0)
        dns_lh_ms = float(((benchmark or {}).get("dns_and_hostname") or {}).get("getaddrinfo_localhost_ms") or 0.0)
        if auth_p95 > 50.0 or dev_fresh_ms > 200.0 or dns_gb_ms > 200.0 or dns_lh_ms > 200.0:
            findings.append(
                {
                    "code": "AUTH_GATE1_SLOW",
                    "severity": "critical" if max(auth_p95, dev_fresh_ms, dns_gb_ms) > 500.0 else "warn",
                    "title": "授权门禁 (Gate 1) 或本机网卡/主机名 DNS 探测阻塞",
                    "evidence": (
                        f"Gate 1 鉴权 P95={auth_p95:.1f} ms，"
                        f"设备指纹刷新={dev_fresh_ms:.1f} ms，"
                        f"gethostbyname={dns_gb_ms:.1f} ms，"
                        f"localhost 解析={dns_lh_ms:.1f} ms。"
                    ),
                    "suggestion": "常见于内网多虚拟网卡/域控 DNS 反查或设备指纹/时钟水印同步在事件循环触发；可考虑将 Gate 1 移出阻塞路径并延长设备探针缓存/异步刷新。",
                }
            )

        # 4. 磁盘 fsync / EDR 扫描 / 远端网络盘瓶颈
        storage_info = environment.get("storage") or {}
        drive_type = str(storage_info.get("storage_drive_type") or "UNKNOWN")
        is_unc = bool(storage_info.get("is_unc_path"))
        fsync_avg = float(((benchmark or {}).get("disk_io") or {}).get("fsync_4k_avg_ms") or 0.0)
        bench_sq_open = float(
            ((benchmark or {}).get("sqlite_io") or {}).get("private_open_close_avg_ms") or 0.0
        )
        priv_opens = int(sqlite_stats.get("private_opens") or 0)
        priv_max = float(sqlite_stats.get("private_open_max_ms") or 0.0)
        sq_priv_open = max(bench_sq_open, priv_max if priv_opens > 2 else 0.0)
        if is_unc or drive_type == "REMOTE" or fsync_avg > 20.0 or sq_priv_open > 15.0:
            findings.append(
                {
                    "code": "DISK_OR_EDR_IO_SLOW",
                    "severity": "critical" if (is_unc or drive_type == "REMOTE" or fsync_avg > 80.0) else "warn",
                    "title": "本地存储 I/O、杀软/EDR 实时扫描或漫游网络盘延迟偏高",
                    "evidence": (
                        f"存储盘类型={drive_type} (UNC={is_unc})，"
                        f"4KB fsync 均值={fsync_avg:.1f} ms，"
                        f"SQLite 私有库开关峰值/均值={sq_priv_open:.1f} ms。"
                    ),
                    "suggestion": "VDI 漫游桌面或企业 EDR 对每次 sqlite3.connect/close 与 fsync 扫描极慢；建议保持持久化连接复用并减少短连接开关，或将 DATAOG_HOME 指向本地 SSD 磁盘。",
                }
            )

        # 5. SQLite 锁竞争
        lock_max = float(sqlite_stats.get("lock_wait_max_ms") or 0.0)
        if lock_max > 50.0:
            findings.append(
                {
                    "code": "SQLITE_LOCK_CONTENTION",
                    "severity": "critical" if lock_max > 300.0 else "warn",
                    "title": "存在 SQLite 状态库内部锁排队竞争",
                    "evidence": f"PlatformStateStore._lock 最大等待={lock_max:.1f} ms，竞争次数={sqlite_stats.get('lock_contention_count', 0)}。",
                    "suggestion": "高耗时写事务或长读连接持锁导致其他并发接口排队，需将读操作分离或缩短事务持锁窗口。",
                }
            )

        # 6. 事件循环 / 线程池阻塞
        loop_max = float(((benchmark or {}).get("event_loop_lag") or {}).get("max_lag_ms") or 0.0)
        tp_max = float(((benchmark or {}).get("threadpool_dispatch") or {}).get("max_ms") or 0.0)
        main_thread_slow_spans = [
            s
            for t in traces
            for s in (t.get("spans") or ())
            if s.get("is_main_thread") and float(s.get("duration_ms") or 0.0) > 100.0
        ]
        if loop_max > 50.0 or tp_max > 50.0 or main_thread_slow_spans:
            findings.append(
                {
                    "code": "EVENT_LOOP_OR_THREADPOOL_LAG",
                    "severity": "warn",
                    "title": "FastAPI 事件循环抖动或工作线程池调度排队",
                    "evidence": (
                        f"事件循环最大抖动={loop_max:.1f} ms，线程池最大排队={tp_max:.1f} ms，"
                        f"主线程耗时 >100ms Span 数={len(main_thread_slow_spans)}。"
                    ),
                    "suggestion": "避免在 async 路由或 HTTP 中间件直接执行同步文件/网络/数据库操作，确保全部重操作落入独立线程池。",
                }
            )

        # 7. Bootstrap 过重
        boot_ms = float(((benchmark or {}).get("bootstrap_profile") or {}).get("total_ms") or 0.0)
        if boot_ms > 500.0:
            subs = ((benchmark or {}).get("bootstrap_profile") or {}).get("subphases") or []
            top_sub = subs[0] if subs else {}
            boot_bytes = int(((benchmark or {}).get("bootstrap_profile") or {}).get("json_bytes") or 0)
            findings.append(
                {
                    "code": "BOOTSTRAP_HEAVY",
                    "severity": "warn",
                    "title": "工作台初始化 Bootstrap 数据聚合偏慢",
                    "evidence": (
                        f"Bootstrap 总耗时={boot_ms:.1f} ms，响应体积={boot_bytes} 字节，"
                        f"最慢子阶段={top_sub.get('name', '-')} ({top_sub.get('duration_ms', 0)} ms)。"
                    ),
                    "suggestion": "检查是否因大量历史元数据缓存或逐连接循环查询 SQLite 拖慢首屏加载，考虑按需懒加载目录树。",
                }
            )

        # 8. 远端 DB / 元数据 / 查询 / LLM 慢调用
        remote_slow = [
            (s.get("category"), s.get("name"), float(s.get("duration_ms") or 0.0))
            for t in traces
            for s in (t.get("spans") or ())
            if s.get("category") in ("db", "metadata", "query", "llm", "jvm")
            and float(s.get("duration_ms") or 0.0) > 1500.0
        ]
        if remote_slow:
            top_r = max(remote_slow, key=lambda x: x[2])
            findings.append(
                {
                    "code": "REMOTE_DB_OR_LLM_SLOW",
                    "severity": "warn",
                    "title": "远端数据库、JDBC/JVM 或 AI 网关响应较慢",
                    "evidence": f"检测到 {len(remote_slow)} 次 >1.5s 外部调用，最慢为 [{top_r[0]}.{top_r[1]}] {top_r[2]:.1f} ms。",
                    "suggestion": "结合对应慢请求 Trace 明细确认耗时落在 TCP 握手、系统目录扫描还是远端 SQL/LLM 返回阶段。",
                }
            )

        if not findings:
            findings.append(
                {
                    "code": "OK_BASELINE",
                    "severity": "info",
                    "title": "暂未发现明显后端环境异常瓶颈",
                    "evidence": f"当前共采样 {len(traces)} 条请求；如您仍感知页面卡顿，请先在慢操作后点击「运行全栈基准测试」并导出日志包供深入比对。",
                    "suggestion": "建议重点核对导出 JSON 中 traces_merged 的 network_gap_ms 与浏览器 slow_static_resources。",
                }
            )
        return findings

    # =========================================================================
    # 总览看板与完整诊断日志包导出
    # =========================================================================

    def _summarize_traces(self, traces: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        totals = [float(t.get("total_ms") or 0.0) for t in traces]
        auths = [float(t.get("auth_gate1_ms") or 0.0) for t in traces]
        handlers = [float(t.get("handler_ms") or 0.0) for t in traces]

        by_ep: Dict[str, List[float]] = {}
        for t in traces:
            ep = f"{t.get('method', 'GET')} {t.get('path', '/')}"
            by_ep.setdefault(ep, []).append(float(t.get("total_ms") or 0.0))

        top_eps = sorted(
            (
                {
                    "endpoint": ep,
                    "count": len(vals),
                    "avg_ms": _round_ms(sum(vals) / len(vals)),
                    "p95_ms": _percentile(vals, 95.0),
                    "max_ms": _round_ms(max(vals)),
                }
                for ep, vals in by_ep.items()
            ),
            key=lambda x: x["p95_ms"],
            reverse=True,
        )[:10]

        with self._lock:
            snap = {
                "total_requests": self.total_requests,
                "error_requests": self.error_requests,
                "suppressed_fast_polls": self.suppressed_fast_polls,
                "in_flight_current": self.in_flight_current,
                "in_flight_peak": self.in_flight_peak,
                "slow_threshold_ms": self.slow_threshold_ms,
                "recent_count": len(self._traces),
                "slow_count": len(self._slow_traces),
            }

        snap.update(
            {
                "server_p50_ms": _percentile(totals, 50.0),
                "server_p95_ms": _percentile(totals, 95.0),
                "server_max_ms": _round_ms(max(totals, default=0.0)),
                "auth_gate1_avg_ms": _round_ms(sum(auths) / len(auths)) if auths else 0.0,
                "auth_gate1_p95_ms": _percentile(auths, 95.0),
                "handler_p95_ms": _percentile(handlers, 95.0),
                "top_slow_endpoints": top_eps,
            }
        )
        return snap

    def get_overview(self) -> Dict[str, Any]:
        with self._lock:
            recent = list(self._traces)[-100:]
            slow = list(self._slow_traces)[-100:]
            bench = dict(self._last_benchmark) if self._last_benchmark else None

        recent_desc = list(reversed(recent))
        slow_desc = list(reversed(slow))
        env = self.collect_environment_snapshot()
        sql_stats = self.get_sqlite_stats()
        conn_hub = getattr(self.ctx, "connection_hub", None)
        pool_stats = conn_hub.get_pool_stats() if (conn_hub and hasattr(conn_hub, "get_pool_stats")) else {}
        summary = self._summarize_traces(recent)
        findings = self.compute_auto_findings(
            traces=recent,
            environment=env,
            sqlite_stats=sql_stats,
            benchmark=bench,
        )

        return self.sanitize_payload(
            {
                "summary": summary,
                "auto_findings": findings,
                "environment": env,
                "sqlite_stats": sql_stats,
                "pool_stats": pool_stats,
                "last_benchmark": bench,
                "recent_traces": recent_desc,
                "slow_traces": slow_desc,
            }
        )

    def _read_recent_service_log_lines(self, max_bytes: int = 128 * 1024, max_lines: int = 300) -> List[str]:
        data_dir = str(getattr(self.ctx, "data_dir", "") or "")
        candidates: List[str] = []
        if data_dir:
            candidates.append(os.path.join(data_dir, "rt_log", "rt_svc.log"))
        try:
            candidates.append(resolve_private_storage_layout(create_dirs=False).service_log_path)
        except Exception:
            pass
        for log_path in candidates:
            if not log_path or not os.path.isfile(log_path):
                continue
            try:
                size = os.path.getsize(log_path)
                with open(log_path, "rb") as fh:
                    if size > max_bytes:
                        fh.seek(size - max_bytes)
                    raw = fh.read(max_bytes)
                lines = raw.decode("utf-8", errors="replace").splitlines()
                return lines[-max_lines:]
            except Exception:
                continue
        return []

    def build_export_bundle(
        self,
        *,
        client_snapshot: Optional[Dict[str, Any]] = None,
        include_service_log: bool = True,
        include_execution_logs: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            recent = list(self._traces)
            slow = list(self._slow_traces)
            bench = dict(self._last_benchmark) if self._last_benchmark else None

        by_tid: Dict[str, Dict[str, Any]] = {
            str(t.get("trace_id") or ""): t for t in recent if t.get("trace_id")
        }
        for t in slow:
            tid = str(t.get("trace_id") or "")
            if tid and tid not in by_tid:
                by_tid[tid] = t

        merged: List[Dict[str, Any]] = []
        matched_tids: set[str] = set()
        if isinstance(client_snapshot, dict):
            for creq in client_snapshot.get("requests") or ():
                if not isinstance(creq, dict):
                    continue
                tid = str(creq.get("trace_id") or "").strip()
                srv = by_tid.get(tid) if tid else None
                if srv is not None:
                    matched_tids.add(tid)
                c_ms = creq.get("client_total_ms")
                s_ms = srv.get("total_ms") if srv else creq.get("server_total_ms")
                gap_ms = creq.get("network_gap_ms")
                if gap_ms is None and isinstance(c_ms, (int, float)) and isinstance(s_ms, (int, float)):
                    gap_ms = _round_ms(max(0.0, float(c_ms) - float(s_ms)))
                merged.append(
                    {
                        "trace_id": tid,
                        "ts_iso": creq.get("ts_iso") or (srv.get("started_at") if srv else ""),
                        "method": creq.get("method") or (srv.get("method") if srv else ""),
                        "path": creq.get("url_path") or (srv.get("path") if srv else ""),
                        "status_code": creq.get("status") if creq.get("status") is not None else (srv.get("status_code") if srv else 0),
                        "client_total_ms": c_ms,
                        "fetch_ms": creq.get("fetch_ms"),
                        "body_parse_ms": creq.get("body_parse_ms"),
                        "server_total_ms": s_ms,
                        "auth_gate1_ms": srv.get("auth_gate1_ms") if srv else creq.get("server_auth_ms"),
                        "handler_ms": srv.get("handler_ms") if srv else creq.get("server_handler_ms"),
                        "network_gap_ms": gap_ms,
                        "in_flight_at_start": creq.get("in_flight_at_start"),
                        "matched_server": srv is not None,
                        "top_spans": (srv.get("spans") or [])[:8] if srv else [],
                    }
                )

        # 补充仅服务端存在（例如客户端历史缓冲之外或独立发起）的慢/近期记录
        for srv in reversed(recent[-200:]):
            tid = str(srv.get("trace_id") or "")
            if tid and tid in matched_tids:
                continue
            merged.append(
                {
                    "trace_id": tid,
                    "ts_iso": srv.get("started_at"),
                    "method": srv.get("method"),
                    "path": srv.get("path"),
                    "status_code": srv.get("status_code"),
                    "client_total_ms": None,
                    "server_total_ms": srv.get("total_ms"),
                    "auth_gate1_ms": srv.get("auth_gate1_ms"),
                    "handler_ms": srv.get("handler_ms"),
                    "network_gap_ms": None,
                    "matched_server": True,
                    "top_spans": (srv.get("spans") or [])[:8],
                }
            )

        env = self.collect_environment_snapshot()
        sql_stats = self.get_sqlite_stats()
        conn_hub = getattr(self.ctx, "connection_hub", None)
        pool_stats = conn_hub.get_pool_stats() if (conn_hub and hasattr(conn_hub, "get_pool_stats")) else {}
        summary = self._summarize_traces(recent)
        findings = self.compute_auto_findings(
            traces=recent,
            environment=env,
            sqlite_stats=sql_stats,
            benchmark=bench,
            traces_merged=merged,
            client_snapshot=client_snapshot,
        )

        exec_logs: List[Dict[str, Any]] = []
        if include_execution_logs and getattr(self.ctx, "store", None) is not None:
            try:
                exec_logs = self.ctx.store.list_logs(limit=200)
            except Exception:
                exec_logs = []

        svc_lines = self._read_recent_service_log_lines() if include_service_log else []

        app_ver = str(getattr(self.ctx, "app_version", "") or "3.2.0")

        bundle = {
            "schema_version": "1.0",
            "app_name": "Data.O.G",
            "app_version": app_ver or "3.2.0",
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "summary": summary,
            "auto_findings": findings,
            "environment": env,
            "sqlite_stats": sql_stats,
            "pool_stats": pool_stats,
            "benchmark": bench,
            "traces_merged": merged[:500],
            "slow_traces": list(reversed(slow)),
            "recent_traces": list(reversed(recent)),
            "client_snapshot": client_snapshot or {},
            "recent_execution_logs": exec_logs,
            "recent_service_log_lines": svc_lines,
        }
        return self.sanitize_payload(bundle)
