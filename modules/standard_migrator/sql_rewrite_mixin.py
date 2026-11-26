"""单库落标贯标模块 — 基于变更日志底座的 SQL 脚本智能自动改写服务混入类."""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import zipfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from core.platform.dialects import clean_identifier_input
from core.platform.import_progress import ProgressCallback, noop_progress
from modules.standard_migrator.changelog_mixin import _format_log_row
from modules.standard_migrator.sql_rewrite_llm import enhance_sql_rewrite_with_llm
from modules.standard_migrator.sql_rewriter import (
    ChangeRegistry,
    RewriteOptions,
    build_unified_diff,
    rewrite_sql_deterministic,
)

MAX_SQL_SCRIPT_BYTES = 4 * 1024 * 1024  # 4 MB per script
MAX_BATCH_SCRIPTS = 50
ALLOWED_SQL_EXTENSIONS = (".sql", ".ddl", ".prc", ".fnc", ".vw", ".hql", ".txt", ".plsql", ".pck", ".bdy")


def _safe_script_filename(name: str, fallback: str = "script.sql") -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", base).strip("._ ")
    if not cleaned:
        return fallback
    return cleaned[:120]


def _decode_sql_bytes(raw: bytes, filename: str) -> str:
    if len(raw) > MAX_SQL_SCRIPT_BYTES:
        raise ValueError(f"脚本 [{filename}] 超过单文件 4MB 大小上限")
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _model_sort_key(item: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """优先推荐已就绪的轻量/快速模型（如 DeepSeek-v4-flash / gemini-flash）."""
    ready = 0 if item.get("ready") else 1
    tier = str(item.get("model_tier") or "standard").lower()
    is_light = 0 if tier == "lightweight" else (1 if tier == "standard" else 2)
    is_def = 0 if item.get("is_default") else 1
    return (ready, is_light, is_def, str(item.get("model") or "").lower())


class _StandardSqlRewriteMixin:
    """SQL 脚本自动改写（确定性规则 + 可选轻量模型手术刀增强 + 批量导出）."""

    def _load_workspace_applied_logs_asc(
        self,
        *,
        connection_id: str = "",
        since_checkpoint_id: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        self._ensure_changelog_tables()
        eff_cid = (connection_id or "").strip()
        anchor_id = 0
        cp_info: Optional[Dict[str, Any]] = None

        with self.ctx.store.module_db(scope="workspace") as conn:
            if since_checkpoint_id is not None and int(since_checkpoint_id) > 0:
                cp_row = conn.execute(
                    "SELECT * FROM migration_checkpoints WHERE id = ?",
                    (int(since_checkpoint_id),),
                ).fetchone()
                if not cp_row:
                    raise ValueError(f"未找到 ID={since_checkpoint_id} 的 Checkpoint")
                cp_info = dict(cp_row)
                anchor_id = int(cp_info.get("anchor_log_id") or 0)
                if not eff_cid and str(cp_info.get("connection_id") or "").strip():
                    eff_cid = str(cp_info["connection_id"]).strip()

            rows = conn.execute(
                """
                SELECT * FROM migration_change_logs
                WHERE event_type = 'MIGRATION' AND status = 'APPLIED' AND id > ?
                ORDER BY id ASC
                """,
                (anchor_id,),
            ).fetchall()

        logs = [_format_log_row(r) for r in rows]
        if eff_cid:
            logs = [r for r in logs if r["connection_id"] == eff_cid]
        return logs, cp_info

    def _load_cached_table_columns_map(
        self, connection_id: str = ""
    ) -> Dict[Tuple[str, str], Set[str]]:
        """从本地 Layer 3 元数据缓存零延迟加载表字段集合，辅助多表 JOIN 消歧."""
        eff_cid = (connection_id or "").strip()
        result: Dict[Tuple[str, str], Set[str]] = {}
        try:
            with self.ctx.store.module_db(scope="workspace") as conn:
                if eff_cid:
                    rows = conn.execute(
                        "SELECT schema_name, table_name, column_name FROM meta_columns WHERE connection_id = ?",
                        (eff_cid,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT schema_name, table_name, column_name FROM meta_columns"
                    ).fetchall()
            for r in rows:
                sch = (clean_identifier_input(str(r["schema_name"] or "")) or "public").lower()
                tbl = (clean_identifier_input(str(r["table_name"] or "")) or "").lower()
                col = (clean_identifier_input(str(r["column_name"] or "")) or "").lower()
                if tbl and col:
                    result.setdefault((sch, tbl), set()).add(col)
        except Exception:
            pass
        return result

    def _build_change_registry(
        self,
        *,
        connection_id: str = "",
        since_checkpoint_id: Optional[int] = None,
        external_changelog_json: Optional[str] = None,
    ) -> Tuple[ChangeRegistry, Dict[str, Any]]:
        if external_changelog_json and external_changelog_json.strip():
            meta_cols = self._load_cached_table_columns_map(connection_id=connection_id)
            reg = ChangeRegistry.from_changelog_json(
                external_changelog_json,
                table_metadata_cols=meta_cols,
            )
            source_info = {
                "source_type": "EXTERNAL_JSON",
                "connection_id": connection_id,
                "checkpoint": None,
                "mapping_count": len(reg.mappings),
                "table_count": len(reg.by_schema_table),
            }
            return reg, source_info

        logs_asc, cp_info = self._load_workspace_applied_logs_asc(
            connection_id=connection_id,
            since_checkpoint_id=since_checkpoint_id,
        )
        eff_cid = (connection_id or "").strip() or str((cp_info or {}).get("connection_id") or "").strip()
        meta_cols = self._load_cached_table_columns_map(connection_id=eff_cid)
        effective_mappings = self._compute_effective_column_mappings(logs_asc)
        reg = ChangeRegistry.from_effective_mappings(
            effective_mappings,
            raw_logs_asc=logs_asc,
            table_metadata_cols=meta_cols,
        )
        source_info = {
            "source_type": "WORKSPACE_CHANGELOG",
            "connection_id": eff_cid,
            "checkpoint": cp_info,
            "raw_log_count": len(logs_asc),
            "mapping_count": len(reg.mappings),
            "table_count": len(reg.by_schema_table),
        }
        return reg, source_info

    def get_sql_rewrite_context(
        self,
        *,
        connection_id: str = "",
        since_checkpoint_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """加载 SQL 脚本改写工作台上下文：折叠净映射、表汇总、Checkpoint 列表与可用大模型列表."""
        self._ensure_changelog_tables()
        reg, src_info = self._build_change_registry(
            connection_id=connection_id,
            since_checkpoint_id=since_checkpoint_id,
        )

        risk_counts = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
        tables_summary: Dict[str, Dict[str, Any]] = {}
        rename_count = 0
        type_change_count = 0
        for m in reg.mappings:
            rl = m.risk_level if m.risk_level in risk_counts else "L4"
            risk_counts[rl] += 1
            if m.name_changed:
                rename_count += 1
            if m.type_changed:
                type_change_count += 1
            fqn = m.table_fqn
            t_item = tables_summary.setdefault(
                fqn,
                {
                    "schema_name": m.schema_name,
                    "table_name": m.table_name,
                    "fqn": fqn,
                    "connection_name": m.connection_name,
                    "db_type": m.db_type,
                    "total_mappings": 0,
                    "rename_count": 0,
                    "type_change_count": 0,
                    "highest_risk_level": "L4",
                    "highest_risk_rank": 4,
                },
            )
            t_item["total_mappings"] += 1
            if m.name_changed:
                t_item["rename_count"] += 1
            if m.type_changed:
                t_item["type_change_count"] += 1
            if m.highest_risk_rank < t_item["highest_risk_rank"]:
                t_item["highest_risk_rank"] = m.highest_risk_rank
                t_item["highest_risk_level"] = m.risk_level

        with self.ctx.store.module_db(scope="workspace") as conn:
            checkpoints = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM migration_checkpoints ORDER BY id DESC"
                ).fetchall()
            ]
        eff_cid = (connection_id or "").strip()
        if eff_cid:
            checkpoints = [
                cp
                for cp in checkpoints
                if not str(cp.get("connection_id") or "").strip()
                or cp["connection_id"] == eff_cid
            ]

        llm_models: List[Dict[str, Any]] = []
        try:
            raw_models = self.ctx.llm_hub.list_available_models()
            llm_models = sorted(raw_models, key=_model_sort_key)
        except Exception:
            llm_models = []

        ready_models = [m for m in llm_models if m.get("ready")]
        recommended_model = ready_models[0] if ready_models else None

        return {
            "ok": True,
            "source": src_info,
            "summary": {
                "table_count": len(tables_summary),
                "mapping_count": len(reg.mappings),
                "rename_count": rename_count,
                "type_change_count": type_change_count,
                "risk_counts": risk_counts,
            },
            "tables": sorted(tables_summary.values(), key=lambda x: (x["highest_risk_rank"], x["fqn"])),
            "effective_mappings": [m.to_dict() for m in reg.mappings],
            "checkpoints": checkpoints,
            "connections": self.ctx.connection_hub.list_profiles(),
            "llm_models": llm_models,
            "has_ready_llm": bool(ready_models),
            "recommended_model": recommended_model,
        }

    def analyze_and_rewrite_scripts(
        self,
        *,
        scripts: Sequence[Dict[str, str]],
        connection_id: str = "",
        since_checkpoint_id: Optional[int] = None,
        external_changelog_json: Optional[str] = None,
        default_schema: str = "",
        cte_alias_policy: str = "propagate",
        rewrite_ddl_types: bool = True,
        rewrite_comments: bool = False,
        auto_apply_medium: bool = False,
        mode: str = "deterministic",
        provider_id: str = "",
        model: str = "",
        llm_audit: bool = False,
        dialect: str = "",
        progress_cb: ProgressCallback = noop_progress,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """对单个或多个 SQL 脚本执行「确定性规则引擎 + 可选轻量模型手术刀增强」改写."""
        report = progress_cb or noop_progress
        used_batch_names: Set[str] = set()
        script_list: List[Dict[str, str]] = []
        for i, s in enumerate(scripts or [], start=1):
            raw_name = str(s.get("filename") or f"script_{i}.sql")
            base_safe = _safe_script_filename(raw_name, fallback=f"script_{i}.sql")
            stem, ext = os.path.splitext(base_safe)
            if not ext:
                ext = ".sql"
            cand = f"{stem}{ext}"
            suffix = 2
            while cand.lower() in used_batch_names:
                cand = f"{stem}_{suffix}{ext}"
                suffix += 1
            used_batch_names.add(cand.lower())
            script_list.append(
                {
                    "filename": cand,
                    "original_filename": raw_name,
                    "content": str(s.get("content") or ""),
                }
            )
        if not script_list:
            raise ValueError("请先上传或粘贴至少一个 SQL 脚本")
        if len(script_list) > MAX_BATCH_SCRIPTS:
            raise ValueError(f"单次批量改写上限为 {MAX_BATCH_SCRIPTS} 个脚本文件")

        report(10.0, "加载字段变更台账底座", "正在汇总工作区生效变更链与元数据缓存...", 0, len(script_list))
        registry, source_info = self._build_change_registry(
            connection_id=connection_id,
            since_checkpoint_id=since_checkpoint_id,
            external_changelog_json=external_changelog_json,
        )

        # Infer default dialect from connection or mappings
        eff_dialect = (dialect or "").strip().upper()
        if not eff_dialect and connection_id:
            prof = self.ctx.connection_hub.get_profile(connection_id)
            if prof and prof.db_type:
                eff_dialect = prof.db_type
        if not eff_dialect and registry.mappings:
            eff_dialect = registry.mappings[0].db_type or "DWS"
        if not eff_dialect:
            eff_dialect = "DWS"

        eff_policy = (cte_alias_policy or "propagate").strip().lower()
        if eff_policy not in ("propagate", "preserve_alias"):
            eff_policy = "propagate"
        eff_mode = (mode or "deterministic").strip().lower()
        if eff_mode not in ("deterministic", "hybrid"):
            eff_mode = "deterministic"

        opts = RewriteOptions(
            default_schema=(default_schema or "").strip(),
            cte_alias_policy=eff_policy,
            rewrite_ddl_types=bool(rewrite_ddl_types),
            rewrite_comments=bool(rewrite_comments),
            auto_apply_medium_confidence=bool(auto_apply_medium),
            preserve_quote_style=True,
        )

        total_files = len(script_list)
        file_results: List[Dict[str, Any]] = []
        agg_det_patches = 0
        agg_llm_patches = 0
        agg_selected_patches = 0
        agg_advisories = 0
        agg_changed_files = 0
        agg_total_tokens = 0

        for idx, item in enumerate(script_list, start=1):
            if cancel_check and cancel_check():
                raise ValueError("任务已取消")
            fname = item["filename"]
            sql_text = item["content"]
            base_pct = 15.0 + 75.0 * ((idx - 1) / max(1, total_files))
            report(
                base_pct,
                "确定性词法与作用域解析",
                f"[{idx}/{total_files}] 正在解析 {fname}...",
                idx - 1,
                total_files,
            )

            det_res = rewrite_sql_deterministic(sql_text, registry, opts)

            if eff_mode == "hybrid" and registry.mappings:
                report(
                    min(92.0, base_pct + (75.0 / max(1, total_files)) * 0.5),
                    "AI 手术刀热点增强",
                    f"[{idx}/{total_files}] 正在对 {fname} 热点微切片执行 AI 校验与类型修复...",
                    idx - 1,
                    total_files,
                )
                final_res = enhance_sql_rewrite_with_llm(
                    original_sql=sql_text,
                    det_result=det_res,
                    registry=registry,
                    llm_hub=self.ctx.llm_hub,
                    provider_id=provider_id,
                    model=model,
                    dialect=eff_dialect,
                    llm_audit=bool(llm_audit),
                    cancel_check=cancel_check,
                )
            else:
                final_res = dict(det_res)
                final_res["llm_meta"] = {
                    "used": False,
                    "provider_id": provider_id or "",
                    "model": model or "",
                    "hunks_sent": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "latency_ms": 0,
                    "accepted_patches": 0,
                    "rejected_patches": 0,
                    "rejected_reasons": [],
                    "notes": [],
                    "skipped_reason": (
                        "当前处于纯确定性规则模式（0 Token）"
                        if eff_mode == "deterministic"
                        else "变更台账为空，无需调用模型"
                    ),
                    "error": "",
                }

            patches_objs = final_res.get("patches") or []
            advisories_objs = final_res.get("advisories") or []
            rewritten_sql = str(final_res.get("rewritten_sql") or sql_text)
            diff_text = build_unified_diff(sql_text, rewritten_sql, filename=fname)

            det_cnt = sum(1 for p in patches_objs if p.source == "DETERMINISTIC")
            llm_cnt = sum(1 for p in patches_objs if p.source == "LLM")
            sel_cnt = sum(1 for p in patches_objs if p.selected)
            high_cnt = sum(1 for p in patches_objs if p.confidence == "HIGH")
            med_cnt = sum(1 for p in patches_objs if p.confidence == "MEDIUM")
            low_cnt = sum(1 for p in patches_objs if p.confidence == "LOW")
            err_adv = sum(1 for a in advisories_objs if a.severity == "ERROR")
            warn_adv = sum(1 for a in advisories_objs if a.severity == "WARN")
            changed = rewritten_sql != sql_text

            agg_det_patches += det_cnt
            agg_llm_patches += llm_cnt
            agg_selected_patches += sel_cnt
            agg_advisories += len(advisories_objs)
            agg_total_tokens += int((final_res.get("llm_meta") or {}).get("total_tokens") or 0)
            if changed:
                agg_changed_files += 1

            file_results.append(
                {
                    "script_index": idx - 1,
                    "filename": fname,
                    "original_filename": item.get("original_filename") or fname,
                    "changed": changed,
                    "line_count": len(sql_text.splitlines()),
                    "original_sql": sql_text,
                    "rewritten_sql": rewritten_sql,
                    "unified_diff": diff_text,
                    "touched_tables": final_res.get("touched_tables") or [],
                    "patches": [p.to_dict() for p in patches_objs],
                    "advisories": [a.to_dict() for a in advisories_objs],
                    "llm_meta": final_res.get("llm_meta") or {},
                    "stats": {
                        "total_patches": len(patches_objs),
                        "deterministic_patches": det_cnt,
                        "llm_patches": llm_cnt,
                        "selected_patches": sel_cnt,
                        "high_confidence": high_cnt,
                        "medium_confidence": med_cnt,
                        "low_confidence": low_cnt,
                        "advisory_count": len(advisories_objs),
                        "error_advisories": err_adv,
                        "warn_advisories": warn_adv,
                    },
                }
            )

        msg = (
            f"已完成 {total_files} 份 SQL 脚本改写分析：命中 {agg_det_patches} 处确定性补丁"
            + (f"、{agg_llm_patches} 处 AI 建议" if agg_llm_patches else "")
            + (f"、{agg_advisories} 处类型/风险预警" if agg_advisories else "")
        )
        if not registry.mappings:
            msg = "当前工作区尚无生效中的字段变更日志（可先执行贯标或导入 JSON 变更台账底座）"

        report(98.0, "生成 Diff 与审计清单", msg, total_files, total_files)
        return {
            "ok": True,
            "mode": eff_mode,
            "dialect": eff_dialect,
            "source": source_info,
            "options": {
                "default_schema": opts.default_schema,
                "cte_alias_policy": opts.cte_alias_policy,
                "rewrite_ddl_types": opts.rewrite_ddl_types,
                "rewrite_comments": opts.rewrite_comments,
                "auto_apply_medium": opts.auto_apply_medium_confidence,
                "llm_audit": bool(llm_audit),
            },
            "aggregate": {
                "total_files": total_files,
                "changed_files": agg_changed_files,
                "deterministic_patches": agg_det_patches,
                "llm_patches": agg_llm_patches,
                "selected_patches": agg_selected_patches,
                "advisory_count": agg_advisories,
                "total_tokens": agg_total_tokens,
            },
            "files": file_results,
            "message": msg,
        }

    def export_rewritten_scripts_zip(
        self, files: Sequence[Dict[str, Any]]
    ) -> bytes:
        """将确认后的改写 SQL 脚本与 Diff / Markdown 审计报告打包为 .zip 字节流."""
        if not files:
            raise ValueError("没有可导出的改写脚本")
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        buf = io.BytesIO()
        used_names: Set[str] = set()
        report_lines = [
            "# Data.O.G SQL 脚本落标自动改写审计报告",
            "",
            f"- 导出时间：{now_str}",
            f"- 脚本总数：{len(files)}",
            "",
            "| 脚本文件名 | 采纳改动数 | 预警数 | 状态 |",
            "| :--- | :---: | :---: | :--- |",
        ]
        audit_items: List[Dict[str, Any]] = []

        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for idx, item in enumerate(files, start=1):
                base_name = _safe_script_filename(str(item.get("filename") or f"script_{idx}.sql"))
                stem, ext = os.path.splitext(base_name)
                if not ext:
                    ext = ".sql"
                candidate = f"{stem}{ext}"
                suffix = 2
                while candidate.lower() in used_names:
                    candidate = f"{stem}_{suffix}{ext}"
                    suffix += 1
                used_names.add(candidate.lower())

                rewritten_sql = str(item.get("rewritten_sql") or "")
                diff_txt = str(item.get("unified_diff") or "")
                applied_cnt = int(item.get("applied_patch_count") or 0)
                adv_cnt = int(item.get("advisory_count") or 0)
                changed = bool(item.get("changed"))

                zf.writestr(f"rewritten/{candidate}", rewritten_sql.encode("utf-8"))
                if diff_txt.strip():
                    zf.writestr(f"diffs/{candidate}.diff", diff_txt.encode("utf-8"))

                status_lbl = "已改写" if changed else "无变更"
                report_lines.append(f"| `{candidate}` | {applied_cnt} | {adv_cnt} | {status_lbl} |")
                audit_items.append(
                    {
                        "filename": candidate,
                        "changed": changed,
                        "applied_patch_count": applied_cnt,
                        "advisory_count": adv_cnt,
                    }
                )

            zf.writestr("REWRITE_REPORT.md", "\n".join(report_lines).encode("utf-8"))
            zf.writestr(
                "REWRITE_AUDIT.json",
                json.dumps(
                    {
                        "exported_at": now_str,
                        "file_count": len(audit_items),
                        "files": audit_items,
                    },
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )

        return buf.getvalue()
