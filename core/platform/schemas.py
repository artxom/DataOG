"""DataOG 平台底座 Pydantic v2 请求与响应契约."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AuthActivatePayload(BaseModel):
    login_mode: str = "activation_code"
    activation_code: str = ""
    username: str = ""
    password: str = ""


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
    standard_format: Optional[str] = None
    auto_refresh_metadata: bool = False


class ConnectionStandardFormatPayload(BaseModel):
    standard_format: str = "AUTO"


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


class StandardDictItemPayload(BaseModel):
    std_code: str = Field(..., min_length=1, max_length=128)
    reg_category: str = ""
    cat_l1: str = ""
    cat_l2: str = ""
    cat_l3: str = ""
    cn_name: str = Field(..., min_length=1, max_length=256)
    cn_alias: str = ""
    en_name: str = ""
    en_abbr: str = Field(..., min_length=1, max_length=256)
    biz_definition: str = ""
    scope_desc: str = ""
    biz_rule: str = ""
    max_length: str = ""
    min_scale: str = ""
    ref_code_no: str = ""
    ref_code_name: str = ""
    ref_code_detail: str = ""
    security_level: str = ""
    ref_std_no: str = ""
    ref_std_name: str = ""
    domain_name: str = ""
    domain_code: str = ""
    data_category: str = ""
    data_format: str = ""
    value_range: str = ""
    source_system: str = ""
    ddm_catalog: str = ""
    is_standard_dict: str = "1"
    version_no: str = "1"
    remark: str = ""
    type_dws: str = ""
    type_dw_mart: str = ""
    type_mysql: str = ""
    type_oracle: str = ""
    type_dameng: str = ""
    type_goldendb: str = ""
    type_ob_oracle: str = ""
    type_ob_mysql: str = ""
    type_argodb: str = ""
    type_oceanbase: str = ""
    extra_attrs: Dict[str, Any] = Field(default_factory=dict)


class WorkspaceCreatePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field(default="", max_length=240)
    clone_from_workspace_id: Optional[str] = None
    switch_immediately: bool = True


class WorkspaceUpdatePayload(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=240)


class WorkspaceMergeExecutePayload(BaseModel):
    merge_token: str = Field(..., min_length=24, max_length=64)
    selected_modules: List[str] = Field(default_factory=list)
    strategy: str = Field(default="supplement")



