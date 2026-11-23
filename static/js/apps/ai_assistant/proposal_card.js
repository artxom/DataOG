/**
 * Data.O.G DDL / 写操作分级二次确认卡片 (proposal_card.js)
 *
 * 安全门禁：
 * - 所有非只读 SQL（CREATE/ALTER/COMMENT/INSERT/UPDATE/DELETE/DROP/TRUNCATE）仅展示于此提示框；
 * - 常规变更点击「确认执行」放行；高危变更需输入确认词 `CONFIRM`；
 * - 支持在卡片内微调 SQL（提交至服务端独立重新定级）、支持多条语句逐条汇报、支持失败一键转 AI 诊断。
 */

import { badge, copyText, el, statusDot, svgIcon } from "../../core/dom.js";
import { store } from "../../core/state.js";
import { Toast } from "../../ui/toast.js";
import { assistantApi } from "./api.js";

function looksHighRiskOnClient(sqlText) {
  const s = String(sqlText || "").toUpperCase();
  return /\b(DROP|TRUNCATE|DELETE|UPDATE|GRANT|REVOKE|KILL|SHUTDOWN|ALTER\s+[\s\S]+?\bDROP)\b/.test(s);
}

export function renderProposalCard(
  proposal,
  {
    activeConnectionId = "",
    onProposalUpdated = null,
    onDiagnoseFailure = null,
  } = {}
) {
  if (!proposal || typeof proposal !== "object") return null;

  const isPending = proposal.status === "pending";
  const isExecuted = proposal.status === "executed";
  const isRejected = proposal.status === "rejected";
  const isFailed = proposal.status === "failed";

  let draftSql = proposal.final_sql || proposal.original_sql || "";
  let confirmToken = "";
  let selectedTargetConnId = proposal.connection_id || activeConnectionId || "";
  let requireConfirmInput = proposal.risk_level === "HIGH_RISK";
  let submitting = false;

  const conns = store.state.connections || [];
  const boundConn = conns.find((c) => c.connection_id === proposal.connection_id);
  const warnings = [...(proposal.warnings || [])];

  const card = el("div", {
    className: `ai-asst-proposal-card ${
      requireConfirmInput ? "is-high-risk" : "is-standard-write"
    } is-${proposal.status}`,
  });

  const riskBadge =
    proposal.risk_level === "HIGH_RISK"
      ? badge("高危破坏性变更", "danger")
      : badge("常规 DDL / 写入变更", "warn");

  const statusBadge = isExecuted
    ? badge("已确认执行", "success")
    : isFailed
    ? badge("执行失败", "danger")
    : isRejected
    ? badge("已取消", "neutral")
    : badge("等待二次确认", "info");

  const header = el(
    "div",
    { className: "ai-asst-proposal-head" },
    el(
      "div",
      { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
      svgIcon("shield", 15),
      el("strong", { style: { fontSize: "13px" }, text: proposal.title || "待确认执行语句" }),
      riskBadge,
      statusBadge
    ),
    el(
      "div",
      { style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } },
      badge(`${proposal.dialect || "SQL"} · ${(proposal.statements || []).length || 1} 条语句`, "neutral"),
      proposal.estimated_rows !== null && proposal.estimated_rows !== undefined
        ? badge(`预估影响 ${proposal.estimated_rows} 行`, "warn")
        : null
    )
  );

  // 目标连接说明或纯方言下的目标连接选择
  const targetMetaRow = el("div", { className: "ai-asst-proposal-target" });
  if (proposal.connection_id) {
    targetMetaRow.appendChild(
      statusDot(
        `目标连接: ${boundConn ? boundConn.name : proposal.connection_id}${
          proposal.schema_name ? ` (${proposal.schema_name})` : ""
        }`,
        "info"
      )
    );
  } else if (isPending) {
    targetMetaRow.appendChild(el("span", { className: "muted", style: { fontSize: "12px" }, text: "选择目标连接执行:" }));
    targetMetaRow.appendChild(
      el(
        "select",
        {
          className: "mac-input mac-input-sm",
          style: { maxWidth: "260px" },
          onChange: (e) => {
            selectedTargetConnId = e.target.value;
          },
        },
        [el("option", { value: "", text: "-- 请选择要执行的数据库连接 --" })].concat(
          conns.map((c) => {
            const opt = el("option", { value: c.connection_id, text: `${c.name} (${c.db_type})` });
            if (c.connection_id === selectedTargetConnId) opt.selected = true;
            return opt;
          })
        )
      )
    );
  }

  const warningsBox = warnings.length
    ? el(
        "div",
        { className: "ai-asst-proposal-warnings" },
        warnings.map((w) => el("div", { text: `⚠ ${w}` }))
      )
    : null;

  const rationaleEl = proposal.rationale
    ? el("div", { className: "ai-asst-proposal-rationale", text: proposal.rationale })
    : null;

  const sqlEditor = el("textarea", {
    className: "mac-input mono ai-asst-proposal-sql",
    rows: Math.min(14, Math.max(4, draftSql.split("\n").length + 1)),
    readonly: !isPending,
    value: draftSql,
    onInput: (e) => {
      draftSql = e.target.value;
      if (isPending && looksHighRiskOnClient(draftSql) && !requireConfirmInput) {
        requireConfirmInput = true;
        tokenWrap.style.display = "flex";
        card.classList.add("is-high-risk");
      }
    },
  });

  const tokenInput = el("input", {
    className: "mac-input mac-input-sm mono",
    style: { width: "160px" },
    placeholder: "输入 CONFIRM",
    value: "",
    onInput: (e) => {
      confirmToken = e.target.value;
    },
  });

  const tokenWrap = el(
    "div",
    {
      className: "ai-asst-proposal-token-row",
      style: { display: isPending && requireConfirmInput ? "flex" : "none" },
    },
    el("span", {
      style: { fontSize: "12px", fontWeight: "600", color: "var(--status-danger)" },
      text: "高危操作安全复核：请输入 CONFIRM 解锁执行",
    }),
    tokenInput
  );

  const actionsRow = el("div", { className: "ai-asst-proposal-actions" });
  const copyBtn = el(
    "button",
    {
      type: "button",
      className: "btn btn-xs",
      onClick: async () => {
        try {
          await copyText(draftSql);
          Toast.success("SQL 已复制");
        } catch (_e) {
          Toast.info("复制失败");
        }
      },
    },
    svgIcon("copy", 11),
    "复制 SQL"
  );
  actionsRow.appendChild(copyBtn);

  if (isPending) {
    const resetBtn = el("button", {
      type: "button",
      className: "btn btn-xs",
      text: "还原语句",
      onClick: () => {
        draftSql = proposal.original_sql || "";
        sqlEditor.value = draftSql;
      },
    });
    const rejectBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-sm",
      text: "取消不执行",
      onClick: async () => {
        if (submitting) return;
        submitting = true;
        try {
          const res = await assistantApi.declineProposal(proposal.proposal_id, "用户在对话中取消执行");
          Toast.info("已取消该操作");
          onProposalUpdated?.(res);
        } catch (err) {
          Toast.error(err.message);
          submitting = false;
        }
      },
    });
    const execBtn = el(
      "button",
      {
        type: "button",
        className: `mac-btn mac-btn-sm ${requireConfirmInput ? "btn-danger" : "mac-btn-primary"}`,
        onClick: async () => {
          if (submitting) return;
          if (!selectedTargetConnId) {
            Toast.info("请先选择要执行的目标数据库连接");
            return;
          }
          if (requireConfirmInput && confirmToken.trim().toUpperCase() !== "CONFIRM") {
            Toast.error("高危语句请先输入 CONFIRM 后再点击执行");
            tokenInput.focus();
            return;
          }
          submitting = true;
          execBtn.disabled = true;
          execBtn.textContent = "执行中...";
          try {
            const res = await assistantApi.approveProposal(proposal.proposal_id, {
              edited_sql: draftSql,
              confirm_token: confirmToken.trim(),
              connection_id: proposal.connection_id ? undefined : selectedTargetConnId,
            });
            if (res.execution_ok !== false && res.proposal?.status === "executed") {
              Toast.success("语句已确认执行完成");
            } else {
              Toast.error("部分或全部语句执行失败，请查看反馈");
            }
            onProposalUpdated?.(res);
          } catch (err) {
            if (err?.data?.code === "CONFIRM_TOKEN_REQUIRED") {
              requireConfirmInput = true;
              tokenWrap.style.display = "flex";
              card.classList.add("is-high-risk");
              execBtn.className = "mac-btn mac-btn-sm btn-danger";
              Toast.error(err.message || "修改后的 SQL 包含高危关键字，请输入 CONFIRM");
              tokenInput.focus();
            } else {
              Toast.error(err.message || "执行失败");
            }
            submitting = false;
            execBtn.disabled = false;
            execBtn.textContent = "确认执行";
          }
        },
      },
      svgIcon("check", 12),
      "确认执行"
    );
    actionsRow.appendChild(resetBtn);
    actionsRow.appendChild(el("div", { style: { flex: "1" } }));
    actionsRow.appendChild(rejectBtn);
    actionsRow.appendChild(execBtn);
  }

  // 执行结果明细展示（executed / failed）
  let execResultBox = null;
  const execRes = proposal.execution_result || {};
  const stmtResults = execRes.statements || [];
  if ((isExecuted || isFailed) && stmtResults.length) {
    execResultBox = el(
      "div",
      { className: "ai-asst-proposal-results" },
      stmtResults.map((sr) =>
        el(
          "div",
          { className: `ai-asst-stmt-res ${sr.ok ? "is-ok" : "is-fail"}` },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" } },
            el("span", {
              style: { fontWeight: "600", fontSize: "12px" },
              text: `#${sr.index} ${sr.primary_verb} · ${sr.ok ? `成功（影响 ${sr.affected_rows} 行）` : "失败"}`,
            }),
            el("span", { className: "muted mono", style: { fontSize: "11px" }, text: `${sr.elapsed_ms} ms` })
          ),
          !sr.ok && sr.error
            ? el("div", { className: "ai-asst-stmt-err mono", text: sr.error })
            : null
        )
      ),
      isFailed && typeof onDiagnoseFailure === "function"
        ? el(
            "div",
            { style: { display: "flex", justifyContent: "flex-end", marginTop: "6px" } },
            el(
              "button",
              {
                type: "button",
                className: "mac-btn mac-btn-sm mac-btn-primary",
                onClick: () => {
                  const failItem = stmtResults.find((x) => !x.ok) || stmtResults[stmtResults.length - 1];
                  onDiagnoseFailure({
                    proposal,
                    failedSql: failItem?.sql || draftSql,
                    errorText: failItem?.error || "执行失败",
                  });
                },
              },
              svgIcon("sparkles", 12),
              "让 AI 诊断修复此报错"
            )
          )
        : null
    );
  }

  card.appendChild(header);
  card.appendChild(targetMetaRow);
  if (rationaleEl) card.appendChild(rationaleEl);
  if (warningsBox) card.appendChild(warningsBox);
  card.appendChild(sqlEditor);
  card.appendChild(tokenWrap);
  card.appendChild(actionsRow);
  if (execResultBox) card.appendChild(execResultBox);

  return card;
}
