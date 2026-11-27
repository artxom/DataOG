/**
 * Data.O.G 单库落标贯标 — 基于字段变更历史的 SQL 脚本自动改写工作台
 *
 * 双引擎融合架构：
 * 1. 纯确定性词法/作用域改写底座（0 Token、100% 精准 Span 替换、保留注释/缩进/引号风格）；
 * 2. 可选轻量大模型手术刀增强（如 DeepSeek-v4-flash / gemini-flash），仅针对歧义列、L2 类型冲突与动态 SQL 微切片输出五道门禁核验补丁；
 * 3. 支持单文件粘贴与多文件批量上传、交互式逐条补丁勾选（实时重算 Diff 与最终 SQL）及 .sql / .zip 审计包一键导出。
 */

import { migratorApi } from "../../api/migrator_api.js";
import { badge, clearEl, el, svgIcon } from "../../core/dom.js";
import { isAbortError } from "../../core/http.js";
import { Modal } from "../../ui/modal.js";
import { SqlCodeBox } from "../../ui/sql_code_box.js";
import { Toast } from "../../ui/toast.js";
import { createRiskBadge } from "./changelog_panel.js";

const CONFIDENCE_META = {
  HIGH: { label: "高置信", tone: "success" },
  MEDIUM: { label: "中置信 · 待审", tone: "warn" },
  LOW: { label: "低置信 · 复核", tone: "danger" },
};

const PATCH_KIND_LABELS = {
  RENAME_QUALIFIED: "限定列改名",
  RENAME_UNQUALIFIED: "未限定列改名",
  RENAME_TARGET_COL: "目标列改名",
  RENAME_CTE_PROPAGATED: "CTE/子查询透传",
  RENAME_DDL_COL: "DDL 列名同步",
  DDL_TYPE_UPDATE: "DDL 类型同步",
  TYPE_LITERAL_REWRITE: "标准 SQL 字面量转换",
  TYPE_NULL_REWRITE: "标准 SQL 空值谓词改写",
  TYPE_COALESCE_REWRITE: "默认值参数类型对齐",
  TYPE_CAST_WRAP: "标准 SQL CAST 包裹",
  TYPE_CAST_SIMPLIFY: "显式强转简化/同步",
  TYPE_JOIN_CAST: "跨类型关联 CAST 对齐",
  TYPE_DEFAULT_REWRITE: "DDL DEFAULT 默认值同步",
  TYPE_VAR_REWRITE: "过程变量声明类型同步",
  COMMENT_UPDATE: "注释同步",
  ALIAS_PRESERVE: "保留旧列别名",
  TYPE_ADAPT: "AI 类型表达式修复",
  AMBIGUOUS_RENAME: "AI 歧义消歧改名",
  DYNAMIC_SQL: "AI 动态 SQL 改写",
  USING_REWRITE: "AI USING 转 ON",
  ALIAS_FIX: "AI 别名修正",
};

const ADVISORY_CATEGORY_LABELS = {
  AMBIGUOUS_COLUMN: "多表歧义列",
  AMBIGUOUS_TABLE: "多模式歧义表",
  TYPE_LITERAL_MISMATCH: "L2 字面量类型冲突",
  TYPE_FUNCTION_MISMATCH: "L2 函数类型不兼容",
  TYPE_COALESCE_MISMATCH: "L2 默认值类型冲突",
  TYPE_CAST_REVIEW: "L2 显式强转复核",
  TYPE_JOIN_MISMATCH: "L2 跨类型关联预警",
  PRECISION_SHRINK_WRITE: "L3 缩精度写入截断风险",
  USING_CLAUSE_CONFLICT: "JOIN USING 单侧改名冲突",
  DYNAMIC_SQL_HIT: "动态 SQL 字符串命中",
  SELECT_STAR_IMPACT: "SELECT * 输出结构变化",
  ALTER_TABLE_REVIEW: "ALTER TABLE 历史脚本复核",
};

function buildLineOffsets(text) {
  const offsets = [0];
  let idx = text.indexOf("\n");
  while (idx !== -1) {
    offsets.push(idx + 1);
    idx = text.indexOf("\n", idx + 1);
  }
  return offsets;
}

function offsetToLineCol(offsets, pos) {
  let lo = 0;
  let hi = offsets.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (offsets[mid] <= pos) lo = mid;
    else hi = mid - 1;
  }
  return { line: lo + 1, col: pos - offsets[lo] + 1 };
}

function applySnippetNearLine(sql, targetLine, oldSnippet, newSnippet, targetCol = 0) {
  if (!oldSnippet || oldSnippet === newSnippet) return { sql, applied: false };
  const offsets = buildLineOffsets(sql);
  const totalLines = offsets.length;
  if (!totalLines) return { sql, applied: false };

  let effOld = oldSnippet;
  let effNew = newSnippet;
  const winStartLine = Math.max(1, targetLine - 3);
  const newlineCnt = (effOld.match(/\n/g) || []).length;
  const winEndLine = Math.min(totalLines, targetLine + 3 + newlineCnt);
  const charStart = offsets[winStartLine - 1];
  const charEnd = winEndLine < totalLines ? offsets[winEndLine] : sql.length;
  const win = sql.slice(charStart, charEnd);

  if (!win.includes(effOld) && win.includes("\r\n") && effOld.includes("\n") && !effOld.includes("\r\n")) {
    effOld = effOld.replace(/\n/g, "\r\n");
    effNew = effNew.replace(/\r\n/g, "\n").replace(/\n/g, "\r\n");
  }

  let bestAbs = -1;
  let bestLineDist = Infinity;
  let bestColDist = Infinity;
  let from = 0;
  while (true) {
    const found = win.indexOf(effOld, from);
    if (found === -1) break;
    const absPos = charStart + found;
    const lc = offsetToLineCol(offsets, absPos);
    const lDist = Math.abs(lc.line - targetLine);
    const cDist = targetCol > 0 ? Math.abs(lc.col - targetCol) : 0;
    if (lDist < bestLineDist || (lDist === bestLineDist && cDist < bestColDist)) {
      bestLineDist = lDist;
      bestColDist = cDist;
      bestAbs = absPos;
    }
    from = found + 1;
  }
  if (bestAbs === -1) return { sql, applied: false };
  return {
    sql: sql.slice(0, bestAbs) + effNew + sql.slice(bestAbs + effOld.length),
    applied: true,
  };
}

/**
 * 与后端严格一致的两阶段补丁合成器（返回最终 SQL + 实际生效/因依赖跳过集合）
 */
