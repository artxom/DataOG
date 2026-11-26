"""SQL 脚本自动改写 Stage 2：面向轻量廉价大模型（如 DeepSeek-v4-flash）的手术刀级混合增强器.

核心设计原则（低成本、低延迟、零幻觉防线）：
1. 仅微切片送审（Micro-Hunk Scoped Prompting）：绝不将整份超长 SQL 丢给模型重写；
   仅提取 Stage 1 确定性引擎标记的歧义列、L2 类型冲突表达式、USING 冲突、动态 SQL 命中等热点周边的 ±8 行上下文；
2. 严苛结构化 JSON 补丁协议：使用 `temperature=0.0`、`thinking_mode="off"` 驱动轻量模型快速输出极简局部补丁；
3. 五道确定性反幻觉门禁：逐条核验原始片段 verbatim 定位、禁止撤回已确认的高置信改名、目标列白名单校验、
   引号/括号语法平衡校验与替换膨胀上限校验；所有 AI 补丁均清晰标注 `source="LLM"` 与理由供用户一键勾选审校。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from modules.standard_migrator.sql_rewriter import (
    ChangeRegistry,
    RewriteAdvisory,
    RewritePatch,
    _SQL_KEYWORDS,
    _build_line_offsets,
    _offset_to_line_col,
    apply_patches,
    build_unified_diff,
)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.I)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_SINGLE_QUOTED_STR_RE = re.compile(r"'(?:''|[^'])*'")

_ALLOWED_LLM_CATEGORIES: Set[str] = {
    "TYPE_ADAPT",
    "AMBIGUOUS_RENAME",
    "DYNAMIC_SQL",
    "ALIAS_FIX",
    "USING_REWRITE",
}

_FORBIDDEN_LLM_ADDED_KEYWORDS: Set[str] = {
    "DROP", "TRUNCATE", "DELETE", "ALTER", "CREATE", "GRANT", "REVOKE",
    "EXECUTE", "EXEC", "CALL", "COMMIT", "ROLLBACK", "INSERT", "UPDATE",
    "MERGE", "UNION", "INTERSECT", "EXCEPT", "COPY", "LOAD", "ATTACH",
    "DETACH", "VACUUM", "PRAGMA", "SHUTDOWN", "KILL", "DO",
}

_SAFE_SQL_CAST_WORDS: Set[str] = (_SQL_KEYWORDS - _FORBIDDEN_LLM_ADDED_KEYWORDS) | {
    "COALESCE", "NVL", "IFNULL", "NULLIF", "TO_CHAR", "TO_NUMBER", "TO_DATE",
    "TO_TIMESTAMP", "TRIM", "LTRIM", "RTRIM", "SUBSTR", "SUBSTRING", "LENGTH",
    "UPPER", "LOWER", "REPLACE", "ROUND", "TRUNC", "FLOOR", "CEIL", "ABS",
    "STR_TO_DATE", "DATE_FORMAT", "DATE_TRUNC", "TRY_CAST", "SAFE_CAST",
    "CONCAT", "CONCAT_WS", "LPAD", "RPAD", "SPLIT_PART", "REGEXP_REPLACE",
}


def _extract_hunks(
    lines: Sequence[str],
    focus_lines: Sequence[int],
    *,
    radius: int = 8,
    max_hunks: int = 8,
    max_total_lines: int = 160,
) -> List[Tuple[int, int]]:
    """将热点行号合并为不重叠的 1-based [start_line, end_line] 微切片列表."""
    total = len(lines)
    if total == 0 or not focus_lines:
        return []
    intervals = sorted(
        (max(1, ln - radius), min(total, ln + radius))
        for ln in focus_lines
        if 1 <= ln <= total
    )
    if not intervals:
        return []

    merged: List[Tuple[int, int]] = [intervals[0]]
    for s, e in intervals[1:]:
        ps, pe = merged[-1]
        if s <= pe + 2:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))

    selected: List[Tuple[int, int]] = []
    used_lines = 0
    for s, e in merged:
        span = e - s + 1
        if len(selected) >= max_hunks:
            break
        if used_lines + span > max_total_lines and selected:
            break
        selected.append((s, e))
        used_lines += span
    return selected


def _parse_llm_json_payload(raw_text: str) -> Dict[str, Any]:
    txt = str(raw_text or "").strip()
    if not txt:
        return {}
    # 1. Direct JSON parse
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 2. Markdown ```json ... ``` fenced block
    m = _JSON_BLOCK_RE.search(txt)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # 3. First balanced `{ ... }` substring
    first = txt.find("{")
    last = txt.rfind("}")
    if first != -1 and last > first:
        try:
            obj = json.loads(txt[first : last + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def _check_balanced_syntax(orig_snippet: str, new_snippet: str) -> Tuple[bool, str]:
    """校验引号/括号平衡，并阻断新注入语句分隔符 `;` 或注释符 `--`/`/*` 截断 SQL."""

    def _stats(s: str) -> Tuple[int, bool, bool, bool, int, int, int]:
        in_sq = False
        in_dq = False
        in_blk = False
        depth = 0
        semis = 0
        line_cmts = 0
        blk_cmts = 0
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if in_blk:
                if ch == "*" and i + 1 < n and s[i + 1] == "/":
                    in_blk = False
                    i += 2
                    continue
                i += 1
                continue
            if in_sq:
                if ch == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    in_sq = False
                i += 1
                continue
            if in_dq:
                if ch == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2
                        continue
                    in_dq = False
                i += 1
                continue
            if ch == "-" and i + 1 < n and s[i + 1] == "-":
                line_cmts += 1
                nl = s.find("\n", i + 2)
                if nl == -1:
                    break
                i = nl + 1
                continue
            if ch == "/" and i + 1 < n and s[i + 1] == "*":
                blk_cmts += 1
                in_blk = True
                i += 2
                continue
            if ch == "'":
                in_sq = True
            elif ch == '"':
                in_dq = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ";":
                semis += 1
            i += 1
        return depth, in_sq, in_dq, in_blk, semis, line_cmts, blk_cmts

    o_depth, o_sq, o_dq, o_blk, o_semis, o_lcmt, o_bcmt = _stats(orig_snippet)
    n_depth, n_sq, n_dq, n_blk, n_semis, n_lcmt, n_bcmt = _stats(new_snippet)
    if o_sq != n_sq:
        return False, "单引号未闭合"
    if o_dq != n_dq:
        return False, "双引号未闭合"
    if n_blk and not o_blk:
        return False, "块注释未闭合"
    if o_depth != n_depth:
        return False, "圆括号层级不平衡"
    if n_semis > o_semis:
        return False, "禁止在局部补丁中注入新的语句分隔符 `;`"
    if n_lcmt > o_lcmt or n_bcmt > o_bcmt:
        return False, "禁止在局部补丁中注入新的 SQL 注释"
    return True, ""


def _locate_verbatim_near_line(
    sql: str,
    offsets: List[int],
    target_line: int,
    snippet: str,
    *,
    window_radius: int = 3,
    occupied_spans: Sequence[Tuple[int, int]] = (),
) -> Optional[Tuple[int, int, int, int, str, bool]]:
    """在 target_line ± window_radius 窗口内寻找 snippet 的 verbatim 字符偏移（兼容 CRLF 并避让已占区间）.

    返回 (start, end, line, col, matched_text, used_crlf_adapt).
    """
    total_lines = len(offsets)
    if not snippet or total_lines == 0:
        return None
    eff_line = max(1, min(total_lines, int(target_line or 1)))
    win_s = max(1, eff_line - window_radius)
    win_e = min(total_lines, eff_line + window_radius + snippet.count("\n"))
    c_start = offsets[win_s - 1]
    c_end = offsets[win_e] if win_e < total_lines else len(sql)
    window = sql[c_start:c_end]

    candidates_to_try: List[Tuple[str, bool]] = [(snippet, False)]
    if "\r\n" in window and "\n" in snippet and "\r\n" not in snippet:
        candidates_to_try.append((snippet.replace("\n", "\r\n"), True))

    for needle, used_crlf in candidates_to_try:
        best_abs = -1
        best_dist = 10**9
        search_from = 0
        while True:
            pos = window.find(needle, search_from)
            if pos == -1:
                break
            abs_pos = c_start + pos
            abs_end = abs_pos + len(needle)
            overlaps = any(not (abs_end <= os_ or abs_pos >= oe_) for os_, oe_ in occupied_spans)
            if not overlaps:
                ln, _ = _offset_to_line_col(offsets, abs_pos)
                dist = abs(ln - eff_line)
                if dist < best_dist:
                    best_dist = dist
                    best_abs = abs_pos
            search_from = pos + 1

        if best_abs != -1:
            ln, cl = _offset_to_line_col(offsets, best_abs)
            return best_abs, best_abs + len(needle), ln, cl, needle, used_crlf

    return None


def _extract_relevant_mappings_text(
    registry: ChangeRegistry,
    touched_tables: Sequence[str],
    advisories: Sequence[RewriteAdvisory],
) -> str:
    """仅筛选与当前脚本/热点表相关的变更映射，最大限度压缩 Prompt Token."""
    wanted_tables: Set[str] = set()
    for t in touched_tables:
        if t:
            wanted_tables.add(t.lower())
            if "." in t:
                wanted_tables.add(t.split(".")[-1].lower())
    for a in advisories:
        if a.table_name:
            for part in a.table_name.split(","):
                p = part.strip().lower()
                if p:
                    wanted_tables.add(p)

    rows: List[str] = []
    for m in registry.mappings:
        fqn_l = m.table_fqn.lower()
        tbl_l = m.table_name.lower()
        if wanted_tables and fqn_l not in wanted_tables and tbl_l not in wanted_tables:
            continue
        hist_str = f" (历史曾用名: {','.join(m.historical_names)})" if len(m.historical_names) > 1 else ""
        rows.append(
            f"- {m.table_fqn}: `{m.original_col_name}` ({m.original_col_type or '?'}) "
            f"-> `{m.current_col_name}` ({m.current_col_type or '?'}) [{m.risk_level}]{hist_str}"
        )
        if len(rows) >= 80:
            break

    if not rows:
        for m in registry.mappings[:50]:
            rows.append(
                f"- {m.table_fqn}: `{m.original_col_name}` ({m.original_col_type or '?'}) "
                f"-> `{m.current_col_name}` ({m.current_col_type or '?'}) [{m.risk_level}]"
            )
    return "\n".join(rows)


def _build_surgical_messages(
    stage1_sql: str,
    hunks: Sequence[Tuple[int, int]],
    advisories: Sequence[RewriteAdvisory],
    mappings_text: str,
    dialect: str = "DWS",
    llm_audit: bool = False,
) -> List[Dict[str, str]]:
    lines = stage1_sql.splitlines()
    hunk_blocks: List[str] = []
    for idx, (s, e) in enumerate(hunks, start=1):
        numbered = "\n".join(f"L{ln}| {lines[ln - 1]}" for ln in range(s, e + 1))
        hunk_blocks.append(f"### 切片 #{idx} (L{s}-L{e})\n```sql\n{numbered}\n```")

    adv_lines: List[str] = []
    for a in advisories:
        if not a.needs_llm and not llm_audit:
            continue
        adv_lines.append(
            f"- L{a.line} [{a.category}/{a.risk_level}] {a.schema_name}.{a.table_name}.{a.column_name}: {a.message}"
        )
    adv_text = "\n".join(adv_lines[:40]) or "- （常规复核切片，确认是否存在遗漏改名或类型不兼容表达式）"

    sys_prompt = (
        "你是 Data.O.G 企业级数据治理 SQL 手术刀改写审核引擎。"
        "前置确定性规则引擎已完成全部明确字段的精准改名；你只需针对下方提供的少量热点微切片，处理以下四类问题：\n"
        "1. TYPE_ADAPT：字段发生 L2 类型转换（如 varchar→numeric/bigint/date 或反向）导致的表达式报错修复"
        "（例如 `NVL(t.amt, '')` → `NVL(t.amt, 0)`、`WHERE t.id = '1001'` → `WHERE t.id = 1001`、"
        "`TRIM(t.num_col)` → `TRIM(CAST(t.num_col AS VARCHAR))`、`col = ''` → `col IS NULL`）；\n"
        "2. AMBIGUOUS_RENAME：多表联查未加前缀的歧义字段或未写 Schema 的歧义表，根据语义与上下文确认应改名时给出极简替换；\n"
        "3. USING_REWRITE：`JOIN ... USING (old_col)` 仅一侧改名时，改写为显式 `ON` 关联条件；\n"
        "4. DYNAMIC_SQL：动态 SQL 字符串字面量（如 EXECUTE IMMEDIATE '...'）内部引用已改名旧字段的精准替换。\n\n"
        "【严苛铁律（违反任意一条将被自动丢弃）】：\n"
        "- 仅输出合法 JSON 对象，禁止任何多余解释文本。\n"
        "- `original_snippet` 必须与切片中对应行（不含 `Lxx| ` 行号前缀）一字不差完全匹配，且尽量短小精准（仅包含需要改动的表达式）。\n"
        "- 严禁发明变更台账中不存在的新字段名；严禁撤回规则引擎已经改好的新字段名；严禁无故改写注释或格式化代码。\n"
        "- 若某条预警无需改代码或信息不足无法确认，不要生成补丁，只需在 `notes` 中简要提示。\n\n"
        "JSON 输出格式：\n"
        '{"patches": [{"line": 12, "original_snippet": "...", "rewritten_snippet": "...", '
        '"category": "TYPE_ADAPT|AMBIGUOUS_RENAME|DYNAMIC_SQL|ALIAS_FIX|USING_REWRITE", '
        '"confidence": "HIGH|MEDIUM|LOW", "reason": "中文简明理由", "mapping_ref": "schema.table.col"}], "notes": ["..."]}'
    )

    user_prompt = (
        f"目标数据库方言：{dialect or 'DWS'}\n\n"
        f"## 相关字段变更台账（唯一事实来源）\n{mappings_text}\n\n"
        f"## 待处理热点预警\n{adv_text}\n\n"
        f"## 待审微切片（已应用确定性高置信改名，行首 Lxx| 为真实行号）\n"
        + "\n\n".join(hunk_blocks)
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]


def enhance_sql_rewrite_with_llm(
    *,
    original_sql: str,
    det_result: Dict[str, Any],
    registry: ChangeRegistry,
    llm_hub: Any,
    provider_id: str = "",
    model: str = "",
    dialect: str = "DWS",
    llm_audit: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """在确定性结果之上运行手术刀级轻量模型增强，并通过五道反幻觉门禁校验."""
    patches: List[RewritePatch] = list(det_result.get("patches") or [])
    advisories: List[RewriteAdvisory] = list(det_result.get("advisories") or [])
    touched_tables: List[str] = list(det_result.get("touched_tables") or [])

    llm_meta: Dict[str, Any] = {
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
        "skipped_reason": "",
        "error": "",
    }

    # Determine focus lines that genuinely benefit from LLM assistance
    focus_lines: List[int] = [a.line for a in advisories if a.needs_llm]
    # Also include MEDIUM-confidence deterministic patches for LLM disambiguation
    for p in patches:
        if p.confidence == "MEDIUM" and not p.selected:
            focus_lines.append(p.line)
    if llm_audit and not focus_lines:
        focus_lines = [p.line for p in patches[:12]]

    if not focus_lines:
        llm_meta["skipped_reason"] = "规则引擎已 100% 确定性覆盖全部命中项，无歧义或类型冲突热点，已自动跳过大模型调用（0 Token）"
        out = dict(det_result)
        out["llm_meta"] = llm_meta
        return out

    if llm_hub is None:
        llm_meta["skipped_reason"] = "未初始化 LLMHub，仅输出确定性规则结果"
        out = dict(det_result)
        out["llm_meta"] = llm_meta
        return out

    # Check provider readiness before invoking
    try:
        resolved = llm_hub.resolve_provider_and_model(
            provider_id=provider_id, model=model, require_ready=True
        )
    except Exception as exc:
        llm_meta["skipped_reason"] = f"大模型未就绪（{exc}），已优雅降级为纯确定性规则结果"
        out = dict(det_result)
        out["llm_meta"] = llm_meta
        return out

    if not resolved:
        llm_meta["skipped_reason"] = "当前未配置可用的大模型服务商，已优雅降级为纯确定性规则结果"
        out = dict(det_result)
        out["llm_meta"] = llm_meta
        return out

    prof, eff_model = resolved
    llm_meta["provider_id"] = prof.provider_id
    llm_meta["model"] = eff_model

    # Apply already-selected phase-1 patches so LLM sees post-rename SQL
    stage1_sql = apply_patches(original_sql, patches)
    stage1_lines = stage1_sql.splitlines()
    stage1_offsets = _build_line_offsets(stage1_sql)
    hunks = _extract_hunks(stage1_lines, focus_lines, radius=8, max_hunks=8, max_total_lines=160)
    if not hunks:
        llm_meta["skipped_reason"] = "未提取到有效热点微切片"
        out = dict(det_result)
        out["llm_meta"] = llm_meta
        return out

    mappings_text = _extract_relevant_mappings_text(registry, touched_tables, advisories)
    messages = _build_surgical_messages(
        stage1_sql,
        hunks,
        advisories,
        mappings_text,
        dialect=dialect,
        llm_audit=llm_audit,
    )

    t0 = time.perf_counter()
    try:
        resp = llm_hub.chat_completion(
            messages=messages,
            provider_id=prof.provider_id,
            model=eff_model,
            temperature=0.0,
            max_tokens=2048,
            timeout_sec=45.0,
            thinking_mode="off",
            retry_on_transient=True,
            max_retries=1,
            cancel_check=cancel_check,
            auto_continue_on_length=False,
        )
    except Exception as exc:
        llm_meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        llm_meta["error"] = str(exc)
        llm_meta["skipped_reason"] = f"大模型调用失败（{exc}），已保留 100% 确定性规则改写结果"
        out = dict(det_result)
        out["llm_meta"] = llm_meta
        return out

    llm_meta["used"] = True
    llm_meta["hunks_sent"] = len(hunks)
    llm_meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    llm_meta["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    llm_meta["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    llm_meta["total_tokens"] = int(
        usage.get("total_tokens") or (llm_meta["prompt_tokens"] + llm_meta["completion_tokens"])
    )

    raw_content = str(resp.get("content") or "")
    parsed = _parse_llm_json_payload(raw_content)
    raw_patches = parsed.get("patches") if isinstance(parsed.get("patches"), list) else []
    raw_notes = parsed.get("notes") if isinstance(parsed.get("notes"), list) else []
    llm_meta["notes"] = [str(n).strip() for n in raw_notes if str(n).strip()][:12]

    # Collect known valid target column names + ALL deprecated old/historical column names
    valid_targets = set(registry.all_target_col_names)
    reverted_old_names: Set[str] = set()
    for m in registry.mappings:
        if not m.name_changed:
            continue
        if m.original_col_name and m.original_col_name.lower() not in valid_targets:
            reverted_old_names.add(m.original_col_name.lower())
        for hist in m.historical_names:
            h_l = (hist or "").strip().lower()
            if h_l and h_l not in valid_targets:
                reverted_old_names.add(h_l)

    accepted_llm_patches: List[RewritePatch] = []
    occupied_spans_stage1: List[Tuple[int, int]] = []
    rejected_reasons: List[str] = []
    next_seq = len(patches) + 1

    for idx, item in enumerate(raw_patches, start=1):
        if not isinstance(item, dict):
            rejected_reasons.append(f"补丁#{idx}: 非法对象结构")
            continue
        orig_snip = str(item.get("original_snippet") or "")
        new_snip = str(item.get("rewritten_snippet") or "")
        cat = str(item.get("category") or "TYPE_ADAPT").strip().upper()
        if cat not in _ALLOWED_LLM_CATEGORIES:
            cat = "TYPE_ADAPT"
        conf = str(item.get("confidence") or "MEDIUM").strip().upper()
        if conf not in ("HIGH", "MEDIUM", "LOW"):
            conf = "MEDIUM"
        reason = str(item.get("reason") or "AI 辅助手术刀改写建议").strip()
        mref = str(item.get("mapping_ref") or "").strip()
        try:
            req_line = int(item.get("line") or 1)
        except (TypeError, ValueError):
            req_line = 1

        # Gate 1: Non-empty & distinct
        if not orig_snip.strip() or orig_snip == new_snip:
            rejected_reasons.append(f"L{req_line}: 原片段为空或与改写片段一致")
            continue

        # Gate 2: Reasonable size bound
        if len(new_snip) > max(240, len(orig_snip) * 4):
            rejected_reasons.append(f"L{req_line}: 替换片段膨胀过度，疑似整段重写")
            continue

        # Gate 3: Verbatim anchor in stage1_sql near req_line (CRLF-aware & non-overlapping)
        loc = _locate_verbatim_near_line(
            stage1_sql,
            stage1_offsets,
            req_line,
            orig_snip,
            window_radius=3,
            occupied_spans=occupied_spans_stage1,
        )
        if loc is None:
            rejected_reasons.append(f"L{req_line}: 原始片段未在对应行附近精确匹配或与已有补丁重叠，已拦截")
            continue
        sp_s, sp_e, actual_ln, actual_cl, matched_orig, used_crlf = loc
        eff_orig_snip = matched_orig
        eff_new_snip = (
            new_snip.replace("\r\n", "\n").replace("\n", "\r\n") if used_crlf else new_snip
        )

        # Gate 4: Balanced quotes & parens
        bal_ok, bal_err = _check_balanced_syntax(eff_orig_snip, eff_new_snip)
        if not bal_ok:
            rejected_reasons.append(f"L{actual_ln}: 语法平衡校验未通过（{bal_err}）")
            continue

        # Gate 5: Whitelist & Anti-Reversion Guard (enforced across ALL categories!)
        if cat == "DYNAMIC_SQL":
            orig_ident_src = eff_orig_snip
            new_ident_src = eff_new_snip
        else:
            orig_ident_src = _SINGLE_QUOTED_STR_RE.sub(" ", eff_orig_snip)
            new_ident_src = _SINGLE_QUOTED_STR_RE.sub(" ", eff_new_snip)

        orig_idents = {w.lower() for w in _IDENT_RE.findall(orig_ident_src)}
        new_idents = {w.lower() for w in _IDENT_RE.findall(new_ident_src)}
        added_idents = new_idents - orig_idents

        # Gate 5a: Reject if LLM tries to revert ANY renamed column back to its deprecated original/intermediate name
        reverted_hits = [w for w in added_idents if w in reverted_old_names]
        if reverted_hits:
            rejected_reasons.append(f"L{actual_ln}: 试图回退已改名旧字段 {reverted_hits}，已拦截")
            continue

        # Gate 5a-2: Reject if LLM newly introduces destructive/statement-level SQL verbs (e.g. DROP/DELETE/TRUNCATE)
        forbidden_kw_hits = [
            w for w in added_idents if w.upper() in _FORBIDDEN_LLM_ADDED_KEYWORDS
        ]
        if forbidden_kw_hits:
            rejected_reasons.append(
                f"L{actual_ln}: 局部补丁禁止新引入高危/语句级关键字 {forbidden_kw_hits}，已拦截"
            )
            continue

        # Gate 5b: Newly added SQL identifiers must be known target cols, safe SQL keywords/funcs,
        # or existing identifiers/aliases already present in the surrounding hunk context
        hunk_lo = max(0, actual_ln - 10)
        hunk_hi = min(len(stage1_lines), actual_ln + 10)
        hunk_ctx_src = "\n".join(stage1_lines[hunk_lo:hunk_hi])
        hunk_ctx_idents = {
            w.lower() for w in _IDENT_RE.findall(hunk_ctx_src)
        } - reverted_old_names
        unknown_added = [
            w
            for w in added_idents
            if w not in valid_targets
            and w not in hunk_ctx_idents
            and w.upper() not in _SAFE_SQL_CAST_WORDS
        ]
        if unknown_added:
            rejected_reasons.append(
                f"L{actual_ln}: 引入台账中不存在的未知字段/标识符 {unknown_added}，疑似幻觉已拦截"
            )
            continue

        # Parse optional schema.table.col from mapping_ref
        sch_n, tbl_n, col_n = "", "", ""
        if mref:
            m_parts = [p.strip() for p in mref.split(".") if p.strip()]
            if len(m_parts) >= 3:
                sch_n, tbl_n, col_n = m_parts[-3], m_parts[-2], m_parts[-1]
            elif len(m_parts) == 2:
                tbl_n, col_n = m_parts[0], m_parts[1]
            elif len(m_parts) == 1:
                col_n = m_parts[0]

        end_ln = actual_ln + eff_new_snip.count("\n")
        line_ctx = stage1_lines[actual_ln - 1] if 1 <= actual_ln <= len(stage1_lines) else ""
        preselect = conf == "HIGH" and cat in ("AMBIGUOUS_RENAME", "TYPE_ADAPT", "DYNAMIC_SQL", "USING_REWRITE")

        occupied_spans_stage1.append((sp_s, sp_e))
        accepted_llm_patches.append(
            RewritePatch(
                patch_id=f"p_llm_{next_seq}",
                start=sp_s,
                end=sp_e,
                line=actual_ln,
                col=actual_cl,
                end_line=end_ln,
                original_text=eff_orig_snip,
                replacement_text=eff_new_snip,
                kind=cat,
                confidence=conf,
                source="LLM",
                selected=preselect,
                schema_name=sch_n,
                table_name=tbl_n,
                original_col="",
                current_col=col_n,
                risk_level="L2" if cat == "TYPE_ADAPT" else "L1",
                reason=f"[AI 手术刀 · {eff_model}] {reason}",
                line_context=line_ctx,
                phase=2,
            )
        )
        next_seq += 1

    llm_meta["accepted_patches"] = len(accepted_llm_patches)
    llm_meta["rejected_patches"] = len(rejected_reasons)
    llm_meta["rejected_reasons"] = rejected_reasons[:15]

    all_patches = patches + accepted_llm_patches
    all_patches.sort(key=lambda p: (p.line, p.phase, p.start))
    rewritten_sql = apply_patches(original_sql, all_patches)
    diff_text = build_unified_diff(original_sql, rewritten_sql)

    out = dict(det_result)
    out["patches"] = all_patches
    out["rewritten_sql"] = rewritten_sql
    out["unified_diff"] = diff_text
    out["llm_meta"] = llm_meta
    return out
