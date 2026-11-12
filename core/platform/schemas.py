"""DataOG 平台底座 Pydantic v2 请求与响应契约."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConnectionPayload(BaseModel):
    connection_id: Optional[str] = None
    name: str = ""
    driver_track: str = "NATIVE"
    db_type: str = "DWS"
    host: str = ""
    port: Optional[int] = None
    dbname: str = ""
    default_schema: str = ""
    schema_name: str = ""
    user: str = ""
    password: Optional[str] = None
    clear_password: bool = False
    sslmode: str = "prefer"
    auth_mode: str = "LDAP"
    sqlite_path: str = ""
    jdbc_driver_id: str = ""
    jdbc_url: str = ""
    jdbc_driver_class: str = ""
    extra_props: Dict[str, Any] = Field(default_factory=dict)
    auto_refresh_metadata: bool = False


class MetadataRefreshPayload(BaseModel):
    schema_name: Optional[str] = None


class QueryExecutePayload(BaseModel):
    connection_id: str
    sql: str
    limit: int = Field(default=500, ge=1, le=5000)
    timeout_sec: int = Field(default=30, ge=2, le=300)


class ReadOnlyQueryExecutePayload(BaseModel):
    connection_id: str
    sql: str
    limit: int = Field(default=100, ge=1, le=200)
    timeout_sec: int = Field(default=12, ge=2, le=25)


class QueryExportPayload(BaseModel):
    connection_id: Optional[str] = None
    sql: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    format: str = "csv"
    title: str = "查询结果"
    limit: int = Field(default=1000, ge=1, le=5000)


class AppearanceUpdatePayload(BaseModel):
    wallpaper_id: Optional[str] = None
    wallpaper_fit: Optional[str] = None


class AppearanceResetPayload(BaseModel):
    slot: str = "wallpaper"


class LLMProviderPayload(BaseModel):
    provider_id: Optional[str] = None
    name: Optional[str] = None
    provider_type: Optional[str] = None
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False
    default_model: Optional[str] = None
    models: Optional[List[str]] = None
    is_default: Optional[bool] = None

