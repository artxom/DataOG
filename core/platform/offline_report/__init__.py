"""Data.O.G 库级离线交互网页报告 (.html) 与单 Sheet 库级 Excel (.xlsx) 生成引擎包 (core/platform/offline_report)."""

from __future__ import annotations

from core.platform.offline_report.cross_env import (
    build_cross_env_excel_bytes,
    build_cross_env_html_bytes,
)
from core.platform.offline_report.runtime import wrap_offline_html
from core.platform.offline_report.single_db import (
    build_single_db_excel_bytes,
    build_single_db_html_bytes,
)

__all__ = [
    "build_cross_env_excel_bytes",
    "build_cross_env_html_bytes",
    "build_single_db_excel_bytes",
    "build_single_db_html_bytes",
    "wrap_offline_html",
]
