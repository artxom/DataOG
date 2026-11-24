"""Data.O.G 智能数据助手物理隔离工具沙箱 (AssistantToolbox).

安全红线：
- 本工具箱仅持有只读底座通道 (`metadata_hub`, `knowledge_hub`, `query_engine.execute_readonly_sql`, `sql_risk_guard`)，
  物理层面上完全不引用 `query_engine.execute_sql`；
- 任何 DDL / DML 写操作均只能通过 `propose_write_sql` 登记为待用户二次确认的提案对象。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from core.platform.models import ConnectionProfile, TableMeta
from core.platform.readonly_guard import ReadOnlyViolationError, validate_readonly_sql
from core.platform.sql_risk_guard import build_affected_rows_probe_sql, classify_sql_batch
from modules.ai_assistant.attachments import query_csv_in_memory

MAX_TOOL_SAMPLE_ROWS = 200
LLM_PREVIEW_ROWS_CAP = 30
_SENSITIVE_KV_RE = re.compile(
    r"(?i)(password|passwd|pwd|api[_-]?key|secret|token)\s*[=:]\s*([^\s,;'\"]+)"
)


def sanitize_error_text(raw_err: Any, secret_values: Optional[List[str]] = None) -> str:
    text = str(raw_err or "未知错误").strip()
    for sec in secret_values or []:
        if sec and len(sec) >= 3 and sec in text:
            text = text.replace(sec, "******")
    text = _SENSITIVE_KV_RE.sub(r"\1=******", text)
    return text[:600]


def _split_table_ref(raw_ref: str, default_schema: str) -> Tuple[str, str]:
    s = (raw_ref or "").strip().strip('"`[]')
    if "." in s:
        parts = [p.strip().strip('"`[]') for p in s.split(".", 1)]
        return (parts[0] or default_schema, parts[1])
    return (default_schema, s)


def _format_table_meta_detailed(meta: TableMeta) -> str:
    header = f"表 `{meta.schema_name}.{meta.table_name}` ({meta.object_type})"
    if meta.table_comment:
        header += f" 注释: {meta.table_comment}"
    extra: List[str] = []
    if meta.primary_keys:
        extra.append(f"PK=[{', '.join(meta.primary_keys)}]")
    if meta.distribute_clause:
        extra.append(f"分布={meta.distribute_clause}")
    elif meta.distribute_keys:
        extra.append(f"分布键=[{', '.join(meta.distribute_keys)}]")
    if meta.partition_clause:
        extra.append(f"分区={meta.partition_clause}")
    if meta.penetrated_base_tables:
        extra.append(f"穿透基表=[{', '.join(meta.penetrated_base_tables[:10])}]")
    if meta.row_count:
        extra.append(f"估算行数≈{meta.row_count}")

    col_lines: List[str] = []
    for c in meta.columns[:100]:
        flags: List[str] = []
        if c.is_primary_key:
            flags.append("PK")
        if not c.nullable:
            flags.append("NOT NULL")
        if c.index_names:
            flags.append(f"IDX:{'/'.join(c.index_names[:3])}")
        flag_str = f" [{' '.join(flags)}]" if flags else ""
        cmt = f" -- {c.column_comment}" if c.column_comment else ""
        col_lines.append(f"  - {c.column_name} {c.data_type}{flag_str}{cmt}")
    if len(meta.columns) > 100:
        col_lines.append(f"  ... (其余 {len(meta.columns) - 100} 列已省略)")

    parts = [header]
    if extra:
        parts.append("元属性: " + " | ".join(extra))
    parts.append("字段清单:\n" + "\n".join(col_lines))
    if meta.raw_ddl:
        parts.append("参考 DDL 片段:\n" + meta.raw_ddl[:1200])
    elif meta.view_definition:
        parts.append("视图定义片段:\n" + meta.view_definition[:1000])
    return "\n".join(parts)


class AssistantToolbox:
    """供 Agent 单轮推理循环调度的 7 个安全工具封装."""

    def __init__(
        self,
        *,
        ctx: Any,
        work_mode: str,
        profile: Optional[ConnectionProfile],
        dialect: str,
        default_schema: str,
        sample_limit: int = 100,
        attachments: Optional[List[Dict[str, Any]]] = None,
        storage: Any = None,
    ) -> None:
        self.ctx = ctx
        self.work_mode = work_mode  # connection | dialect
        self.profile = profile
        self.dialect = dialect
        self.default_schema = default_schema
        self.sample_limit = max(10, min(MAX_TOOL_SAMPLE_ROWS, int(sample_limit or 100)))
        self.secret_values = [profile.password] if (profile and profile.password) else []
        self.storage = storage
        self.attachments_map: Dict[str, Dict[str, Any]] = {
            str(att.get("attachment_id") or ""): att
            for att in (attachments or [])
            if isinstance(att, dict) and att.get("attachment_id")
        }

        self.citations_map: Dict[str, Dict[str, Any]] = {}
        self.latest_result_table: Optional[Dict[str, Any]] = None
        self.pending_proposal: Optional[Dict[str, Any]] = None

    def _resolve_target_profile(self, connection_id: str = "") -> Optional[ConnectionProfile]:
        cid = (connection_id or "").strip()
        if cid and (not self.profile or cid != self.profile.connection_id):
            found = self.ctx.connection_hub.get_profile(cid)
            if found is not None:
                if found.password and found.password not in self.secret_values:
                    self.secret_values.append(found.password)
                return found
        return self.profile

    def dispatch(self, tool_name: str, args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        name = (tool_name or "").strip().lower()
        params = args if isinstance(args, dict) else {}

        handlers = {
            "search_catalog": self.tool_search_catalog,
            "inspect_table": self.tool_inspect_table,
            "search_knowledge": self.tool_search_knowledge,
            "read_knowledge_section": self.tool_read_knowledge_section,
            "run_readonly_sql": self.tool_run_readonly_sql,
            "explain_sql": self.tool_explain_sql,
            "propose_write_sql": self.tool_propose_write_sql,
            "search_attachment": self.tool_search_attachment,
            "read_attachment_lines": self.tool_read_attachment_lines,
            "inspect_attachment_chunk": self.tool_inspect_attachment_chunk,
            "query_csv_attachment": self.tool_query_csv_attachment,
        }
        fn = handlers.get(name)
        if fn is None:
            return {
                "ok": False,
                "error": f"未知工具 `{tool_name}`，可用工具: {', '.join(sorted(handlers.keys()))}",
            }
        try:
            return fn(params)
        except Exception as exc:
            return {
                "ok": False,
                "error": sanitize_error_text(exc, self.secret_values),
            }

    def tool_search_catalog(self, params: Dict[str, Any]) -> Dict[str, Any]:
        prof = self._resolve_target_profile(str(params.get("connection_id") or ""))
        if prof is None:
            return {
                "ok": False,
                "error": "当前处于离线纯方言模式，未绑定实时数据源连接，无法检索在线库表目录。",
            }
        kw = str(params.get("keyword") or "").strip()
        schema_filter = str(params.get("schema_name") or "").strip().lower()
        limit = max(1, min(60, int(params.get("limit") or 30)))

        if kw:
            hits = self.ctx.metadata_hub.search_objects(
                keyword=kw, connection_id=prof.connection_id, limit=limit * 2
            )
        else:
            tree = self.ctx.metadata_hub.get_catalog_tree(
                prof.connection_id, auto_refresh=False
            )
            hits = list(tree.get("tables") or [])

        if schema_filter:
            hits = [
                h for h in hits if str(h.get("schema_name") or "").lower() == schema_filter
            ]
        compact = [
            {
                "schema_name": h.get("schema_name", ""),
                "table_name": h.get("table_name", ""),
                "object_type": h.get("object_type", "TABLE"),
                "table_comment": h.get("table_comment", ""),
            }
            for h in hits[:limit]
        ]
        return {
            "ok": True,
            "connection_name": prof.name,
            "db_type": prof.db_type,
            "count": len(compact),
            "objects": compact,
        }

    def tool_inspect_table(self, params: Dict[str, Any]) -> Dict[str, Any]:
        prof = self._resolve_target_profile(str(params.get("connection_id") or ""))
        if prof is None:
            return {
                "ok": False,
                "error": "当前处于离线纯方言模式，无法连接数据库探查表结构。",
            }
        raw_ref = str(
            params.get("table_ref") or params.get("table_name") or ""
        ).strip()
        if not raw_ref:
            return {"ok": False, "error": "缺少参数 `table_ref`"}
        def_schema = str(params.get("schema_name") or "").strip() or prof.effective_default_schema()
        sch, tbl = _split_table_ref(raw_ref, def_schema)
        meta = self.ctx.metadata_hub.get_table_metadata(
            prof.connection_id,
            schema_name=sch,
            table_name=tbl,
            force_refresh=bool(params.get("force_refresh", False)),
            penetrate_views=True,
        )
        if not meta.exists:
            return {
                "ok": False,
                "error": f"在连接 [{prof.name}] 中未找到表 `{sch}.{tbl}`，建议先调用 search_catalog 核实准确表名。",
            }
        return {
            "ok": True,
            "schema_name": meta.schema_name,
            "table_name": meta.table_name,
            "object_type": meta.object_type,
            "detail": _format_table_meta_detailed(meta),
        }

    def tool_search_knowledge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "缺少检索关键词 `query`"}
        req_dialect = str(params.get("dialect") or self.dialect or "GENERIC").strip().upper()
        top_k = max(1, min(10, int(params.get("top_k") or 5)))
        res = self.ctx.knowledge_hub.search(
            query, dialect=req_dialect, top_k=top_k, include_builtin=True
        )
        hits = res.get("hits") or []
        for h in hits:
            cid = str(h.get("chunk_id") or "")
            if cid:
                self.citations_map[cid] = {
                    "chunk_id": cid,
                    "doc_id": h.get("doc_id", ""),
                    "doc_title": h.get("doc_title", ""),
                    "dialect": h.get("dialect", ""),
                    "heading_path": h.get("heading_path", ""),
                    "snippet": h.get("snippet", ""),
                    "is_builtin": bool(h.get("is_builtin")),
                }
        return {
            "ok": True,
            "dialect": res.get("dialect"),
            "retrieval_mode": res.get("retrieval_mode"),
            "hit_count": len(hits),
            "hits": [
                {
                    "chunk_id": h["chunk_id"],
                    "doc_title": h["doc_title"],
                    "dialect": h["dialect"],
                    "is_builtin": h["is_builtin"],
                    "heading_path": h["heading_path"],
                    "snippet": h["snippet"],
                    "content": h["content"][:900],
                }
                for h in hits
            ],
        }

    def tool_read_knowledge_section(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(params.get("chunk_id") or "").strip()
        if not cid:
            return {"ok": False, "error": "缺少 `chunk_id`"}
        win = max(0, min(3, int(params.get("window") or 1)))
        sec = self.ctx.knowledge_hub.read_section(cid, window=win)
        if sec is None:
            return {"ok": False, "error": f"未找到知识片段 `{cid}`"}
        doc = sec.get("document") or {}
        sections = sec.get("sections") or []
        for s in sections:
            scid = str(s.get("chunk_id") or "")
            if scid and scid not in self.citations_map:
                self.citations_map[scid] = {
                    "chunk_id": scid,
                    "doc_id": doc.get("doc_id", ""),
                    "doc_title": doc.get("title", ""),
                    "dialect": doc.get("dialect", ""),
                    "heading_path": s.get("heading_path", ""),
                    "snippet": str(s.get("content") or "")[:200],
                    "is_builtin": bool(doc.get("is_builtin")),
                }
        return {
            "ok": True,
            "doc_title": doc.get("title", ""),
            "dialect": doc.get("dialect", ""),
            "sections": [
                {
                    "chunk_id": s["chunk_id"],
                    "heading_path": s["heading_path"],
                    "content": str(s["content"])[:2200],
                }
                for s in sections
            ],
            "outline_headings": [
                {"chunk_id": o["chunk_id"], "heading_path": o["heading_path"]}
                for o in (sec.get("outline") or [])[:25]
            ],
        }

    def tool_run_readonly_sql(self, params: Dict[str, Any]) -> Dict[str, Any]:
        prof = self._resolve_target_profile(str(params.get("connection_id") or ""))
        if prof is None:
            return {
                "ok": False,
                "error": "当前处于离线纯方言模式，未连接数据库，无法执行 SQL。可专注于方言语法分析或输出提案语句。",
            }
        sql_text = str(params.get("sql") or "").strip()
        if not sql_text:
            return {"ok": False, "error": "SQL 不能为空"}
        req_limit = max(1, min(self.sample_limit, int(params.get("limit") or self.sample_limit)))
        try:
            qres = self.ctx.query_engine.execute_readonly_sql(
                prof.connection_id,
                sql_text,
                limit=req_limit,
                max_limit_cap=MAX_TOOL_SAMPLE_ROWS,
                timeout_sec=12,
            )
        except ReadOnlyViolationError as vio:
            return {
                "ok": False,
                "blocked_by_sandbox": True,
                "error": (
                    f"只读沙箱已拦截非只读语句: {vio}。"
                    "若你需要生成 DDL 或数据修改 (INSERT/UPDATE/DELETE) 语句，请改用 `propose_write_sql` 提交待确认卡片，禁止直接执行！"
                ),
            }
        except Exception as exc:
            err_msg = sanitize_error_text(exc, self.secret_values)
            return {
                "ok": False,
                "sql_error": err_msg,
                "error": (
                    f"只读 SQL 执行失败: {err_msg}。"
                    "请结合 `inspect_table` 或 `search_knowledge` 核对字段名与方言语法后自我修正并重试。"
                ),
            }

        col_names = [c.name for c in qres.columns]
        preview_rows = qres.rows[:LLM_PREVIEW_ROWS_CAP]
        self.latest_result_table = {
            "connection_id": prof.connection_id,
            "connection_name": prof.name,
            "dialect": prof.db_type,
            "sql": qres.sql,
            "columns": [c.to_dict() for c in qres.columns],
            "rows": qres.rows,
            "row_count": qres.row_count,
            "truncated": qres.truncated,
            "elapsed_ms": round(qres.elapsed_ms, 1),
        }
        return {
            "ok": True,
            "sql": qres.sql,
            "columns": col_names,
            "rows": preview_rows,
            "row_count": qres.row_count,
            "truncated": qres.truncated,
            "elapsed_ms": round(qres.elapsed_ms, 1),
            "note": (
                f"共返回 {qres.row_count} 行（向大模型展示前 {len(preview_rows)} 行）"
                if qres.row_count > len(preview_rows)
                else f"共返回 {qres.row_count} 行"
            ),
        }

    def tool_explain_sql(self, params: Dict[str, Any]) -> Dict[str, Any]:
        prof = self._resolve_target_profile(str(params.get("connection_id") or ""))
        if prof is None:
            return {
                "ok": False,
                "error": "当前处于离线纯方言模式，无法获取实时执行计划。请基于知识库与方言规则提供静态 SQL 优化建议。",
            }
        raw_sql = str(params.get("sql") or "").strip()
        if not raw_sql:
            return {"ok": False, "error": "缺少待分析的 SQL"}
        try:
            validated = validate_readonly_sql(raw_sql, db_type=prof.db_type)
        except ReadOnlyViolationError as vio:
            return {
                "ok": False,
                "error": f"EXPLAIN 仅支持纯只读 SELECT/WITH 语句（且严禁 EXPLAIN ANALYZE）: {vio}",
            }

        upper_head = validated.lstrip().upper()
        if upper_head.startswith("EXPLAIN"):
            explain_sql = validated
        elif prof.db_type == "SQLITE":
            explain_sql = f"EXPLAIN QUERY PLAN {validated}"
        else:
            explain_sql = f"EXPLAIN {validated}"

        try:
            qres = self.ctx.query_engine.execute_readonly_sql(
                prof.connection_id,
                explain_sql,
                limit=120,
                max_limit_cap=200,
                timeout_sec=12,
            )
            return {
                "ok": True,
                "explain_sql": explain_sql,
                "columns": [c.name for c in qres.columns],
                "plan_rows": qres.rows[:80],
                "elapsed_ms": round(qres.elapsed_ms, 1),
            }
        except Exception as exc:
            err_msg = sanitize_error_text(exc, self.secret_values)
            return {
                "ok": False,
                "error": f"获取执行计划失败 ({err_msg})，请结合 `inspect_table` 获取的索引与分布键信息做静态性能分析。",
            }

    def tool_propose_write_sql(self, params: Dict[str, Any]) -> Dict[str, Any]:
        prof = self._resolve_target_profile(str(params.get("connection_id") or ""))
        eff_dialect = prof.db_type if prof is not None else self.dialect
        sql_text = str(params.get("sql") or "").strip()
        if not sql_text:
            return {"ok": False, "error": "提案 SQL 不能为空"}

        try:
            assessment = classify_sql_batch(sql_text, db_type=eff_dialect)
        except Exception as exc:
            return {"ok": False, "error": f"SQL 词法校验失败: {exc}"}

        if assessment.risk_level == "READONLY":
            return {
                "ok": False,
                "error": "检测到该 SQL 属于纯只读查询，请直接调用 `run_readonly_sql` 获取结果，无需生成写操作确认卡片。",
            }

        # 在线连库模式下，若含单表 DELETE/UPDATE，尝试安全预估受影响行数
        estimated_rows: Optional[int] = None
        if prof is not None:
            est_sum = 0
            probed_any = False
            for r in assessment.statement_risks:
                if r.primary_verb not in ("DELETE", "UPDATE"):
                    continue
                probe_sql = build_affected_rows_probe_sql(r.sql, db_type=prof.db_type)
                if not probe_sql:
                    continue
                try:
                    qres = self.ctx.query_engine.execute_readonly_sql(
                        prof.connection_id,
                        probe_sql,
                        limit=1,
                        max_limit_cap=5,
                        timeout_sec=5,
                    )
                    if qres.rows and qres.rows[0]:
                        est_sum += int(qres.rows[0][0] or 0)
                        probed_any = True
                except Exception:
                    continue
            if probed_any:
                estimated_rows = est_sum

        target_schema = (
            str(params.get("target_schema") or "").strip()
            or (prof.effective_default_schema() if prof else self.default_schema)
        )
        title = str(params.get("title") or "").strip() or f"{eff_dialect} {assessment.category} 待确认语句"
        rationale = str(params.get("rationale") or "").strip()
        formatted_sql = ";\n\n".join(assessment.statements) + ";"

        self.pending_proposal = {
            "connection_id": prof.connection_id if prof else "",
            "dialect": eff_dialect,
            "schema_name": target_schema,
            "title": title[:120],
            "rationale": rationale[:800],
            "original_sql": formatted_sql,
            "assessment": assessment.to_dict(),
            "estimated_rows": estimated_rows,
        }
        return {
            "ok": True,
            "status": "PENDING_USER_CONFIRMATION",
            "risk_level": assessment.risk_level,
            "category": assessment.category,
            "statement_count": assessment.statement_count,
            "estimated_rows": estimated_rows,
            "warnings": assessment.warnings,
            "note": (
                "已登记为待确认执行提案（系统绝对不会自动执行）。"
                "请立即使用 `answer` 或 `propose_write` 向用户清晰说明变更意图、影响与注意事项，等待用户在卡片中二次确认。"
            ),
        }

    def _resolve_attachment(self, raw_aid: str = "", *, only_csv: bool = False) -> Optional[Dict[str, Any]]:
        aid = (raw_aid or "").strip()
        if aid:
            if aid in self.attachments_map:
                att = self.attachments_map[aid]
                if not only_csv or att.get("file_kind") == "csv":
                    return att
            if self.storage is not None:
                found = self.storage.get_attachment(aid, include_raw=False, include_chunks=False)
                if found and (not only_csv or found.get("file_kind") == "csv"):
                    return found
            return None
        candidates = [
            a
            for a in self.attachments_map.values()
            if (not only_csv or a.get("file_kind") == "csv")
        ]
        if len(candidates) == 1:
            return candidates[0]
        return candidates[-1] if only_csv and candidates else None

    def tool_search_attachment(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.storage is None:
            return {"ok": False, "error": "附件检索存储尚未就绪"}
        att = self._resolve_attachment(str(params.get("attachment_id") or ""))
        if att is None:
            return {"ok": False, "error": "未找到指定的附件，请核对 `attachment_id`"}
        kw = str(params.get("keyword") or "").strip()
        if not kw:
            return {"ok": False, "error": "缺少检索关键词 `keyword`"}
        regex = bool(params.get("regex", False))
        try:
            ctx_lines = max(0, min(6, int(params.get("context_lines", 2))))
        except Exception:
            ctx_lines = 2
        limit = max(1, min(30, int(params.get("limit") or 12)))
        res = self.storage.search_attachment_content(
            att["attachment_id"],
            kw,
            regex=regex,
            context_lines=ctx_lines,
            limit=limit,
        )
        if res is None:
            return {"ok": False, "error": "附件不存在或已被移除"}
        return {"ok": True, **res}

    def tool_read_attachment_lines(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.storage is None:
            return {"ok": False, "error": "附件存储尚未就绪"}
        att = self._resolve_attachment(str(params.get("attachment_id") or ""))
        if att is None:
            return {"ok": False, "error": "未找到指定的附件，请核对 `attachment_id`"}
        try:
            start_line = max(1, int(params.get("start_line") or 1))
        except Exception:
            start_line = 1
        try:
            raw_end = params.get("end_line")
            end_line = int(raw_end) if raw_end is not None else (start_line + 249)
        except Exception:
            end_line = start_line + 249
        res = self.storage.read_attachment_lines(
            att["attachment_id"],
            start_line=start_line,
            end_line=end_line,
            max_lines=400,
            max_tokens=10000,
        )
        if res is None:
            return {"ok": False, "error": "附件不存在或已被移除"}
        return {
            "ok": True,
            **res,
            "note": f"已读取 {res['filename']} 第 {res['start_line']}~{res['end_line']} 行 (共 {res['total_lines']} 行)",
        }

    def tool_inspect_attachment_chunk(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.storage is None:
            return {"ok": False, "error": "附件分片存储尚未就绪"}
        att = self._resolve_attachment(str(params.get("attachment_id") or ""))
        if att is None:
            return {"ok": False, "error": "未找到指定的附件，请核对 `attachment_id`"}
        try:
            c_idx = int(params.get("chunk_index", 0))
        except Exception:
            c_idx = 0
        win = max(0, min(2, int(params.get("window") or 0)))
        res = self.storage.get_attachment_chunk(att["attachment_id"], c_idx, window=win)
        if res is None:
            return {
                "ok": False,
                "error": f"未找到附件 `{att['filename']}` 的第 {c_idx} 号分片（有效范围 0..{max(0, att.get('chunk_count', 1) - 1)}）",
            }
        return {
            "ok": True,
            "attachment_id": att["attachment_id"],
            "filename": att["filename"],
            "chunks": [
                {
                    "chunk_index": ch["chunk_index"],
                    "title": ch["title"],
                    "range_label": ch["range_label"],
                    "content": ch["content"],
                }
                for ch in res["chunks"]
            ],
        }

    def tool_query_csv_attachment(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.storage is None:
            return {"ok": False, "error": "CSV 查询存储尚未就绪"}
        att = self._resolve_attachment(str(params.get("attachment_id") or ""), only_csv=True)
        if att is None:
            return {"ok": False, "error": "未找到可查询的 CSV/TSV 附件，请核对 `attachment_id`"}
        raw_att = self.storage.get_attachment(att["attachment_id"], include_raw=True)
        if raw_att is None or raw_att.get("file_kind") != "csv":
            return {"ok": False, "error": "仅支持对 CSV/TSV 附件执行只读 SQL 查询"}
        sql_text = str(params.get("sql") or "").strip()
        if not sql_text:
            return {"ok": False, "error": "缺少查询 SQL（表名固定写作 `csv_data`）"}
        req_limit = max(1, min(self.sample_limit, int(params.get("limit") or 50)))
        try:
            qres = query_csv_in_memory(
                raw_att["raw_text"],
                raw_att["file_ext"],
                sql_text,
                limit=req_limit,
            )
        except ReadOnlyViolationError as vio:
            return {
                "ok": False,
                "blocked_by_sandbox": True,
                "error": f"CSV 只读查询沙箱已拦截非只读语句: {vio}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"CSV SQL 查询失败: {sanitize_error_text(exc)}（提示：表名固定为 csv_data，含中文/空格列名请用双引号包裹）",
            }

        self.latest_result_table = {
            "connection_id": "",
            "connection_name": f"CSV附件:{raw_att['filename']}",
            "dialect": "SQLITE",
            "sql": qres["sql"],
            "columns": qres["columns"],
            "rows": qres["rows"],
            "row_count": qres["row_count"],
            "truncated": qres["truncated"],
            "elapsed_ms": qres["elapsed_ms"],
        }
        preview_rows = qres["rows"][:LLM_PREVIEW_ROWS_CAP]
        return {
            "ok": True,
            "attachment_id": raw_att["attachment_id"],
            "filename": raw_att["filename"],
            "sql": qres["sql"],
            "columns": [c["name"] for c in qres["columns"]],
            "rows": preview_rows,
            "row_count": qres["row_count"],
            "truncated": qres["truncated"],
            "elapsed_ms": qres["elapsed_ms"],
            "note": (
                f"CSV 内存只读查询返回 {qres['row_count']} 行（向大模型展示前 {len(preview_rows)} 行）"
                if qres["row_count"] > len(preview_rows)
                else f"CSV 内存只读查询共返回 {qres['row_count']} 行"
            ),
        }
