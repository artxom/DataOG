"""16:9 跨环境三栏数标比对中心模块 (cross_comparator) 数据契约."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BindRoleConnectionPayload(BaseModel):
    role: str  # source | target
    connection_id: str = ""
    active_table_key: str = ""
    auto_refresh: bool = False


class ColumnBindingUpdateItem(BaseModel):
    id: int
    bound_source_col: str = ""
    bound_target_col: str = ""


class SaveCrossBindingsPayload(BaseModel):
    std_table_key: str = ""
    source_schema: str = ""
    source_table: str = ""
    target_schema: str = ""
    target_table: str = ""
    column_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    force_refresh: bool = False


class PushToMigratorPayload(BaseModel):
    std_table_key: str = ""