export function applySelectedPatchesDetailed(originalSql, patches, selectedIds) {
  const active = (patches || []).filter((p) => selectedIds.has(p.patch_id));
  const appliedIds = new Set();
  const skippedPhase2Ids = new Set();

  // Phase 1: exact spans on originalSql
  const p1 = active
    .filter((p) => (p.phase || 1) === 1)
    .sort((a, b) => b.start - a.start);
  let res = originalSql;
  let lastStart = originalSql.length + 1;
  for (const p of p1) {
    if (p.start < 0 || p.end > originalSql.length || p.start > p.end) continue;
    if (p.end > lastStart) continue;
    res = res.slice(0, p.start) + p.replacement_text + res.slice(p.end);
    lastStart = p.start;
    appliedIds.add(p.patch_id);
  }

  // Phase 2: post-rename deterministic & LLM surgical patches anchored on phase-1 output
  for (const p2Group of [
    active.filter((p) => p.phase === 2 && p.source !== "LLM"),
    active.filter((p) => p.phase === 2 && p.source === "LLM"),
  ]) {
    p2Group.sort((a, b) => b.line - a.line || (b.col || 0) - (a.col || 0) || b.start - a.start);
    for (const p of p2Group) {
      const out = applySnippetNearLine(
        res,
        Number(p.line || 1),
        p.original_text,
        p.replacement_text,
        Number(p.col || 0)
      );
      res = out.sql;
      if (out.applied) appliedIds.add(p.patch_id);
      else skippedPhase2Ids.add(p.patch_id);
    }
  }
  return { sql: res, appliedIds, skippedPhase2Ids };
}

/**
 * 与后端严格一致的两阶段补丁合成器（0ms 前端实时勾选重算）
 */
export function applySelectedPatchesClient(originalSql, patches, selectedIds) {
  return applySelectedPatchesDetailed(originalSql, patches, selectedIds).sql;
}

/**
 * 生成紧凑的行级上下文 Diff 行列表（用于纯安全 DOM 渲染与 .diff 导出）
 */
export function computeLineDiffRows(originalSql, rewrittenSql, contextRadius = 2) {
  const normOrig = String(originalSql || "").replace(/\r\n/g, "\n");
  const normNew = String(rewrittenSql || "").replace(/\r\n/g, "\n");
  const a = normOrig.split("\n");
  const b = normNew.split("\n");
  if (normOrig === normNew) {
    return { rows: [], unifiedText: "", changedLines: 0 };
  }

  // Fast line-by-line LCS when same line count (common for in-place column renames) or general DP
  const n = a.length;
  const m = b.length;
  let ops = [];

  if (n === m) {
    for (let i = 0; i < n; i++) {
      if (a[i] === b[i]) {
        ops.push({ type: "eq", oldLine: i + 1, newLine: i + 1, text: a[i] });
      } else {
        ops.push({ type: "del", oldLine: i + 1, newLine: null, text: a[i] });
        ops.push({ type: "add", oldLine: null, newLine: i + 1, text: b[i] });
      }
    }
  } else if (n * m <= 250000) {
    // Classic LCS DP
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        if (a[i] === b[j]) dp[i][j] = dp[i + 1][j + 1] + 1;
        else dp[i][j] = dp[i + 1][j] >= dp[i][j + 1] ? dp[i + 1][j] : dp[i][j + 1];
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        ops.push({ type: "eq", oldLine: i + 1, newLine: j + 1, text: a[i] });
        i++;
        j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        ops.push({ type: "del", oldLine: i + 1, newLine: null, text: a[i] });
        i++;
      } else {
        ops.push({ type: "add", oldLine: null, newLine: j + 1, text: b[j] });
        j++;
      }
    }
    while (i < n) {
      ops.push({ type: "del", oldLine: i + 1, newLine: null, text: a[i] });
      i++;
    }
    while (j < m) {
      ops.push({ type: "add", oldLine: null, newLine: j + 1, text: b[j] });
      j++;
    }
  } else {
    // Fallback for huge scripts with different line counts: trim common prefix/suffix
    let pre = 0;
    while (pre < n && pre < m && a[pre] === b[pre]) {
      ops.push({ type: "eq", oldLine: pre + 1, newLine: pre + 1, text: a[pre] });
      pre++;
    }
    let suf = 0;
    while (n - 1 - suf >= pre && m - 1 - suf >= pre && a[n - 1 - suf] === b[m - 1 - suf]) {
      suf++;
    }
    for (let i = pre; i < n - suf; i++) {
      ops.push({ type: "del", oldLine: i + 1, newLine: null, text: a[i] });
    }
    for (let j = pre; j < m - suf; j++) {
      ops.push({ type: "add", oldLine: null, newLine: j + 1, text: b[j] });
    }
    for (let k = 0; k < suf; k++) {
      ops.push({
        type: "eq",
        oldLine: n - suf + k + 1,
        newLine: m - suf + k + 1,
        text: a[n - suf + k],
      });
    }
  }

  // Keep only changed ops + contextRadius surrounding equal lines
  const keep = new Array(ops.length).fill(false);
  let changedLines = 0;
  for (let i = 0; i < ops.length; i++) {
    if (ops[i].type !== "eq") {
      changedLines += 1;
      for (let k = Math.max(0, i - contextRadius); k <= Math.min(ops.length - 1, i + contextRadius); k++) {
        keep[k] = true;
      }
    }
  }

  const rows = [];
  const unifiedLines = ["--- original", "+++ rewritten"];
  let skipping = false;
  for (let i = 0; i < ops.length; i++) {
    if (!keep[i]) {
      if (!skipping && rows.length > 0) {
        rows.push({ type: "hunk", text: "..." });
        unifiedLines.push("@@ ... @@");
        skipping = true;
      }
      continue;
    }
    skipping = false;
    rows.push(ops[i]);
    const prefix = ops[i].type === "del" ? "-" : ops[i].type === "add" ? "+" : " ";
    unifiedLines.push(prefix + ops[i].text);
  }
  return { rows, unifiedText: unifiedLines.join("\n"), changedLines };
}

