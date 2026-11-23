"""DataOG 微内核公共底座包 (core.platform)."""

from core.platform.connection_hub import ConnectionHub
from core.platform.context import PlatformContext, get_platform_context
from core.platform.driver_manager import DRIVER_TEMPLATES, DriverManager, JdbcConnectionAdapter
from core.platform.inceptor_client import InceptorThriftClient
from core.platform.metadata_hub import MetadataHub
from core.platform.module_registry import (
    ModuleRegistry,
    ModuleSpec,
    discover_module_specs,
    mount_discovered_modules,
    mount_module_spec,
)
from core.platform.models import (
    ColumnMeta,
    ConnectionProfile,
    DriverPackage,
    QueryColumn,
    QueryResult,
    SchemaMeta,
    TableMeta,
)
from core.platform.query_engine import QueryEngine
from core.platform.storage import PlatformStateStore

__all__ = [
    "DRIVER_TEMPLATES",
    "ColumnMeta",
    "ConnectionHub",
    "ConnectionProfile",
    "DriverManager",
    "DriverPackage",
    "InceptorThriftClient",
    "JdbcConnectionAdapter",
    "MetadataHub",
    "ModuleRegistry",
    "ModuleSpec",
    "PlatformContext",
    "PlatformStateStore",
    "QueryColumn",
    "QueryEngine",
    "QueryResult",
    "SchemaMeta",
    "TableMeta",
    "discover_module_specs",
    "get_platform_context",
    "mount_discovered_modules",
    "mount_module_spec",
]
