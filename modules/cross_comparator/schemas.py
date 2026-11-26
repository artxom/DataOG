"""跨环境五虚拟列数标比对中心模块 (cross_comparator) 数据契约."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class BindRoleConnectionPayload(BaseModel):
    role: str  # source | target
    connection_id: str = ""
    active_table_key: str = ""
    auto_refresh: bool = False


class SaveCrossBindingsPayload(BaseModel):
    std_table_key: str = ""
    source_schema: str = ""
    source_table: str = ""
    target_schema: str = ""
    target_table: str = ""
    column_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    force_refresh: bool = False


class SaveCrossPairingsPayload(BaseModel):
    std_table_key: str = ""
    source_schema: str = ""
    source_table: str = ""
    target_schema: str = ""
    target_table: str = ""
    format_profile: str = "AUTO"
    source_format_profile: str = "AUTO"
    target_format_profile: str = "AUTO"
    pairings: List[Dict[str, Any]] = Field(default_factory=list)
    force_refresh: bool = False
    sync_to_field_bindings: bool = True


class PushToMigratorPayload(BaseModel):
    std_table_key: str = ""
    format_profile: str = "AUTO"
    source_format_profile: str = "AUTO"
    target_format_profile: str = "AUTO"


class PrefetchPairsPayload(BaseModel):
    max_pairs: int = Field(default=60, ge=1, le=200)