function downloadTextFile(filename, content) {
  const blob = new Blob([content], { type: "text/sql;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * 优先按严格 UTF-8 解码上传脚本，若遇 GB18030/GBK 字节流则自动回退解码避免中文注释乱码
 */
export async function decodeFileWithFallback(file) {
  if (!file) return "";
  if (typeof file.arrayBuffer === "function" && typeof TextDecoder !== "undefined") {
    const buf = await file.arrayBuffer();
    const bytes = new Uint8Array(buf);
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes).replace(/^\uFEFF/, "");
    } catch (_utfErr) {
      for (const enc of ["gb18030", "gbk"]) {
        try {
          return new TextDecoder(enc).decode(bytes);
        } catch (_encErr) {
          // try next encoding
        }
      }
      return new TextDecoder("utf-8").decode(bytes);
    }
  }
  return await file.text();
}

export class SqlRewritePanel {
  constructor(parentView) {
    this.view = parentView;
    this.context = null;
    this.loadingContext = false;
    this.analyzing = false;

    // 映射来源过滤
    this.connectionFilter = "";
    this.checkpointFilter = "";
    this.externalJsonName = "";
    this.externalJsonText = "";

    // 改写策略与模型配置
    this.mode = "deterministic"; // deterministic | hybrid
    this.selectedModelValue = ""; // `${provider_id}::${model}`
    this.defaultSchema = "";
    this.cteAliasPolicy = "propagate"; // propagate | preserve_alias
    this.rewriteDdlTypes = true;
    this.rewriteTypeExpressions = true;
    this.rewriteComments = false;
    this.autoApplyMedium = false;
    this.llmAudit = false;

    // 脚本工作集（支持多文件上传与粘贴编辑）
    this.scripts = [
      {
        id: "draft_1",
        filename: "etl_script.sql",
        content: "",
      },
    ];
    this.activeScriptId = "draft_1";

    // 分析结果与交互式补丁勾选状态
    this.result = null;
    this.selectedPatchesByFile = new Map(); // filename -> Set<patch_id>
    this.rightTab = "diff"; // diff | sql | patches | advisories

    // 隐藏文件选择器
    this.sqlFileInput = el("input", {
      type: "file",
      multiple: true,
      accept: ".sql,.ddl,.prc,.fnc,.vw,.hql,.txt,.plsql,.pck,.bdy",
      style: { display: "none" },
      onChange: (e) => {
        this._handleSqlFilesSelected(e.target.files);
        e.target.value = "";
      },
    });
    this.jsonFileInput = el("input", {
      type: "file",
      accept: ".json,application/json",
      style: { display: "none" },
      onChange: (e) => {
        this._handleJsonFileSelected(e.target.files);
        e.target.value = "";
      },
    });
  }

  destroy() {
    this.sqlFileInput?.remove?.();
    this.jsonFileInput?.remove?.();
  }

  async loadContext({ silent = false } = {}) {
    if (this.loadingContext) return;
    this.loadingContext = true;
    try {
      const cpId = this.checkpointFilter ? Number(this.checkpointFilter) : null;
      const res = await migratorApi.getSqlRewriteContext({
        connectionId: this.connectionFilter,
        sinceCheckpointId: cpId,
      });
      if (this.view._destroyed) return;
      this.context = res;
      if (!this.selectedModelValue && res.recommended_model?.value) {
        this.selectedModelValue = res.recommended_model.value;
      }
      if (this.view.subMode === "sql_rewrite") {
        this.render();
        this.view.onStateUpdate?.();
      }
    } catch (err) {
      if (!silent) Toast.error(err.message);
    } finally {
      this.loadingContext = false;
    }
  }

  _getActiveScript() {
    let found = this.scripts.find((s) => s.id === this.activeScriptId);
    if (!found && this.scripts.length > 0) {
      found = this.scripts[0];
      this.activeScriptId = found.id;
    }
    return found || null;
  }

  _findResultFileForScript(script) {
    if (!script || !this.result?.files?.length) return null;
    return (
      this.result.files.find(
        (f) => f.original_filename === script.filename || f.filename === script.filename
      ) || null
    );
  }

  _getActiveResultFile() {
    const active = this._getActiveScript();
    if (!active || !this.result?.files?.length) return null;
    return this._findResultFileForScript(active);
  }

  _getSelectedPatchSet(fileRes) {
    if (!fileRes) return new Set();
    let set = this.selectedPatchesByFile.get(fileRes.filename);
    if (!set) {
      set = new Set((fileRes.patches || []).filter((p) => p.selected).map((p) => p.patch_id));
      this.selectedPatchesByFile.set(fileRes.filename, set);
    }
    return set;
  }

  _computeLiveOutput(fileRes) {
    if (!fileRes) {
      return {
        rewrittenSql: "",
        diff: { rows: [], unifiedText: "", changedLines: 0 },
        appliedCount: 0,
        skippedPhase2Ids: new Set(),
        changed: false,
      };
    }
    const sel = this._getSelectedPatchSet(fileRes);
    const detailed = applySelectedPatchesDetailed(fileRes.original_sql, fileRes.patches, sel);
    const diff = computeLineDiffRows(fileRes.original_sql, detailed.sql, 2);
    return {
      rewrittenSql: detailed.sql,
      diff,
      appliedCount: detailed.appliedIds.size,
      skippedPhase2Ids: detailed.skippedPhase2Ids,
      changed: detailed.sql !== fileRes.original_sql,
    };
  }

  async _handleSqlFilesSelected(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    if (files.length > 50) {
      Toast.warn("单次最多载入 50 个 SQL 脚本文件");
      return;
    }
    const loaded = [];
    for (const f of files) {
      try {
        const text = await decodeFileWithFallback(f);
        loaded.push({
          id: `file_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
          filename: f.name || "script.sql",
          content: text,
        });
      } catch (_e) {
        Toast.warn(`读取文件 ${f.name} 失败`);
      }
    }
    if (!loaded.length) return;
    let firstTargetId = loaded[0].id;
    // 若当前只有空白默认草稿，则直接替换为上传文件列表；否则追加或覆盖同名脚本
    if (this.scripts.length === 1 && !this.scripts[0].content.trim() && this.scripts[0].id.startsWith("draft_")) {
      this.scripts = loaded;
      firstTargetId = loaded[0].id;
    } else {
      firstTargetId = "";
      for (const item of loaded) {
        const lowerName = item.filename.toLowerCase();
        const idx = this.scripts.findIndex((s) => s.filename.toLowerCase() === lowerName);
        if (idx >= 0) {
          this.scripts[idx].content = item.content;
          if (!firstTargetId) firstTargetId = this.scripts[idx].id;
        } else {
          this.scripts.push(item);
          if (!firstTargetId) firstTargetId = item.id;
        }
      }
    }
    this.activeScriptId = firstTargetId || this.scripts[0]?.id || "";
    this.result = null;
    this.selectedPatchesByFile.clear();
    Toast.success(`已载入 ${loaded.length} 份 SQL 脚本`);
    this.render();
  }

  async _handleJsonFileSelected(fileList) {
    const f = fileList?.[0];
    if (!f) return;
    try {
      const txt = await decodeFileWithFallback(f);
      const parsed = JSON.parse(txt);
      const count =
        (parsed.effective_column_mappings || []).length || (parsed.changes || []).length;
      if (!count) {
        Toast.warn("所选 JSON 文件中未检测到有效字段变更映射");
        return;
      }
      this.externalJsonName = f.name;
      this.externalJsonText = txt;
      Toast.success(`已加载离线变更台账 [${f.name}]（含 ${count} 条映射）`);
      this.render();
    } catch (err) {
      Toast.error(`解析 JSON 台账失败：${err.message}`);
    }
  }

  _loadSampleSqlFromMappings() {
    const mappings = this._getActiveMappingsList();
    if (!mappings.length) {
      Toast.info("当前尚无生效变更记录，请先在贯标队列执行变更或导入 JSON 台账底座");
      return;
    }
    const first = mappings[0];
    const tblMappings = mappings.filter(
      (m) =>
        m.schema_name.toLowerCase() === first.schema_name.toLowerCase() &&
        m.table_name.toLowerCase() === first.table_name.toLowerCase()
    );
    const fqn = `${first.schema_name}.${first.table_name}`;
    const colSelects = tblMappings.slice(0, 4).map((m) => `    a.${m.original_col_name}`).join(",\n");
    const firstRename = tblMappings.find((m) => m.name_changed) || first;
    const firstType = tblMappings.find((m) => m.type_changed);
    const extraSelect = firstType
      ? `,\n    TRIM(a.${firstType.original_col_name}) AS formatted_${firstType.current_col_name},\n    COALESCE(a.${firstType.original_col_name}, '') AS safe_${firstType.current_col_name}`
      : "";
    const extraWhere = firstType
      ? `\n  AND a.${firstType.original_col_name} <> ''`
      : "";
    const sample = [
      `-- 示例脚本：基于工作区变更表 ${fqn} 自动生成`,
      `WITH base_cte AS (`,
      `  SELECT`,
      `${colSelects}${extraSelect}`,
      `  FROM ${fqn} a`,
      `  WHERE a.${firstRename.original_col_name} IS NOT NULL${extraWhere}`,
      `)`,
      `SELECT`,
      `  b.${firstRename.original_col_name},`,
      `  COUNT(*) AS cnt`,
      `FROM base_cte b`,
      `GROUP BY b.${firstRename.original_col_name};`,
    ].join("\n");

    const active = this._getActiveScript();
    if (active) {
      active.content = sample;
      this.result = null;
      this.selectedPatchesByFile.clear();
      this.render();
      Toast.info("已根据当前工作区变更记录生成示例 SQL，可直接点击「开始分析与改写」");
    }
  }

  _getActiveMappingsList() {
    if (this.externalJsonText) {
      try {
        const obj = JSON.parse(this.externalJsonText);
        if (Array.isArray(obj.effective_column_mappings) && obj.effective_column_mappings.length) {
          return obj.effective_column_mappings;
        }
        if (Array.isArray(obj.changes) && obj.changes.length) {
          return obj.changes
            .filter(
              (c) =>
                String(c.event_type || "MIGRATION").toUpperCase() === "MIGRATION" &&
                String(c.status || "APPLIED").toUpperCase() === "APPLIED"
            )
            .map((c) => ({
              schema_name: c.schema_name || "public",
              table_name: c.table_name || "",
              original_col_name: c.before_col_name || "",
              current_col_name: c.after_col_name || c.before_col_name || "",
              original_col_type: c.before_col_type || "",
              current_col_type: c.after_col_type || c.before_col_type || "",
              name_changed:
                String(c.before_col_name || "").toLowerCase() !==
                String(c.after_col_name || c.before_col_name || "").toLowerCase(),
              type_changed: Boolean(c.type_changed),
              risk_level: c.risk_level || "L4",
              std_code: c.std_code || "",
            }));
        }
        return [];
      } catch (_e) {
        return [];
      }
    }
    return this.context?.effective_mappings || [];
  }

  _parseSelectedModel() {
    const val = String(this.selectedModelValue || "");
    if (!val.includes("::")) return { provider_id: "", model: "" };
    const idx = val.indexOf("::");
    return { provider_id: val.slice(0, idx), model: val.slice(idx + 2) };
  }

  async runAnalyze() {
    if (this.analyzing) return;
    const nonEmpty = this.scripts.filter((s) => String(s.content || "").trim().length > 0);
    if (!nonEmpty.length) {
      Toast.warn("请先在左侧粘贴 SQL 脚本或上传 .sql 文件");
      return;
    }

    const { provider_id, model } = this._parseSelectedModel();
    if (this.mode === "hybrid" && !this.context?.has_ready_llm) {
      Toast.info("当前未配置可用大模型，将自动以纯确定性规则引擎执行");
    }

    this.analyzing = true;
    try {
      const cpId = this.checkpointFilter ? Number(this.checkpointFilter) : null;
      const payload = {
        scripts: nonEmpty.map((s) => ({ filename: s.filename, content: s.content })),
        connection_id: this.connectionFilter,
        since_checkpoint_id: cpId,
        external_changelog_json: this.externalJsonText || "",
        default_schema: this.defaultSchema.trim(),
        cte_alias_policy: this.cteAliasPolicy,
        rewrite_ddl_types: this.rewriteDdlTypes,
        rewrite_type_expressions: this.rewriteTypeExpressions,
        rewrite_comments: this.rewriteComments,
        auto_apply_medium: this.autoApplyMedium,
        mode: this.mode,
        provider_id,
        model,
        llm_audit: this.llmAudit,
      };
      const res = await this.view._runTracked(
        this.mode === "hybrid" ? "SQL 脚本混合智能改写" : "SQL 脚本确定性改写",
        ({ taskId, signal }) => migratorApi.analyzeSqlRewrite(payload, { taskId, signal })
      );
      if (this.view._destroyed) return;
      this.result = res;
      this.selectedPatchesByFile.clear();
      for (const f of res.files || []) {
        const initSet = new Set(
          (f.patches || []).filter((p) => p.selected).map((p) => p.patch_id)
        );
        this.selectedPatchesByFile.set(f.filename, initSet);
      }
      if (res.aggregate?.deterministic_patches === 0 && res.aggregate?.llm_patches === 0 && res.aggregate?.advisory_count > 0) {
        this.rightTab = "advisories";
      } else {
        this.rightTab = "diff";
      }
      Toast.success(res.message || "SQL 脚本分析改写完成");
      this.render();
      this.view.onStateUpdate?.();
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    } finally {
      this.analyzing = false;
    }
  }

  async exportZipBundle() {
    const files = this.result?.files || [];
    if (!files.length) {
      Toast.warn("请先执行分析改写后再导出结果包");
      return;
    }
    const exportPayload = files.map((f) => {
      const live = this._computeLiveOutput(f);
      return {
        filename: f.filename,
        rewritten_sql: live.rewrittenSql,
        unified_diff: live.diff.unifiedText,
        changed: live.changed,
        applied_patch_count: live.appliedCount,
        advisory_count: (f.advisories || []).length,
      };
    });
    try {
      const fname = await this.view._runTracked(
        "打包导出改写 SQL 与审计报告 (.zip)",
        ({ taskId, signal }) => migratorApi.exportSqlRewriteZip(exportPayload, { taskId, signal }),
        { exclusive: false }
      );
      if (!this.view._destroyed) {
        Toast.success(`已导出：${fname}`);
      }
    } catch (err) {
      if (isAbortError(err)) return;
      Toast.error(err.message);
    }
  }

  openMappingsModal() {
    const list = this._getActiveMappingsList();
    const rowsWrap = el(
      "div",
      { className: "sql-rewrite-mappings-list" },
      list.length
        ? list.map((m) =>
            el(
              "div",
              { className: "sql-rewrite-mapping-row" },
              el(
                "div",
                { style: { display: "flex", flexDirection: "column", gap: "2px", minWidth: "0" } },
                el("div", {
                  className: "mono",
                  style: { fontWeight: "600", fontSize: "12px" },
                  text: `${m.schema_name}.${m.table_name}: ${m.original_col_name} → ${m.current_col_name}`,
                }),
                el("div", {
                  className: "mono",
                  style: { fontSize: "11px", color: "var(--os-muted)" },
                  text: [
                    `${m.original_col_type || "?"} → ${m.current_col_type || "?"}`,
                    Array.isArray(m.historical_names) && m.historical_names.length > 1
                      ? `历史链: ${m.historical_names.join(" → ")} → ${m.current_col_name}`
                      : "",
                    m.std_code ? `标准: ${m.std_code}` : "",
                  ]
                    .filter(Boolean)
                    .join(" · "),
                })
              ),
              createRiskBadge({ risk_level: m.risk_level || m.highest_risk_level || "L4" })
            )
          )
        : el("div", {
            className: "grid-empty",
            style: { padding: "24px" },
            text: "当前筛选条件下暂无生效中的字段变更映射；请先在贯标队列执行变更，或点击「导入 JSON 底座」加载外部台账。",
          })
    );

    Modal.open({
      title: `当前生效字段净映射底座 (${list.length} 条)`,
      body: el(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "8px" } },
        el("div", {
          style: { fontSize: "11.5px", color: "var(--os-muted)" },
          text: "系统已自动折叠链式变更（如 A→B→C 归并为 A→C 且兼容中间名 B），已回滚记录已自动剔除。",
        }),
        rowsWrap
      ),
      footer: [
        el("button", { type: "button", className: "mac-btn mac-btn-primary", text: "关闭", onClick: () => Modal.close() }),
      ],
    });
  }

  openOptionsModal() {
    const schemaInput = el("input", {
      className: "mac-input",
      placeholder: "例如：dwd（SQL 省略 Schema 前缀时的默认归属）",
      value: this.defaultSchema,
    });
    const policySelect = el(
      "select",
      { className: "mac-input" },
      [
        { value: "propagate", label: "向下游透传新列名（推荐：CTE/子查询引用同步改名）" },
        { value: "preserve_alias", label: "保留旧列别名（AS old_col，兼容未改造下游接口）" },
      ].map((opt) => {
        const o = el("option", { value: opt.value, text: opt.label });
        if (opt.value === this.cteAliasPolicy) o.selected = true;
        return o;
      })
    );
    const ddlCb = el("input", { type: "checkbox", checked: this.rewriteDdlTypes });
    const typeExprCb = el("input", { type: "checkbox", checked: this.rewriteTypeExpressions });
    const cmtCb = el("input", { type: "checkbox", checked: this.rewriteComments });
    const medCb = el("input", { type: "checkbox", checked: this.autoApplyMedium });
    const auditCb = el("input", { type: "checkbox", checked: this.llmAudit });

    const body = el(
      "div",
      { className: "form-grid" },
      el(
        "div",
        { className: "form-field span-2" },
        el("label", { className: "form-label", text: "默认 Schema（当 SQL 未写库模式前缀时优先匹配）" }),
        schemaInput
      ),
      el(
        "div",
        { className: "form-field span-2" },
        el("label", { className: "form-label", text: "CTE / 派生子查询别名策略" }),
        policySelect
      ),
      el(
        "div",
        { className: "form-field span-2", style: { display: "flex", flexDirection: "column", gap: "8px", marginTop: "4px" } },
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "7px", fontSize: "12px", cursor: "pointer" } },
          ddlCb,
          el("span", { text: "同步更新 CREATE TABLE DDL 中的字段物理类型（如 varchar(32) → varchar(64)）" })
        ),
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "7px", fontSize: "12px", cursor: "pointer" } },
          typeExprCb,
          el("span", {
            text: "按通用 ANSI 标准 SQL 自动改写类型转换表达式（CAST / 字面量 / IS NULL / COALESCE / INSERT VALUES / DEFAULT）",
          })
        ),
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "7px", fontSize: "12px", cursor: "pointer" } },
          cmtCb,
          el("span", { text: "同步更新 COMMENT ON COLUMN 中的字段注释文本" })
        ),
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "7px", fontSize: "12px", cursor: "pointer" } },
          medCb,
          el("span", { text: "默认勾选「中置信」多表未限定字段补丁（关闭则仅自动勾选高置信，最稳妥）" })
        ),
        el(
          "label",
          { style: { display: "inline-flex", alignItems: "center", gap: "7px", fontSize: "12px", cursor: "pointer" } },
          auditCb,
          el("span", { text: "混合模式下即使无预警也对改动切片执行一次 AI 复核（消耗少量 Token）" })
        )
      )
    );

    Modal.open({
      title: "SQL 脚本自动改写策略设置",
      body,
      footer: [
        el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close() }),
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-primary",
          text: "保存策略",
          onClick: () => {
            this.defaultSchema = schemaInput.value.trim();
            this.cteAliasPolicy = policySelect.value;
            this.rewriteDdlTypes = Boolean(ddlCb.checked);
            this.rewriteTypeExpressions = Boolean(typeExprCb.checked);
            this.rewriteComments = Boolean(cmtCb.checked);
            this.autoApplyMedium = Boolean(medCb.checked);
            this.llmAudit = Boolean(auditCb.checked);
            Modal.close("saved");
            Toast.success("改写策略已更新");
            this.render();
          },
        }),
      ],
    });
  }

  renderHeaderActions() {
    const hasResult = Boolean(this.result?.files?.length);
    return el(
      "div",
      { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm",
          onClick: () => this.sqlFileInput.click(),
        },
        svgIcon("upload", 13),
        el("span", { text: "上传 SQL 脚本" })
      ),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        title: "可选：导入导出的 DataOG_落标变更台账底座.json 进行离线或跨环境改写",
        text: this.externalJsonName ? `JSON: ${this.externalJsonName}` : "导入 JSON 底座",
        onClick: () => this.jsonFileInput.click(),
      }),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        text: "改写策略",
        onClick: () => this.openOptionsModal(),
      }),
      hasResult
        ? el("button", {
            type: "button",
            className: "mac-btn mac-btn-sm",
            text: "导出 .zip",
            onClick: () => this.exportZipBundle(),
          })
        : null,
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-sm mac-btn-primary",
          disabled: this.analyzing,
          onClick: () => this.runAnalyze(),
        },
        svgIcon("sparkles", 13),
        el("span", { text: this.analyzing ? "改写中..." : "开始分析与改写" })
      )
    );
  }

  render() {
    const ctx = this.context;
    const sum = ctx?.summary || { table_count: 0, mapping_count: 0, risk_counts: {} };
    const conns = ctx?.connections || this.view.ws?.connections || [];
    const cps = ctx?.checkpoints || [];
    const llmModels = ctx?.llm_models || [];
    const readyModels = llmModels.filter((m) => m.ready);

    // 1. Subbar：映射范围 + 引擎模式 + 模型选择
    const connSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "变更来源数据源",
        disabled: Boolean(this.externalJsonText),
        style: { maxWidth: "155px" },
        onChange: (e) => {
          this.connectionFilter = e.target.value;
          this.checkpointFilter = "";
          this.loadContext();
        },
      },
      [
        el("option", { value: "", text: "全部数据源变更" }),
        ...conns.map((c) => {
          const o = el("option", { value: c.connection_id, text: `${c.name}` });
          if (c.connection_id === this.connectionFilter) o.selected = true;
          return o;
        }),
      ]
    );

    const cpSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "变更起点 Checkpoint",
        disabled: Boolean(this.externalJsonText),
        style: { maxWidth: "170px" },
        onChange: (e) => {
          this.checkpointFilter = e.target.value;
          this.loadContext();
        },
      },
      [
        el("option", { value: "", text: "全部生效变更" }),
        ...cps.map((cp) => {
          const o = el("option", { value: String(cp.id), text: `自 ${cp.name} 起` });
          if (String(cp.id) === String(this.checkpointFilter)) o.selected = true;
          return o;
        }),
      ]
    );

    const activeMappingList = this._getActiveMappingsList();
    const mappingBtn = el("button", {
      type: "button",
      className: "btn btn-xs",
      title: "点击查看当前用于改写的所有生效字段净映射明细",
      text: this.externalJsonName
        ? `离线台账 · ${activeMappingList.length} 列映射`
        : `生效底座 · ${sum.table_count || 0} 表 / ${activeMappingList.length} 列映射`,
      onClick: () => this.openMappingsModal(),
    });

    const modeSelect = el(
      "select",
      {
        className: "mac-input mac-input-sm",
        "aria-label": "改写引擎模式",
        onChange: (e) => {
          this.mode = e.target.value;
          this.render();
        },
      },
      [
        { value: "deterministic", label: "确定性规则引擎 (0 Token)" },
        { value: "hybrid", label: "规则引擎 + AI 手术刀增强" },
      ].map((m) => {
        const o = el("option", { value: m.value, text: m.label });
        if (m.value === this.mode) o.selected = true;
        return o;
      })
    );

    const modelSelect =
      this.mode === "hybrid"
        ? el(
            "select",
            {
              className: "mac-input mac-input-sm",
              "aria-label": "选择 AI 辅助模型",
              style: { maxWidth: "220px" },
              onChange: (e) => {
                this.selectedModelValue = e.target.value;
              },
            },
            readyModels.length
              ? readyModels.map((m) => {
                  const tag = m.model_tier === "lightweight" ? "⚡轻量推荐" : m.provider_name;
                  const o = el("option", {
                    value: m.value,
                    text: `${m.model} (${tag})`,
                  });
                  if (m.value === this.selectedModelValue) o.selected = true;
                  return o;
                })
              : [el("option", { value: "", text: "未配置可用模型（将自动降级）" })]
          )
        : null;

    const subbar = el(
      "div",
      { className: "mac-subbar" },
      el(
        "div",
        { className: "mac-subbar-group" },
        connSelect,
        cpSelect,
        mappingBtn,
        this.externalJsonName
          ? el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              title: "清除离线 JSON 台账，恢复读取当前工作区变更日志",
              text: "切回工作区日志",
              onClick: () => {
                this.externalJsonName = "";
                this.externalJsonText = "";
                this.render();
                this.view.onStateUpdate?.();
              },
            })
          : null
      ),
      el(
        "div",
        { className: "mac-subbar-group" },
        modeSelect,
        modelSelect
      )
    );

    // 2. Main Split Workspace
    const leftPane = this._renderLeftInputPane();
    const rightPane = this._renderRightResultPane();
    const splitWrap = el(
      "div",
      { className: "sql-rewrite-split" },
      this.sqlFileInput,
      this.jsonFileInput,
      leftPane,
      rightPane
    );

    clearEl(this.view.subbarMount, subbar);
    clearEl(this.view.gridMount, splitWrap);
  }

  _renderLeftInputPane() {
    const active = this._getActiveScript();

    const fileTabs = el(
      "div",
      { className: "sql-rewrite-file-tabs" },
      this.scripts.map((s) => {
        const fRes = this._findResultFileForScript(s);
        const isActive = active && s.id === active.id;
        const patchCnt = fRes ? (fRes.patches || []).length : 0;
        const advCnt = fRes ? (fRes.advisories || []).length : 0;
        return el(
          "div",
          {
            className: `sql-rewrite-file-tab ${isActive ? "is-active" : ""}`.trim(),
            onClick: () => {
              this.activeScriptId = s.id;
              this.render();
            },
          },
          el("span", { className: "mono sql-rewrite-file-name", title: s.filename, text: s.filename }),
          patchCnt > 0 ? badge(`+${patchCnt}`, "success") : null,
          advCnt > 0 ? badge(`!${advCnt}`, "warn") : null,
          this.scripts.length > 1
            ? el("button", {
                type: "button",
                className: "sql-rewrite-file-close",
                title: "移除该脚本",
                text: "×",
                onClick: (e) => {
                  e.stopPropagation();
                  const removedRes = this._findResultFileForScript(s);
                  this.scripts = this.scripts.filter((x) => x.id !== s.id);
                  if (removedRes && this.result?.files) {
                    this.selectedPatchesByFile.delete(removedRes.filename);
                    this.result.files = this.result.files.filter((f) => f !== removedRes);
                    if (!this.result.files.length) {
                      this.result = null;
                    }
                  }
                  if (this.activeScriptId === s.id) {
                    this.activeScriptId = this.scripts[0]?.id || "";
                  }
                  this.render();
                  this.view.onStateUpdate?.();
                },
              })
            : null
        );
      }),
      el("button", {
        type: "button",
        className: "btn btn-xs btn-ghost",
        title: "新建一个空白 SQL 脚本标签页",
        text: "+ 新建脚本",
        onClick: () => {
          const existing = new Set(this.scripts.map((x) => x.filename.toLowerCase()));
          let nextNum = this.scripts.length + 1;
          while (existing.has(`script_${nextNum}.sql`)) nextNum += 1;
          const id = `draft_${Date.now()}_${nextNum}`;
          this.scripts.push({ id, filename: `script_${nextNum}.sql`, content: "" });
          this.activeScriptId = id;
          this.render();
        },
      })
    );

    const lineCount = active && active.content ? active.content.split(/\r?\n/).length : 0;
    const activeRes = this._findResultFileForScript(active);
    const isStale = Boolean(active && activeRes && active.content !== activeRes.original_sql);
    const lineCountBadge = badge(`${lineCount} 行`, "neutral");
    const staleBadge = badge("已修改 · 请重新分析", "warn");
    staleBadge.hidden = !isStale;

    const textarea = el("textarea", {
      className: "sql-rewrite-editor mono",
      spellcheck: "false",
      autocomplete: "off",
      placeholder:
        "粘贴您的业务 SQL / 存储过程 / 建表 DDL，或拖拽多个 .sql 文件到此处...\n\n" +
        "确定性引擎将精准识别表别名、多层 CTE、子查询、INSERT/UPDATE/MERGE/CREATE TABLE，" +
        "绝不误伤注释与字符串字面量；如需处理 L2 类型表达式冲突或动态 SQL，可切换为「规则引擎 + AI 手术刀增强」。",
      value: active ? active.content : "",
      onInput: (e) => {
        if (active) {
          active.content = e.target.value;
          const cnt = active.content ? active.content.split(/\r?\n/).length : 0;
          lineCountBadge.textContent = `${cnt} 行`;
          if (activeRes) {
            staleBadge.hidden = active.content === activeRes.original_sql;
          }
        }
      },
    });

    const leftCard = el(
      "div",
      {
        className: "sql-rewrite-pane sql-rewrite-left",
        onDragover: (e) => {
          e.preventDefault();
          leftCard.classList.add("is-dragover");
        },
        onDragleave: () => leftCard.classList.remove("is-dragover"),
        onDrop: (e) => {
          e.preventDefault();
          leftCard.classList.remove("is-dragover");
          if (e.dataTransfer?.files?.length) {
            this._handleSqlFilesSelected(e.dataTransfer.files);
          }
        },
      },
      el(
        "div",
        { className: "sql-rewrite-pane-head" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "6px" } },
          el("span", { style: { fontWeight: "600", fontSize: "12px" }, text: "原始 SQL 脚本" }),
          lineCountBadge,
          staleBadge
        ),
        el(
          "div",
          { style: { display: "inline-flex", alignItems: "center", gap: "5px" } },
          el("button", {
            type: "button",
            className: "btn btn-xs btn-ghost",
            text: "生成示例",
            title: "根据当前工作区变更日志生成可立即测试的 SQL 示例",
            onClick: () => this._loadSampleSqlFromMappings(),
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "上传文件",
            onClick: () => this.sqlFileInput.click(),
          }),
          el("button", {
            type: "button",
            className: "btn btn-xs btn-ghost",
            text: "清空",
            onClick: () => {
              if (active) {
                active.content = "";
                this.result = null;
                this.selectedPatchesByFile.clear();
                this.render();
              }
            },
          })
        )
      ),
      fileTabs,
      textarea
    );

    return leftCard;
  }

  _renderRightResultPane() {
    const fileRes = this._getActiveResultFile();
    if (!fileRes) {
      return el(
        "div",
        { className: "sql-rewrite-pane sql-rewrite-right" },
        el(
          "div",
          { className: "sql-rewrite-empty" },
          el("div", { style: { fontWeight: "600", fontSize: "13.5px", color: "var(--os-text)" }, text: "基于字段变更历史的 SQL 脚本智能改写底座" }),
          el("div", {
            style: { fontSize: "12px", color: "var(--os-muted)", maxWidth: "460px", lineHeight: "1.6", textAlign: "center" },
            text: "在左侧上传或粘贴待改造的 SQL 脚本（支持多文件批量处理），点击右上角「开始分析与改写」。系统优先使用零幻觉确定性规则引擎完成 100% 精准 Span 替换，并可按需调用 DeepSeek-v4-flash 等轻量模型对 L2 类型表达式冲突与歧义微切片执行手术刀修复。",
          }),
          el(
            "div",
            { style: { display: "flex", gap: "8px", marginTop: "6px" } },
            el("button", {
              type: "button",
              className: "mac-btn mac-btn-sm",
              text: "查看生效变更映射",
              onClick: () => this.openMappingsModal(),
            }),
            el("button", {
              type: "button",
              className: "mac-btn mac-btn-sm mac-btn-primary",
              text: "开始分析与改写",
              onClick: () => this.runAnalyze(),
            })
          )
        )
      );
    }

    const live = this._computeLiveOutput(fileRes);
    const patches = fileRes.patches || [];
    const advisories = fileRes.advisories || [];
    const detCount = patches.filter((p) => p.source === "DETERMINISTIC").length;
    const llmCount = patches.filter((p) => p.source === "LLM").length;
    const llmMeta = fileRes.llm_meta || {};

    const summaryBar = el(
      "div",
      { className: "sql-rewrite-pane-head" },
      el(
        "div",
        { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
        badge(`已采纳 ${live.appliedCount}/${patches.length} 处`, live.appliedCount > 0 ? "success" : "neutral"),
        detCount > 0 ? badge(`规则命中 ${detCount}`, "info") : null,
        llmCount > 0 ? badge(`AI 建议 ${llmCount}`, "warn") : null,
        advisories.length > 0 ? badge(`风险预警 ${advisories.length}`, "danger") : null,
        llmMeta.used
          ? badge(
              `${llmMeta.model} · ${llmMeta.hunks_sent} 切片 · ${llmMeta.total_tokens || 0} tok · ${((llmMeta.latency_ms || 0) / 1000).toFixed(1)}s`,
              "neutral"
            )
          : null
      ),
      el(
        "div",
        { style: { display: "inline-flex", alignItems: "center", gap: "5px" } },
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "复制 SQL",
          onClick: async () => {
            try {
              await navigator.clipboard.writeText(live.rewrittenSql);
              Toast.success("已复制最终改写 SQL");
            } catch (_e) {
              Toast.warn("复制失败，请切换至「最终 SQL」手动复制");
            }
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "下载 .sql",
          onClick: () => {
            const stem = fileRes.filename.replace(/\.sql$/i, "");
            downloadTextFile(`${stem}_rewritten.sql`, live.rewrittenSql);
            Toast.success(`已下载 ${stem}_rewritten.sql`);
          },
        })
      )
    );

    const tabsBar = el(
      "div",
      { className: "sql-rewrite-result-tabs" },
      [
        { id: "diff", label: `交互式 Diff (${live.diff.changedLines})` },
        { id: "sql", label: "最终改写 SQL" },
        { id: "patches", label: `逐条审校 (${patches.length})` },
        { id: "advisories", label: `类型与风险预警 (${advisories.length})` },
      ].map((t) =>
        el("button", {
          type: "button",
          className: `filter-pill ${this.rightTab === t.id ? "is-active" : ""}`.trim(),
          text: t.label,
          onClick: () => {
            this.rightTab = t.id;
            this.render();
          },
        })
      )
    );

    const llmBanner =
      llmMeta.skipped_reason && this.mode === "hybrid"
        ? el("div", { className: "sql-rewrite-llm-banner", text: `AI 增强状态：${llmMeta.skipped_reason}` })
        : llmMeta.used && llmMeta.rejected_patches > 0
        ? el("div", {
            className: "sql-rewrite-llm-banner",
            text: `反幻觉门禁已拦截 ${llmMeta.rejected_patches} 条不合规范的 AI 候选补丁（${(llmMeta.rejected_reasons || []).slice(0, 2).join("；")}）`,
          })
        : null;

    let bodyNode = null;
    if (this.rightTab === "diff") {
      bodyNode = this._renderDiffView(live);
    } else if (this.rightTab === "sql") {
      const sqlBox = new SqlCodeBox({
        title: `${fileRes.filename}（已采纳 ${live.appliedCount} 处改写）`,
        value: live.rewrittenSql,
        readOnly: true,
        minHeight: "340px",
        filename: `${fileRes.filename.replace(/\.sql$/i, "")}_rewritten.sql`,
      });
      bodyNode = el("div", { className: "sql-rewrite-body-pad" }, sqlBox.root);
    } else if (this.rightTab === "patches") {
      bodyNode = this._renderPatchesReviewView(fileRes, live);
    } else {
      bodyNode = this._renderAdvisoriesView(fileRes);
    }

    return el(
      "div",
      { className: "sql-rewrite-pane sql-rewrite-right" },
      summaryBar,
      tabsBar,
      llmBanner,
      el("div", { className: "sql-rewrite-result-body" }, bodyNode)
    );
  }

  _renderDiffView(live) {
    if (!live.changed || !live.diff.rows.length) {
      return el("div", {
        className: "grid-empty",
        style: { padding: "36px 20px" },
        text: "当前采纳的改写结果与原始 SQL 完全一致（无差异行）；若存在待审中置信或 AI 建议，可切至「逐条审校」或「类型与风险预警」查看。",
      });
    }
    return el(
      "div",
      { className: "sql-diff-viewer mono" },
      live.diff.rows.map((r) => {
        if (r.type === "hunk") {
          return el("div", { className: "sql-diff-line is-hunk", text: "···" });
        }
        const cls =
          r.type === "del"
            ? "sql-diff-line is-del"
            : r.type === "add"
            ? "sql-diff-line is-add"
            : "sql-diff-line is-eq";
        const sign = r.type === "del" ? "-" : r.type === "add" ? "+" : " ";
        return el(
          "div",
          { className: cls },
          el("span", { className: "sql-diff-ln", text: r.oldLine != null ? String(r.oldLine) : "" }),
          el("span", { className: "sql-diff-ln", text: r.newLine != null ? String(r.newLine) : "" }),
          el("span", { className: "sql-diff-sign", text: sign }),
          el("span", { className: "sql-diff-code", text: r.text })
        );
      })
    );
  }

  _renderPatchesReviewView(fileRes, live = null) {
    const patches = fileRes.patches || [];
    if (!patches.length) {
      return el("div", {
        className: "grid-empty",
        style: { padding: "36px 20px" },
        text: "当前脚本未命中需要自动改写的字段。",
      });
    }
    const sel = this._getSelectedPatchSet(fileRes);
    const skippedP2 = live?.skippedPhase2Ids || new Set();
    const llmPatches = patches.filter((p) => p.source === "LLM");

    const bulkBar = el(
      "div",
      { className: "sql-rewrite-bulk-bar" },
      el("button", {
        type: "button",
        className: "btn btn-xs",
        text: "仅选确定性高置信",
        onClick: () => {
          sel.clear();
          for (const p of patches) {
            if (p.source === "DETERMINISTIC" && p.confidence === "HIGH") sel.add(p.patch_id);
          }
          this.render();
        },
      }),
      llmPatches.length > 0
        ? el("button", {
            type: "button",
            className: "btn btn-xs",
            text: `采纳全部 AI 建议 (+${llmPatches.length})`,
            onClick: () => {
              for (const p of llmPatches) sel.add(p.patch_id);
              this.render();
            },
          })
        : null,
      el("button", {
        type: "button",
        className: "btn btn-xs btn-ghost",
        text: "全选",
        onClick: () => {
          for (const p of patches) sel.add(p.patch_id);
          this.render();
        },
      }),
      el("button", {
        type: "button",
        className: "btn btn-xs btn-ghost",
        text: "清空",
        onClick: () => {
          sel.clear();
          this.render();
        },
      })
    );

    const listEl = el(
      "div",
      { className: "sql-rewrite-card-list" },
      patches.map((p) => {
        const checked = sel.has(p.patch_id);
        const conf = CONFIDENCE_META[p.confidence] || CONFIDENCE_META.MEDIUM;
        const kindLbl = PATCH_KIND_LABELS[p.kind] || p.kind;
        const cb = el("input", {
          type: "checkbox",
          checked,
          onChange: (e) => {
            if (e.target.checked) sel.add(p.patch_id);
            else sel.delete(p.patch_id);
            this.render();
          },
        });
        return el(
          "label",
          { className: `sql-rewrite-patch-card ${checked ? "is-checked" : ""}`.trim() },
          el(
            "div",
            { className: "sql-rewrite-patch-top" },
            el(
              "div",
              { style: { display: "inline-flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
              cb,
              badge(`L${p.line}`, "neutral"),
              badge(p.source === "LLM" ? "AI 手术刀" : "确定性规则", p.source === "LLM" ? "warn" : "info"),
              badge(kindLbl, "neutral"),
              badge(conf.label, conf.tone),
              createRiskBadge({ risk_level: p.risk_level || "L1" }),
              skippedP2.has(p.patch_id)
                ? badge("依赖的前置改名未勾选，暂未生效", "warn")
                : null
            )
          ),
          el(
            "div",
            { className: "sql-rewrite-patch-diff mono" },
            el("span", { className: "sql-snippet-del", text: p.original_text }),
            el("span", { style: { color: "var(--os-muted)", margin: "0 6px" }, text: "→" }),
            el("span", { className: "sql-snippet-add", text: p.replacement_text })
          ),
          el("div", { className: "sql-rewrite-patch-reason", text: p.reason || "" })
        );
      })
    );

    return el("div", { className: "sql-rewrite-body-pad" }, bulkBar, listEl);
  }

  _renderAdvisoriesView(fileRes) {
    const advisories = fileRes.advisories || [];
    const llmNotes = fileRes.llm_meta?.notes || [];
    if (!advisories.length && !llmNotes.length) {
      return el("div", {
        className: "grid-empty",
        style: { padding: "36px 20px" },
        text: "未检测到 L2 类型冲突、L3 缩精度写入截断或歧义列风险，脚本状态良好。",
      });
    }

    return el(
      "div",
      { className: "sql-rewrite-body-pad" },
      el(
        "div",
        { className: "sql-rewrite-card-list" },
        advisories.map((a) => {
          const sevTone = a.severity === "ERROR" ? "danger" : a.severity === "WARN" ? "warn" : "info";
          const catLbl = ADVISORY_CATEGORY_LABELS[a.category] || a.category;
          return el(
            "div",
            { className: `sql-rewrite-adv-card is-${String(a.severity || "warn").toLowerCase()}` },
            el(
              "div",
              { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
              badge(`L${a.line}`, "neutral"),
              badge(catLbl, sevTone),
              createRiskBadge({ risk_level: a.risk_level || "L2" }),
              a.needs_llm && this.mode === "deterministic"
                ? badge("可启用 AI 手术刀辅助修复", "info")
                : null
            ),
            el("div", { style: { fontSize: "12px", color: "var(--os-text)" }, text: a.message }),
            a.line_context
              ? el("div", {
                  className: "mono sql-rewrite-line-ctx",
                  text: `L${a.line} | ${a.line_context.trim()}`,
                })
              : null
          );
        }),
        llmNotes.length
          ? el(
              "div",
              { className: "sql-rewrite-adv-card is-info" },
              el("div", { style: { fontWeight: "600", fontSize: "12px" }, text: "AI 复核补充说明" }),
              ...llmNotes.map((n) => el("div", { style: { fontSize: "11.5px", color: "var(--os-text-secondary)" }, text: `• ${n}` }))
            )
          : null
      )
    );
  }
}
