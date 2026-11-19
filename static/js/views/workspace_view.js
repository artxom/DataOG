/**
 * Data.O.G 系统配置子面板：团队共享工作区与引导式增量合并向导 (WorkspaceView)
 *
 * 架构红线：
 * 1. 严禁展示任何本地私有库物理路径；
 * 2. 100% 使用安全 DOM 工厂 (el / clearEl)，零 innerHTML，零阻塞弹窗；
 * 3. 支持工作区新建、克隆、重命名、热切换、零密钥纯净导出 (.dataog.db)、
 *    导入为新工作区及三步引导式增量合并同事工作区进度（含跨环境表对差异预览）。
 */

import { workspaceApi } from "../core/api.js";
import {
  badge,
  clearEl,
  el,
  formatBytes,
  pickLocalFile,
  statusDot,
  svgIcon,
} from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

const MERGE_MODULE_META = [
  { id: "standard_dictionary", label: "企业数据标准字典", desc: "按数据项编号 (std_code) 合并标准定义与多库物理类型" },
  { id: "field_standard_bindings", label: "单库字段数标绑定", desc: "自动按数据源端点对齐各表字段的数据标准绑定关系" },
  { id: "cross_mappings", label: "跨环境表对与字段映射", desc: "合并来源库 × 目标库的表配对及字段级绑标映射" },
  { id: "migration_items", label: "单库贯标执行队列", desc: "合并待贯标/已贯标字段改造清单" },
  { id: "connection_topology", label: "数据源连接拓扑（无密码）", desc: "仅合并地址/端口/库名/落标格式，个人凭据自动按端点匹配" },
  { id: "metadata_cache", label: "库表元数据缓存", desc: "合并离线采集的表结构、字段注释与视图血缘缓存" },
];

const MERGE_STRATEGIES = [
  {
    id: "supplement",
    title: "仅补充缺失与未绑定项",
    tag: "推荐",
    desc: "最安全：仅写入本地缺失或未绑定的内容，绝不覆盖本机已有绑定与映射",
  },
  {
    id: "newer",
    title: "按更新时间保留较新记录",
    tag: "协同",
    desc: "冲突项按 updated_at 保留最新编辑版本，非冲突项全部补充",
  },
  {
    id: "overwrite",
    title: "以导入文件覆盖冲突项",
    tag: "覆盖",
    desc: "冲突项采用同事文件覆盖，但仍完整保留本地独有的表与绑定",
  },
];

function openExportWorkspaceDialog(ws) {
  let includeMeta = true;
  const checkbox = el("input", {
    type: "checkbox",
    checked: true,
    onChange: (e) => {
      includeMeta = e.target.checked;
    },
  });

  const body = el(
    "div",
    { className: "ws-dialog-stack" },
    el(
      "div",
      { className: "ws-security-banner" },
      svgIcon("shield", 16),
      el(
        "div",
        {},
        el("div", { className: "ws-security-banner-title", text: "零密钥纯净导出保障" }),
        el("div", {
          className: "ws-security-banner-sub",
          text: "导出文件 (.dataog.db) 仅含业务协同数据，绝不包含本机激活码、AI 密钥、数据库账号密码或个人日志。",
        })
      )
    ),
    el(
      "label",
      { className: "ws-check-row" },
      checkbox,
      el(
        "div",
        {},
        el("div", { style: { fontWeight: "600", fontSize: "12.5px" }, text: "包含库表元数据缓存" }),
        el("div", {
          style: { fontSize: "11.5px", color: "var(--os-muted)" },
          text: "同事导入后无需重新连库即可离线查看表结构与 ⌘K 检索（取消勾选可显著减小文件体积）",
        })
      )
    )
  );

  const cancelBtn = el("button", {
    type: "button",
    className: "mac-btn",
    text: "取消",
    onClick: () => Modal.close("cancel"),
  });
  const okBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    text: "导出 .dataog.db",
    onClick: async () => {
      okBtn.disabled = true;
      okBtn.textContent = "正在生成...";
      try {
        const fn = await workspaceApi.exportFile({
          workspaceId: ws?.workspace_id || "",
          includeMetadataCache: includeMeta,
          fallbackName: `${ws?.name || "DataOG_Workspace"}.dataog.db`,
        });
        Modal.close("done");
        Toast.success(`已导出共享工作区：${fn}`);
      } catch (err) {
        okBtn.disabled = false;
        okBtn.textContent = "导出 .dataog.db";
        Toast.error(err.message);
      }
    },
  });

  Modal.open({
    title: `导出共享工作区 · ${ws?.name || "当前工作区"}`,
    width: "min(480px, 92vw)",
    body,
    footer: [cancelBtn, okBtn],
  });
}

