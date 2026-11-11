"""单库批量落标贯标执行台模块 (standard_migrator) 数据契约."""

from __future__ import annotations

from typing import List, Optional

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


class TableRebuildPayload(BaseModel):
    schema_name: str
    table_name: str
    item_ids: Optional[List[int]] = None
    keep_backup: bool = True
