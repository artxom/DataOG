"""Data.O.G 平台级 Excel / 批量任务实时导入进度追踪中心 (ImportProgressHub)."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, Optional

ProgressCallback = Callable[[float, str, str, int, int], None]

_PROGRESS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,80}$")


def noop_progress(
    percent: float,
    stage: str,
    detail: str = "",
    processed: int = 0,
    total: int = 0,
) -> None:
    return None


class ImportProgressHub:
    """线程安全的内存态导入进度池（自动过期淘汰，供前端弹窗实时轮询进度条）."""

    def __init__(self, *, max_entries: int = 128, ttl_seconds: float = 600.0) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._max_entries = max(16, int(max_entries))
        self._ttl_seconds = max(60.0, float(ttl_seconds))

    @staticmethod
    def is_valid_id(progress_id: str) -> bool:
        return bool(progress_id and _PROGRESS_ID_RE.match(progress_id.strip()))

    def _prune_locked(self, now: float) -> None:
        expired = [
            k
            for k, v in self._items.items()
            if now - float(v.get("updated_ts", now)) > self._ttl_seconds
        ]
        for k in expired:
            self._items.pop(k, None)
        while len(self._items) > self._max_entries:
            oldest_key = min(
                self._items.keys(),
                key=lambda k: float(self._items[k].get("updated_ts", 0.0)),
            )
            self._items.pop(oldest_key, None)

    def start(self, progress_id: str, title: str = "正在解析导入") -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            self._items[pid] = {
                "progress_id": pid,
                "status": "running",
                "percent": 12.0,
                "stage": title,
                "detail": "文件已接收，准备安全校验与解析...",
                "processed": 0,
                "total": 0,
                "started_ts": now,
                "updated_ts": now,
            }

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
        with self._lock:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "status": "running",
                    "percent": clamped,
                    "stage": stage,
                    "detail": detail,
                    "processed": max(0, int(processed)),
                    "total": max(0, int(total)),
                    "started_ts": now,
                    "updated_ts": now,
                }
                self._items[pid] = cur
            else:
                cur["status"] = "running"
                cur["percent"] = max(float(cur.get("percent", 0.0)), clamped)
                if stage:
                    cur["stage"] = stage
                cur["detail"] = detail
                cur["processed"] = max(0, int(processed))
                cur["total"] = max(0, int(total))
                cur["updated_ts"] = now

    def finish(
        self,
        progress_id: str,
        *,
        detail: str = "导入完成",
        processed: int = 0,
        total: int = 0,
    ) -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        with self._lock:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "started_ts": now,
                }
                self._items[pid] = cur
            cur.update(
                {
                    "status": "completed",
                    "percent": 100.0,
                    "stage": "导入完成",
                    "detail": detail,
                    "processed": max(0, int(processed or cur.get("processed", 0))),
                    "total": max(0, int(total or cur.get("total", 0))),
                    "updated_ts": now,
                }
            )

    def fail(self, progress_id: str, error: str) -> None:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return
        now = time.monotonic()
        with self._lock:
            cur = self._items.get(pid)
            if cur is None:
                self._prune_locked(now)
                cur = {
                    "progress_id": pid,
                    "percent": 0.0,
                    "processed": 0,
                    "total": 0,
                    "started_ts": now,
                }
                self._items[pid] = cur
            cur.update(
                {
                    "status": "failed",
                    "stage": "导入中止",
                    "detail": str(error or "导入失败"),
                    "error": str(error or "导入失败"),
                    "updated_ts": now,
                }
            )

    def get(self, progress_id: str) -> Optional[Dict[str, Any]]:
        pid = (progress_id or "").strip()
        if not self.is_valid_id(pid):
            return None
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            cur = self._items.get(pid)
            if cur is None:
                return None
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
            }

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