function openCreateWorkspaceDialog(activeWs, onSuccess) {
  let wsName = "";
  let wsDesc = "";
  let cloneCurrent = false;

  const nameInput = el("input", {
    className: "mac-input",
    placeholder: "例如：零售数仓落标一期",
    maxlength: "80",
    onInput: (e) => {
      wsName = e.target.value;
    },
  });
  const descInput = el("input", {
    className: "mac-input",
    placeholder: "可选：记录项目背景或团队协同范围",
    maxlength: "240",
    onInput: (e) => {
      wsDesc = e.target.value;
    },
  });

  const body = el(
    "div",
    { className: "ws-dialog-stack" },
    el(
      "div",
      { className: "form-field" },
      el("label", { className: "form-label", text: "工作区名称" }),
      nameInput
    ),
    el(
      "div",
      { className: "form-field" },
      el("label", { className: "form-label", text: "说明（可选）" }),
      descInput
    ),
    activeWs
      ? el(
          "label",
          { className: "ws-check-row" },
          el("input", {
            type: "checkbox",
            checked: false,
            onChange: (e) => {
              cloneCurrent = e.target.checked;
            },
          }),
          el(
            "div",
            {},
            el("div", { style: { fontWeight: "600", fontSize: "12.5px" }, text: `复制「${activeWs.name}」的业务数据作为起点` }),
            el("div", {
              style: { fontSize: "11.5px", color: "var(--os-muted)" },
              text: "不勾选则创建一个干净的空白共享工作区（本机激活码、AI 密钥与 DB 密码始终独立保留）",
            })
          )
        )
      : null
  );

  const cancelBtn = el("button", {
    type: "button",
    className: "mac-btn",
    text: "取消",
    onClick: () => Modal.close("cancel"),
  });
  const createBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    text: "创建并切换",
    onClick: async () => {
      const trimmed = wsName.trim();
      if (!trimmed) {
        Toast.warn("请输入工作区名称");
        nameInput.focus();
        return;
      }
      createBtn.disabled = true;
      createBtn.textContent = "创建中...";
      try {
        const res = await workspaceApi.create({
          name: trimmed,
          description: wsDesc.trim(),
          clone_from_workspace_id: cloneCurrent && activeWs ? activeWs.workspace_id : null,
          switch_immediately: true,
        });
        store.updateWorkspaces(res.active_workspace, res.workspaces, res.bootstrap);
        Modal.close("done");
        Toast.success(res.message || "已创建并切换工作区");
        onSuccess?.();
      } catch (err) {
        createBtn.disabled = false;
        createBtn.textContent = "创建并切换";
        Toast.error(err.message);
      }
    },
  });

  Modal.open({
    title: "新建团队共享工作区",
    width: "min(460px, 92vw)",
    body,
    footer: [cancelBtn, createBtn],
  });
  setTimeout(() => nameInput.focus(), 40);
}

function openRenameWorkspaceDialog(ws, onSuccess) {
  let wsName = ws.name || "";
  let wsDesc = ws.description || "";

  const nameInput = el("input", {
    className: "mac-input",
    value: wsName,
    maxlength: "80",
    onInput: (e) => {
      wsName = e.target.value;
    },
  });
  const descInput = el("input", {
    className: "mac-input",
    value: wsDesc,
    placeholder: "工作区备注说明（可选）",
    maxlength: "240",
    onInput: (e) => {
      wsDesc = e.target.value;
    },
  });

  const body = el(
    "div",
    { className: "ws-dialog-stack" },
    el("div", { className: "form-field" }, el("label", { className: "form-label", text: "工作区名称" }), nameInput),
    el("div", { className: "form-field" }, el("label", { className: "form-label", text: "说明" }), descInput)
  );

  const cancelBtn = el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close("cancel") });
  const saveBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    text: "保存",
    onClick: async () => {
      if (!wsName.trim()) {
        Toast.warn("工作区名称不能为空");
        return;
      }
      saveBtn.disabled = true;
      try {
        const res = await workspaceApi.update(ws.workspace_id, {
          name: wsName.trim(),
          description: wsDesc.trim(),
        });
        store.updateWorkspaces(res.active_workspace, res.workspaces);
        Modal.close("done");
        Toast.success("工作区信息已更新");
        onSuccess?.();
      } catch (err) {
        saveBtn.disabled = false;
        Toast.error(err.message);
      }
    },
  });

  Modal.open({
    title: "编辑工作区信息",
    width: "min(440px, 92vw)",
    body,
    footer: [cancelBtn, saveBtn],
  });
  setTimeout(() => {
    nameInput.focus();
    nameInput.select();
  }, 40);
}

