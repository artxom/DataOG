"""SQLite 本地状态持久化存储兼容门面（底层统一由 core.platform.storage.PlatformStateStore 驱动）."""

from __future__ import annotations

from core.platform.storage import PlatformStateStore


class StateStore(PlatformStateStore):
    """兼容原有 StateStore 导入路径，完整继承 DataOG 全局状态存储与无损迁移能力."""

    pass


__all__ = ["StateStore"]
