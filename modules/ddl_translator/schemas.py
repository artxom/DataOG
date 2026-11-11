"""跨方言建表 DDL 翻译与四层视图血缘穿透器模块 (ddl_translator) 数据契约."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TranslateDdlPayload(BaseModel):
    source_mode: str = "connection"  # connection | standard | view_sql | source | target
    source_role: str = ""  # 兼容旧参数: source | target | standard | view_sql
    connection_id: str = ""
    probe_connection_id: str = ""
    source_schema: str = ""
    source_table: str = ""
    target_dialect: str = "DWS"
    storage_mode: str = "AUTO"
    target_schema: str = ""
    target_table: str = ""
    apply_standard_mapping: bool = False
    std_table_key: str = ""
    view_sql: str = ""
    force_refresh: bool = False