async function openImportAsNewWorkspaceFlow(onSuccess) {
  const file = await pickLocalFile(".dataog.db,.db,.sqlite,.sqlite3");
  if (!file) return;

  const defaultName = file.name
    .replace(/\.dataog\.db$/i, "")
    .replace(/\.(sqlite3|sqlite|db)$/i, "")
    .slice(0, 80);

  let wsName = defaultName;
  let wsDesc = "";
  let switchNow = true;

  const nameInput = el("input", {
    className: "mac-input",
    value: wsName,
    maxlength: "80",
    onInput: (e) => {
      wsName = e.target.value;
    },
  });
  const descInput = el("input", {
    className: "mac-input",
    placeholder: "可选：注明来源同事或版本号",
    maxlength: "240",
    onInput: (e) => {
      wsDesc = e.target.value;
    },
  });

  const body = el(
    "div",
    { className: "ws-dialog-stack" },
    el(
      "div",
      { className: "ws-security-banner" },
      svgIcon("shield", 16),
      el(
        "div",
        {},
        el("div", { className: "ws-security-banner-title", text: `待导入文件：${file.name} (${formatBytes(file.size)})` }),
        el("div", {
          className: "ws-security-banner-sub",
          text: "仅提取白名单业务数据，自动剥离旧库中可能残留的敏感配置；本机已配置的数据库个人凭据将按端点自动匹配。",
        })
      )
    ),
    el("div", { className: "form-field" }, el("label", { className: "form-label", text: "新工作区名称" }), nameInput),
    el("div", { className: "form-field" }, el("label", { className: "form-label", text: "备注说明（可选）" }), descInput),
    el(
      "label",
      { className: "ws-check-row" },
      el("input", {
        type: "checkbox",
        checked: true,
        onChange: (e) => {
          switchNow = e.target.checked;
        },
      }),
      el("span", { style: { fontSize: "12.5px" }, text: "导入完成后立即切换至该工作区" })
    )
  );

  const cancelBtn = el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close("cancel") });
  const importBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    text: "导入工作区",
    onClick: async () => {
      importBtn.disabled = true;
      importBtn.textContent = "安全解析导入中...";
      Modal.setCloseLocked(true);
      try {
        const res = await workspaceApi.importAsNew(file, {
          name: wsName.trim(),
          description: wsDesc.trim(),
          switchImmediately: switchNow,
        });
        store.updateWorkspaces(res.active_workspace, res.workspaces, res.bootstrap);
        Modal.setCloseLocked(false);
        Modal.close("done");
        const cs = res.credential_summary || {};
        const extraNote =
          cs.needs_credentials_count > 0
            ? `（已自动匹配 ${cs.auto_matched_count || 0} 个库凭据，另有 ${cs.needs_credentials_count} 个库待补充个人账号密码）`
            : cs.auto_matched_count > 0
            ? `（已自动匹配 ${cs.auto_matched_count} 个数据库个人凭据）`
            : "";
        Toast.success(`${res.message || "工作区导入成功"}${extraNote}`);
        onSuccess?.();
      } catch (err) {
        Modal.setCloseLocked(false);
        importBtn.disabled = false;
        importBtn.textContent = "导入工作区";
        Toast.error(err.message);
      }
    },
  });

  Modal.open({
    title: "导入为独立工作区",
    width: "min(500px, 92vw)",
    body,
    footer: [cancelBtn, importBtn],
  });
}

/**
 * 引导式三步增量合并向导：
 * Step 1: 上传/选择同事分享的 .dataog.db 或旧版 dataog_state.db
 * Step 2: 差异预览（模块计数 + 跨环境表对 diff + 数据源拓扑映射）+ 勾选模块 + 选择冲突策略
 * Step 3: 单事务原子合并结果汇总
 */
