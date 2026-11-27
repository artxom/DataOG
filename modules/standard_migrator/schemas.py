"""单库批量落标贯标执行台模块 (standard_migrator) 数据契约."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BindMigratorConnectionPayload(BaseModel):
    connection_id: str
    auto_sync: bool = True


class LoadDbTablesPayload(BaseModel):
    connection_id: Optional[str] = None
    schema_name: str = ""
    table_name: str = ""
    max_tables: int = Field(default=30, ge=1, le=200)


class ExecuteBatchPayload(BaseModel):
    item_ids: List[int] = Field(default_factory=list)
    max_workers: int = Field(default=5, ge=1, le=16)
    keep_backup: bool = True
    auto_checkpoint: bool = False
    checkpoint_name: str = ""


class TableRebuildPayload(BaseModel):
    schema_name: str
    table_name: str
    connection_id: str = ""
    item_ids: Optional[List[int]] = None
    keep_backup: bool = True
    auto_checkpoint: bool = False
    checkpoint_name: str = ""


class CreateCheckpointPayload(BaseModel):
    name: str
    description: str = ""
    connection_id: str = ""


class RollbackRequestPayload(BaseModel):
    checkpoint_id: Optional[int] = None
    change_ids: Optional[List[int]] = None
    prefer_backup_table: bool = True


class DeleteMigrationItemsPayload(BaseModel):
    item_ids: List[int] = Field(default_factory=list)


class SaveFieldBindingsPayload(BaseModel):
    connection_id: str
    format_profile: str = "AUTO"
    bindings: List[Dict[str, Any]] = Field(default_factory=list)
    active_schema: str = ""
    active_table: str = ""


class AutoMatchBindingsPayload(BaseModel):
    connection_id: str
    tables: List[Any] = Field(default_factory=list)
    schema_name: str = ""
    table_name: str = ""
    format_profile: str = "AUTO"


class CheckCompliancePayload(BaseModel):
    connection_id: str
    tables: List[Any] = Field(default_factory=list)
    schemas: List[str] = Field(default_factory=list)
    schema_name: str = ""
    scope: str = ""
    refresh_catalog: bool = False
    only_bound_tables: bool = False
    format_profile: str = "AUTO"
    force_refresh: bool = True


class StageMigrationFromBindingsPayload(BaseModel):
    connection_id: str
    schema_name: str
    table_name: str
    column_names: List[str] = Field(default_factory=list)
    format_profile: str = "AUTO"
    append: bool = True


class SqlRewriteScriptItem(BaseModel):
    filename: str = "script.sql"
    content: str = ""


class SqlRewriteAnalyzePayload(BaseModel):
    sql_text: str = ""
    filename: str = "script.sql"
    scripts: List[SqlRewriteScriptItem] = Field(default_factory=list)
    connection_id: str = ""
    since_checkpoint_id: Optional[int] = None
    external_changelog_json: str = ""
    default_schema: str = ""
    cte_alias_policy: str = "propagate"
    rewrite_ddl_types: bool = True
    rewrite_type_expressions: bool = True
    rewrite_comments: bool = False
    auto_apply_medium: bool = False
    mode: str = "deterministic"  # deterministic | hybrid
    provider_id: str = ""
    model: str = ""
    llm_audit: bool = False
    dialect: str = ""


class SqlRewriteExportFileItem(BaseModel):
    filename: str = "script.sql"
    rewritten_sql: str = ""
    unified_diff: str = ""
    changed: bool = False
    applied_patch_count: int = 0
    advisory_count: int = 0


class SqlRewriteExportZipPayload(BaseModel):
    files: List[SqlRewriteExportFileItem] = Field(default_factory=list)
