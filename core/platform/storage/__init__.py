"""Data.O.G 三层分离状态存储与无损迁移引擎包 (core/platform/storage).

保持 `from core.platform.storage import PlatformStateStore, ...` 100% 向后兼容，
按职责拆分为 schema / base / legacy_migration / platform_repos / governance_repos 五个高内聚子模块。
"""

from __future__ import annotations

from core.platform.storage.base import _BaseStateStore
from core.platform.storage.governance_repos import _GovernanceReposMixin
from core.platform.storage.legacy_migration import _LegacyMigrationMixin
from core.platform.storage.platform_repos import _PlatformReposMixin
from core.platform.storage.schema import (
    PRIVATE_ONLY_TABLES,
    PRIVATE_SCHEMA_SQL,
    WORKSPACE_BUSINESS_TABLES,
    WORKSPACE_META_KEYS,
    WORKSPACE_SCHEMA_SQL,
    ensure_columns_for_table,
    ensure_private_schema_columns,
    ensure_workspace_schema_columns,
    init_workspace_schema_on_conn,
)


class PlatformStateStore(
    _LegacyMigrationMixin,
    _PlatformReposMixin,
    _GovernanceReposMixin,
    _BaseStateStore,
):
    """线程安全的 Data.O.G 双库分离状态引擎（本地私有保险箱 + 可共享团队工作区）."""


__all__ = [
    "PRIVATE_ONLY_TABLES",
    "PRIVATE_SCHEMA_SQL",
    "PlatformStateStore",
    "WORKSPACE_BUSINESS_TABLES",
    "WORKSPACE_META_KEYS",
    "WORKSPACE_SCHEMA_SQL",
    "ensure_columns_for_table",
    "ensure_private_schema_columns",
    "ensure_workspace_schema_columns",
    "init_workspace_schema_on_conn",
]