export function openWorkspaceMergeWizard(onMerged = null) {
  const state = {
    step: 1,
    loading: false,
    inspect: null,
    selectedModules: new Set(),
    strategy: "supplement",
    tableDiffFilter: "ALL",
    result: null,
  };

  const bodyHost = el("div", { className: "ws-merge-wizard" });
  const footerHost = el("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%", gap: "10px" } });

  const renderStepHeader = () =>
    el(
      "div",
      { className: "ws-wizard-steps" },
      [
        { num: 1, label: "选择同事文件" },
        { num: 2, label: "差异预览与策略" },
        { num: 3, label: "合并结果" },
      ].map((s) =>
        el(
          "div",
          { className: `ws-wizard-step ${state.step === s.num ? "is-active" : state.step > s.num ? "is-done" : ""}` },
          el("span", { className: "ws-wizard-step-num", text: state.step > s.num ? "✓" : String(s.num) }),
          el("span", { text: s.label })
        )
      )
    );

  const handlePickMergeFile = async () => {
    const file = await pickLocalFile(".dataog.db,.db,.sqlite,.sqlite3");
    if (!file) return;
    state.loading = true;
    renderWizard();
    Modal.setCloseLocked(true);
    try {
      const res = await workspaceApi.inspectMerge(file);
      state.inspect = res;
      state.selectedModules = new Set();
      for (const m of MERGE_MODULE_META) {
        const info = res.modules?.[m.id] || {};
        const srcCnt = Number(info.source_count ?? info.source_tables_count ?? 0);
        if (srcCnt > 0) {
          state.selectedModules.add(m.id);
        }
      }
      if (state.selectedModules.size === 0) {
        state.selectedModules.add("standard_dictionary");
      }
      state.step = 2;
    } catch (err) {
      Toast.error(err.message);
    } finally {
      state.loading = false;
      Modal.setCloseLocked(false);
      renderWizard();
    }
  };

  const handleExecuteMerge = async () => {
    if (!state.inspect?.merge_token) return;
    if (state.selectedModules.size === 0) {
      Toast.warn("请至少勾选一个要合并的业务模块");
      return;
    }
    state.loading = true;
    renderWizard();
    Modal.setCloseLocked(true);
    try {
      const res = await workspaceApi.executeMerge({
        merge_token: state.inspect.merge_token,
        selected_modules: Array.from(state.selectedModules),
        strategy: state.strategy,
      });
      state.result = res;
      state.step = 3;
      store.updateWorkspaces(res.active_workspace, res.workspaces, res.bootstrap);
      Toast.success(res.message || "增量合并完成");
      onMerged?.();
    } catch (err) {
      Toast.error(err.message);
    } finally {
      state.loading = false;
      Modal.setCloseLocked(false);
      renderWizard();
    }
  };

  const renderStep1 = () =>
    el(
      "div",
      { className: "ws-dialog-stack" },
      el(
        "div",
        { className: "ws-security-banner" },
        svgIcon("shield", 16),
        el(
          "div",
          {},
          el("div", { className: "ws-security-banner-title", text: "白名单业务表隔离解析" }),
          el("div", {
            className: "ws-security-banner-sub",
            text: "支持同事导出的 .dataog.db 共享工作区，也兼容旧版 dataog_state.db。系统严格只读白名单业务表，绝不读取或覆盖本机激活码、AI 密钥与数据库账号密码。",
          })
        )
      ),
      el(
        "div",
        {
          className: "ws-upload-dropzone",
          onClick: () => {
            if (!state.loading) handlePickMergeFile();
          },
        },
        svgIcon("upload", 24),
        el("div", {
          style: { fontWeight: "600", fontSize: "13px" },
          text: state.loading ? "正在只读扫描文件并计算差异预览..." : "点击选择同事分享的工作区文件 (.dataog.db / .db)",
        }),
        el("div", {
          style: { fontSize: "11.5px", color: "var(--os-muted)" },
          text: "扫描阶段不会修改本机任何数据，下一步将展示完整冲突统计与跨环境表对差异预览",
        })
      )
    );

  const renderStep2 = () => {
    const ins = state.inspect || {};
    const srcInfo = ins.source_info || {};
    const mods = ins.modules || {};
    const tableDiffs = ins.table_diffs || [];
    const connPrev = ins.connections_preview || [];

    const moduleRows = MERGE_MODULE_META.map((m) => {
      const info = mods[m.id] || {};
      const srcCnt = Number(info.source_count ?? info.source_tables_count ?? 0);
      const newCnt = Number(info.new_count ?? 0);
      const confCnt = Number(info.conflict_count ?? 0);
      const sameCnt = Number(info.same_count ?? 0);
      const checked = state.selectedModules.has(m.id);

      const badges = [];
      if (srcCnt === 0) {
        badges.push(badge("对方无数据", "neutral"));
      } else {
        if (newCnt > 0) badges.push(badge(`新增 +${newCnt}`, "success"));
        if (confCnt > 0) badges.push(badge(`${m.id === "connection_topology" ? "复用现有" : "冲突"} ${confCnt}`, m.id === "connection_topology" ? "info" : "warn"));
        if (sameCnt > 0 && m.id !== "connection_topology") badges.push(badge(`一致 ${sameCnt}`, "neutral"));
      }
      if (m.id === "connection_topology" && Number(info.auto_cred_matched_count || 0) > 0) {
        badges.push(badge(`自动配凭据 ${info.auto_cred_matched_count}`, "info"));
      }
      if (m.id === "connection_topology" && Number(info.needs_cred_count || 0) > 0) {
        badges.push(badge(`待填密码 ${info.needs_cred_count}`, "warn"));
      }

      return el(
        "label",
        { className: `ws-merge-mod-row ${checked ? "is-checked" : ""} ${srcCnt === 0 ? "is-empty" : ""}` },
        el("input", {
          type: "checkbox",
          checked,
          disabled: state.loading,
          onChange: (e) => {
            if (e.target.checked) state.selectedModules.add(m.id);
            else state.selectedModules.delete(m.id);
            renderWizard();
          },
        }),
        el(
          "div",
          { style: { flex: "1", minWidth: "0" } },
          el("div", { style: { fontWeight: "600", fontSize: "12.5px" }, text: m.label }),
          el("div", { style: { fontSize: "11px", color: "var(--os-muted)" }, text: m.desc })
        ),
        el("div", { className: "ws-merge-mod-badges" }, ...badges)
      );
    });

    const strategyCards = el(
      "div",
      { className: "ws-strategy-grid" },
      MERGE_STRATEGIES.map((st) =>
        el(
          "button",
          {
            type: "button",
            className: `ws-strategy-card ${state.strategy === st.id ? "is-active" : ""}`,
            disabled: state.loading,
            onClick: () => {
              state.strategy = st.id;
              renderWizard();
            },
          },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "6px" } },
            el("span", { style: { fontWeight: "600", fontSize: "12px" }, text: st.title }),
            badge(st.tag, state.strategy === st.id ? "info" : "neutral")
          ),
          el("div", { style: { fontSize: "11px", color: "var(--os-muted)", lineHeight: "1.45" }, text: st.desc })
        )
      )
    );

    // 跨环境映射表对差异预览
    const filteredDiffs = tableDiffs.filter(
      (d) => state.tableDiffFilter === "ALL" || d.status === state.tableDiffFilter
    );
    const diffPreviewBox = tableDiffs.length
      ? el(
          "div",
          { className: "ws-diff-section" },
          el(
            "div",
            { className: "ws-diff-head" },
            el("span", { style: { fontWeight: "600", fontSize: "12px" }, text: `跨环境映射表对差异预览 (${ins.table_diffs_total || tableDiffs.length})` }),
            el(
              "div",
              { className: "seg-group" },
              [
                { id: "ALL", label: `全部 ${tableDiffs.length}` },
                { id: "CONFLICT", label: `冲突 ${tableDiffs.filter((d) => d.status === "CONFLICT").length}` },
                { id: "NEW", label: `新增 ${tableDiffs.filter((d) => d.status === "NEW").length}` },
                { id: "SAME", label: `一致 ${tableDiffs.filter((d) => d.status === "SAME").length}` },
              ].map((f) =>
                el("button", {
                  type: "button",
                  className: `seg-btn ${state.tableDiffFilter === f.id ? "is-active" : ""}`,
                  text: f.label,
                  onClick: () => {
                    state.tableDiffFilter = f.id;
                    renderWizard();
                  },
                })
              )
            )
          ),
          el(
            "div",
            { className: "ws-diff-table-wrap" },
            el(
              "table",
              { className: "ws-diff-table" },
              el(
                "thead",
                {},
                el(
                  "tr",
                  {},
                  el("th", { text: "状态" }),
                  el("th", { text: "标准表键" }),
                  el("th", { text: "来源表 → 目标表" }),
                  el("th", { text: "字段 (对方/本地)" }),
                  el("th", { text: "差异说明" })
                )
              ),
              el(
                "tbody",
                {},
                filteredDiffs.length
                  ? filteredDiffs.map((d) => {
                      const stTone = d.status === "NEW" ? "success" : d.status === "CONFLICT" ? "warn" : "neutral";
                      const stText = d.status === "NEW" ? "新增" : d.status === "CONFLICT" ? "冲突" : "一致";
                      const diffText =
                        d.status === "NEW"
                          ? `新增 ${d.source_col_count} 个字段映射`
                          : d.status === "SAME"
                          ? "映射完全一致"
                          : [
                              d.added_cols_count ? `新字段 +${d.added_cols_count}` : "",
                              d.changed_cols_count ? `异标 ${d.changed_cols_count}` : "",
                              d.source_is_newer ? "对方较新" : "本地较新/同步",
                            ]
                              .filter(Boolean)
                              .join(" · ") || "表对或字段数有差异";
                      const refPairText = `${d.source_table_ref || "-"} → ${d.target_table_ref || "-"}`;
                      return el(
                        "tr",
                        {},
                        el("td", {}, badge(stText, stTone)),
                        el("td", { className: "mono ws-diff-col-key", style: { fontWeight: "600" }, title: d.std_table_key, text: d.std_table_key }),
                        el("td", { className: "mono ws-diff-col-ref", title: refPairText, text: refPairText }),
                        el("td", { className: "mono", text: `${d.source_col_count} / ${d.local_col_count}` }),
                        el("td", { style: { color: "var(--os-text-secondary)" }, text: diffText })
                      );
                    })
                  : [el("tr", {}, el("td", { colspan: "5", style: { textAlign: "center", color: "var(--os-muted)", padding: "12px" }, text: "当前筛选条件下无匹配表对" }))]
              )
            )
          )
        )
      : null;

    // 数据源端点映射预览
    const connPreviewBox = connPrev.length
      ? el(
          "div",
          { className: "ws-diff-section" },
          el(
            "div",
            { className: "ws-diff-head" },
            el("span", { style: { fontWeight: "600", fontSize: "12px" }, text: `数据源拓扑与本机凭据匹配预览 (${connPrev.length})` })
          ),
          el(
            "div",
            { className: "ws-conn-prev-list" },
            connPrev.map((c) => {
              const actBadge =
                c.action === "ADD"
                  ? badge("新增拓扑", "success")
                  : c.action === "MAP_EXISTING"
                  ? badge(`自动并入「${c.mapped_local_name || "本机同端点库"}」`, "info")
                  : badge("匹配本机同 ID 库", "neutral");
              const credBadge =
                c.credential_match === "AUTO_MATCHED"
                  ? badge("自动匹配个人凭据", "info")
                  : c.credential_match === "NEEDS_INPUT"
                  ? badge("合并后需填个人密码", "warn")
                  : c.credential_match === "SQLITE_LOCAL"
                  ? badge("本地 SQLite", "neutral")
                  : badge("沿用本机凭据", "success");
              return el(
                "div",
                { className: "ws-conn-prev-item" },
                el(
                  "div",
                  { style: { minWidth: "0", flex: "1" } },
                  el("span", { style: { fontWeight: "600", fontSize: "12px", marginRight: "6px" }, text: c.name }),
                  badge(c.db_type, "neutral"),
                  el("div", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "2px" }, text: c.endpoint })
                ),
                el("div", { style: { display: "flex", gap: "5px", flexWrap: "wrap", justifyContent: "flex-end" } }, actBadge, credBadge)
              );
            })
          )
        )
      : null;

    return el(
      "div",
      { className: "ws-dialog-stack" },
      el(
        "div",
        { className: "ws-source-pill-bar" },
        el("span", { style: { fontWeight: "600", fontSize: "12.5px" }, text: srcInfo.filename || "workspace.dataog.db" }),
        badge(srcInfo.source_kind_label || "共享工作区", srcInfo.source_kind === "LEGACY_STATE_DB" ? "warn" : "info"),
        badge(formatBytes(srcInfo.file_size || 0), "neutral")
      ),
      el(
        "div",
        { className: "ws-sub-block" },
        el("div", { className: "form-label", text: "冲突合并策略" }),
        strategyCards
      ),
      el(
        "div",
        { className: "ws-sub-block" },
        el("div", { className: "form-label", text: "勾选需要合并的业务模块" }),
        el("div", { className: "ws-merge-mod-list" }, ...moduleRows)
      ),
      diffPreviewBox,
      connPreviewBox
    );
  };

  const renderStep3 = () => {
    const res = state.result || {};
    const sum = res.summary || { added: 0, updated: 0, skipped: 0 };
    const details = res.details || {};
    return el(
      "div",
      { className: "ws-dialog-stack" },
      el(
        "div",
        { className: "auth-success-hero" },
        el("div", { className: "auth-success-icon" }, svgIcon("check", 20)),
        el(
          "div",
          {},
          el("div", { className: "auth-success-headline", text: `增量合并成功：新增 ${sum.added} 项 · 更新 ${sum.updated} 项 · 跳过 ${sum.skipped} 项` }),
          el("div", { className: "auth-success-sub", text: "全部更改已在单事务内原子写入当前工作区，本机私有激活码、AI 密钥与数据库账号密码未受任何影响。" })
        )
      ),
      el(
        "div",
        { className: "ws-diff-table-wrap" },
        el(
          "table",
          { className: "ws-diff-table" },
          el("thead", {}, el("tr", {}, el("th", { text: "业务模块" }), el("th", { text: "新增" }), el("th", { text: "更新" }), el("th", { text: "跳过" }))),
          el(
            "tbody",
            {},
            MERGE_MODULE_META.map((m) => {
              const d = details[m.id] || { added: 0, updated: 0, skipped: 0 };
              const ran = (res.selected_modules || []).includes(m.id);
              return el(
                "tr",
                { style: ran ? {} : { opacity: "0.5" } },
                el("td", { style: { fontWeight: "600" }, text: m.label + (ran ? "" : "（未勾选）") }),
                el("td", { className: "mono", style: { color: d.added ? "var(--status-success)" : "var(--os-muted)" }, text: String(d.added || 0) }),
                el("td", { className: "mono", style: { color: d.updated ? "var(--os-accent)" : "var(--os-muted)" }, text: String(d.updated || 0) }),
                el("td", { className: "mono", style: { color: "var(--os-muted)" }, text: String(d.skipped || 0) })
              );
            })
          )
        )
      )
    );
  };

  const renderWizard = () => {
    const scrollBox = typeof document !== "undefined" ? document.getElementById("global-modal-body") : null;
    const prevScrollTop = scrollBox ? scrollBox.scrollTop : 0;
    const content =
      state.step === 1 ? renderStep1() : state.step === 2 ? renderStep2() : renderStep3();
    clearEl(bodyHost, renderStepHeader(), content);
    if (scrollBox) {
      scrollBox.scrollTop = prevScrollTop;
    }

    if (state.step === 1) {
      clearEl(
        footerHost,
        el("span", { style: { fontSize: "11.5px", color: "var(--os-muted)" }, text: "本地私有保险箱全程隔离保护" }),
        el(
          "div",
          { style: { display: "flex", gap: "8px" } },
          el("button", { type: "button", className: "mac-btn", text: "取消", disabled: state.loading, onClick: () => Modal.close("cancel") }),
          el("button", { type: "button", className: "mac-btn mac-btn-primary", text: state.loading ? "扫描中..." : "选择文件", disabled: state.loading, onClick: handlePickMergeFile })
        )
      );
    } else if (state.step === 2) {
      clearEl(
        footerHost,
        el("button", {
          type: "button",
          className: "mac-btn",
          text: "← 重新选文件",
          disabled: state.loading,
          onClick: () => {
            state.step = 1;
            state.inspect = null;
            renderWizard();
          },
        }),
        el(
          "div",
          { style: { display: "flex", gap: "8px" } },
          el("button", { type: "button", className: "mac-btn", text: "取消", disabled: state.loading, onClick: () => Modal.close("cancel") }),
          el("button", {
            type: "button",
            className: "mac-btn mac-btn-primary",
            text: state.loading ? "正在原子合并..." : `确认合并 (${state.selectedModules.size} 个模块)`,
            disabled: state.loading || state.selectedModules.size === 0,
            onClick: handleExecuteMerge,
          })
        )
      );
    } else {
      clearEl(
        footerHost,
        el("span"),
        el("button", { type: "button", className: "mac-btn mac-btn-primary", text: "完成", onClick: () => Modal.close("done") })
      );
    }
  };

  renderWizard();
  Modal.open({
    title: "合并同事工作区进度",
    width: "min(860px, 94vw)",
    body: bodyHost,
    footer: [footerHost],
  });
}

