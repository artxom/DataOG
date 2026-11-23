/**
 * Data.O.G 系统配置子面板：团队共享工作区管理 (WorkspaceView)
 *
 * 架构红线：
 * 1. 严禁展示任何本地私有库物理路径；
 * 2. 100% 使用安全 DOM 工厂 (el / clearEl)，零 innerHTML，零阻塞弹窗；
 * 3. 三步引导式增量合并向导独立拆分至 `./workspace_merge_wizard.js` 并在此保持向后兼容重导出。
 */

import { workspaceApi } from "../core/platform_api.js";
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
import { openWorkspaceMergeWizard } from "./workspace_merge_wizard.js";

export { openWorkspaceMergeWizard };

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
        store.updateWorkspaces(res.active_workspace, res.workspaces, switchNow ? res.bootstrap : null);
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
