"""Data.O.G 平台级实时任务与导入进度追踪中心 (ImportProgressHub / TaskProgressHub)."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

ProgressCallback = Callable[[float, str, str, int, int], None]

_PROGRESS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,80}$")
_CRED_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api_key|apikey|secret|access_token|token)\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_CRED_RE = re.compile(r"(://[^:/\s]+):([^@/\s]+)@")
_SK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]+")
_MAX_RECENT_LOGS = 8
_LOG_THROTTLE_SEC = 0.35


def noop_progress(
    percent: float,
    stage: str,
    detail: str = "",
    processed: int = 0,
    total: int = 0,
) -> None:
    return None


def _sanitize_summary(text: Any) -> str:
    """折叠空白、脱敏凭证并截断为 <= 64 字符的可读微日志摘要."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    s = _CRED_KV_RE.sub(r"\1\2***", s)
    s = _URL_CRED_RE.sub(r"\1:***@", s)
    s = _SK_TOKEN_RE.sub("sk-***", s)
    if len(s) > 64:
        s = s[:61].rstrip() + "..."
    return s


class ImportProgressHub:
    """线程安全的内存态任务与导入进度池（支持条件变量实时推送、节流微日志、告警与取消信号）."""

    def __init__(self, *, max_entries: int = 128, ttl_seconds: float = 600.0) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._items: Dict[str, Dict[str, Any]] = {}
        self._max_entries = max(16, int(max_entries))
        self._ttl_seconds = max(60.0, float(ttl_seconds))

    @staticmethod
    def is_valid_id(progress_id: str) -> bool:
        return bool(progress_id and _PROGRESS_ID_RE.match(progress_id.strip()))

    def _prune_locked(self, now: float) -> None:
        running_ttl = max(3600.0, self._ttl_seconds * 6.0)
        expired = [
            k
            for k, v in self._items.items()
            if now - float(v.get("updated_ts", now))
            > (running_ttl if v.get("status") == "running" else self._ttl_seconds)
        ]
        for k in expired:
            self._items.pop(k, None)
        while len(self._items) > self._max_entries:
            oldest_key = min(
                self._items.keys(),
                key=lambda k: (
                    1 if self._items[k].get("status") == "running" else 0,
                    float(self._items[k].get("updated_ts", 0.0)),
                ),
            )
            self._items.pop(oldest_key, None)

    @staticmethod
    def _append_log_locked(
        cur: Dict[str, Any],
        *,
        now: float,
        stage: str,
        detail: str,
        level: str,
        processed: int = 0,
        total: int = 0,
        force: bool = False,
    ) -> None:
        logs: List[Dict[str, Any]] = cur.setdefault("recent_logs", [])
        eff_stage = str(stage or cur.get("stage") or "").strip()
        eff_detail = str(detail or "").strip()
        last_stage = logs[-1]["stage"] if logs else None
        last_detail = str(cur.get("last_log_detail", ""))
        last_ts = float(cur.get("last_log_ts", 0.0))

        should_append = False
        if force or not logs:
            should_append = True
        elif eff_stage != last_stage:
            should_append = True
        elif eff_detail != last_detail and (
            (now - last_ts) >= _LOG_THROTTLE_SEC
            or (total > 0 and processed == total)
        ):
            should_append = True

        if not should_append:
            return

        started = float(cur.get("started_ts", now))
        elapsed_ms = round(max(0.0, (now - started) * 1000.0), 1)
        logs.append(
            {
                "seq": int(cur.get("seq", 1)),
                "elapsed_ms": elapsed_ms,
                "stage": eff_stage,
                "summary": _sanitize_summary(eff_detail or eff_stage),
                "level": level,
            }
        )
        if len(logs) > _MAX_RECENT_LOGS:
            del logs[:-_MAX_RECENT_LOGS]
        cur["last_log_ts"] = now
        cur["last_log_detail"] = eff_detail

    @staticmethod
    def _snapshot_locked(cur: Dict[str, Any], now: float) -> Dict[str, Any]:
        started = float(cur.get("started_ts", now))
        return {
            "progress_id": cur["progress_id"],
            "status": cur["status"],
            "percent": float(cur.get("percent", 0.0)),
            "stage": str(cur.get("stage", "")),
            "detail": str(cur.get("detail", "")),
            "processed": int(cur.get("processed", 0)),
            "total": int(cur.get("total", 0)),
            "elapsed_ms": round(max(0.0, (now - started) * 1000.0), 1),
            "error": str(cur.get("error", "")),
            "seq": int(cur.get("seq", 1)),
            "warning": str(cur.get("warning", "")),
            "recent_logs": [dict(entry) for entry in cur.get("recent_logs", [])],
        }

    def start(
        self,
        progress_id: str,
        title: str = "正在解析导入",
        detail: str = "文件已接收，准备安全校验与解析...",
    ) -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        init_detail = str(detail or "任务已启动，准备执行...")
        with self._cond:
            prev = self._items.get(pid)
            next_seq = int(prev.get("seq", 0)) + 1 if prev is not None else 1
            self._prune_locked(now)
            cur: Dict[str, Any] = {
                "progress_id": pid,
                "status": "running",
                "percent": 12.0,
                "stage": title,
                "detail": init_detail,
                "processed": 0,
                "total": 0,
                "error": "",
                "warning": "",
                "seq": next_seq,
                "recent_logs": [],
                "last_log_ts": 0.0,
                "last_log_detail": "",
                "started_ts": now,
                "updated_ts": now,
            }
            self._append_log_locked(
                cur,
                now=now,
                stage=title,
                detail=init_detail,
                level="info",
                force=True,
            )
            self._items[pid] = cur
            self._cond.notify_all()

    def update(
        self,
        progress_id: str,
        *,
        percent: float,
        stage: str,
        detail: str = "",
        processed: int = 0,
        total: int = 0,
    ) -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        clamped = round(max(0.0, min(99.5, float(percent))), 1)
        proc_i = max(0, int(processed))
        tot_i = max(0, int(total))
        with self._cond:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "status": "running",
                    "percent": clamped,
                    "stage": stage,
                    "detail": detail,
                    "processed": proc_i,
                    "total": tot_i,
                    "error": "",
                    "warning": "",
                    "seq": 1,
                    "recent_logs": [],
                    "last_log_ts": 0.0,
                    "last_log_detail": "",
                    "started_ts": now,
                    "updated_ts": now,
                }
                self._items[pid] = cur
                self._append_log_locked(
                    cur,
                    now=now,
                    stage=stage,
                    detail=detail,
                    level="info",
                    processed=proc_i,
                    total=tot_i,
                    force=True,
                )
            else:
                if cur.get("status") == "cancelled":
                    return
                cur["seq"] = int(cur.get("seq", 0)) + 1
                cur["status"] = "running"
                cur["percent"] = max(float(cur.get("percent", 0.0)), clamped)
                if stage:
                    cur["stage"] = stage
                cur["detail"] = detail
                cur["processed"] = proc_i
                cur["total"] = tot_i
                cur["updated_ts"] = now
                self._append_log_locked(
                    cur,
                    now=now,
                    stage=str(cur.get("stage", "")),
                    detail=detail,
                    level="info",
                    processed=proc_i,
                    total=tot_i,
                    force=False,
                )
            self._cond.notify_all()

    def warn(self, progress_id: str, stage: str, detail: str = "") -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        warn_text = str(detail or stage or "").strip()
        with self._cond:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "status": "running",
                    "percent": 0.0,
                    "stage": stage or "执行警告",
                    "detail": detail,
                    "processed": 0,
                    "total": 0,
                    "error": "",
                    "warning": warn_text,
                    "seq": 1,
                    "recent_logs": [],
                    "last_log_ts": 0.0,
                    "last_log_detail": "",
                    "started_ts": now,
                    "updated_ts": now,
                }
                self._items[pid] = cur
            else:
                if cur.get("status") == "cancelled":
                    return
                cur["seq"] = int(cur.get("seq", 0)) + 1
                cur["warning"] = warn_text
                cur["updated_ts"] = now
            self._append_log_locked(
                cur,
                now=now,
                stage=stage or str(cur.get("stage", "")),
                detail=detail or stage,
                level="warn",
                processed=int(cur.get("processed", 0)),
                total=int(cur.get("total", 0)),
                force=True,
            )
            self._cond.notify_all()

    def cancel(self, progress_id: str, reason: str = "操作已取消") -> bool:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return False
        now = time.monotonic()
        clean_reason = str(reason or "操作已取消")
        with self._cond:
            cur = self._items.get(pid)
            if cur is None or cur.get("status") in ("completed", "failed", "cancelled"):
                return False
            cur["seq"] = int(cur.get("seq", 0)) + 1
            cur["status"] = "cancelled"
            cur["stage"] = "已取消"
            cur["detail"] = clean_reason
            cur["updated_ts"] = now
            self._append_log_locked(
                cur,
                now=now,
                stage="已取消",
                detail=clean_reason,
                level="warn",
                processed=int(cur.get("processed", 0)),
                total=int(cur.get("total", 0)),
                force=True,
            )
            self._cond.notify_all()
            return True

    def is_cancelled(self, progress_id: str) -> bool:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return False
        with self._cond:
            cur = self._items.get(pid)
            return bool(cur is not None and cur.get("status") == "cancelled")

    def finish(
        self,
        progress_id: str,
        *,
        detail: str = "导入完成",
        stage: str = "",
        warning: str = "",
        processed: int = 0,
        total: int = 0,
    ) -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        eff_stage = (stage or "").strip() or "导入完成"
        with self._cond:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "seq": 0,
                    "warning": "",
                    "recent_logs": [],
                    "last_log_ts": 0.0,
                    "last_log_detail": "",
                    "started_ts": now,
                }
                self._items[pid] = cur
            elif cur.get("status") == "cancelled":
                return

            eff_processed = max(0, int(processed or cur.get("processed", 0)))
            eff_total = max(0, int(total or cur.get("total", 0)))
            eff_warning = str(warning or "").strip() or str(cur.get("warning", ""))
            cur.update(
                {
                    "status": "completed",
                    "percent": 100.0,
                    "stage": eff_stage,
                    "detail": detail,
                    "warning": eff_warning,
                    "processed": eff_processed,
                    "total": eff_total,
                    "seq": int(cur.get("seq", 0)) + 1,
                    "updated_ts": now,
                }
            )
            self._append_log_locked(
                cur,
                now=now,
                stage=eff_stage,
                detail=detail,
                level="success",
                processed=eff_processed,
                total=eff_total,
                force=True,
            )
            self._cond.notify_all()

    def fail(self, progress_id: str, error: str) -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        err_msg = str(error or "导入失败")
        with self._cond:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "percent": 0.0,
                    "processed": 0,
                    "total": 0,
                    "warning": "",
                    "seq": 0,
                    "recent_logs": [],
                    "last_log_ts": 0.0,
                    "last_log_detail": "",
                    "started_ts": now,
                }
                self._items[pid] = cur
            elif cur.get("status") == "cancelled":
                return

            cur.update(
                {
                    "status": "failed",
                    "stage": "导入中止",
                    "detail": err_msg,
                    "error": err_msg,
                    "seq": int(cur.get("seq", 0)) + 1,
                    "updated_ts": now,
                }
            )
            self._append_log_locked(
                cur,
                now=now,
                stage="导入中止",
                detail=err_msg,
                level="error",
                processed=int(cur.get("processed", 0)),
                total=int(cur.get("total", 0)),
                force=True,
            )
            self._cond.notify_all()

    def get(self, progress_id: str) -> Optional[Dict[str, Any]]:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return None
        now = time.monotonic()
        with self._cond:
            self._prune_locked(now)
            cur = self._items.get(pid)
            if cur is None:
                return None
            return self._snapshot_locked(cur, now)

    def wait_for_change(
        self,
        progress_id: str,
        after_seq: int,
        timeout_sec: float = 0.4,
    ) -> Optional[Dict[str, Any]]:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return None
        deadline = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
        with self._cond:
            while True:
                now = time.monotonic()
                self._prune_locked(now)
                cur = self._items.get(pid)
                if cur is None:
                    return None
                if int(cur.get("seq", 1)) > int(after_seq) or cur.get("status") in (
                    "completed",
                    "failed",
                    "cancelled",
                ):
                    return self._snapshot_locked(cur, now)
                remaining = deadline - now
                if remaining <= 0:
                    return self._snapshot_locked(cur, now)
                self._cond.wait(timeout=remaining)

    def make_reporter(self, progress_id: str) -> ProgressCallback:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return noop_progress

        def _report(
            percent: float,
            stage: str,
            detail: str = "",
            processed: int = 0,
            total: int = 0,
        ) -> None:
            self.update(
                pid,
                percent=percent,
                stage=stage,
                detail=detail,
                processed=processed,
                total=total,
            )

        return _report


TaskProgressHub = ImportProgressHub