export class WorkspaceView {
  constructor({ onChange = null } = {}) {
    this.onChange = onChange;
    this.root = el("div", { className: "mac-section", style: { gap: "14px" } });
    this._refreshing = false;
  }

  async refreshFromServer() {
    if (this._refreshing) return;
    this._refreshing = true;
    try {
      const res = await workspaceApi.list();
      store.updateWorkspaces(res.active_workspace, res.workspaces);
    } catch (_e) {
      // 忽略静默刷新异常
    } finally {
      this._refreshing = false;
      this.render();
    }
  }

  render() {
    const active = store.state.activeWorkspace || null;
    const workspaces = store.state.workspaces || [];
    const stats = active?.stats || {};

    const kpiTiles = [
      { label: "标准字典", value: stats.standards_count ?? 0 },
      { label: "字段绑标", value: stats.field_bindings_count ?? 0 },
      { label: "跨环境表对", value: stats.cross_tables_count ?? 0 },
      { label: "贯标队列", value: stats.migration_items_count ?? 0 },
      { label: "数据源拓扑", value: stats.connections_count ?? 0 },
      { label: "缓存库表", value: stats.meta_tables_count ?? 0 },
    ].map((k) =>
      el(
        "div",
        { className: "ws-kpi-tile" },
        el("div", { className: "ws-kpi-val mono", text: String(k.value) }),
        el("div", { className: "ws-kpi-lbl", text: k.label })
      )
    );

    const activeCard = el(
      "div",
      { className: "mac-group-box ws-hero-card" },
      el(
        "div",
        { className: "ws-hero-top" },
        el(
          "div",
          { style: { minWidth: "0", flex: "1" } },
          el(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
            svgIcon("layers", 17),
            el("h3", { className: "ws-hero-title", text: active?.name || "默认工作区" }),
            badge("当前活动工作区", "success"),
            badge("三层零密钥分离", "info")
          ),
          el("div", {
            className: "ws-hero-desc",
            text:
              active?.description ||
              "业务标准、绑标与跨环境映射均保存在独立共享工作区中；软件更新覆盖安装目录绝不影响个人配置与工作区进度。",
          })
        ),
        active
          ? el("button", {
              type: "button",
              className: "btn btn-xs",
              text: "编辑名称",
              onClick: () => openRenameWorkspaceDialog(active, () => this.render()),
            })
          : null
      ),
      el("div", { className: "ws-kpi-grid" }, ...kpiTiles),
      el(
        "div",
        { className: "ws-action-bar" },
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-primary",
            onClick: () => openExportWorkspaceDialog(active),
          },
          svgIcon("download", 14),
          el("span", { text: "导出共享工作区 (.dataog.db)" })
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn",
            onClick: () => openWorkspaceMergeWizard(() => {
              this.render();
              this.onChange?.();
            }),
          },
          svgIcon("merge", 14),
          el("span", { text: "合并同事进度" })
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn",
            onClick: () => openImportAsNewWorkspaceFlow(() => {
              this.render();
              this.onChange?.();
            }),
          },
          svgIcon("upload", 14),
          el("span", { text: "导入为新工作区" })
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn",
            onClick: () => openCreateWorkspaceDialog(active, () => {
              this.render();
              this.onChange?.();
            }),
          },
          svgIcon("plus", 14),
          el("span", { text: "新建工作区" })
        )
      )
    );

    const listRows = workspaces.length
      ? workspaces.map((w) => {
          const wsStats = w.stats || {};
          const summaryLine = [
            `标准 ${wsStats.standards_count ?? 0}`,
            `绑标 ${wsStats.field_bindings_count ?? 0}`,
            `跨环境表对 ${wsStats.cross_tables_count ?? 0}`,
            `数据源 ${wsStats.connections_count ?? 0}`,
            formatBytes(w.size_bytes || 0),
          ].join(" · ");
          return el(
            "div",
            { className: `list-row-card ${w.is_active ? "ws-row-active" : ""}` },
            el(
              "div",
              { style: { minWidth: "0", flex: "1" } },
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "7px", flexWrap: "wrap" } },
                w.is_active ? statusDot(w.name, "success") : el("span", { style: { fontWeight: "600", fontSize: "12.5px" }, text: w.name }),
                w.is_default ? badge("默认", "neutral") : null,
                w.is_active ? badge("使用中", "success") : null
              ),
              el("div", {
                className: "mono",
                style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "3px" },
                text: summaryLine,
              }),
              w.description
                ? el("div", { style: { fontSize: "11.5px", color: "var(--os-text-secondary)", marginTop: "2px" }, text: w.description })
                : null
            ),
            el(
              "div",
              { style: { display: "flex", gap: "5px", flexShrink: "0" } },
              !w.is_active
                ? el("button", {
                    type: "button",
                    className: "btn btn-xs mac-btn-primary",
                    text: "切换",
                    onClick: async (e) => {
                      const btn = e.currentTarget;
                      if (btn) {
                        btn.disabled = true;
                        btn.textContent = "切换中...";
                      }
                      try {
                        const res = await workspaceApi.switchTo(w.workspace_id);
                        store.updateWorkspaces(res.active_workspace, res.workspaces, res.bootstrap);
                        Toast.success(res.message || `已切换至「${w.name}」`);
                        this.render();
                        this.onChange?.();
                      } catch (err) {
                        if (btn) {
                          btn.disabled = false;
                          btn.textContent = "切换";
                        }
                        Toast.error(err.message);
                      }
                    },
                  })
                : null,
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: "导出",
                onClick: () => openExportWorkspaceDialog(w),
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: "重命名",
                onClick: () => openRenameWorkspaceDialog(w, () => this.render()),
              }),
              !w.is_active
                ? el("button", {
                    type: "button",
                    className: "btn btn-xs btn-danger",
                    text: "删除",
                    onClick: async () => {
                      const ok = await Modal.confirm({
                        title: "删除工作区",
                        message: `确定永久删除工作区「${w.name}」及其业务数据吗？本机个人账号密码与 AI 密钥不受影响。`,
                        confirmText: "删除",
                        danger: true,
                      });
                      if (!ok) return;
                      try {
                        const res = await workspaceApi.remove(w.workspace_id);
                        store.updateWorkspaces(res.active_workspace, res.workspaces);
                        Toast.success("工作区已删除");
                        this.render();
                      } catch (err) {
                        Toast.error(err.message);
                      }
                    },
                  })
                : null
            )
          );
        })
      : [el("div", { className: "grid-empty", text: "暂无本地工作区" })];

    const listBox = el(
      "div",
      { className: "mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: `本地托管工作区 (${workspaces.length})` }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "刷新统计",
          onClick: () => this.refreshFromServer(),
        })
      ),
      el("div", {}, ...listRows)
    );

    const archGuide = el(
      "div",
      { className: "ws-arch-grid" },
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-arch-card-head" }, svgIcon("lock", 14), el("span", { text: "本机私有保险箱（严格隔离）" })),
        el(
          "div",
          { className: "ws-arch-pills" },
          ["离线激活码", "AI API 密钥", "数据库账号密码", "桌面壁纸与图标", "JDBC 驱动包", "个人 AI 探查历史", "本地执行日志"].map(
            (t) => badge(t, "neutral")
          )
        )
      ),
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-arch-card-head" }, svgIcon("layers", 14), el("span", { text: "团队共享工作区 (.dataog.db)" })),
        el(
          "div",
          { className: "ws-arch-pills" },
          ["企业数据标准字典", "单库字段绑标", "跨环境表/字段映射", "贯标执行队列", "无密码数据源拓扑", "库表元数据缓存"].map(
            (t) => badge(t, "info")
          )
        )
      )
    );

    clearEl(this.root, activeCard, listBox, archGuide);
  }
}
