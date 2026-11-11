"""多邻国模块：智能数仓命名与跨方言 DDL/视图翻译引擎 (ddl_translator) 数据契约."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class TranslateDdlPayload(BaseModel):
    source_mode: str = "connection"  # connection | standard | view_sql | sql_text | source | target
    source_role: str = ""  # 兼容旧参数: source | target | standard | view_sql
    connection_id: str = ""
    probe_connection_id: str = ""
    source_schema: str = ""
    source_table: str = ""
    source_dialect_hint: str = ""
    target_dialect: str = "DWS"
    storage_mode: str = "AUTO"
    target_schema: str = ""
    target_table: str = ""
    apply_standard_mapping: bool = False
    std_table_key: str = ""
    view_sql: str = ""
    sql_text: str = ""
    force_refresh: bool = False
    ai_assist: bool = False
    provider_id: str = ""
    model: str = ""


class SmartNamingPayload(BaseModel):
    text: str = ""
    items: List[str] = Field(default_factory=list)
    granularity: str = "column"  # column | table
    naming_style: str = "snake_case"  # snake_case | UPPER_SNAKE | camelCase
    table_prefix: str = ""
    use_abbrev: bool = True
    target_dialect: str = "DWS"
    use_ai: bool = True
    provider_id: str = ""
    model: str = ""
