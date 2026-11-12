"""AI 只读智能数据探查模块 Pydantic 契约定义 (modules/data_explorer/schemas.py)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class CreateExplorerSessionPayload(BaseModel):
    connection_id: str = Field(..., min_length=1, max_length=128)
    schema_name: str = Field(default="", max_length=128)
    focus_tables: List[str] = Field(default_factory=list, max_length=24)
    title: str = Field(default="", max_length=120)
    provider_id: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)


class UpdateExplorerSessionPayload(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)
    schema_name: Optional[str] = Field(default=None, max_length=128)
    focus_tables: Optional[List[str]] = Field(default=None, max_length=24)


class AskExplorerPayload(BaseModel):
    session_id: str = Field(default="", max_length=64)
    connection_id: str = Field(..., min_length=1, max_length=128)
    schema_name: str = Field(default="", max_length=128)
    focus_tables: List[str] = Field(default_factory=list, max_length=24)
    prompt: str = Field(..., min_length=1, max_length=4000)
    goal_type: str = Field(default="general", max_length=32)
    provider_id: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)
    max_steps: int = Field(default=5, ge=1, le=8)
    sample_limit: int = Field(default=100, ge=10, le=200)
