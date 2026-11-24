"""Data.O.G 3.0 智能数据助手 Pydantic v2 请求契约."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AssistantSessionCreatePayload(BaseModel):
    title: str = ""
    work_mode: str = "auto"  # auto | connection | dialect
    connection_id: str = ""
    dialect: str = "DWS"
    schema_name: str = ""
    focus_tables: List[str] = Field(default_factory=list)
    task_mode: str = "auto"  # auto | query | troubleshoot | optimize | syntax | ddl
    provider_id: str = ""
    model: str = ""


class AssistantSessionUpdatePayload(BaseModel):
    title: Optional[str] = None
    work_mode: Optional[str] = None
    connection_id: Optional[str] = None
    dialect: Optional[str] = None
    schema_name: Optional[str] = None
    focus_tables: Optional[List[str]] = None
    task_mode: Optional[str] = None
    provider_id: Optional[str] = None
    model: Optional[str] = None


class AssistantPastedAttachmentPayload(BaseModel):
    content: str = Field(..., min_length=1)
    filename: str = ""
    file_hint: str = ""  # auto | text | sql | csv | tsv
    session_id: str = ""


class AssistantChatPayload(BaseModel):
    session_id: Optional[str] = None
    prompt: str = Field(default="", max_length=32000)
    work_mode: str = "auto"
    connection_id: str = ""
    dialect: str = ""
    schema_name: str = ""
    focus_tables: List[str] = Field(default_factory=list)
    task_mode: str = "auto"
    provider_id: str = ""
    model: str = ""
    max_steps: int = Field(default=6, ge=1, le=10)
    sample_limit: int = Field(default=100, ge=10, le=200)
    clarification_reply: Optional[Dict[str, Any]] = None
    attachment_ids: List[str] = Field(default_factory=list)
    large_payload_strategy: str = "auto"  # auto | stepped_batch | on_demand
    batch_start_chunk: int = Field(default=0, ge=0)
    batch_max_chunks: int = Field(default=8, ge=1, le=12)


class ProposalConfirmPayload(BaseModel):
    edited_sql: Optional[str] = None
    confirm_token: str = ""
    connection_id: Optional[str] = None
    note: str = ""


class ProposalRejectPayload(BaseModel):
    reason: str = ""


class AssistantExportResultPayload(BaseModel):
    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    fmt: str = "xlsx"
    title: str = "DataOG_Assistant_Result"
